"""Session runtime: one feed, the open positions, and the advice they produce.

Mode is the only thing that differs between LIVE and REPLAY, and it differs in exactly
two places: which feed object is constructed, and what ``now()`` returns. Everything
downstream -- evaluation, P&L, the payload the UI renders -- is identical. That is
deliberate: a replay that exercised a different code path would prove nothing about
the live one.

REPLAY drives a virtual clock through stored bars at a chosen speed, so a whole
session can be watched in a couple of minutes outside market hours.

Nothing here places an order.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from config.settings import (
    get_cost_settings,
    get_exit_engine_settings,
    get_fyers_settings,
)
from src.market.calendar import (
    IST,
    SESSION_CLOSE,
    SESSION_OPEN,
    INTRADAY_SQUARE_OFF,
    ensure_ist,
    is_trading_day,
    now_ist,
)
from src.positions.exit_engine import (
    bars_have_stopped,
    check_hypothetical_entry,
    evaluate,
)
from src.costs import unrealised_pnl
from src.positions.feed import MultiSymbolReplayFeed
from src.positions.indicators import compute_indicators
from src.positions.live_feed import expected_completed_minutes
from src.positions.models import (
    Position,
    PositionRuntime,
    PositionState,
    completed_bars,
)
from src.positions.store import PositionStore

log = logging.getLogger("fno.api.runtime")

__all__ = [
    "SessionRuntime", "Mode", "DuplicatePosition", "FeedHealth", "TOKEN_INVALID",
]


class DuplicatePosition(ValueError):
    """An identical open position is already recorded for this trade date."""


class Mode:
    LIVE = "LIVE"
    REPLAY = "REPLAY"


def _session_state(now: datetime) -> str:
    """Coarse market state for the header. Never inferred from the feed."""
    if not is_trading_day(now):
        return "CLOSED (not a trading day)"
    t = now.timetz().replace(tzinfo=None)
    if t < SESSION_OPEN:
        return "PRE-OPEN"
    if t >= SESSION_CLOSE:
        return "CLOSED"
    if t >= INTRADAY_SQUARE_OFF:
        return "SQUARE-OFF WINDOW"
    return "OPEN"


@dataclass
class _Subscriber:
    queue: asyncio.Queue


#: The one suppression cause this boundary knows about. Deliberately NOT a
#: :class:`~src.positions.models.PositionState`: an expired credential is a fact about
#: our authorisation, not a state the position is in, and the exit engine stays
#: credential-agnostic. It is also NOT the same thing as STALE_DATA -- a quiet socket
#: and a lapsed token have different causes and different fixes, and collapsing them
#: would send the user to restart a feed that is working.
TOKEN_INVALID = "TOKEN_INVALID"

#: A JWT shape. ``FeedHealth.detail`` is built from an auth exception message and then
#: rendered into /health, onto the dashboard banner, and into a log line -- three places
#: a live credential must never reach. No auth error in src/data/fyers_auth.py embeds a
#: token today (TokenBundle's repr is redacted and every message carries paths, dates or
#: field names). This is what keeps that true if one ever changes.
_TOKEN_SHAPED = re.compile(r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-.]{6,}")


def _redact(message: str) -> str:
    """Strip anything token-shaped from a message before it leaves the process."""
    return _TOKEN_SHAPED.sub("<redacted token>", message)


@dataclass(frozen=True)
class FeedHealth:
    """Whether price-based advice is permitted right now, and why not if it is not.

    An infrastructure result, returned by :meth:`SessionRuntime.feed_health` and
    embedded in ``/health``. It gates evaluation; it never becomes a PositionState and
    never reaches the exit engine.
    """

    #: False in REPLAY -- a replayed session needs no credential and reads none.
    applicable: bool
    token_valid: bool
    advice_suppressed: bool
    #: ``TOKEN_INVALID`` or None. A machine-readable cause, never prose to grep.
    reason: str | None
    #: One actionable line for a human. Never contains the token.
    detail: str
    checked_at: datetime

    def as_dict(self) -> dict:
        return {
            "applicable": self.applicable,
            "token_valid": self.token_valid,
            "advice_suppressed": self.advice_suppressed,
            "reason": self.reason,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat(),
        }


def _event_risk_field(report, symbol: str) -> dict:
    """The event-risk annotation attached to a card or a check-entry row.

    Deliberately shaped so it cannot become a ranking input: ``risk`` is one of three
    unordered strings, ``categories`` names kinds of event and never a direction, and
    there is no number anywhere in it. A symbol the report does not mention is UNKNOWN,
    never CLEAR.
    """
    found = report.for_symbol(symbol) if report is not None else None
    if found is None:
        from src.market.event_risk import UNKNOWN

        return {
            "risk": UNKNOWN,
            "categories": [],
            "reason": f"No event-risk information for {symbol}.",
        }
    return {"risk": found.risk, "categories": list(found.categories),
            "reason": found.reason}


class SessionRuntime:
    """Holds the feed and the positions, and evaluates one against the other."""

    def __init__(
        self,
        *,
        mode: str,
        store: PositionStore,
        symbols: Sequence[str],
        trade_date: date,
        feed,
        speed: float = 1.0,
        tick_seconds: float = 1.0,
        engine_cfg=None,
        cost_cfg=None,
        event_risk_source=None,
        check_log=None,
        token_loader=None,
    ) -> None:
        self.mode = mode
        self.store = store
        self.symbols = [s.strip().upper() for s in symbols]
        self.trade_date = trade_date
        self.feed = feed
        self.speed = speed
        self.tick_seconds = tick_seconds
        self.engine_cfg = engine_cfg or get_exit_engine_settings()
        self.cost_cfg = cost_cfg or get_cost_settings()
        #: Optional. Injected rather than constructed, same rule as the feed: a source
        #: reached for inside a method cannot be driven by a test. ``None`` means every
        #: symbol reports UNKNOWN -- never CLEAR.
        self.event_risk_source = event_risk_source
        #: The "checked today" scoreboard. Persisted beside the store so the sample
        #: accumulates across sessions; injectable so tests get their own file.
        from src.positions.check_log import CheckLog  # noqa: PLC0415 - avoids a cycle

        self.check_log = check_log if check_log is not None else CheckLog(store.root)

        #: Reads the cached broker token. Injected so the LIVE-only suppression path is
        #: testable with no broker, no filesystem and no market. Built lazily and never
        #: called in REPLAY.
        self._token_loader = token_loader

        #: Wall clock when this process built the runtime -- never the virtual clock.
        #: Used only to report how old the running server is.
        self.started_at = now_ist()

        self._virtual_now: datetime | None = None
        self._subscribers: list[_Subscriber] = []
        self._task: asyncio.Task | None = None
        self._runtimes: dict[str, PositionRuntime] = {}
        self._last_payload: dict[str, Any] = {}
        self._last_advice_logged: dict[str, tuple[str, str]] = {}
        self._market_structure_last_bar: dict[str, datetime] = {}
        self._market_structure_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._market_alert_seen_ids: set[str] = set()
        self._market_alerts: list[dict[str, Any]] = []
        self._market_alerts_initialized = False
        self._stopped = False

        if mode == Mode.REPLAY:
            start = getattr(feed, "first_bar_time", lambda: None)()
            self._virtual_now = start or datetime.combine(
                trade_date, SESSION_OPEN, tzinfo=IST
            )

    # ---- clock -----------------------------------------------------------------

    def now(self) -> datetime:
        """Wall clock in LIVE, virtual clock in REPLAY. The only mode-dependent read."""
        if self.mode == Mode.REPLAY and self._virtual_now is not None:
            return self._virtual_now
        return now_ist()

    # ---- positions --------------------------------------------------------------

    def open_positions(self) -> list[Position]:
        return self.store.list_positions(self.trade_date, status="OPEN")

    def add_position(
        self,
        symbol: str,
        quantity: int,
        entry_price: Decimal,
        entry_time: datetime | None = None,
        stop_loss: Decimal | None = None,
        target: Decimal | None = None,
        position_id: str | None = None,
        allow_duplicate: bool = False,
    ) -> Position:
        """Record a fill the user already made. This never places an order.

        Rejects an EXACT duplicate of an open position -- same symbol, quantity, entry
        price and entry time -- unless ``allow_duplicate=True``. Two identical cards on
        the dashboard are indistinguishable from one position rendered twice, and the
        difference matters: it is the difference between holding 100 shares and 200.
        Averaging into a position at the same price at the same second is not a thing
        that happens; a double-submitted form is.
        """
        import uuid

        symbol = symbol.strip().upper()
        when = ensure_ist(entry_time or self.now())

        if not allow_duplicate:
            for existing in self.open_positions():
                if (existing.symbol == symbol
                        and existing.quantity == quantity
                        and existing.entry_price == Decimal(str(entry_price))
                        and existing.entry_time == when):
                    raise DuplicatePosition(
                        f"{symbol} x{quantity} @ {entry_price} at "
                        f"{when:%H:%M:%S} is already recorded as "
                        f"{existing.position_id}. If you really did buy the same "
                        f"quantity twice at the same price and second, re-send with "
                        f"allow_duplicate=true; otherwise this is a double submit."
                    )

        position = Position(
            position_id=position_id or f"{symbol}-{uuid.uuid4().hex[:8]}",
            symbol=symbol,
            quantity=quantity,
            entry_price=entry_price,
            entry_time=when,
            stop_loss=stop_loss,
            target=target,
        )
        self.store.save_position(position)
        runtime = PositionRuntime.initial(position)
        self.store.save_runtime(runtime, position.trade_date)
        self._runtimes[position.position_id] = runtime
        log.info("recorded %s %s x%d @ %s",
                 position.position_id, symbol, quantity, entry_price)
        return position

    def close_position(self, position_id: str, exit_price: Decimal,
                       exit_time: datetime | None = None) -> dict:
        """Record a manual exit. This does NOT place anything with a broker."""
        from src.costs import realised_pnl

        position = self.store.get_position(position_id, self.trade_date)
        if position is None:
            raise KeyError(position_id)
        runtime = self._runtime_for(position)
        quantity = runtime.remaining_quantity
        self.store.close_position(position_id, self.trade_date, exit_price,
                                  ensure_ist(exit_time or self.now()))
        self._runtimes.pop(position_id, None)
        pnl = realised_pnl(position.entry_price, exit_price, quantity, self.cost_cfg)
        return {
            "position_id": position_id,
            "symbol": position.symbol,
            "quantity": quantity,
            "exit_price": str(exit_price),
            "gross_pnl": str(pnl.gross_pnl),
            "net_pnl": str(pnl.net_pnl),
            "costs": str(pnl.costs.total),
            "summary": pnl.summary(),
        }

    def _runtime_for(self, position: Position) -> PositionRuntime:
        rt = self._runtimes.get(position.position_id)
        if rt is None:
            rt = self.store.load_or_init_runtime(position)
            self._runtimes[position.position_id] = rt
        return rt

    # ---- token gate ---------------------------------------------------------------

    def _load_token(self):
        if self._token_loader is None:
            from src.data.fyers_auth import FyersAuth  # noqa: PLC0415 - LIVE only

            auth = FyersAuth(get_fyers_settings(), clock=self.now)
            self._token_loader = auth.load
        return self._token_loader()

    def feed_health(self) -> FeedHealth:
        """Is price-based advice permitted right now? Re-read on EVERY call.

        The cache is genuinely re-read each time rather than remembered, and that is a
        deliberate cost. A remembered bundle stays valid for its whole IST day, so a
        token deleted, replaced or revoked out of band would go unnoticed until
        midnight -- while the dashboard kept advising. One small JSON read per
        evaluation buys immediate detection in BOTH directions: invalidation the moment
        the cache goes, and recovery the moment ``python -m src.cli auth`` writes a new
        one, with no restart.

        (This does mean a read per tick. That was already true of the previous
        display-only token check, so it is not a regression; if it ever matters, the
        fix is the stat-stamp pattern used by ``StoreEventRiskSource._load``, not a
        blind time-based cache.)

        REPLAY short-circuits before any credential code is imported. A replayed session
        needs no token and must not go looking for one.
        """
        now = self.now()
        if self.mode == Mode.REPLAY:
            return FeedHealth(
                applicable=False, token_valid=True, advice_suppressed=False,
                reason=None, detail="REPLAY uses stored bars and needs no broker token.",
                checked_at=now,
            )

        try:
            bundle = self._load_token()
        except Exception as exc:  # noqa: BLE001 - every auth failure suppresses
            detail = _redact(
                str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
            log.warning("advice suppressed: %s: %s", TOKEN_INVALID, detail)
            return FeedHealth(
                applicable=True, token_valid=False, advice_suppressed=True,
                reason=TOKEN_INVALID,
                detail=(
                    f"{detail} Price-based advice is suppressed until a valid token "
                    f"exists. Run: python -m src.cli auth"
                ),
                checked_at=now,
            )

        if bundle is None or not bundle.is_valid(now):
            log.warning("advice suppressed: %s (cached token is not valid now)",
                        TOKEN_INVALID)
            return FeedHealth(
                applicable=True, token_valid=False, advice_suppressed=True,
                reason=TOKEN_INVALID,
                detail=(
                    "The cached Fyers token is not valid for right now. SEBI requires "
                    "daily regeneration. Run: python -m src.cli auth"
                ),
                checked_at=now,
            )

        return self._token_ok(bundle, now)

    @staticmethod
    def _token_ok(bundle, now) -> FeedHealth:
        return FeedHealth(
            applicable=True, token_valid=True, advice_suppressed=False, reason=None,
            detail=(
                f"Token valid, {bundle.seconds_remaining(now) / 60:.0f} min remaining."
            ),
            checked_at=now,
        )

    # ---- evaluation --------------------------------------------------------------

    def evaluate_all(self, event_risk_report=None) -> list[dict]:
        """Advice for every open position, as the UI renders it.

        A failure evaluating ONE position yields an ERROR card for that position and
        does not stop the others. This is deliberate and it is not a silent fallback:
        the exception text goes on the card, in the loudest style the UI has.

        The alternative was letting it propagate, which is what used to happen -- one
        unformattable label raised out of evaluate(), 500'd /positions AND the
        websocket snapshot, and left a blank page while /health kept returning 200.
        Losing every position's advice because one of them broke is the worst
        available outcome at 15:10.
        """
        now = self.now()
        events = event_risk_report if event_risk_report is not None else self.event_risk()
        health = self.feed_health()
        out: list[dict] = []
        for position in self.open_positions():
            try:
                runtime = self._runtime_for(position)
                if health.advice_suppressed:
                    # The engine is not called at all: it is pure and would happily
                    # produce price-based advice from bars we can no longer verify.
                    # Nothing is persisted either -- the trail, the breakeven flag and
                    # the last state stay exactly as the last authorised evaluation
                    # left them.
                    card = self._suppressed_card(position, runtime, health, now)
                else:
                    bars = self.feed.bars(position.symbol)
                    advice = evaluate(
                        position, runtime, bars, now,
                        self.feed.last_tick_at(position.symbol),
                        engine_cfg=self.engine_cfg, cost_cfg=self.cost_cfg,
                    )
                    self.store.save_runtime(runtime, position.trade_date)
                    card = self._card(position, runtime, advice, now)
            except Exception as exc:  # noqa: BLE001 - surfaced on the card, never hidden
                log.exception("evaluating %s failed", position.position_id)
                card = self._error_card(position, exc, now)
            # Annotation only. The advice above was computed without reference to it
            # and is not altered here -- see SessionRuntime.event_risk.
            card["event_risk"] = _event_risk_field(events, position.symbol)
            self._record_exit_advice(card, position.symbol)
            out.append(card)
        return out

    def _record_exit_advice(self, card: dict, symbol: str) -> None:
        """Append an advice snapshot once per completed bar or state change."""
        if card.get("advice_suppressed"):
            return
        bars = self.feed.bars(symbol)
        if not bars:
            return
        bar_at = bars[-1].close_time.isoformat()
        state = str(card.get("state", "UNKNOWN"))
        marker = (bar_at, state)
        position_id = card.get("position_id", symbol)
        if self._last_advice_logged.get(position_id) == marker:
            return
        from src.journal.store import JournalStore

        JournalStore(self.store.root).append_advice({
            "position_id": position_id, "symbol": symbol,
            "asof": self.now().isoformat(), "bar_at": bar_at,
            "state": state, "reason": card.get("reason", ""),
            "last_price": card.get("last_price"),
            "suggested_exit_quantity": card.get("suggested_exit_quantity", 0),
            "gross_pnl": card.get("gross_pnl"), "net_pnl": card.get("net_pnl"),
            "costs": card.get("costs"), "stale": bool(card.get("feed_stale")),
            "indicators": card.get("indicators"),
        })
        self._last_advice_logged[position_id] = marker

    def _suppressed_card(self, position, runtime, health: FeedHealth, now) -> dict:
        """A card for a position whose price-based advice is suppressed.

        Two things still have to be true here, and they pull in opposite directions:

        * **FORCE_EXIT is a clock fact and must still fire.** Square-off needs no price
          and no credential, and going silent at 15:15 about a position that is really
          open is the worst available outcome. :func:`square_off_reason` is consulted
          either way.
        * **Nothing else may be advised, and nothing may be persisted.** The last
          authorised state is shown, explicitly marked as NOT current, so the card says
          what was last known rather than implying it still holds.

        Every price carries its age and is flagged ``price_is_current: False``. Any P&L
        is computed from the last known bar and labelled stale, or is absent entirely --
        never presented as a live figure.
        """
        from src.positions.exit_engine import square_off_reason

        bars = completed_bars(self.feed.bars(position.symbol), now)
        snap = compute_indicators(bars, session_date=position.trade_date) if bars else None
        pnl = None
        if snap is not None:
            pnl = unrealised_pnl(
                position.entry_price, snap.last_price, runtime.remaining_quantity,
                self.cost_cfg, price_is_stale=True,
            )

        force = square_off_reason(position, now)
        if force is not None:
            _, force_reason = force
            state, current, exit_qty = PositionState.FORCE_EXIT.value, True, \
                runtime.remaining_quantity
            reason = (
                f"{force_reason} {health.detail} The P&L below is from the last price "
                f"seen while authorised and is NOT current -- verify in your broker app."
            )
        else:
            last = runtime.last_state
            state = last.value if last is not None else "UNKNOWN"
            current, exit_qty = False, 0
            seen = ("never evaluated" if runtime.last_evaluated_at is None
                    else f"as of {runtime.last_evaluated_at:%H:%M:%S} IST")
            reason = (
                f"Advice suppressed: {health.reason}. {health.detail} The state shown "
                f"({state}) is the last one reached while authorised, {seen}, and is "
                f"NOT current. No price-based advice is being given and no state has "
                f"been recorded."
            )

        return {
            "position_id": position.position_id,
            "symbol": position.symbol,
            "quantity": position.quantity,
            "remaining_quantity": runtime.remaining_quantity,
            "entry_price": str(position.entry_price),
            "entry_time": position.entry_time.isoformat(),
            "minutes_held": round((now - position.entry_time).total_seconds() / 60, 1),
            "state": state,
            "state_is_current": current,
            "advice_suppressed": True,
            "suppressed_reason": health.reason,
            "token_valid": health.token_valid,
            "rule": None,
            "reason": reason,
            "suggests_exit": exit_qty > 0,
            "suggested_exit_quantity": exit_qty,
            "last_price": None if snap is None else snap.last_price,
            "price_age_seconds": (
                None if snap is None else round((now - snap.asof).total_seconds(), 1)),
            "price_is_current": False,
            "gross_pnl": None if pnl is None else str(pnl.gross_pnl),
            "net_pnl": None if pnl is None else str(pnl.net_pnl),
            "gross_pct": None if pnl is None else str(pnl.gross_pct),
            "net_pct": None if pnl is None else str(pnl.net_pct),
            "costs": None if pnl is None else str(pnl.costs.total),
            "pnl_is_stale": pnl is not None,
            "feed_stale": self.feed.status.is_stale(now),
            "stale_seconds": None,
            "stop": None if runtime.current_stop is None else str(runtime.current_stop),
            "initial_stop": (
                None if position.stop_loss is None else str(position.stop_loss)),
            "stop_moved": False,
            "breakeven_armed": runtime.breakeven_armed,
            "highest_price": runtime.highest_price,
            "target1": None,
            "entry_verdict": runtime.entry_verdict or None,
            "indicators": None,
            "evaluated_at": now.isoformat(),
        }

    def _error_card(self, position, exc: Exception, now) -> dict:
        """A card saying this position could not be evaluated, and why.

        Carries no price and no P&L: there are no numbers to show, and inventing a
        neutral-looking zero would be worse than an obvious hole.
        """
        return {
            "position_id": position.position_id,
            "symbol": position.symbol,
            "quantity": position.quantity,
            "entry_price": str(position.entry_price),
            "entry_time": position.entry_time.isoformat(),
            "state": "ERROR",
            "state_is_current": True,
            "advice_suppressed": True,
            "suppressed_reason": "EVALUATION_FAILED",
            "token_valid": True,
            "price_is_current": False,
            "rule": None,
            "reason": (
                f"Could not evaluate this position: {type(exc).__name__}: {exc}. "
                f"No advice is available for it. The other positions on this page are "
                f"unaffected. Use `python -m src.cli watch` as a cross-check."
            ),
            "suggests_exit": False,
            "suggested_exit_quantity": 0,
            "last_price": None,
            "price_age_seconds": None,
            "gross_pnl": None,
            "net_pnl": None,
            "gross_pct": None,
            "net_pct": None,
            "costs": None,
            "stop": None,
            "target": None,
            "entry_verdict": None,
            "evaluated_at": now.isoformat(),
        }

    def _card(self, position, runtime, advice, now) -> dict:
        """One position card. Every price carries its age; P&L always carries both."""
        pnl = advice.pnl
        price_age = (
            None if advice.indicators is None
            else round((now - advice.indicators.asof).total_seconds(), 1)
        )
        return {
            "position_id": position.position_id,
            "symbol": position.symbol,
            "quantity": position.quantity,
            "remaining_quantity": runtime.remaining_quantity,
            "entry_price": str(position.entry_price),
            "entry_time": position.entry_time.isoformat(),
            "minutes_held": round((now - position.entry_time).total_seconds() / 60, 1),
            "last_price": advice.last_price,
            "price_age_seconds": price_age,
            "state": advice.state.value,
            "state_is_current": True,
            "advice_suppressed": False,
            "suppressed_reason": None,
            "token_valid": True,
            "price_is_current": not advice.feed_stale,
            "rule": advice.rule.value,
            "reason": advice.reason,
            "suggested_exit_quantity": advice.suggested_exit_quantity,
            "stop": None if advice.stop_price is None else str(advice.stop_price),
            "initial_stop": None if position.stop_loss is None else str(position.stop_loss),
            "stop_moved": (
                advice.stop_price is not None and position.stop_loss is not None
                and advice.stop_price != position.stop_loss
            ),
            "breakeven_armed": runtime.breakeven_armed,
            "highest_price": runtime.highest_price,
            "target1": None if advice.target1 is None else str(advice.target1),
            "gross_pnl": None if pnl is None else str(pnl.gross_pnl),
            "net_pnl": None if pnl is None else str(pnl.net_pnl),
            "gross_pct": None if pnl is None else str(pnl.gross_pct),
            "net_pct": None if pnl is None else str(pnl.net_pct),
            "costs": None if pnl is None else str(pnl.costs.total),
            "pnl_is_stale": bool(pnl is not None and pnl.price_is_stale),
            "feed_stale": advice.feed_stale,
            "stale_seconds": advice.stale_seconds,
            "entry_verdict": runtime.entry_verdict or None,
            "indicators": None if advice.indicators is None else {
                "vwap": advice.indicators.vwap,
                "atr14": advice.indicators.atr14,
                "ema9": advice.indicators.ema9,
                "ema21": advice.indicators.ema21,
                "rsi14": advice.indicators.rsi14,
                "bars_used": advice.indicators.bars_used,
            },
        }

    # ---- event risk -------------------------------------------------------------

    def event_risk(self):
        """Event-risk report for the subscribed symbols. An EXCLUSION FILTER.

        **This never changes any advice.** It annotates. A symbol under
        ELEVATED_EVENT_RISK still gets its normal exit advice, still gets FORCE_EXIT at
        square-off, and still gets STALE_DATA when the feed dies. Suppressing advice on
        an event day would be the worst possible reading of "normal technical reasoning
        does not apply": the one thing you certainly still need on a day the stock is
        gapping around on news is to be told to be flat by 15:15.
        ``test_event_risk_never_changes_advice`` pins that.

        REPLAY always yields UNKNOWN, and that is correct rather than a limitation.
        Announcements are forward-collected only; there is no historical corpus and
        there deliberately never will be, so a replay of a past session has nothing
        contemporaneous to consult. Rendering it CLEAR would state, of every historical
        date, that nothing happened.
        """
        from src.market.event_risk import EventRiskReport

        now = self.now()
        if self.mode == Mode.REPLAY:
            return EventRiskReport.unavailable(
                self.symbols, self.trade_date, now,
                "REPLAY: event risk is forward-collected only. There is no historical "
                "announcements data for this date and there deliberately never will be "
                "-- retroactively edited filings cannot be used to reconstruct what was "
                "knowable at the time. UNKNOWN is the honest answer, not CLEAR.",
            )
        if self.event_risk_source is None:
            return EventRiskReport.unavailable(
                self.symbols, self.trade_date, now,
                "No announcements source is configured for this session, so nothing "
                "can be cleared. Start the collector and relaunch the dashboard.",
            )
        try:
            return self.event_risk_source.assess(self.symbols, self.trade_date, now)
        except Exception as exc:  # noqa: BLE001 - a broken source must not clear anything
            log.exception("event risk assessment failed")
            return EventRiskReport.unavailable(
                self.symbols, self.trade_date, now,
                f"Event-risk assessment failed: {type(exc).__name__}: {exc}. Treating "
                f"every symbol as UNKNOWN rather than CLEAR.",
            )

    # ---- entry check ---------------------------------------------------------------

    def check_entry(self, symbol: str, price: Decimal | None = None,
                    stop: Decimal | None = None, *, event_risk_report=None,
                    source: str = "SINGLE", log_it: bool = True) -> dict:
        """Evaluate a hypothetical entry. Creates no position and places no order.

        It DOES append a line to the check log (the "checked today" scoreboard), which
        is the one thing here that persists. That is an observation, not a decision: no
        position is created, no runtime state is written, and nothing reaches a broker.
        The response is unchanged by logging -- deliberately, so the bulk and single
        endpoints stay byte-for-byte identical.

        Only a real verdict is logged. NOT_SUBSCRIBED and NO_BAR_YET evaluated nothing,
        have no price to measure from, and could never settle; logging them would fill
        the scoreboard with rows that can never have an outcome.

        ``event_risk_report`` lets a caller checking many symbols assess event risk once
        instead of per symbol. It changes no value: the field is identical either way,
        which is what keeps the bulk and single endpoints byte-for-byte equal.

        The event-risk annotation does NOT feed the verdict. ``valid`` is the same
        three technical predicates it has always been; an event flag sits beside it and
        is for the human to weigh.
        """
        symbol = symbol.strip().upper()
        events = (event_risk_report if event_risk_report is not None
                  else self.event_risk())

        # Two different reasons to have no bars, and only one of them resolves by
        # waiting. An unsubscribed symbol has no feed behind it, so its first bar is
        # never going to arrive -- saying "not yet" would send the user back to refresh
        # a panel that cannot change.
        if symbol not in {s.upper() for s in self.symbols}:
            tracked = ", ".join(sorted(self.symbols))
            return {
                "symbol": symbol, "valid": None, "reason": "NOT_SUBSCRIBED",
                "summary": (
                    f"{symbol} is not in the tracked universe, so no bar for it will "
                    f"ever arrive. Tracked symbols: {tracked}. Add it with --symbols "
                    f"and relaunch to check it."
                ),
                "problems": [], "unevaluated": [], "recorded": False,
                "tracked_symbols": sorted(self.symbols),
                "event_risk": _event_risk_field(events, symbol),
            }

        # A verdict is a statement about the price NOW. Two conditions make that
        # statement impossible, and both refuse rather than answer from a dead price.
        #
        # The rule this follows is the exit engine's: everything price-based is
        # suppressed on stale data. All three entry predicates -- VWAP, EMA/RSI, stop --
        # are price-based, computed from the same bars the exit rules use. Suppressing
        # the exit advice and then answering the entry question from the same stale bars
        # would be incoherent, and the answer would be worse than incoherent: "price is
        # already below VWAP" is present tense about a price that is an hour and a half
        # old. That is the header-says-LIVE-with-no-data failure in a different place.
        #
        # It also cannot settle. A check made after the close has a settle horizon
        # already in the past with no future bars to reach, so it would go straight to
        # UNRESOLVED and add a dead row to the scoreboard.
        now = self.now()
        state = _session_state(now)
        if state.startswith("CLOSED") or state == "PRE-OPEN":
            return {
                "symbol": symbol, "valid": None, "reason": "MARKET_CLOSED",
                "summary": (
                    f"The market is {state.lower()} at {now:%H:%M}, so there is no "
                    f"current price to judge an entry against. Nothing was logged."
                ),
                "problems": [], "unevaluated": [], "recorded": False,
                "session_state": state,
                "event_risk": _event_risk_field(events, symbol),
            }

        # "No bar has ever arrived" is NOT automatically a stale feed. Before the
        # session's first minute completes it is the normal state and it resolves by
        # waiting -- which is the NO_BAR_YET path, and saying STALE_FEED there would be
        # the same "waiting will not help" mistake in reverse. After the session HAS
        # produced completed minutes, the same silence means the feed is not delivering.
        last_tick = self.feed.last_tick_at(symbol)
        if last_tick is None and expected_completed_minutes(self.trade_date, now) == 0:
            stopped, age = False, None
        else:
            stopped, age = bars_have_stopped(now, last_tick, self.engine_cfg)
        if stopped:
            age_text = "no data has ever arrived" if age is None else f"{age / 60:.0f}m old"
            return {
                "symbol": symbol, "valid": None, "reason": "STALE_FEED",
                "summary": (
                    f"The feed for {symbol} has stopped: the last completed bar is "
                    f"{age_text}. Exit advice is already suppressed on stale data and "
                    f"an entry verdict is the same price-based judgement, so it is "
                    f"suppressed too. Nothing was logged."
                ),
                "problems": [], "unevaluated": [], "recorded": False,
                "stale_seconds": age,
                "event_risk": _event_risk_field(events, symbol),
            }

        bars = self.feed.bars(symbol)
        check = check_hypothetical_entry(
            bars, self.now(), price, stop,
            session_date=self.trade_date, engine_cfg=self.engine_cfg,
        )
        if check is None:
            return {
                "symbol": symbol, "valid": None, "reason": "NO_BAR_YET",
                "summary": (
                    f"First bar for {symbol} has not closed yet — nothing to judge "
                    f"the entry against."
                ),
                "problems": [], "unevaluated": [], "recorded": False,
                "event_risk": _event_risk_field(events, symbol),
            }

        if log_it:
            self._log_check(symbol, check, source)

        return {
            "symbol": symbol,
            "valid": check.valid,
            "reason": None,  # a verdict was reached; nothing stood in the way
            "event_risk": _event_risk_field(events, symbol),
            "asof": check.asof.isoformat(),
            "entry_price": str(check.entry_price),
            "price_at_check": check.price_at_check,
            "summary": check.summary(),
            "problems": list(check.problems),
            "unevaluated": list(check.unevaluated),
            "indicators": None if check.indicators is None else {
                "vwap": check.indicators.vwap,
                "atr14": check.indicators.atr14,
                "ema9": check.indicators.ema9,
                "ema21": check.indicators.ema21,
                "rsi14": check.indicators.rsi14,
            },
            "recorded": False,
        }

    def check_entry_all(self) -> dict:
        """Run the SAME entry check across subscribed stock symbols. Records NOTHING.

        **This is a filter, not a ranking, and the distinction is the whole point.**

        It answers one question per symbol -- "is this entry obviously unsound right
        now?" -- and it answers it independently for each. It does not compare symbols
        to each other, and there is deliberately no way to make it: no score, no rank,
        no "best" candidate, and the order is alphabetical by symbol and nothing else.

        Module B ranked this universe and produced no edge over a naive comparator at
        any horizon tested (CLAUDE.md 2.3). A panel that started implying which symbol
        to prefer would be that failed engine wearing a different hat, which is why
        the API shape refuses to carry the fields such a thing would need.
        SOUND means "not obviously unsound". It does not mean the stock will rise.

        NIFTY and BANKNIFTY, when subscribed, are market-context instruments and are
        deliberately excluded from this stock-entry filter. Each stock row is exactly
        what ``check_entry`` returns for that symbol -- same predicates, one function.
        """
        events = self.event_risk()
        from src.market.structure import INDEX_SYMBOLS

        rows = [self.check_entry(symbol, event_risk_report=events, source="BULK")
                for symbol in sorted(self.symbols) if symbol not in INDEX_SYMBOLS]
        return {
            "asof": self.now().isoformat(),
            "results": rows,
            "recorded": False,
            "ordering": "alphabetical",
            "is_ranking": False,
            "event_risk_state": events.data_state,
            "disclaimer": (
                "SOUND means only that this entry is not obviously unsound right now. "
                "It is NOT a prediction that the price will rise, and these rows are "
                "not ranked -- they are listed alphabetically. Nothing here says which "
                "symbol to prefer. ELEVATED_EVENT_RISK is a separate exclusion: it "
                "means a material announcement landed or is scheduled, so normal "
                "technical reasoning does not apply -- it is not a view on direction."
            ),
        }

    # ---- descriptive market view -------------------------------------------------

    def market_overview(self) -> dict:
        """Observed movement and breadth for this runtime's subscribed symbols.

        The alphabetic observation rows remain separate from Module B. The optional
        scanner sub-object is a transparent descriptive ordering of completed-bar
        movement and recent volume; it is not an entry signal or return forecast.
        Missing symbols and indices stay visible in the coverage boundary.

        Data-availability boundary:
            Uses only completed bars returned by the runtime feed at ``now()``.
            No forming candle, future candle, or same-day delayed OI is used.
        """
        from src.market.structure import INDEX_SYMBOLS, market_observation, scanner_observation, sector_breadth
        from src.signals.universe import SECTORS
        from src.positions.exit_engine import feed_staleness
        from src.data.backfill import LIQUID_FNO_UNIVERSE
        from src.data.fno_universe import load_snapshot, snapshot_path

        now = self.now()
        rows, index_rows = [], []
        bars_by_symbol: dict[str, list] = {}
        for symbol in sorted(self.symbols):
            bars = self.feed.bars(symbol)
            bars_by_symbol[symbol] = bars
            last = self.feed.last_tick_at(symbol)
            stale, age = feed_staleness(now, last, self.engine_cfg)
            row = market_observation(
                symbol, bars, stale=stale,
                last_data_at=None if last is None else last.isoformat(),
            )
            row["data_age_seconds"] = None if age is None else round(age, 1)
            row["sector"] = "INDEX" if symbol in INDEX_SYMBOLS else SECTORS.get(symbol, "UNKNOWN")
            row["scanner"] = scanner_observation(bars) if symbol not in INDEX_SYMBOLS else None
            (index_rows if symbol in INDEX_SYMBOLS else rows).append(row)

        # Evaluate structure only when a new completed candle arrives. Seed existing
        # history silently so a server restart never replays old observations as alerts.
        alerts = self._update_market_alerts(rows) if self.mode == Mode.LIVE else []

        fresh = [r for r in rows if not r["stale"] and r["state"] != "NO_DATA"]
        up = sum(r["state"] == "UP_FROM_BASELINE" for r in fresh)
        down = sum(r["state"] == "DOWN_FROM_BASELINE" for r in fresh)
        flat = sum(r["state"] == "AT_BASELINE" for r in fresh)
        session_state = _session_state(now)
        market_active = session_state in {"OPEN", "SQUARE-OFF WINDOW"}
        scan_rows = [r for r in rows if r["scanner"] and r["scanner"]["eligible"]
                     and r["scanner"]["direction"] and (not market_active or not r["stale"])]
        def ranked(direction: str) -> list[dict]:
            leaders = sorted((r for r in scan_rows if r["scanner"]["direction"] == direction),
                             key=lambda r: (-r["scanner"]["score"], r["symbol"]))[:20]
            result = []
            for rank, row in enumerate(leaders, 1):
                structure = self._market_structure_for(row["symbol"], bars_by_symbol[row["symbol"]])
                result.append({**row, "rank": rank, "structure": {
                    "potential_zones": structure.get("order_blocks", [])[-3:],
                    "swing_highs": structure.get("swing_highs", [])[-3:],
                    "swing_lows": structure.get("swing_lows", [])[-3:],
                    "equal_highs": structure.get("equal_highs", []),
                    "equal_lows": structure.get("equal_lows", []),
                }})
            return result
        scanner = {"bullish": ranked("BULLISH"), "bearish": ranked("BEARISH"),
            "eligible_count": len(scan_rows), "universe_count": len(rows),
            "choppy_count": sum(r["scanner"] and r["scanner"]["reason"] == "CHOPPY_PATH" for r in rows),
            "not_ready_count": sum(not r["scanner"] or r["scanner"]["reason"] in
                                    {"NEED_MORE_BARS", "VOLUME_BASELINE_UNAVAILABLE"} for r in rows),
            "asof": now.isoformat(), "is_ranking": True,
            "formula": "Score = |first available bar open to latest completed close (%)| × min(recent volume ratio, 5) × path efficiency. Recent volume ratio compares average volume in the latest 5 completed bars with the average in up to the preceding 15 bars. Path efficiency below 0.20 is marked choppy and excluded.",
            "limitations": "Descriptive ranking only; no forecast or trade recommendation. OI is unavailable in the intraday candle feed and is excluded. Volume ratio is an intraday recent-versus-prior comparison, not a historical time-of-day norm. Ranks use fresh data during market hours; closed-session rows are historical context.",
            "mechanical_levels": "Potential zones, confirmed swing pivots and equal-price references are computed from completed candles. They do not identify verified institutional orders or project stop/target prices."}
        sector_rows = fresh if market_active else [r for r in rows if r["state"] != "NO_DATA"]
        universe_snapshot = load_snapshot(self.store.root)
        subscribed_stocks = {r["symbol"] for r in rows}
        if universe_snapshot and set(universe_snapshot.symbols).issubset(subscribed_stocks):
            universe = {"state": "FULL_CURRENT_MASTER", "as_of": universe_snapshot.as_of.isoformat(),
                        "master_symbols": len(universe_snapshot.symbols), "source": universe_snapshot.source}
        elif subscribed_stocks == set(LIQUID_FNO_UNIVERSE):
            universe = {"state": "MAINTAINED_LIQUID_SUBSET", "as_of": None,
                        "master_symbols": None, "source": None}
        else:
            universe = {"state": "CUSTOM_SUBSCRIBED_SET", "as_of": None,
                        "master_symbols": None, "source": None}
        if universe_snapshot is None and snapshot_path(self.store.root).exists():
            universe["state"] = "MASTER_STALE_OR_INVALID"
        return {
            "mode": self.mode,
            "asof": now.isoformat(),
            "trade_date": self.trade_date.isoformat(),
            "session_state": session_state,
            "coverage": {"subscribed": len(rows), "fresh": len(fresh), "universe": universe,
                         "stale": sum(bool(r["stale"]) for r in rows),
                         "without_bars": sum(r["state"] == "NO_DATA" for r in rows)},
            "breadth": {"up_from_baseline": up, "down_from_baseline": down,
                        "at_baseline": flat, "sample": len(fresh)},
            "sectors": sector_breadth(sector_rows, SECTORS),
            "sectors_are_historical": not market_active,
            "indices": {"state": "SUBSCRIBED" if index_rows else "NOT_SUBSCRIBED",
                        "symbols": index_rows,
                        "detail": "Index data is shown only when those indices are subscribed and fresh."},
            "symbols": rows,
            "scanner": scanner,
            "alerts": alerts,
            "alerts_disclaimer": "Completed-candle price patterns only. Equal highs/lows are price references, not verified resting orders; no order book or open-interest data is used.",
            "ordering": "alphabetical",
            "is_ranking": False,
            "definition": "Observed completed-bar change from the earliest available bar. "
                          "Descriptive only; it is not a forecast or a trade recommendation.",
        }

    def _update_market_alerts(self, rows: list[dict]) -> list[dict]:
        """Add newly observed patterns from completed bars during active live hours.

        Data-availability boundary:
            Each symbol is evaluated from ``feed.bars(symbol)`` through its latest
            completed bar. Historical patterns seed deduplication on first scan and
            are not emitted as new alerts.
        """
        active = _session_state(self.now()) in {"OPEN", "SQUARE-OFF WINDOW"}
        if not active:
            return list(self._market_alerts)
        for row in rows:
            symbol = row["symbol"]
            bars = self.feed.bars(symbol)
            if not bars:
                continue
            last_time = bars[-1].close_time
            if self._market_structure_last_bar.get(symbol) == last_time:
                continue
            structure = self._market_structure_for(symbol, bars)
            self._market_structure_last_bar[symbol] = last_time
            candidates = []
            for z in structure.get("order_blocks", []):
                candidates.append({"id": f"{symbol}|ZONE|{z['confirmed_at']}|{z['direction']}",
                    "symbol": symbol, "kind": "POTENTIAL_ZONE", "title": f"Potential {z['direction'].lower()} zone",
                    "at": z["confirmed_at"], "low": z["low"], "high": z["high"],
                    "detail": f"Completed-bar displacement: {z['displacement_atr']} prior ATR; zone is the last opposite candle."})
            for kind, levels, title in (("EQUAL_HIGH", structure.get("equal_highs", []), "Potential equal-high price reference"),
                                         ("EQUAL_LOW", structure.get("equal_lows", []), "Potential equal-low price reference")):
                for level in levels:
                    pivot_index = min(level["last_bar"] + 2, len(bars) - 1)
                    at = bars[pivot_index].close_time.isoformat()
                    candidates.append({"id": f"{symbol}|{kind}|{at}|{level['price']}", "symbol": symbol,
                        "kind": kind, "title": title, "at": at, "price": level["price"],
                        "detail": f"{level['touches']} confirmed pivots within the mechanical price tolerance; not evidence of resting orders."})
            for event in candidates:
                if event["id"] in self._market_alert_seen_ids:
                    continue
                self._market_alert_seen_ids.add(event["id"])
                if not self._market_alerts_initialized:
                    continue
                self._market_alerts.insert(0, event)
        self._market_alerts_initialized = True
        self._market_alerts = self._market_alerts[:40]
        return list(self._market_alerts)

    def _market_structure_for(self, symbol: str, bars) -> dict:
        """Cache structure from the supplied completed-bar history.

        Data-availability boundary:
            Uses only the supplied completed bars through the latest bar's close time;
            a forming candle and any later bar are excluded by the caller.
        """
        from src.market.structure import market_structure
        if not bars:
            return {"state": "INSUFFICIENT_BARS", "order_blocks": [], "swing_highs": [],
                    "swing_lows": [], "equal_highs": [], "equal_lows": []}
        last_time = bars[-1].close_time
        cached = self._market_structure_cache.get(symbol)
        if cached and cached[0] == last_time:
            return cached[1]
        structure = market_structure(bars)
        self._market_structure_cache[symbol] = (last_time, structure)
        self._market_structure_last_bar[symbol] = last_time
        return structure

    def market_chart(self, symbol: str, interval: int = 1) -> dict:
        """Completed OHLCV bars and potential structure for one subscribed symbol."""
        from src.market.structure import market_structure, resample_bars
        from src.positions.exit_engine import feed_staleness

        symbol = symbol.strip().upper()
        if symbol not in {s.upper() for s in self.symbols}:
            raise KeyError(symbol)
        now = self.now()
        bars = resample_bars(self.feed.bars(symbol), interval, now)
        last = self.feed.last_tick_at(symbol)
        stale, age = feed_staleness(now, last, self.engine_cfg)
        return {
            "symbol": symbol, "mode": self.mode, "interval_minutes": interval,
            "asof": now.isoformat(), "trade_date": self.trade_date.isoformat(),
            "stale": stale, "data_age_seconds": None if age is None else round(age, 1),
            "last_data_at": None if last is None else last.isoformat(),
            "candles": [{"time": b.ts.isoformat(), "open": b.open, "high": b.high,
                         "low": b.low, "close": b.close, "volume": b.volume}
                        for b in bars],
            "structure": market_structure(bars),
            "disclaimer": "Zones and swing levels are mechanical patterns from completed "
                          "candles. They do not confirm institutional orders or predict price.",
        }

    def record_journal_note(self, note) -> dict:
        """Append a manual taken/skipped note. Never creates a position or order."""
        from src.journal.store import JournalStore

        JournalStore(self.store.root).append(note)
        return {"recorded": True, "event_id": note.event_id,
                "note": "Saved to the local append-only journal. No order was placed."}

    def journal_history(self, limit: int = 300) -> dict:
        """Join manual decisions with actual closed fills and existing records."""
        from src.costs import realised_pnl
        from src.journal.store import JournalStore

        closed = []
        read_errors = []
        try:
            rows = self.store.query(
                "SELECT p.position_id, p.symbol, p.quantity, p.entry_price, p.entry_time, "
                "p.exit_price, p.exit_time, COALESCE(r.remaining_quantity, p.quantity) "
                "AS closed_quantity FROM positions p LEFT JOIN runtime r "
                "ON p.position_id = r.position_id WHERE p.status = 'CLOSED' "
                "ORDER BY p.exit_time DESC LIMIT ?", [limit],
            )
            for pid, symbol, quantity, entry, entry_at, exit_price, exit_at, closed_qty in rows:
                if exit_price is None or entry is None:
                    continue
                pnl = realised_pnl(entry, exit_price, int(closed_qty or quantity), self.cost_cfg)
                closed.append({
                    "position_id": pid, "symbol": symbol, "quantity": int(closed_qty or quantity),
                    "entry_price": str(entry), "entry_time": entry_at.isoformat(),
                    "exit_price": str(exit_price), "exit_time": exit_at.isoformat(),
                    "gross_pnl": str(pnl.gross_pnl), "net_pnl": str(pnl.net_pnl),
                    "costs": str(pnl.costs.total),
                })
        except Exception:
            log.exception("could not read closed trades for journal")
            read_errors.append("Closed fills could not be loaded; see the server log.")

        journal_store = JournalStore(self.store.root)
        advice = journal_store.advice_entries(limit)
        decisions = journal_store.entries(limit)
        closed_by_id = {row["position_id"]: row for row in closed}
        for decision in decisions:
            linked = closed_by_id.get(decision.get("position_id"))
            if linked:
                decision["outcome"] = {"state": "CLOSED", "net_pnl": linked["net_pnl"],
                                       "exit_time": linked["exit_time"]}
            elif decision.get("position_id"):
                decision["outcome"] = {"state": "POSITION_NOT_CLOSED"}
        checks = self.checked_log(limit)
        gross_total = sum((Decimal(r["gross_pnl"]) for r in closed), Decimal("0"))
        net_total = sum((Decimal(r["net_pnl"]) for r in closed), Decimal("0"))
        costs_total = sum((Decimal(r["costs"]) for r in closed), Decimal("0"))

        return {
            "trades": closed,
            "decisions": decisions,
            "checks": checks.get("rows", []),
            "exit_advice": advice,
            "analytics": {"closed_trade_count": len(closed), "gross_realised": str(gross_total),
                          "modelled_costs": str(costs_total), "net_realised": str(net_total),
                          "taken_notes": sum(d["decision"] == "TAKEN" for d in decisions),
                          "skipped_notes": sum(d["decision"] == "SKIPPED" for d in decisions),
                          "check_summary": checks.get("summary")},
            "read_errors": read_errors,
            "cost_model_verified": bool(getattr(self.cost_cfg, "rates_verified", False)),
            "asof": self.now().isoformat(),
            "disclaimer": "Closed trade P&L uses the entered fills and configured cost model. "
                          "Cost rates are provisional until reconciled with contract notes.",
        }

    # ---- the "checked today" scoreboard ---------------------------------------------

    def _log_check(self, symbol: str, check, source: str) -> None:
        """Append one evaluation to the ledger. Never raises into the endpoint.

        A broken log file must not take the entry check down with it: the check is the
        thing the user asked for, and the scoreboard is a record of it.
        """
        import uuid

        from src.positions.check_log import CheckRecord

        try:
            self.check_log.record(CheckRecord(
                check_id=uuid.uuid4().hex,
                symbol=symbol,
                trade_date=self.trade_date,
                checked_at=check.asof,
                verdict="SOUND" if check.valid else "UNSOUND",
                # The FACT half of the first problem, matching what the result card
                # leads with. The full text stays in the check response.
                reason=(check.problems[0].split(" -- ")[0].strip()
                        if check.problems else
                        "no entry condition was already violated"),
                price_at_check=check.price_at_check,
                source=source,
            ))
        except Exception:  # noqa: BLE001 - logged, never surfaced as a failed check
            log.exception("could not append to the check log")

    def _price_at_or_after(self, symbol: str, trade_date: date, when: datetime):
        """(price, at) from the first completed bar at or after ``when``, or None.

        Reads the live feed for the current session and the store for a past one. If the
        session ended before ``when`` was reached, the last bar that ever printed is the
        answer -- there is no later price and there never will be.

        Data-availability boundary:
            Completed bars only, and only bars at or after ``when``. Nothing here can
            reach back before the check it is settling.
        """
        bars = self.feed.bars(symbol) if trade_date == self.trade_date else []
        if not bars and self.store.has_bars(symbol, trade_date):
            bars = self.store.load_bars(symbol, trade_date)
        if not bars:
            return None
        for bar in bars:
            if bar.close_time >= when:
                return bar.close, bar.close_time
        if trade_date < self.trade_date or self.now() >= datetime.combine(
                trade_date, SESSION_CLOSE, tzinfo=IST):
            last = bars[-1]
            return last.close, last.close_time
        return None

    def settle_due_checks(self) -> int:
        """Freeze the outcome of every check whose horizon has passed.

        Called from :meth:`snapshot`, so it runs on the background loop AND on a plain
        GET -- a dashboard left open all day and one refreshed at 15:20 settle the same
        rows. Settling is idempotent: the first SETTLE line for a check wins on read.
        """
        now = self.now()
        settled = 0
        for entry in self.check_log.unsettled_due(now):
            record = entry.record
            from src.positions.check_log import settle_time

            due = settle_time(record.checked_at, record.trade_date)
            found = self._price_at_or_after(record.symbol, record.trade_date, due)
            if found is not None:
                self.check_log.settle(record.check_id, found[0], found[1])
                settled += 1
            elif record.trade_date < self.trade_date:
                # A past session with no bars we can reach. Say so rather than leaving
                # it on screen as though it were still running.
                self.check_log.mark_unresolved(
                    record.check_id,
                    f"No stored bars for {record.symbol} on {record.trade_date}, so "
                    f"what happened after this check cannot be determined.",
                )
        return settled

    def checked_log(self, limit: int = 200) -> dict:
        """The scoreboard: what was checked, and what happened after it.

        **This is a record of the past, not a list of what to buy**, and the shape is
        what keeps it one:

        * rows come back ordered by CHECK TIME, most recent first, and there is no
          parameter that changes that;
        * there is no sort-by-return, no filter by verdict, and no aggregate "top"
          view -- SOUND and UNSOUND always arrive together;
        * the change field is named ``change_since_you_checked_pct``, past tense,
          because it describes something that already happened.

        A ranking of symbols by outcome is the failed Module B engine wearing a
        different hat (CLAUDE.md 2.3); the only aggregate here is a test OF the check,
        not a league table of symbols.
        """
        from src.positions.check_log import SETTLE_MINUTES, summarise_verdicts

        # Settle before reading, so the scoreboard is correct on a bare GET even if the
        # background loop is not running. Idempotent: the first SETTLE line wins.
        try:
            self.settle_due_checks()
        except Exception:  # noqa: BLE001 - a broken ledger must not blank the panel
            log.exception("settling due checks failed")

        entries = self.check_log.entries()
        today = self.trade_date
        for entry in entries:
            if not entry.settled and not entry.unresolved \
                    and entry.record.trade_date == today:
                bars = self.feed.bars(entry.record.symbol)
                if bars:
                    entry.live_price = bars[-1].close
                    entry.live_at = bars[-1].close_time

        summary = summarise_verdicts(entries)
        return {
            "asof": self.now().isoformat(),
            "rows": [e.as_dict() for e in entries[:limit]],
            "total": len(entries),
            "ordering": "checked_at_desc",
            "is_ranking": False,
            "settle_minutes": SETTLE_MINUTES,
            "summary": summary.as_dict(),
            "disclaimer": (
                "A record of checks you already made, newest first. It is NOT a list of "
                "what to buy: rows are ordered by when you checked and by nothing else, "
                "there is no sort by return, SOUND and UNSOUND are always shown "
                "together, and no row is marked as the one to pick. The change column "
                "is past tense -- it describes what happened after a check, and says "
                "nothing about what happens next."
            ),
        }

    # ---- health ---------------------------------------------------------------------

    def health(self) -> dict:
        now = self.now()
        status = self.feed.status.as_dict(now)
        token = self._token_status()
        replay = None
        if self.mode == Mode.REPLAY:
            last = getattr(self.feed, "last_bar_time", lambda: None)()
            replay = {
                "cursor": now.isoformat(),
                "session_end": None if last is None else last.isoformat(),
                "speed": self.speed,
                "finished": bool(last and now >= last),
            }
        return {
            "mode": self.mode,
            "now": now.isoformat(),
            "trade_date": self.trade_date.isoformat(),
            "session_state": _session_state(now),
            "square_off_at": datetime.combine(
                self.trade_date, INTRADAY_SQUARE_OFF, tzinfo=IST).isoformat(),
            "feed": status,
            "token": token,
            # Whether advice is PERMITTED, and the reason if not. Distinct from
            # feed.stale on purpose: a quiet socket and a lapsed token are different
            # causes with different fixes.
            "feed_health": self.feed_health().as_dict(),
            "replay": replay,
            "symbols": self.symbols,
            # When THIS process started. A server left running from before a code change
            # serves the old routes to a freshly-loaded page, and from the browser that
            # is indistinguishable from a missing feature -- it is how /checks 404'd on
            # a dashboard whose code had the route. Reporting it lets the page say so.
            "started_at": self.started_at.isoformat(),
        }

    def _token_status(self) -> dict:
        """Token expiry for the header countdown. Never returns the token itself.

        Derived from the SAME loader and the SAME clock as :meth:`feed_health`, so the
        two can never disagree. They could, and did: this built its own ``FyersAuth``
        against the real wall clock while ``feed_health`` used the injected loader, so
        ``/health`` could report ``token.valid: true`` beside
        ``feed_health.token_valid: false`` -- a healthy-looking token badge sitting over
        suppressed advice, which is the one thing the header must never show. It also
        made every test of this field depend on whatever token happened to be cached on
        the developer's disk.
        """
        if self.mode == Mode.REPLAY:
            return {"applicable": False, "note": "replay mode uses stored bars"}
        now = self.now()
        try:
            bundle = self._load_token()
        except Exception as exc:  # noqa: BLE001 - surfaced to the header
            return {"applicable": True, "valid": False,
                    "error": _redact(str(exc).splitlines()[0])}
        if bundle is None or not bundle.is_valid(now):
            return {"applicable": True, "valid": False,
                    "error": "the cached token is not valid for the session clock"}
        return {
            "applicable": True,
            "valid": True,
            "expires_at": bundle.expires_at.isoformat(),
            "seconds_remaining": round(bundle.seconds_remaining(now)),
        }

    # ---- streaming -------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        sub = _Subscriber(asyncio.Queue(maxsize=8))
        self._subscribers.append(sub)
        if self._last_payload:
            sub.queue.put_nowait(self._last_payload)
        return sub.queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers = [s for s in self._subscribers if s.queue is not queue]

    def _broadcast(self, payload: dict) -> None:
        self._last_payload = payload
        for sub in list(self._subscribers):
            if sub.queue.full():
                # Drop the oldest rather than block: a slow client must never hold up
                # the session, and on a live dashboard the newest state is the only
                # one that matters.
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            sub.queue.put_nowait(payload)

    def snapshot(self) -> dict:
        # Settle here rather than only on the loop, so a dashboard left open all day and
        # one refreshed once at 15:20 freeze the same outcomes at the same horizon.
        try:
            self.settle_due_checks()
        except Exception:  # noqa: BLE001 - a broken ledger must not blank the page
            log.exception("settling due checks failed")
        events = self.event_risk()
        return {
            "health": self.health(),
            "positions": self.evaluate_all(events),
            "event_risk": events.as_dict(),
            "market": self.market_overview(),
        }

    async def run(self) -> None:
        """The loop. Advances the clock, evaluates, broadcasts."""
        log.info("runtime starting in %s mode for %s", self.mode, self.trade_date)
        try:
            while not self._stopped:
                if self.mode == Mode.REPLAY:
                    self._advance_replay()
                else:
                    self.feed.close_due_bars(self.now())
                self._broadcast(self.snapshot())
                await asyncio.sleep(self.tick_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("runtime loop failed")
            raise

    #: How far past the last stored bar the replay clock runs before halting. Enough
    #: to show the terminal state settling (FORCE_EXIT in particular) without the
    #: clock wandering indefinitely into a session that never happened.
    REPLAY_TAIL = timedelta(minutes=5)

    def _advance_replay(self) -> None:
        step = timedelta(seconds=self.tick_seconds * self.speed)
        proposed = (self._virtual_now or self.now()) + step
        last = getattr(self.feed, "last_bar_time", lambda: None)()

        # Clamp to the tail rather than overshooting by up to one step. At 600x a
        # single tick is 10 virtual minutes, so an unclamped clock parked itself at
        # 15:45 for a session whose last bar was 15:29 -- a quarter hour of market
        # that does not exist, displayed as the current time.
        if last is not None:
            proposed = min(proposed, last + self.REPLAY_TAIL)

        self._virtual_now = proposed
        self.feed.advance_to(self._virtual_now)
        if last is not None and self._virtual_now >= last + self.REPLAY_TAIL:
            log.info("replay reached the end of the stored session")
            self._stopped = True

    def stop(self) -> None:
        self._stopped = True
        try:
            self.feed.stop()
        except Exception:  # noqa: BLE001
            pass


def build_replay_runtime(
    store: PositionStore,
    symbols: Sequence[str],
    trade_date: date,
    *,
    speed: float = 60.0,
    tick_seconds: float = 1.0,
) -> SessionRuntime:
    """A runtime driven from stored bars. Required for testing outside market hours."""
    bars = {s: store.load_bars(s, trade_date) for s in symbols
            if store.has_bars(s, trade_date)}
    if not bars:
        raise ValueError(f"no stored bars for {trade_date} in {list(symbols)[:5]}")
    feed = MultiSymbolReplayFeed(bars)
    return SessionRuntime(
        mode=Mode.REPLAY, store=store, symbols=list(bars), trade_date=trade_date,
        feed=feed, speed=speed, tick_seconds=tick_seconds,
    )


def build_live_runtime(
    store: PositionStore,
    symbols: Sequence[str],
    trade_date: date,
    *,
    tick_seconds: float = 1.0,
) -> SessionRuntime:
    """A runtime driven from the live Fyers websocket, seeded from REST."""
    from src.data.fyers_auth import require_token
    from src.positions.live_feed import FyersLiveFeed

    from config.settings import get_settings
    from src.data.announcements import AnnouncementStore
    from src.market.event_risk import StoreEventRiskSource

    settings = get_fyers_settings()
    token = require_token(settings)
    feed = FyersLiveFeed(symbols, access_token=token.access_token, app_id=settings.app_id)
    feed.seed_history(trade_date)
    feed.start()
    # Reads the forward-collected ledger only; it never fetches. Attached in LIVE and
    # not in REPLAY because a replayed session has no contemporaneous observations to
    # consult, and SessionRuntime.event_risk short-circuits REPLAY to UNKNOWN anyway.
    return SessionRuntime(
        mode=Mode.LIVE, store=store, symbols=list(symbols), trade_date=trade_date,
        feed=feed, tick_seconds=tick_seconds,
        event_risk_source=StoreEventRiskSource(
            AnnouncementStore(get_settings().data_dir)),
    )
