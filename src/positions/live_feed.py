"""Live Fyers feed: websocket ticks, REST seeding, reconnect with gap backfill.

Satisfies the same interface as :class:`~src.positions.feed.ReplayFeed` --
``bars(symbol)`` and ``last_tick_at(symbol)`` -- so nothing downstream can tell which
one it is talking to. That is the property that lets the whole UI be exercised in
replay before it ever sees a live session.

Four things this has to get right, all of which are silent when wrong:

**Warm start.** A process that begins mid-session with an empty aggregator has no
ATR, no RSI and no VWAP for the next half hour, so it reports "warming up" on a
position that is already live. :meth:`FyersLiveFeed.seed_history` loads the session's
completed bars over REST first.

**Reconnect gap.** A dropped socket loses every tick until it comes back. Resuming
without backfilling leaves a hole that looks like a quiet market rather than an
outage, and the indicators quietly compute over the wrong window.
:meth:`FyersLiveFeed.reconnect` refetches the missing minutes over REST.

**Staleness.** Reported, never papered over. The engine decides what to do about it;
this only reports honestly when the last tick arrived.

**Bar boundaries.** A bar closes when its minute has fully elapsed, whether or not a
new tick arrives to push it out. Without that, the final bar of a quiet stretch never
completes and the engine keeps evaluating stale-but-not-flagged data.

The SDK is injected. Every network dependency -- the websocket and the REST history
client -- is a constructor argument with a lazily-imported default, so the reconnect,
backfill and staleness paths are all testable with no broker and no market.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Callable, Protocol, Sequence

from src.market.calendar import (
    IST,
    ensure_ist,
    holiday_name,
    is_muhurat_session,
    is_trading_day,
    now_ist,
    session_close,
    session_open,
)
from src.positions.feed import BarAggregator
from src.positions.models import BAR_INTERVAL, Bar

log = logging.getLogger("fno.livefeed")

__all__ = [
    "FeedStatus",
    "HistorySource",
    "SocketLike",
    "FyersLiveFeed",
    "DEFAULT_STALE_SECONDS",
    "CONTROL_FRAME_TYPES",
    "is_control_frame",
    "SeedOutcome",
    "SymbolSeed",
    "SeedReport",
    "expected_completed_minutes",
    "market_state_note",
]

DEFAULT_STALE_SECONDS = 30.0

#: Fyers' websocket multiplexes protocol acknowledgements onto the same callback as
#: quotes: connection ("cn"), subscription ("sub"/"unsub") and mode ("ful"/"lite")
#: frames all arrive at ``on_message``. They carry no ``ltp``, so a tick parser must
#: reject them -- and if that rejection is a warning, every real parse failure is
#: buried in three-per-connection noise. Recognised by TYPE, never by "it did not
#: parse", so an unrecognised frame stays loud.
CONTROL_FRAME_TYPES = frozenset({"cn", "sub", "unsub", "ful", "lite", "ack"})


def is_control_frame(message) -> bool:
    """True for a Fyers protocol handshake frame rather than a quote."""
    return isinstance(message, dict) and message.get("type") in CONTROL_FRAME_TYPES


class HistorySource(Protocol):
    """Minimal REST history interface, for seeding and gap backfill."""

    def fetch(self, symbol: str, start: date, end: date) -> list[list]:
        """Raw candles as ``[epoch_seconds, open, high, low, close, volume]``."""


class SocketLike(Protocol):
    """The slice of ``FyersDataSocket`` this module drives."""

    def connect(self) -> None: ...
    def subscribe(self, symbols: Sequence[str], data_type: str) -> None: ...
    def close_connection(self) -> None: ...


@dataclass
class FeedStatus:
    """What the feed knows about itself. Rendered straight onto the header."""

    mode: str = "LIVE"
    connected: bool = False
    subscribed: tuple[str, ...] = ()
    reconnects: int = 0
    last_error: str | None = None
    seeded_bars: int = 0
    backfilled_bars: int = 0
    last_tick_at: datetime | None = None
    stale_seconds_threshold: float = DEFAULT_STALE_SECONDS
    #: Last :class:`SeedOutcome` value, or None if seeding has not been attempted.
    seed_outcome: str | None = None
    #: Control frames seen. A quote arriving as one would show up as a gap here.
    control_frames: int = 0

    def tick_age_seconds(self, now: datetime | None = None) -> float | None:
        if self.last_tick_at is None:
            return None
        return (ensure_ist(now or now_ist()) - self.last_tick_at).total_seconds()

    def is_stale(self, now: datetime | None = None) -> bool:
        age = self.tick_age_seconds(now)
        return age is None or age > self.stale_seconds_threshold

    def as_dict(self, now: datetime | None = None) -> dict:
        age = self.tick_age_seconds(now)
        return {
            "mode": self.mode,
            "connected": self.connected,
            "subscribed": list(self.subscribed),
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "seeded_bars": self.seeded_bars,
            "seed_outcome": self.seed_outcome,
            "backfilled_bars": self.backfilled_bars,
            "control_frames": self.control_frames,
            "last_tick_at": None if self.last_tick_at is None else self.last_tick_at.isoformat(),
            "tick_age_seconds": None if age is None else round(age, 1),
            "stale": self.is_stale(now),
            "stale_threshold_seconds": self.stale_seconds_threshold,
        }


class SeedOutcome(str, Enum):
    """Why a symbol ended up with the number of seeded bars it has.

    The three failure-ish outcomes look identical from inside ``seed_history`` -- zero
    bars, every time -- and only one of them is normal. Collapsing them is how a genuine
    Monday-morning seed failure reads as "market closed" and the dashboard starts with
    cold indicators on a live session.
    """

    SEEDED = "SEEDED"
    #: The history call raised. An outage, an expired token, a bad symbol. Loud.
    API_ERROR = "API_ERROR"
    #: No session has produced a completed minute yet. Expected, not a problem.
    MARKET_CLOSED = "MARKET_CLOSED"
    #: The session HAS completed minutes and history returned none anyway. Loud.
    NO_DATA_IN_SESSION = "NO_DATA_IN_SESSION"


#: Worst-first, for reducing per-symbol outcomes to one verdict for the run.
_OUTCOME_SEVERITY = {
    SeedOutcome.API_ERROR: 3,
    SeedOutcome.NO_DATA_IN_SESSION: 2,
    SeedOutcome.MARKET_CLOSED: 1,
    SeedOutcome.SEEDED: 0,
}


@dataclass(frozen=True)
class SymbolSeed:
    """One symbol's seeding result, with enough detail to diagnose it later."""

    symbol: str
    bars: int
    outcome: SeedOutcome
    detail: str

    @property
    def is_failure(self) -> bool:
        return self.outcome in (SeedOutcome.API_ERROR, SeedOutcome.NO_DATA_IN_SESSION)

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "bars": self.bars,
                "outcome": self.outcome.value, "detail": self.detail}


@dataclass
class SeedReport:
    """What one ``seed_history`` call did, per symbol and overall."""

    trade_date: date
    asof: datetime
    expected_minutes: int
    market_note: str
    per_symbol: dict[str, SymbolSeed] = field(default_factory=dict)

    def record(self, seed: SymbolSeed) -> None:
        self.per_symbol[seed.symbol] = seed

    @property
    def outcome(self) -> SeedOutcome:
        """The worst per-symbol outcome. One broken symbol makes the run not-OK."""
        if not self.per_symbol:
            return SeedOutcome.SEEDED
        return max((s.outcome for s in self.per_symbol.values()),
                   key=lambda o: _OUTCOME_SEVERITY[o])

    @property
    def failures(self) -> list[SymbolSeed]:
        return [s for s in self.per_symbol.values() if s.is_failure]

    @property
    def total_bars(self) -> int:
        return sum(s.bars for s in self.per_symbol.values())

    def summary(self) -> str:
        if not self.failures:
            return (
                f"seed {self.outcome.value}: {self.total_bars} bar(s) across "
                f"{len(self.per_symbol)} symbol(s) for {self.trade_date} "
                f"(session has {self.expected_minutes} completed minute(s) "
                f"at {self.asof:%H:%M:%S}); {self.market_note}"
            )
        detail = "; ".join(f"{s.symbol}: {s.detail}" for s in self.failures)
        return (
            f"seed {self.outcome.value}: {len(self.failures)} of "
            f"{len(self.per_symbol)} symbol(s) failed for {self.trade_date} "
            f"(session has {self.expected_minutes} completed minute(s) "
            f"at {self.asof:%H:%M:%S}) -- {detail}"
        )

    def as_dict(self) -> dict:
        return {
            "trade_date": self.trade_date.isoformat(),
            "asof": self.asof.isoformat(),
            "expected_minutes": self.expected_minutes,
            "market_note": self.market_note,
            "outcome": self.outcome.value,
            "total_bars": self.total_bars,
            "symbols": [s.as_dict() for s in self.per_symbol.values()],
        }


def expected_completed_minutes(trade_date: date, now: datetime) -> int:
    """How many 1-minute bars the exchange should already have published.

    This is the whole basis for telling "the market is shut" apart from "the market is
    open and the data is missing". Zero expected minutes means an empty history
    response is the correct answer; one or more means it is a fault.

    Data-availability boundary:
        Counts only minutes that have FULLY elapsed at ``now``. A bar stamped 09:15
        covers [09:15, 09:16) and is complete at 09:16, so at 09:15:59 the answer is 0.
        Bounded above by the 15:30 close, so a past date yields the full 375 and a
        future one yields 0. Non-trading days -- weekends, holidays, and muhurat
        sessions, which ``is_trading_day`` deliberately excludes -- yield 0.

    Raises:
        NaiveDatetimeError: if ``now`` is naive.
        UnknownHolidayYearError: if the calendar has no data for the year. Failing
            closed is correct: without a holiday list we cannot say whether an empty
            response is normal, and guessing "trading day" turns a holiday into a
            false alarm while guessing "holiday" hides a real outage.
    """
    now = ensure_ist(now)
    if not is_trading_day(trade_date):
        return 0
    cutoff = min(now, session_close(trade_date))
    elapsed = (cutoff - session_open(trade_date)).total_seconds() / 60.0
    return max(0, int(elapsed))


def market_state_note(trade_date: date, now: datetime) -> str:
    """One plain sentence about why the session does or does not have bars yet."""
    now = ensure_ist(now)
    if not is_trading_day(trade_date):
        if is_muhurat_session(trade_date):
            return f"{trade_date} is a muhurat session, not a regular trading day"
        name = holiday_name(trade_date)
        if name:
            return f"{trade_date} is an NSE holiday ({name})"
        if trade_date.weekday() >= 5:
            return f"{trade_date} is a weekend"
        return f"{trade_date} is not an NSE trading day"
    open_at, close_at = session_open(trade_date), session_close(trade_date)
    if now < open_at + BAR_INTERVAL:
        return (
            f"{trade_date} is a trading day but the session has not completed a minute "
            f"yet (opens {open_at:%H:%M}, now {now:%H:%M:%S})"
        )
    if now >= close_at:
        return f"{trade_date} session is complete (closed {close_at:%H:%M})"
    return f"{trade_date} session is under way (opened {open_at:%H:%M})"


def _candles_to_bars(candles) -> list[Bar]:
    from src.data.backfill import candles_to_bars  # local: avoids a cycle

    return candles_to_bars(candles)


class FyersLiveFeed:
    """Live 1-minute bars for a set of symbols, from ticks plus REST."""

    def __init__(
        self,
        symbols: Sequence[str],
        *,
        access_token: str = "",
        app_id: str = "",
        history: HistorySource | None = None,
        socket_factory: Callable[[dict], SocketLike] | None = None,
        rate_limiter=None,
        clock: Callable[[], datetime] = now_ist,
        sleep: Callable[[float], None] = _time.sleep,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        max_reconnect_attempts: int = 8,
        backoff_base: float = 2.0,
        backoff_cap: float = 60.0,
    ) -> None:
        self.symbols = [s.strip().upper() for s in symbols]
        self._token = access_token
        self._app_id = app_id
        self._history = history
        self._socket_factory = socket_factory
        self._limiter = rate_limiter
        self.clock = clock
        self._sleep = sleep
        self.max_reconnect_attempts = max_reconnect_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap

        self._agg = {s: BarAggregator() for s in self.symbols}
        self._lock = threading.RLock()
        self._socket: SocketLike | None = None
        self.status = FeedStatus(stale_seconds_threshold=stale_seconds)
        #: The last :meth:`seed_history` result, or None before one has run.
        self.last_seed: SeedReport | None = None
        #: Set when a completed bar appears, so a driver can react without polling.
        self.on_bar: Callable[[str, Bar], None] | None = None

    # ---- interface shared with ReplayFeed --------------------------------------

    def bars(self, symbol: str) -> list[Bar]:
        """Completed bars for the symbol. Never includes the forming bar."""
        with self._lock:
            agg = self._agg.get(symbol.strip().upper())
            return agg.completed if agg else []

    def last_tick_at(self, symbol: str | None = None) -> datetime | None:
        """When data for this symbol last arrived, or None.

        With no symbol, the most recent tick across the whole subscription -- which is
        what the header shows, because a feed that is silent on ONE name is a
        different problem from one that is silent on all of them.
        """
        with self._lock:
            if symbol is not None:
                agg = self._agg.get(symbol.strip().upper())
                return agg.last_tick_at if agg else None
            seen = [a.last_tick_at for a in self._agg.values() if a.last_tick_at]
            return max(seen) if seen else None

    # ---- REST ------------------------------------------------------------------

    def _history_client(self) -> HistorySource:
        if self._history is None:
            from src.data.backfill import FyersHistoryClient  # noqa: PLC0415

            self._history = FyersHistoryClient(self._token, self._app_id)
        return self._history

    def _pace(self) -> None:
        """Block until the next history request may go out.

        Seeding and gap backfill both fan out one REST call PER SYMBOL. At six symbols
        that is invisible; at the ~180-name F&O universe it is 180 requests in a burst,
        which is how a personal account gets throttled at 09:14. The limiter is shared
        by both paths and uses the injected ``sleep``, so tests pay no wall time.
        """
        if self._limiter is None:
            from src.data.backfill import RateLimiter  # noqa: PLC0415

            self._limiter = RateLimiter(sleep=self._sleep)
        self._limiter.acquire()

    def seed_history(self, trade_date: date | None = None) -> dict[str, int]:
        """Load the session's completed bars over REST so indicators start warm.

        Zero bars has three causes and only one of them is normal, so each is
        classified explicitly (see :class:`SeedOutcome`) rather than left to look
        alike: the call raised (``API_ERROR``), the market has produced no completed
        minute yet (``MARKET_CLOSED``), or the session is under way and history
        returned nothing anyway (``NO_DATA_IN_SESSION``). The request parameters and
        the outcome are logged for every symbol so the case is diagnosable after the
        fact; the full report is kept on :attr:`last_seed`.

        Data-availability boundary:
            Fyers stamps historical candles at bar OPEN. Only bars whose minute has
            fully elapsed at ``clock()`` are seeded; the forming bar is dropped, so a
            mid-session start cannot see a partial minute as if it were complete. The
            same boundary defines ``expected_minutes``, so the two cannot disagree.
        """
        now = ensure_ist(self.clock())
        trade_date = trade_date or now.date()
        expected = expected_completed_minutes(trade_date, now)
        report = SeedReport(
            trade_date=trade_date, asof=now, expected_minutes=expected,
            market_note=market_state_note(trade_date, now),
        )
        loaded: dict[str, int] = {}
        client = self._history_client()

        for symbol in self.symbols:
            self._pace()
            try:
                candles = client.fetch(symbol, trade_date, trade_date)
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                log.error(
                    "seed API_ERROR symbol=%s range=%s..%s asof=%s expected_minutes=%d "
                    "error=%s: %s",
                    symbol, trade_date, trade_date, now.isoformat(), expected,
                    type(exc).__name__, exc,
                )
                report.record(SymbolSeed(symbol, 0, SeedOutcome.API_ERROR,
                                         f"{type(exc).__name__}: {exc}"))
                loaded[symbol] = 0
                continue

            fresh = [b for b in _candles_to_bars(candles) if b.ts + BAR_INTERVAL <= now]
            with self._lock:
                self._agg[symbol].seed(fresh)
            loaded[symbol] = len(fresh)

            if fresh:
                report.record(SymbolSeed(
                    symbol, len(fresh), SeedOutcome.SEEDED,
                    f"{len(fresh)} of {len(candles)} candle(s) complete",
                ))
                log.info(
                    "seeded symbol=%s range=%s..%s candles=%d complete=%d",
                    symbol, trade_date, trade_date, len(candles), len(fresh),
                )
            elif expected == 0:
                report.record(SymbolSeed(
                    symbol, 0, SeedOutcome.MARKET_CLOSED, report.market_note))
                log.info(
                    "seed MARKET_CLOSED symbol=%s range=%s..%s asof=%s candles=%d -- %s",
                    symbol, trade_date, trade_date, now.isoformat(), len(candles),
                    report.market_note,
                )
            else:
                detail = (
                    f"history returned {len(candles)} candle(s) and 0 completed bar(s) "
                    f"for a session with {expected} completed minute(s)"
                )
                report.record(SymbolSeed(
                    symbol, 0, SeedOutcome.NO_DATA_IN_SESSION, detail))
                log.error(
                    "seed NO_DATA_IN_SESSION symbol=%s range=%s..%s asof=%s "
                    "expected_minutes=%d candles=%d -- %s",
                    symbol, trade_date, trade_date, now.isoformat(), expected,
                    len(candles), report.market_note,
                )

        self.status.seeded_bars = sum(loaded.values())
        self.last_seed = report
        self.status.seed_outcome = report.outcome.value
        if report.failures:
            # MARKET_CLOSED deliberately does NOT set last_error: the header must not
            # show an alarm for a Sunday, or the alarm stops meaning anything.
            self.status.last_error = report.summary()
            log.error("%s", report.summary())
        else:
            log.info("%s", report.summary())
        return loaded

    def backfill_gap(self, trade_date: date | None = None) -> int:
        """Refetch the minutes missed while disconnected and merge them in.

        A hole left unfilled does not announce itself: the indicators just compute
        over a shorter window and report a number that looks fine.
        """
        now = ensure_ist(self.clock())
        trade_date = trade_date or now.date()
        client = self._history_client()
        added = 0

        for symbol in self.symbols:
            self._pace()
            try:
                candles = client.fetch(symbol, trade_date, trade_date)
            except Exception as exc:  # noqa: BLE001
                log.error("gap backfill failed for %s: %s", symbol, exc)
                self.status.last_error = f"backfill {symbol}: {exc}"
                continue
            fresh = [b for b in _candles_to_bars(candles) if b.ts + BAR_INTERVAL <= now]
            with self._lock:
                added += self._agg[symbol].merge(fresh)

        self.status.backfilled_bars += added
        log.info("gap backfill added %d bar(s)", added)
        return added

    # ---- websocket -------------------------------------------------------------

    def _build_socket(self) -> SocketLike:
        if self._socket_factory is not None:
            return self._socket_factory({
                "access_token": f"{self._app_id}:{self._token}",
                "on_message": self._on_message,
                "on_error": self._on_error,
                "on_connect": self._on_connect,
                "on_close": self._on_close,
            })
        from fyers_apiv3.FyersWebsocket import data_ws  # noqa: PLC0415

        return data_ws.FyersDataSocket(
            access_token=f"{self._app_id}:{self._token}",
            write_to_file=False,
            log_path="",
            litemode=False,
            reconnect=False,  # reconnect is handled here, with gap backfill
            on_message=self._on_message,
            on_error=self._on_error,
            on_connect=self._on_connect,
            on_close=self._on_close,
        )

    def start(self) -> None:
        """Connect and subscribe. Seeding is the caller's job and comes first."""
        self._socket = self._build_socket()
        self._socket.connect()

    def _fyers_symbols(self) -> list[str]:
        from src.data.backfill import to_fyers_symbol  # noqa: PLC0415

        return [to_fyers_symbol(s) for s in self.symbols]

    def _on_connect(self, *_args) -> None:
        self.status.connected = True
        self.status.last_error = None
        if self._socket is not None:
            self._socket.subscribe(self._fyers_symbols(), "SymbolUpdate")
        self.status.subscribed = tuple(self.symbols)
        log.info("connected, subscribed to %d symbol(s)", len(self.symbols))

    def _on_close(self, *args) -> None:
        self.status.connected = False
        log.warning("socket closed: %s", args[0] if args else "")

    def _on_error(self, *args) -> None:
        self.status.connected = False
        self.status.last_error = str(args[0]) if args else "unknown socket error"
        log.error("socket error: %s", self.status.last_error)

    def _on_message(self, message) -> None:
        """Route one tick payload into its aggregator.

        Protocol frames share this callback with quotes and are dropped by TYPE before
        parsing is attempted. Letting them fall through to the parser buries every real
        parse failure under a handshake's worth of warnings -- which is exactly what
        the first live run looked like. A frame reporting a non-ok status is still an
        error: a rejected subscription is silence, not noise.
        """
        if is_control_frame(message):
            self.status.control_frames += 1
            if message.get("s") in (None, "ok"):
                log.debug("control frame %r", message)
            else:
                self.status.last_error = f"control frame {message.get('type')}: {message}"
                log.error("control frame reports a problem: %r", message)
            return
        try:
            symbol, ts, price, volume = self.parse_tick(message)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("unparseable tick %r: %s", message, exc)
            return
        self.on_tick(symbol, ts, price, volume)

    def parse_tick(self, message) -> tuple[str, datetime, float, int]:
        """Pull (symbol, timestamp, price, volume) out of a Fyers tick.

        Kept separate and permissive about field names so the wire format can be
        pinned by a test rather than only discovered against a live socket.
        """
        if not isinstance(message, dict):
            raise TypeError(f"expected a dict tick, got {type(message).__name__}")
        raw = message.get("symbol") or message.get("s") or ""
        symbol = raw.split(":")[-1].split("-")[0].upper()
        symbol = {"NIFTY50": "NIFTY", "NIFTYBANK": "BANKNIFTY"}.get(symbol, symbol)
        price = message.get("ltp", message.get("last_price"))
        if price is None:
            raise KeyError("tick has no ltp")
        epoch = message.get("exch_feed_time") or message.get("last_traded_time")
        ts = (
            datetime.fromtimestamp(int(epoch), tz=IST) if epoch
            else ensure_ist(self.clock())
        )
        volume = int(message.get("last_traded_qty") or message.get("vol_traded_today") or 0)
        return symbol, ts, float(price), volume

    def on_tick(self, symbol: str, ts: datetime, price: float, volume: int = 0) -> None:
        """Feed one tick in. Emits ``on_bar`` when a minute completes."""
        symbol = symbol.strip().upper()
        with self._lock:
            agg = self._agg.get(symbol)
            if agg is None:
                return
            completed = agg.add_tick(ts, price, volume)
            self.status.last_tick_at = self.last_tick_at()
        if completed is not None and self.on_bar is not None:
            self.on_bar(symbol, completed)

    def close_due_bars(self, now: datetime | None = None) -> list[tuple[str, Bar]]:
        """Complete any bucket whose minute has fully elapsed.

        Without this the last bar of a quiet stretch never closes, because a bucket is
        otherwise only pushed out by a tick belonging to the NEXT one. The engine would
        keep evaluating an old completed bar while a finished minute sat unpublished.
        """
        now = ensure_ist(now or self.clock())
        out: list[tuple[str, Bar]] = []
        with self._lock:
            for symbol, agg in self._agg.items():
                bar = agg.close_if_elapsed(now)
                if bar is not None:
                    out.append((symbol, bar))
        for symbol, bar in out:
            if self.on_bar is not None:
                self.on_bar(symbol, bar)
        return out

    # ---- reconnect --------------------------------------------------------------

    def reconnect(self, trade_date: date | None = None) -> bool:
        """Reconnect with exponential backoff, then backfill whatever was missed.

        Backfill runs AFTER the socket is up, not before: reconnecting first bounds
        the hole, and refetching first would leave a second gap between the fetch and
        the connection.
        """
        for attempt in range(self.max_reconnect_attempts):
            delay = min(self.backoff_cap, self.backoff_base ** attempt)
            log.warning("reconnect attempt %d/%d in %.1fs",
                        attempt + 1, self.max_reconnect_attempts, delay)
            self._sleep(delay)
            try:
                if self._socket is not None:
                    try:
                        self._socket.close_connection()
                    except Exception:  # noqa: BLE001 - already broken, keep going
                        pass
                self.start()
            except Exception as exc:  # noqa: BLE001
                self.status.last_error = f"reconnect: {exc}"
                log.error("reconnect attempt %d failed: %s", attempt + 1, exc)
                continue

            self.status.reconnects += 1
            if self.status.connected:
                self.backfill_gap(trade_date)
                return True
        log.error("giving up after %d reconnect attempts", self.max_reconnect_attempts)
        return False

    def stop(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close_connection()
            except Exception:  # noqa: BLE001
                pass
        self.status.connected = False
