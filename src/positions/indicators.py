"""Indicators for the exit engine.

Every function takes a sequence of **already-completed** bars in ascending time
order, and returns the value as of the LAST bar in that sequence. Callers obtain
that sequence from :func:`src.positions.models.completed_bars`, which is where the
forming-bar exclusion happens. Nothing here re-checks it, so nothing here should be
handed a raw bar list.

Insufficient history returns ``None``, never a partial or seeded-from-nothing value.
``None`` means "unknown" and the state machine must treat it as neither pass nor
fail -- a half-warmed ATR would silently produce a stop in the wrong place.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from src.market.calendar import IST
from src.positions.models import Bar, IndicatorSnapshot

__all__ = [
    "session_vwap",
    "true_ranges",
    "atr",
    "atr_series",
    "ema",
    "rsi",
    "average_volume",
    "volume_ratio",
    "compute_indicators",
]


# --------------------------------------------------------------------------------
# VWAP
# --------------------------------------------------------------------------------


def session_vwap(bars: Sequence[Bar], session_date: date | None = None) -> float | None:
    """Session-anchored VWAP: cumulative from the session open, NOT a rolling window.

    Anchoring matters. A rolling VWAP drifts with the window and gives a different
    answer at 15:00 depending on lookback length; the session VWAP is the number the
    rest of the market is watching, and it resets every morning.

    Args:
        bars: completed bars, ascending.
        session_date: IST date to anchor to. Defaults to the date of the last bar,
            so bars carried over from a previous day are excluded automatically
            rather than contaminating today's average.

    Data-availability boundary:
        Uses bars with ``ts + BAR_INTERVAL <= asof`` (the caller's filter) that fall
        on ``session_date``. Cumulative up to and including the last such bar. No
        future bar can influence the result, and yesterday's bars cannot either.

    Returns:
        Volume-weighted average of typical price ((H+L+C)/3), or None if there are no
        bars for the session or total volume is zero.
    """
    if not bars:
        return None
    anchor = session_date or bars[-1].ts.astimezone(IST).date()
    todays = [b for b in bars if b.ts.astimezone(IST).date() == anchor]
    if not todays:
        return None
    total_volume = sum(b.volume for b in todays)
    if total_volume <= 0:
        return None
    weighted = sum(b.typical_price * b.volume for b in todays)
    return weighted / total_volume


# --------------------------------------------------------------------------------
# ATR
# --------------------------------------------------------------------------------


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    """True Range for each bar after the first.

    TR = max(high - low, |high - prev_close|, |low - prev_close|). The prev_close
    terms are what make TR account for overnight and inter-bar gaps.

    Data-availability boundary:
        Bar i uses only bars i and i-1. Returns ``len(bars) - 1`` values; the first
        bar has no TR because it has no previous close.
    """
    out: list[float] = []
    for prev, cur in zip(bars, bars[1:]):
        out.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )
    return out


def atr_series(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    """ATR after each bar, aligned to ``bars``. ``None`` until the window is warm.

    Element ``i`` is the ATR you would have known at the close of bar ``i``, computed
    from bars ``0..i`` and nothing later. Exists so a calibration pass can ask "what
    was ATR at 10:00?" for every bar in one sweep instead of recomputing from scratch
    at each one.

    :func:`atr` is defined in terms of this, so the two can never drift apart.

    Data-availability boundary:
        Element ``i`` uses bars ``0..i`` only. Requires ``period + 1`` bars before the
        first non-None value appears, at index ``period``.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    out: list[float | None] = [None] * len(bars)
    trs = true_ranges(bars)
    if len(trs) < period:
        return out
    value = sum(trs[:period]) / period
    out[period] = value  # trs[i] belongs to bars[i+1], so trs[0..period-1] -> bars[period]
    for offset, tr in enumerate(trs[period:], start=period + 1):
        value = (value * (period - 1) + tr) / period
        out[offset] = value
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Average True Range, Wilder smoothing, as of the last bar.

    Wilder's method, not a simple moving average: seed with the mean of the first
    ``period`` true ranges, then ``ATR = (prev_ATR * (period-1) + TR) / period``.
    An SMA-based "ATR" reacts faster and would put the trailing stop closer than
    intended.

    Data-availability boundary:
        Requires ``period + 1`` completed bars (one extra for the first previous
        close). Uses bars up to and including the last one supplied; returns None
        below that threshold rather than a partially-warmed value.
    """
    series = atr_series(bars, period)
    return series[-1] if series else None


# --------------------------------------------------------------------------------
# EMA
# --------------------------------------------------------------------------------


def ema(bars: Sequence[Bar], period: int) -> float | None:
    """Exponential moving average of closes, as of the last bar.

    Seeded with the SMA of the first ``period`` closes, then
    ``EMA = close * k + prev * (1 - k)`` with ``k = 2 / (period + 1)``.

    Data-availability boundary:
        Requires ``period`` completed bars. Uses closes up to and including the last
        bar supplied. Returns None below the threshold.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    closes = [b.close for b in bars]
    if len(closes) < period:
        return None
    value = sum(closes[:period]) / period
    k = 2.0 / (period + 1)
    for c in closes[period:]:
        value = c * k + value * (1 - k)
    return value


# --------------------------------------------------------------------------------
# RSI
# --------------------------------------------------------------------------------


def rsi(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Relative Strength Index, Wilder smoothing, as of the last bar.

    Data-availability boundary:
        Requires ``period + 1`` completed bars (``period`` close-to-close changes).
        Uses closes up to and including the last bar supplied.

    Returns:
        0-100, or None if there is insufficient history. Returns 100.0 when there
        have been no down-closes in the window (average loss is zero), which is the
        conventional handling of the divide-by-zero.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    closes = [b.close for b in bars]
    if len(closes) < period + 1:
        return None

    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# --------------------------------------------------------------------------------
# Volume
# --------------------------------------------------------------------------------


def average_volume(bars: Sequence[Bar], period: int = 20) -> float | None:
    """Mean volume of the ``period`` bars BEFORE the last bar.

    The current bar is excluded from its own baseline on purpose: including it drags
    the average toward the value being tested, which mutes exactly the volume surge
    the engine is trying to detect.

    Data-availability boundary:
        Requires ``period + 1`` completed bars. Uses bars ``[-period-1:-1]``.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1) : -1]
    return sum(b.volume for b in window) / period


def volume_ratio(bars: Sequence[Bar], period: int = 20) -> float | None:
    """Last bar's volume divided by the preceding ``period``-bar average.

    ``> 1.0`` means the last bar traded above its recent average -- what "on
    above-average volume" means in the VWAP-loss exit rule.

    Data-availability boundary:
        Requires ``period + 1`` completed bars. Returns None if the baseline average
        is zero (no trades in the window), rather than dividing by zero or reporting
        an infinite surge.
    """
    avg = average_volume(bars, period)
    if avg is None or avg <= 0:
        return None
    return bars[-1].volume / avg


# --------------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------------


def compute_indicators(
    bars: Sequence[Bar],
    *,
    session_date: date | None = None,
    atr_period: int = 14,
    rsi_period: int = 14,
    fast_ema: int = 9,
    slow_ema: int = 21,
    volume_period: int = 20,
) -> IndicatorSnapshot | None:
    """Compute every indicator the exit engine needs, as of the last completed bar.

    Data-availability boundary:
        ``bars`` must already be filtered to completed bars via
        :func:`src.positions.models.completed_bars`. Every value returned is a
        function of those bars only. ``asof`` on the snapshot is the CLOSE time of
        the last bar -- the moment these numbers first existed, which is what the
        journal should record, not the bar's open stamp.

    Returns:
        A snapshot with None for any indicator lacking history, or None if there are
        no bars at all.
    """
    if not bars:
        return None
    last = bars[-1]
    avg_vol = average_volume(bars, volume_period)
    return IndicatorSnapshot(
        asof=last.close_time,
        last_price=last.close,
        bars_used=len(bars),
        vwap=session_vwap(bars, session_date),
        atr14=atr(bars, atr_period),
        ema9=ema(bars, fast_ema),
        ema21=ema(bars, slow_ema),
        rsi14=rsi(bars, rsi_period),
        volume=last.volume,
        avg_volume20=avg_vol,
        volume_ratio=volume_ratio(bars, volume_period),
    )
