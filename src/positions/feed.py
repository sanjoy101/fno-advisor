"""Price feeds and the replay driver.

The exit engine is a pure function of ``(position, runtime, bars, now, last_tick_at)``.
Everything in this module exists to produce those four arguments from somewhere --
a stored historical session, or a live Fyers connection. The engine cannot tell the
difference, which is the whole point: what you validate on a replayed day is the same
code that runs live.

Feeds
-----
:class:`ReplayFeed`
    Deterministic. Walks a stored session bar by bar. Fully tested.

:class:`FyersLiveFeed`
    Websocket ticks aggregated into 1-minute bars, REST for history and as fallback.
    Read-only endpoints only -- quotes and historical candles. **Not covered by
    tests**: it needs live credentials and a market session. Treat it as unverified
    until it has been watched through a real day.

Staleness is derived, not asserted. A feed reports when it last received data; the
engine decides whether that is too old. A replayed session with a genuine data gap
therefore surfaces STALE_DATA in exactly the same way a dead socket would, which is
what makes the stale path testable at all.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Iterator, Protocol, Sequence

from src.market.calendar import IST, ensure_ist
from src.positions.models import BAR_INTERVAL, Bar, completed_bars

__all__ = [
    "PriceFeed",
    "data_available_through",
    "ReplayFeed",
    "MultiSymbolReplayFeed",
    "BarAggregator",
    "FyersLiveFeed",
    "synthetic_session",
]


class PriceFeed(Protocol):
    """What the engine driver needs from any source of prices."""

    def bars(self, symbol: str) -> Sequence[Bar]:
        """All bars known for the symbol, ascending. May include a forming bar."""

    def last_tick_at(self, symbol: str) -> datetime | None:
        """The most recent moment this feed HAD DATA for the symbol, or None.

        **One meaning, both modes.** This is the definition every implementation must
        satisfy, and it is what makes replay a valid rehearsal rather than a different
        experiment that happens to share a UI.

        It is NOT "the close of the last completed bar". A completed bar only appears
        once a minute by construction, so that reading makes a perfectly healthy feed
        look progressively staler for the second half of every minute -- and it did:
        REPLAY reported it that way while LIVE reported tick arrival, so the same
        30-second threshold meant two different things and a replayed position flipped
        HOLD -> STALE_DATA -> HOLD once a minute.

        Live feeds satisfy it by recording tick arrival. Stored feeds satisfy it via
        :func:`data_available_through`.
        """


def data_available_through(
    bars: Sequence[Bar],
    cursor: datetime | None,
    interval: timedelta = BAR_INTERVAL,
) -> datetime | None:
    """The most recent moment a STORED session had data, as of ``cursor``.

    The replay analogue of "when did a tick last arrive". If the minute containing
    ``cursor`` has a bar, this feed had data throughout that minute, so the answer is
    ``cursor`` itself -- a live socket watching the same tape would be receiving ticks
    right now. If it does not, the last data was the end of the previous bar, and the
    age grows from there exactly as a dead socket's would.

    That is what makes a HOLE in stored data still surface as STALE_DATA, which was the
    whole point of the original bar-close definition, WITHOUT the artifact that came
    with it -- a healthy session reading staler and staler until the next bar closed.

    Data-availability boundary:
        Only the EXISTENCE of the bar covering ``cursor`` is consulted -- never its
        open, high, low, close or volume. That existence is not look-ahead: it is
        exactly what a live feed knows at the same instant, namely that this symbol is
        printing. Bar VALUES still reach the engine only through
        :func:`~src.positions.models.completed_bars`, which excludes the forming bar.

    Returns None before the first bar and for an empty series.
    """
    if cursor is None or not bars:
        return None
    cursor = ensure_ist(cursor)
    latest: Bar | None = None
    for bar in bars:                       # ascending
        if bar.ts > cursor:
            break
        latest = bar
    if latest is None:
        return None                        # the cursor is before this session started
    if cursor < latest.ts + interval:
        return cursor                      # this minute is in progress and has data
    return latest.ts + interval            # the last data was that bar's close


class ReplayFeed:
    """Replays a stored session bar by bar, deterministically.

    Drives the engine over historical data so its behaviour can be examined before it
    is trusted live. Reproducible: the same bars and the same config produce the same
    advice sequence every run, because nothing here reads a clock.

    ``last_tick_at`` means what it means on a live feed: the most recent moment this
    feed HAD DATA. A session with a missing bar therefore reports a growing staleness
    and trips the engine's STALE_DATA path exactly as a dead socket would -- gaps in
    historical data are surfaced rather than papered over -- while a session that is
    printing normally reports no staleness at all. See :func:`data_available_through`.
    """

    def __init__(self, symbol: str, bars: Sequence[Bar]) -> None:
        self.symbol = symbol.upper()
        self._bars = sorted(bars, key=lambda b: b.ts)
        self._cursor: datetime | None = None

    def advance_to(self, now: datetime) -> None:
        """Move the replay clock. Only bars completed by ``now`` become visible."""
        self._cursor = ensure_ist(now)

    @property
    def cursor(self) -> datetime | None:
        return self._cursor

    def bars(self, symbol: str | None = None) -> list[Bar]:
        if self._cursor is None:
            return []
        return completed_bars(self._bars, self._cursor)

    def last_tick_at(self, symbol: str | None = None) -> datetime | None:
        return data_available_through(self._bars, self._cursor)

    def steps(self) -> Iterator[datetime]:
        """Each bar close time in order -- the natural decision points of a session.

        The engine evaluates on each new completed bar, so these are the moments a
        live run would have woken up at.
        """
        for bar in self._bars:
            yield bar.close_time

    @classmethod
    def from_store(cls, store, symbol: str, trade_date: date) -> "ReplayFeed":
        """Build a replay feed from bars previously saved to the Parquet store."""
        return cls(symbol, store.load_bars(symbol, trade_date))


class MultiSymbolReplayFeed:
    """Replays several symbols against one virtual clock.

    Interface-identical to :class:`~src.positions.live_feed.FyersLiveFeed`:
    ``bars(symbol)``, ``last_tick_at(symbol)`` and a ``status``. That is what lets the
    entire UI be driven from stored bars and proved out before it ever faces a live
    session -- and it is why the mode badge has to be unmistakable, because nothing
    else about the page will differ.

    ``last_tick_at`` carries the SAME meaning it carries on the live feed -- the most
    recent moment this feed had data -- which is the property that makes replay a
    rehearsal for live rather than a different experiment sharing a UI. A hole in the
    stored session surfaces as growing staleness exactly as a dead socket would,
    instead of being silently bridged. See :func:`data_available_through`.
    """

    def __init__(self, bars_by_symbol: dict[str, Sequence[Bar]], *, stale_seconds: float = 30.0):
        self._bars = {
            s.upper(): sorted(b, key=lambda x: x.ts) for s, b in bars_by_symbol.items()
        }
        self.symbols = sorted(self._bars)
        self._cursor: datetime | None = None
        from src.positions.live_feed import FeedStatus  # local: avoids a cycle

        self.status = FeedStatus(mode="REPLAY", connected=True,
                                 subscribed=tuple(self.symbols),
                                 stale_seconds_threshold=stale_seconds)
        self.on_bar: Callable[[str, Bar], None] | None = None
        self._emitted: dict[str, int] = {s: 0 for s in self.symbols}

    @property
    def cursor(self) -> datetime | None:
        return self._cursor

    def advance_to(self, now: datetime) -> list[tuple[str, Bar]]:
        """Move the virtual clock; emit any bars that completed in between."""
        now = ensure_ist(now)
        self._cursor = now
        emitted: list[tuple[str, Bar]] = []
        for symbol in self.symbols:
            done = completed_bars(self._bars[symbol], now)
            already = self._emitted[symbol]
            for bar in done[already:]:
                emitted.append((symbol, bar))
            self._emitted[symbol] = len(done)
        self.status.last_tick_at = self.last_tick_at()
        for symbol, bar in emitted:
            if self.on_bar is not None:
                self.on_bar(symbol, bar)
        return emitted

    def bars(self, symbol: str) -> list[Bar]:
        if self._cursor is None:
            return []
        return completed_bars(self._bars.get(symbol.upper(), []), self._cursor)

    def last_tick_at(self, symbol: str | None = None) -> datetime | None:
        if self._cursor is None:
            return None
        if symbol is not None:
            return data_available_through(
                self._bars.get(symbol.upper(), []), self._cursor)
        # Per-symbol, then the most recent across the subscription -- a feed silent on
        # ONE thin name is a different problem from one silent on all of them, and the
        # header shows the second.
        seen = [t for t in (data_available_through(b, self._cursor)
                            for b in self._bars.values()) if t is not None]
        return max(seen) if seen else None

    def first_bar_time(self) -> datetime | None:
        starts = [b[0].ts for b in self._bars.values() if b]
        return min(starts) if starts else None

    def last_bar_time(self) -> datetime | None:
        ends = [b[-1].close_time for b in self._bars.values() if b]
        return max(ends) if ends else None

    def stop(self) -> None:  # interface parity; nothing to tear down
        self.status.connected = False


class BarAggregator:
    """Folds a live tick stream into completed 1-minute bars.

    A bar is emitted only once its minute has fully elapsed. The bar currently
    accumulating is deliberately NOT exposed -- handing it out would let the engine
    read a high or close that is still changing, which is look-ahead in its most
    literal form.
    """

    def __init__(self, interval: timedelta = BAR_INTERVAL) -> None:
        self.interval = interval
        self._completed: list[Bar] = []
        self._open_bucket: datetime | None = None
        self._o = self._h = self._l = self._c = 0.0
        self._v = 0
        self._last_tick_at: datetime | None = None

    def _bucket(self, ts: datetime) -> datetime:
        ts = ensure_ist(ts)
        seconds = int(self.interval.total_seconds())
        epoch_minute = int(ts.timestamp()) // seconds * seconds
        return datetime.fromtimestamp(epoch_minute, tz=IST)

    def add_tick(self, ts: datetime, price: float, volume: int = 0) -> Bar | None:
        """Feed one tick. Returns a Bar when a bucket just completed, else None."""
        ts = ensure_ist(ts)
        self._last_tick_at = ts
        bucket = self._bucket(ts)

        if self._open_bucket is None:
            self._start(bucket, price, volume)
            return None

        if bucket == self._open_bucket:
            self._h = max(self._h, price)
            self._l = min(self._l, price)
            self._c = price
            self._v += volume
            return None

        finished = self._flush()
        self._start(bucket, price, volume)
        return finished

    def _start(self, bucket: datetime, price: float, volume: int) -> None:
        self._open_bucket = bucket
        self._o = self._h = self._l = self._c = price
        self._v = volume

    def _flush(self) -> Bar:
        bar = Bar(
            ts=self._open_bucket,
            open=self._o,
            high=self._h,
            low=self._l,
            close=self._c,
            volume=self._v,
        )
        self._completed.append(bar)
        return bar

    def seed(self, bars: Sequence[Bar]) -> int:
        """Prime with historical bars so indicators start warm rather than cold.

        Replaces whatever is held, which is right for a startup seed: the REST history
        IS the session so far. Use :meth:`merge` to fill a hole in an existing series.
        """
        self._completed = sorted(bars, key=lambda b: b.ts)
        return len(self._completed)

    def merge(self, bars: Sequence[Bar]) -> int:
        """Add bars missing from the series, keeping what is already held.

        Returns how many were new. Existing bars WIN over incoming duplicates: a bar
        built from live ticks was observed directly, whereas the REST copy of the same
        minute is a vendor reconstruction, and preferring the reconstruction would
        silently rewrite history the feed actually saw.
        """
        have = {b.ts for b in self._completed}
        added = [b for b in bars if b.ts not in have]
        if added:
            self._completed = sorted(self._completed + added, key=lambda b: b.ts)
        return len(added)

    def close_if_elapsed(self, now: datetime) -> Bar | None:
        """Complete the open bucket if its minute has fully passed.

        A bucket is otherwise only pushed out by a tick belonging to the NEXT minute,
        so the last bar of a quiet stretch would never close and the engine would keep
        evaluating an older bar while a finished minute sat unpublished.

        Data-availability boundary:
            Closes only when ``now >= bucket_start + interval``. A bucket still inside
            its own minute is left alone -- publishing it early would expose a high,
            low and close that are still changing.
        """
        now = ensure_ist(now)
        if self._open_bucket is None:
            return None
        if now < self._open_bucket + self.interval:
            return None
        bar = self._flush()
        self._open_bucket = None
        return bar

    @property
    def completed(self) -> list[Bar]:
        """Bars whose minute has fully elapsed. Never includes the forming bar."""
        return list(self._completed)

    @property
    def last_tick_at(self) -> datetime | None:
        return self._last_tick_at


class FyersLiveFeed:
    """Live Fyers v3 feed: websocket ticks, REST for history and fallback.

    **READ-ONLY.** This wraps quotes and historical candles. It does not import,
    reference, or expose any order-placement call, and none may be added -- see
    CLAUDE.md 1.1.

    **UNVERIFIED.** Requires live credentials and a market session, so it is not
    covered by the test suite. Do not trust it until it has been watched through a
    full session against the broker's own screen.

    The SDK is imported lazily so the rest of Module C -- engine, store, replay --
    works without ``fyers_apiv3`` installed and without credentials on disk.
    """

    def __init__(self, symbols: Sequence[str], access_token: str, app_id: str) -> None:
        self.symbols = [s.upper() for s in symbols]
        self._token = access_token
        self._app_id = app_id
        self._aggregators: dict[str, BarAggregator] = {s: BarAggregator() for s in self.symbols}
        self._socket = None
        self._rest = None

    def _rest_client(self):
        """Lazily build the REST client. Import is deferred on purpose."""
        if self._rest is None:
            from fyers_apiv3 import fyersModel  # noqa: PLC0415 - optional dependency

            self._rest = fyersModel.FyersModel(
                client_id=self._app_id, token=self._token, is_async=False
            )
        return self._rest

    def seed_history(self, symbol: str, trade_date: date) -> list[Bar]:
        """Backfill today's completed 1-minute bars over REST.

        Needed at startup: indicators want 15-30 bars of warm-up, and a process that
        starts at 11:00 has not seen the morning. Without this the engine spends its
        first half hour reporting "warming up" on a position that is already live.

        Data-availability boundary:
            Fyers stamps historical candles at bar OPEN. Only bars whose interval has
            fully elapsed are returned; the forming bar is dropped.
        """
        raise NotImplementedError(
            "FyersLiveFeed.seed_history needs live credentials and is not implemented "
            "or tested yet. Use ReplayFeed against stored bars, or implement this "
            "against the v3 history endpoint (read-only) and verify it against the "
            "broker's own chart before relying on it."
        )

    def on_tick(self, symbol: str, ts: datetime, price: float, volume: int = 0) -> Bar | None:
        """Route one tick into that symbol's aggregator."""
        return self._aggregators[symbol.upper()].add_tick(ts, price, volume)

    def bars(self, symbol: str) -> list[Bar]:
        return self._aggregators[symbol.upper()].completed

    def last_tick_at(self, symbol: str) -> datetime | None:
        return self._aggregators[symbol.upper()].last_tick_at


def synthetic_session(
    symbol: str,
    trade_date: date,
    *,
    open_price: float = 1000.0,
    path: Sequence[float] | None = None,
    volume: int = 5000,
    seed_spread: float = 2.0,
) -> list[Bar]:
    """A deterministic fake session, for exercising replay with no broker data.

    Not market data and not a simulation -- it is a fixture, so the CLI's replay mode
    can be demonstrated end to end before any real bars have been downloaded. Never
    use it to evaluate whether a rule works.

    ``path`` is a sequence of closes; it defaults to a rise, a stall, and a fade,
    which between them exercise TRAIL, PARTIAL_EXIT and a stop breach.
    """
    from src.market.calendar import session_open

    if path is None:
        path = (
            [open_price + i * 0.6 for i in range(40)]  # morning push
            + [open_price + 24 - (i % 3) * 0.4 for i in range(60)]  # stall
            + [open_price + 24 - i * 0.8 for i in range(50)]  # fade
        )
    start = session_open(trade_date)
    bars: list[Bar] = []
    for i, close in enumerate(path):
        bars.append(
            Bar(
                ts=start + i * BAR_INTERVAL,
                open=close,
                high=close + seed_spread,
                low=close - seed_spread,
                close=close,
                volume=volume + (i % 7) * 300,
            )
        )
    return bars
