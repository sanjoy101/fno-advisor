"""Start the Module C dashboard, in REPLAY or LIVE mode.

One implementation, two front doors: ``python -m src.cli dashboard`` and
``scripts/serve_dashboard.py`` both call :func:`serve`. Duplicating the wiring is how
the two drift until one of them silently starts LIVE when you asked for REPLAY.

**REPLAY is the default, and LIVE must be asked for explicitly.** That is not a
convenience default -- replay needs no token, cannot place anything, and cannot be
confused for today's market once the header renders. Defaulting to LIVE would mean a
mistyped command opens a page showing real money.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Sequence

from src.api.app import create_app
from src.api.runtime import build_live_runtime, build_replay_runtime
from src.data.backfill import LIQUID_FNO_UNIVERSE
from src.data.fno_universe import load_snapshot
from src.market.calendar import is_trading_day, now_ist
from src.positions.store import PositionStore

log = logging.getLogger("fno.serve")

__all__ = [
    "serve", "resolve_symbols", "DEFAULT_SYMBOLS", "MODES",
    "SOCKET_SYMBOL_CAP", "FYERS_STANDARD_SYMBOL_CAP", "MAX_SYMBOLS",
    "SEED_WARN_SYMBOLS", "seed_seconds_estimate",
]

#: Safe offline/replay default. A recent dated symbol-master snapshot is selected
#: automatically for LIVE when one is available.
MARKET_INDEX_SYMBOLS: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
DEFAULT_SYMBOLS: tuple[str, ...] = LIQUID_FNO_UNIVERSE + MARKET_INDEX_SYMBOLS

MODES = ("replay", "live")

# --------------------------------------------------------------------------------
# What actually limits --symbols
#
# The live limit depends on the user's Fyers plan. Standard permits 100 active Data
# Socket symbols per user across connections; Prime permits 5,000. The API request
# limit is separate and still controls startup history pacing.
#
#   Fyers Standard plan   100 symbols per user across all connections. The full
#                         refreshed F&O universe (224 including indices here) cannot
#                         be streamed on this plan in one session. Prime's 5,000-symbol
#                         cap is separate from the REST request limit below.
#
#   Evaluation per bar    NOT binding, and not even close. The per-tick loop iterates
#                         OPEN POSITIONS, not symbols -- adding symbols costs nothing
#                         there. The symbol-scaling cost is /check-entry-all, at
#                         0.25 ms per symbol against a full 375-bar session: 46 ms for
#                         180 symbols, on an endpoint that only runs when the button
#                         is pressed. Event risk is cached on the ledger's stat stamp.
#
#   Memory                NOT binding. 465 kB per symbol-session of completed bars ->
#                         84 MB for 180 symbols x a full 375-bar day.
#
#   Startup seed  <-- THIS ONE. seed_history() makes one REST history call PER SYMBOL
#                         before the socket opens, paced by RateLimiter. Of its two
#                         caps the per-MINUTE one binds at every count above 1 (40/min
#                         is 1.5 s apart, against 3/s = 0.33 s), so the cost is ~1.5 s
#                         per symbol: about 269 s -- call it five minutes -- for 180
#                         names, paid on every launch before the page renders. It was
#                         unpaced until 2026-08-24, which would have put all 180
#                         requests on the wire in a burst.
#
# Standard users can stream a chosen set of up to 100 symbols; full-universe live
# scanning requires a plan with a larger Data Socket subscription cap.
# --------------------------------------------------------------------------------

#: The broker's own limit, from the SDK. Documented for orientation; nothing here
#: goes near it.
SOCKET_SYMBOL_CAP = 5000
FYERS_STANDARD_SYMBOL_CAP = 100

#: The practical guard. Comfortably above the ~180-name F&O universe, far below the
#: socket cap, and chosen so the startup seed stays inside ~6.5 minutes. A number
#: above this is far more likely to be a typo or a pasted file than an intention.
MAX_SYMBOLS = 250

#: Above this, say how long startup will take rather than appearing to hang.
SEED_WARN_SYMBOLS = 25

#: RateLimiter's defaults, mirrored here so the estimate cannot silently drift from
#: the pacing.
_SEED_PER_SECOND = 3.0
_SEED_PER_MINUTE = 40


def seed_seconds_estimate(count: int) -> float:
    """Roughly how long the startup history seed will take for ``count`` symbols.

    One REST call per symbol, paced by both the per-second and per-minute caps;
    whichever binds harder wins. In practice that is always the per-minute cap above
    a single symbol, but both are computed so a change to either default stays
    reflected here. Approximate by design -- it exists to set the user's expectation
    before the silent seed, not to be accurate to the second.
    """
    if count <= 0:
        return 0.0
    return max((count - 1) / _SEED_PER_SECOND, 60.0 * (count - 1) / _SEED_PER_MINUTE)


def live_symbol_limit() -> int:
    """Configured Fyers Data Socket plan cap; safely assume Standard if config is absent."""
    try:
        from config.settings import get_fyers_settings

        return get_fyers_settings().websocket_symbol_limit
    except Exception:  # noqa: BLE001 - request validation must still report safely
        return FYERS_STANDARD_SYMBOL_CAP


def resolve_symbols(spec: str | Sequence[str] | None, *, full_universe=None) -> list[str]:
    """Parse a comma-separated symbol list, or fall back to the liquid default.

    Duplicates are collapsed, preserving first-seen order. Subscribing twice to one
    symbol would seed it twice and put two identical rows in the bulk check table --
    and two identical rows are indistinguishable from one row rendered twice, which is
    the same confusion the duplicate-position rule exists to prevent.
    """
    if spec is None:
        return list(full_universe or DEFAULT_SYMBOLS)
    raw = (spec.split(",") if isinstance(spec, str) else [str(s) for s in spec])
    seen: dict[str, None] = {}
    for item in raw:
        cleaned = item.strip().upper()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def validate_request(
    store: PositionStore,
    *,
    mode: str = "replay",
    trade_date: date | None = None,
    symbols: str | Sequence[str] | None = None,
) -> list[str]:
    """Check the request itself, cheaply, and return the usable symbols.

    Separate from :func:`build_runtime` so that argument errors are raised BEFORE
    environmental ones. A missing ``--date`` is wrong no matter what is listening on
    the port; reporting the port instead would answer a question the user did not ask
    and hide the one they did.

    Raises:
        ValueError: unknown mode, replay with no date, a date with no stored bars,
            more than ``MAX_SYMBOLS`` symbols, or a live Standard subscription above
            the broker's 100-symbol plan cap.
    """
    mode = mode.lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

    snapshot = load_snapshot(store.root) if mode == "live" and symbols is None else None
    full_universe = (snapshot.symbols + snapshot.indices) if snapshot else None
    wanted = resolve_symbols(symbols, full_universe=full_universe)
    if not wanted:
        raise ValueError("--symbols was given but resolved to nothing usable.")
    plan_symbol_cap = live_symbol_limit() if mode == "live" else FYERS_STANDARD_SYMBOL_CAP
    if mode == "live" and len(wanted) > plan_symbol_cap:
        raise ValueError(
            f"{len(wanted)} live symbols requested, but the configured Fyers Data Socket "
            f"plan limit is {plan_symbol_cap} active symbols per user across all "
            f"connections. Choose a --symbols list of at most {plan_symbol_cap} total "
            "instruments (including indices), close other Fyers data-socket clients, or "
            "set FYERS_WEBSOCKET_SYMBOL_LIMIT to "
            "your higher plan's cap after upgrading. This check runs before history seeding."
        )
    if len(wanted) > MAX_SYMBOLS:
        raise ValueError(
            f"{len(wanted)} symbols requested; the application guard is {MAX_SYMBOLS}. "
            "Reduce the symbol list. The library supports up to "
            f"{SOCKET_SYMBOL_CAP} on Prime, but the application's startup-history and "
            "resource limits are lower."
        )
    if len(wanted) > SEED_WARN_SYMBOLS and mode == "live":
        log.warning(
            "%d symbols: the startup history seed is one paced request each, about "
            "%.0fs before the dashboard opens. This is not a hang.",
            len(wanted), seed_seconds_estimate(len(wanted)),
        )
    if mode == "live":
        return wanted

    if trade_date is None:
        raise ValueError(
            "replay needs a date: pass --date YYYY-MM-DD. Replay is the default mode "
            "because it is safe, but it still has to know which session to replay."
        )

    available = [s for s in wanted if store.has_bars(s, trade_date)]
    if not available:
        wider = [s for s in LIQUID_FNO_UNIVERSE if store.has_bars(s, trade_date)]
        raise ValueError(
            f"no stored bars for {trade_date} in {wanted}."
            + (f" Available that day: {', '.join(wider[:8])}" if wider
               else " Nothing is stored for that date at all.")
        )
    return available


def build_runtime(
    store: PositionStore,
    *,
    mode: str = "replay",
    trade_date: date | None = None,
    speed: float = 60.0,
    symbols: str | Sequence[str] | None = None,
    tick_seconds: float = 1.0,
):
    """Build the SessionRuntime for the requested mode.

    Raises:
        ValueError: on anything that would otherwise produce a blank or misleading
            page. Failing here beats serving an empty dashboard that looks like a
            quiet market.
    """
    mode = mode.lower()
    available = validate_request(store, mode=mode, trade_date=trade_date,
                                 symbols=symbols)

    if mode == "live":
        today = now_ist().date()
        if not is_trading_day(today):
            log.warning("%s is not an NSE trading day -- the feed will be quiet", today)
        log.info("LIVE mode, %d symbols", len(available))
        return build_live_runtime(store, available, today, tick_seconds=tick_seconds)

    log.info("REPLAY mode, %s, %d symbols, %.0fx speed", trade_date, len(available), speed)
    return build_replay_runtime(
        store, available, trade_date, speed=speed, tick_seconds=tick_seconds
    )


def check_port_free(host: str, port: int) -> None:
    """Refuse to start if something is already serving on this port.

    Without this, launching with ``--date A`` while an older dashboard still holds the
    port means uvicorn fails to bind, and the browser at that address shows the OLDER
    session -- different date, different positions, different clock -- with nothing on
    the page indicating it is not the one just launched. That is the same class of
    fault as a page that says LIVE when it does not know: the dashboard would be
    misreporting which session you are looking at.

    Raises:
        ValueError: naming what is already there, including its trade date when the
            occupant is another instance of this dashboard.
    """
    import socket

    with socket.socket() as probe:
        probe.settimeout(1.0)
        if probe.connect_ex((host, port)) != 0:
            return  # nothing listening

    occupant = ""
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as resp:
            health = json.load(resp)
        occupant = (
            f" It is another fno-advisor dashboard: {health.get('mode')} mode, "
            f"trade date {health.get('trade_date')}, clock {health.get('now')}. "
            f"That is what your browser would have shown you -- NOT the session you "
            f"just asked for."
        )
    except Exception:  # noqa: BLE001 - some other service; naming the port is enough
        occupant = " Something else is listening on it."

    raise ValueError(
        f"{host}:{port} is already in use.{occupant} Stop it first, or pass "
        f"--port to run alongside it."
    )


def serve(
    store: PositionStore,
    *,
    mode: str = "replay",
    trade_date: date | None = None,
    speed: float = 60.0,
    symbols: str | Sequence[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    tick_seconds: float = 1.0,
) -> int:
    """Build the runtime, then block serving the dashboard.

    Binds loopback by default. This is a single-user local tool and there is no
    authentication on it; do not bind it to a routable interface.
    """
    import uvicorn  # imported here so `--help` does not pay for it

    # Order matters. The request is checked first, because an argument error is wrong
    # regardless of what is listening on the port -- reporting the port instead would
    # answer a question the user did not ask. Then the port, before the expensive
    # build, so a collision is not preceded by a slow history seed.
    validate_request(store, mode=mode, trade_date=trade_date, symbols=symbols)
    check_port_free(host, port)

    runtime = build_runtime(
        store, mode=mode, trade_date=trade_date, speed=speed,
        symbols=symbols, tick_seconds=tick_seconds,
    )
    app = create_app(runtime)

    banner = "REPLAY -- NOT LIVE" if runtime.mode == "REPLAY" else "LIVE"
    print()
    print(f"  {banner} dashboard: http://{host}:{port}")
    if runtime.mode == "REPLAY":
        print(f"  Replaying {trade_date} at {speed:.0f}x. This is not today's market.")
    print("  Advisory only -- this never places an order.")
    print()

    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
