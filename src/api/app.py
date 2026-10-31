"""FastAPI app for Module C. Position tracking and exit advisory only.

Binds 127.0.0.1 by default. Single user, local tool, never exposed on a network
interface.

**There is deliberately no signals endpoint.** Module B's features were evaluated at
~16-minute, 1-day, 3-day and 5-day horizons and produced no excess over a naive
comparator at any of them. Surfacing a scored ranking here would present a
known-negative result as a recommendation, which is worse than showing nothing. See
CLAUDE.md section 2.3.

Every route is a read or an advisory computation. None places, modifies or cancels an
order, and none may be added.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from src.journal.store import DecisionNote

from src.api.runtime import DuplicatePosition, Mode, SessionRuntime

log = logging.getLogger("fno.api")

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"

#: Frontend libraries, served from disk so the page needs no network to render.
REQUIRED_VENDOR = (
    "react.production.min.js",
    "react-dom.production.min.js",
    "babel.min.js",
    "tailwind.js",
)

__all__ = ["create_app", "AddPosition", "ClosePosition", "CheckEntry", "REQUIRED_VENDOR"]


class AddPosition(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    entry_time: datetime | None = None
    stop_loss: Decimal | None = None
    target: Decimal | None = None
    #: Opt in to recording a position identical to one already open. Off by default:
    #: an exact repeat is far more likely to be a double submit than a real second fill.
    allow_duplicate: bool = False


class ClosePosition(BaseModel):
    exit_price: Decimal = Field(gt=0)
    exit_time: datetime | None = None


class CheckEntry(BaseModel):
    symbol: str
    price: Decimal | None = None
    stop: Decimal | None = None




def create_app(runtime: SessionRuntime, *, run_loop: bool = True) -> FastAPI:
    """Build the app.

    ``run_loop=False`` skips the background evaluation task, so tests can drive the
    clock explicitly and stay deterministic instead of racing a timer.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(runtime.run()) if run_loop else None
        if task is not None:
            log.info("runtime task started (%s)", runtime.mode)
        try:
            yield
        finally:
            runtime.stop()
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    app = FastAPI(
        title="fno-advisor — Module C",
        description="Position tracking and exit advisory. Advisory only; never places orders.",
        version="1.0",
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    # React/Babel/Tailwind are served from disk, not a CDN. An unpinned CDN shipped a
    # breaking major version once and the dashboard rendered as a blank black page
    # with the backend perfectly healthy. Missing vendor files must fail at startup,
    # loudly, rather than at 14:50 in the browser.
    vendor = WEB_DIR / "vendor"
    missing = [f for f in REQUIRED_VENDOR if not (vendor / f).exists()]
    if missing:
        raise RuntimeError(
            f"vendored frontend assets missing from {vendor}: {', '.join(missing)}. "
            f"The dashboard cannot render without them and will not fetch them from a "
            f"CDN. See web/vendor/README.md for the pinned URLs."
        )
    app.mount("/vendor", StaticFiles(directory=vendor), name="vendor")

    # ---- pages ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        page = WEB_DIR / "index.html"
        if not page.exists():
            raise HTTPException(500, f"UI not found at {page}")
        return page.read_text(encoding="utf-8")

    # ---- positions -----------------------------------------------------------

    @app.get("/positions")
    async def list_positions() -> dict:
        events = runtime.event_risk()
        return {
            "positions": runtime.evaluate_all(events),
            "health": runtime.health(),
            "event_risk": events.as_dict(),
            "market": runtime.market_overview(),
        }

    @app.post("/positions", status_code=201)
    async def add_position(body: AddPosition) -> dict:
        try:
            position = runtime.add_position(
                symbol=body.symbol, quantity=body.quantity,
                entry_price=body.entry_price, entry_time=body.entry_time,
                stop_loss=body.stop_loss, target=body.target,
                allow_duplicate=body.allow_duplicate,
            )
        except DuplicatePosition as exc:
            # 409, not 400: the request is well-formed, it just collides with state.
            # Silently accepting it would put two identical cards on the dashboard,
            # which reads as one position rendered twice.
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - validation surfaced to the user
            raise HTTPException(400, str(exc)) from exc
        return {
            "position_id": position.position_id,
            "symbol": position.symbol,
            "quantity": position.quantity,
            "entry_price": str(position.entry_price),
            "entry_time": position.entry_time.isoformat(),
            "note": "Recorded. This system did not place an order.",
        }

    @app.delete("/positions/{position_id}")
    async def close_position(position_id: str, body: ClosePosition) -> dict:
        try:
            return runtime.close_position(position_id, body.exit_price, body.exit_time)
        except KeyError as exc:
            raise HTTPException(404, f"no open position {position_id}") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc

    # ---- advisory ------------------------------------------------------------

    @app.post("/check-entry")
    async def check_entry(body: CheckEntry) -> dict:
        return runtime.check_entry(body.symbol, body.price, body.stop)

    @app.get("/check-entry-all")
    async def check_entry_all() -> dict:
        """The same entry check across every subscribed symbol. Records nothing.

        A FILTER, not a ranking. Alphabetical order, no score, no preferred row. See
        SessionRuntime.check_entry_all and CLAUDE.md 2.3 -- this must not become the
        signal panel that failed evaluation.
        """
        return runtime.check_entry_all()

    @app.get("/checks")
    async def checks() -> dict:
        """The "checked today" scoreboard: past checks and what happened after them.

        A RECORD OF THE PAST, not a list of what to buy. Rows are ordered by check time,
        newest first, and there is no query parameter to sort, filter or rank them --
        deliberately, because a sortable return column is a buy list whatever the header
        says. See SessionRuntime.checked_log and CLAUDE.md 2.4.
        """
        return runtime.checked_log()

    @app.get("/event-risk")
    async def event_risk() -> dict:
        """Which symbols are under ELEVATED_EVENT_RISK. An EXCLUSION FILTER.

        Not a signal and not a ranking. It reports that a material announcement landed
        or is scheduled, so price is being driven by information rather than flow and
        normal technical reasoning does not apply. It carries no score and says nothing
        about direction. UNKNOWN is not CLEAR.

        Reads only what the forward collector has already written -- this route never
        touches the network, so an NSE outage degrades the panel instead of hanging it.
        """
        return runtime.event_risk().as_dict()

    @app.get("/market")
    async def market_overview() -> dict:
        """Observed breadth and movement for symbols subscribed in this session."""
        return runtime.market_overview()

    @app.get("/market/{symbol}")
    async def market_chart(symbol: str, interval: Literal[1, 5, 15] = 1) -> dict:
        """Completed candles and potential structure for one subscribed symbol."""
        try:
            return runtime.market_chart(symbol, interval)
        except KeyError as exc:
            raise HTTPException(404, f"{symbol.upper()} is not subscribed in this session") from exc

    @app.get("/journal")
    async def journal() -> dict:
        return runtime.journal_history()

    @app.post("/journal/decisions", status_code=201)
    async def journal_decision(body: DecisionNote) -> dict:
        return runtime.record_journal_note(body)

    @app.get("/health")
    async def health() -> dict:
        return runtime.health()

    # ---- stream --------------------------------------------------------------

    @app.websocket("/stream")
    async def stream(ws: WebSocket) -> None:
        await ws.accept()
        queue = runtime.subscribe()
        try:
            await ws.send_json(runtime.snapshot())
            while True:
                payload = await queue.get()
                await ws.send_json(payload)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("stream failed")
        finally:
            runtime.unsubscribe(queue)

    return app
