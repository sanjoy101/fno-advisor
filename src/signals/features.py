"""Signal features. Every one is causal by construction and proved so by test.

The contract, without exception: a feature takes ``(bars, at)`` and does its OWN
completed-bar filtering via :func:`~src.positions.models.completed_bars`. It never
receives pre-filtered bars, because a function handed clean input can never be caught
leaking. ``tests/test_signal_leak.py`` runs every function here through
:func:`src.analysis.lookahead.assert_causal`, which evaluates it with future bars
present, absent, and poisoned, and fails if the three disagree.

A signal generated at 09:45 may use bars up to and including the one stamped 09:44,
which is the last one whose minute has fully elapsed. The 09:45 bar is still forming
and only its open is knowable; nothing here reads it.

Insufficient history returns ``None``, never a partial value. ``None`` means unknown
and the scorer must treat it as neither for nor against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Mapping, Sequence

from src.market.calendar import IST, SESSION_OPEN, ensure_ist
from src.positions.indicators import atr, ema, rsi
from src.positions.models import BAR_INTERVAL, Bar, completed_bars

__all__ = [
    "OPENING_RANGE_MINUTES",
    "OpeningRange",
    "session_bars",
    "opening_range",
    "breakout_state",
    "breakout_atr_units",
    "breakout_held_bars",
    "ema_alignment_pct",
    "price_vs_vwap_pct",
    "bars_above_vwap",
    "rsi_at",
    "rate_of_change_pct",
    "atr_pct",
    "session_return_pct",
    "running_vwap_series",
    "time_of_day_volume_profile",
    "volume_profile_for",
    "relative_volume",
    "last_price_at",
]

#: The opening range is the first 15 minutes, 09:15-09:29 inclusive. It is fully
#: formed -- and therefore usable -- from 09:30.
OPENING_RANGE_MINUTES = 15


def _done(bars: Sequence[Bar], at: datetime) -> list[Bar]:
    return completed_bars(bars, ensure_ist(at))


def session_bars(bars: Sequence[Bar], at: datetime) -> list[Bar]:
    """Completed bars belonging to the IST session that ``at`` falls in.

    Anchors everything session-scoped -- VWAP, the opening range, today's return --
    so yesterday's bars can never bleed into today's feature.

    Data-availability boundary:
        Bars with ``ts + BAR_INTERVAL <= at`` whose IST date equals ``at``'s IST date.
    """
    at = ensure_ist(at)
    return [b for b in _done(bars, at) if b.ts.astimezone(IST).date() == at.date()]


def last_price_at(bars: Sequence[Bar], at: datetime) -> float | None:
    """Close of the last completed bar. The only price that has actually printed.

    Data-availability boundary:
        Uses bars with ``ts + BAR_INTERVAL <= at``. Returns None before the first
        bar of the session has finished forming.
    """
    done = session_bars(bars, at)
    return done[-1].close if done else None


# --------------------------------------------------------------------------------
# Opening range
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    bars: int

    @property
    def width(self) -> float:
        return self.high - self.low


def opening_range(
    bars: Sequence[Bar],
    at: datetime,
    *,
    minutes: int = OPENING_RANGE_MINUTES,
) -> OpeningRange | None:
    """High and low of the first ``minutes`` of the session.

    Data-availability boundary:
        Uses session bars with ``ts`` in ``[09:15, 09:15 + minutes)`` and
        ``ts + BAR_INTERVAL <= at``. Returns None until ALL of those bars have
        completed -- a partial opening range is a different, narrower range, and
        acting on one would systematically overstate breakout magnitude. In practice
        this means None before 09:30.
    """
    at = ensure_ist(at)
    start = datetime.combine(at.date(), SESSION_OPEN, tzinfo=IST)
    end = start + timedelta(minutes=minutes)
    window = [b for b in session_bars(bars, at) if start <= b.ts < end]
    if len(window) < minutes:
        return None
    return OpeningRange(
        high=max(b.high for b in window),
        low=min(b.low for b in window),
        bars=len(window),
    )


def breakout_state(bars: Sequence[Bar], at: datetime) -> str | None:
    """``ABOVE``, ``BELOW`` or ``INSIDE`` the opening range, or None.

    Data-availability boundary:
        Compares the last completed bar's close against the completed opening range.
        None before the range is formed or before any bar has completed.
    """
    orange = opening_range(bars, at)
    price = last_price_at(bars, at)
    if orange is None or price is None:
        return None
    if price > orange.high:
        return "ABOVE"
    if price < orange.low:
        return "BELOW"
    return "INSIDE"


def breakout_atr_units(bars: Sequence[Bar], at: datetime) -> float | None:
    """How far past the opening range the price is, measured in ATRs.

    Positive above the range, negative below, zero inside. ATR units rather than
    rupees or percent so a breakout in a quiet stock and a wild one are comparable.

    Data-availability boundary:
        Uses completed session bars only, for both the range and ATR(14). None until
        the range is formed and ATR has 15 completed bars.
    """
    orange = opening_range(bars, at)
    price = last_price_at(bars, at)
    if orange is None or price is None:
        return None
    atr_value = atr(session_bars(bars, at))
    if atr_value is None or atr_value <= 0:
        return None
    if price > orange.high:
        return (price - orange.high) / atr_value
    if price < orange.low:
        return (price - orange.low) / atr_value
    return 0.0


def breakout_held_bars(bars: Sequence[Bar], at: datetime) -> int | None:
    """Consecutive completed bars whose close held ABOVE the opening-range high.

    Counts backwards from the most recent completed bar and stops at the first close
    that was not above the range. A breakout that has held for several bars is a
    different animal from one that printed a single bar ago, and the score treats
    them differently.

    Data-availability boundary:
        Uses completed session bars only. None before the range is formed.
    """
    orange = opening_range(bars, at)
    if orange is None:
        return None
    held = 0
    for bar in reversed(session_bars(bars, at)):
        if bar.close > orange.high:
            held += 1
        else:
            break
    return held


# --------------------------------------------------------------------------------
# Trend
# --------------------------------------------------------------------------------


def ema_alignment_pct(bars: Sequence[Bar], at: datetime) -> float | None:
    """``(EMA9 - EMA21) / EMA21`` as a percent. Positive means the fast EMA leads.

    Data-availability boundary:
        Uses completed session bars. None until 21 have completed.
    """
    done = session_bars(bars, at)
    fast, slow = ema(done, 9), ema(done, 21)
    if fast is None or slow is None or slow == 0:
        return None
    return 100.0 * (fast - slow) / slow


def running_vwap_series(bars: Sequence[Bar]) -> list[float]:
    """Session VWAP as it stood at the close of each bar, in one pass.

    Needed because "bars above VWAP" is a question about the past: at 10:00 the VWAP
    was not what it is now, and comparing an old close to today's VWAP would be a
    quiet look-ahead.

    Data-availability boundary:
        Element ``i`` uses bars ``0..i`` only. Caller supplies already-completed bars.
    """
    out: list[float] = []
    weighted = 0.0
    volume = 0
    for bar in bars:
        weighted += bar.typical_price * bar.volume
        volume += bar.volume
        out.append(weighted / volume if volume else bar.typical_price)
    return out


def price_vs_vwap_pct(bars: Sequence[Bar], at: datetime) -> float | None:
    """Last completed close relative to session VWAP, as a percent.

    Data-availability boundary:
        Uses completed session bars only. None with no bars or no volume.
    """
    done = session_bars(bars, at)
    if not done:
        return None
    vwap = running_vwap_series(done)[-1]
    if vwap <= 0:
        return None
    return 100.0 * (done[-1].close - vwap) / vwap


def bars_above_vwap(bars: Sequence[Bar], at: datetime) -> int | None:
    """Consecutive completed bars that closed above the VWAP AS IT STOOD THEN.

    Data-availability boundary:
        Bar ``i`` is compared against the VWAP computed from bars ``0..i``, never
        against the current VWAP. Uses completed session bars only.
    """
    done = session_bars(bars, at)
    if not done:
        return None
    vwaps = running_vwap_series(done)
    held = 0
    for bar, vwap in zip(reversed(done), reversed(vwaps)):
        if bar.close > vwap:
            held += 1
        else:
            break
    return held


# --------------------------------------------------------------------------------
# Momentum
# --------------------------------------------------------------------------------


def rsi_at(bars: Sequence[Bar], at: datetime, period: int = 14) -> float | None:
    """RSI(14) on completed session bars.

    Data-availability boundary:
        Uses completed session bars only. None until ``period + 1`` have completed.
    """
    return rsi(session_bars(bars, at), period)


def rate_of_change_pct(
    bars: Sequence[Bar],
    at: datetime,
    lookback: int = 10,
) -> float | None:
    """Percent change in close over the last ``lookback`` completed bars.

    Data-availability boundary:
        Compares the last completed close with the close ``lookback`` bars earlier.
        Requires ``lookback + 1`` completed session bars; None below that.
    """
    done = session_bars(bars, at)
    if len(done) < lookback + 1:
        return None
    then = done[-(lookback + 1)].close
    if then <= 0:
        return None
    return 100.0 * (done[-1].close - then) / then


# --------------------------------------------------------------------------------
# Volatility
# --------------------------------------------------------------------------------


def atr_pct(bars: Sequence[Bar], at: datetime) -> float | None:
    """ATR(14) as a percent of the last completed close.

    Sizes targets realistically: a target two ATRs away means something different on
    a stock with 0.2% ATR than on one with 1.5%.

    Data-availability boundary:
        Uses completed session bars only. None until ATR has 15 of them.
    """
    done = session_bars(bars, at)
    atr_value = atr(done)
    if atr_value is None or not done or done[-1].close <= 0:
        return None
    return 100.0 * atr_value / done[-1].close


# --------------------------------------------------------------------------------
# Relative strength
# --------------------------------------------------------------------------------


def session_return_pct(bars: Sequence[Bar], at: datetime) -> float | None:
    """Return from the session's opening print to the last completed close.

    Anchored to the session OPEN rather than yesterday's close on purpose: the
    overnight gap is not something an intraday entry at 09:45 can act on, and
    including it would rank stocks on a move that already happened before the
    session started.

    Data-availability boundary:
        Uses completed session bars only. The anchor is the first completed bar's
        open, which is known from 09:16.
    """
    done = session_bars(bars, at)
    if not done or done[0].open <= 0:
        return None
    return 100.0 * (done[-1].close - done[0].open) / done[0].open


# --------------------------------------------------------------------------------
# Volume, normalised by time of day
# --------------------------------------------------------------------------------


def time_of_day_volume_profile(
    store,
    symbol: str,
    sessions: Sequence[date],
) -> dict[time, float]:
    """Mean volume per minute-of-day across ``sessions``.

    Raw volume is meaningless without this. 09:30 trades structurally heavier than
    12:30 in every stock on every day, so an un-normalised "volume is high" score
    would simply rank the morning above the afternoon and call it signal.

    Data-availability boundary:
        The CALLER must pass only sessions strictly BEFORE the one being scored --
        :func:`trailing_sessions` does that. This function cannot check it, so the
        leak test covers the caller instead.
    """
    totals: dict[time, float] = {}
    counts: dict[time, int] = {}
    for day in sessions:
        for bar in store.load_bars(symbol, day):
            key = bar.ts.astimezone(IST).time()
            totals[key] = totals.get(key, 0.0) + bar.volume
            counts[key] = counts.get(key, 0) + 1
    return {k: totals[k] / counts[k] for k in totals if counts[k]}


def volume_profile_for(
    store,
    symbol: str,
    all_sessions: Sequence[date],
    current: date,
    *,
    lookback: int = 20,
) -> dict[time, float]:
    """Time-of-day volume baseline for ``current``, from the sessions before it.

    The public entry point. It takes the FULL session list and narrows it itself, so
    a caller cannot accidentally hand it a window that includes today -- and so
    :func:`src.analysis.lookahead.assert_session_causal` can catch it if the
    narrowing ever breaks.

    Data-availability boundary:
        Uses the ``lookback`` sessions with ``date < current``. The current session
        contributes nothing to its own baseline; if it did, every bar would score
        about 1.0x and the feature would be measuring nothing.
    """
    from src.signals.universe import trailing_sessions  # local: avoids a cycle

    prior = trailing_sessions(all_sessions, current, lookback=lookback)
    return time_of_day_volume_profile(store, symbol, prior)


def relative_volume(
    bars: Sequence[Bar],
    at: datetime,
    profile: Mapping[time, float],
) -> float | None:
    """Last completed bar's volume over its own time-of-day average.

    ``1.0`` means this minute traded exactly its usual amount for this time of day.
    ``2.0`` means twice as much, whether that minute is 09:30 or 14:30.

    Data-availability boundary:
        Uses the last completed session bar and a profile built from prior sessions
        only. None when that minute has no history in the profile, or its baseline is
        zero -- never a fabricated 1.0.
    """
    done = session_bars(bars, at)
    if not done:
        return None
    bar = done[-1]
    baseline = profile.get(bar.ts.astimezone(IST).time())
    if not baseline or baseline <= 0:
        return None
    return bar.volume / baseline
