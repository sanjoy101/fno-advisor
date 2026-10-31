"""Daily aggregates and daily-horizon features for the swing experiment.

A daily bar is just a :class:`~src.positions.models.Bar` stamped at the session's
15:15 decision moment. That is deliberate: it means ``atr``, ``ema`` and ``rsi`` from
``src.positions.indicators`` apply unchanged, so the daily and intraday feature sets
cannot drift apart in their arithmetic. Wilder's true range then picks up the
overnight gap automatically, which is exactly what a swing ATR should measure and an
intraday one should not.

Everything is aggregated up to 15:15, not to the close. Entry happens at 15:15, so a
feature built from the full session would be using thirteen minutes the decision could
not have seen.

Causality
---------
Every feature takes ``(daily, sessions, upto)`` and calls :func:`daily_series`
itself. It is never handed a pre-filtered list, because a function given clean input
cannot be caught leaking. This is the same cross-session route the time-of-day volume
baseline leaked through, and it is checked the same way, with
:func:`src.analysis.lookahead.assert_session_causal`, on every feature here.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Mapping, Sequence

from src.market.calendar import IST, INTRADAY_SQUARE_OFF
from src.positions.indicators import atr, ema, rsi
from src.positions.models import Bar

__all__ = [
    "DECISION_TIME",
    "aggregate_daily",
    "build_daily_map",
    "daily_series",
    "range_breakout_atr",
    "range_breakout_held",
    "daily_ema_alignment_pct",
    "close_vs_sma_pct",
    "days_above_sma",
    "daily_rsi",
    "daily_roc_pct",
    "daily_relative_volume",
    "daily_atr_pct",
    "daily_return_pct",
    "daily_close",
]

#: Entry and aggregation cut-off. Everything is measured as of the moment we act.
DECISION_TIME: time = INTRADAY_SQUARE_OFF  # 15:15 IST


def aggregate_daily(bars: Sequence[Bar], trade_date: date) -> Bar | None:
    """Collapse one session's 1-minute bars into a daily bar, cut off at 15:15.

    The bar is stamped at 15:15 rather than at the session open so that "the last
    completed daily bar at time T" means what it should for a decision taken then.

    Data-availability boundary:
        Uses bars with ``ts < 15:15`` on ``trade_date``. The last thirteen minutes of
        the session are excluded because the entry happens at 15:15 and cannot see
        them.
    """
    cutoff = datetime.combine(trade_date, DECISION_TIME, tzinfo=IST)
    window = [
        b for b in bars
        if b.ts.astimezone(IST).date() == trade_date and b.ts < cutoff
    ]
    if not window:
        return None
    return Bar(
        ts=cutoff,
        open=window[0].open,
        high=max(b.high for b in window),
        low=min(b.low for b in window),
        close=window[-1].close,
        volume=sum(b.volume for b in window),
    )


def build_daily_map(store, symbol: str, sessions: Sequence[date]) -> dict[date, Bar]:
    """Daily bars for every session with data. Built once, reused across features."""
    out: dict[date, Bar] = {}
    for day in sessions:
        if not store.has_bars(symbol, day):
            continue
        daily = aggregate_daily(store.load_bars(symbol, day), day)
        if daily is not None:
            out[day] = daily
    return out


def daily_series(
    daily: Mapping[date, Bar],
    sessions: Sequence[date],
    upto: date,
) -> list[Bar]:
    """Daily bars for sessions at or before ``upto``, ascending. The causality gate.

    Every feature in this module goes through here. The ``<= upto`` is the whole
    guarantee, and ``assert_session_causal`` is what proves each feature actually
    uses it rather than reaching into the list it was handed.
    """
    return [daily[d] for d in sorted(sessions) if d <= upto and d in daily]


def daily_close(daily, sessions, upto) -> float | None:
    """Close of the most recent daily bar at or before ``upto``."""
    series = daily_series(daily, sessions, upto)
    return series[-1].close if series else None


# --------------------------------------------------------------------------------
# Breakout: the daily analogue of the opening range
# --------------------------------------------------------------------------------


def range_breakout_atr(daily, sessions, upto, *, lookback: int = 5) -> float | None:
    """Close relative to the PRIOR ``lookback``-day high/low, in daily ATRs.

    The daily counterpart of the opening-range breakout: instead of "past the first
    fifteen minutes", it is "past the last week". The prior window EXCLUDES today, so
    today's own high cannot define the level it is being compared against -- that
    would make a breakout arithmetically impossible.
    """
    series = daily_series(daily, sessions, upto)
    if len(series) < lookback + 1:
        return None
    prior = series[-(lookback + 1) : -1]
    atr_value = atr(series)
    if atr_value is None or atr_value <= 0:
        return None
    close = series[-1].close
    high, low = max(b.high for b in prior), min(b.low for b in prior)
    if close > high:
        return (close - high) / atr_value
    if close < low:
        return (close - low) / atr_value
    return 0.0


def range_breakout_held(daily, sessions, upto, *, lookback: int = 5) -> int | None:
    """Consecutive daily closes above their own prior ``lookback``-day high.

    Each day is compared against the range as it stood THEN, not against today's, for
    the same reason ``bars_above_vwap`` recomputes VWAP per bar.
    """
    series = daily_series(daily, sessions, upto)
    if len(series) < lookback + 1:
        return None
    held = 0
    for i in range(len(series) - 1, lookback - 1, -1):
        prior = series[i - lookback : i]
        if series[i].close > max(b.high for b in prior):
            held += 1
        else:
            break
    return held


# --------------------------------------------------------------------------------
# Trend
# --------------------------------------------------------------------------------


def daily_ema_alignment_pct(daily, sessions, upto) -> float | None:
    """Daily EMA9 over EMA21, as a percent."""
    series = daily_series(daily, sessions, upto)
    fast, slow = ema(series, 9), ema(series, 21)
    if fast is None or slow is None or slow == 0:
        return None
    return 100.0 * (fast - slow) / slow


def close_vs_sma_pct(daily, sessions, upto, *, lookback: int = 20) -> float | None:
    """Close relative to its ``lookback``-day simple average, as a percent.

    Stands in for price-vs-VWAP on this horizon. A session VWAP resets every morning
    and says nothing about a five-day hold.
    """
    series = daily_series(daily, sessions, upto)
    if len(series) < lookback:
        return None
    sma = sum(b.close for b in series[-lookback:]) / lookback
    if sma <= 0:
        return None
    return 100.0 * (series[-1].close - sma) / sma


def days_above_sma(daily, sessions, upto, *, lookback: int = 20) -> int | None:
    """Consecutive daily closes above the moving average AS IT STOOD each day."""
    series = daily_series(daily, sessions, upto)
    if len(series) < lookback:
        return None
    held = 0
    for i in range(len(series) - 1, lookback - 2, -1):
        window = series[i - lookback + 1 : i + 1]
        if len(window) < lookback:
            break
        sma = sum(b.close for b in window) / lookback
        if series[i].close > sma:
            held += 1
        else:
            break
    return held


# --------------------------------------------------------------------------------
# Momentum, volume, volatility
# --------------------------------------------------------------------------------


def daily_rsi(daily, sessions, upto, period: int = 14) -> float | None:
    return rsi(daily_series(daily, sessions, upto), period)


def daily_roc_pct(daily, sessions, upto, *, lookback: int = 5) -> float | None:
    """Percent change over the last ``lookback`` daily closes."""
    series = daily_series(daily, sessions, upto)
    if len(series) < lookback + 1:
        return None
    then = series[-(lookback + 1)].close
    if then <= 0:
        return None
    return 100.0 * (series[-1].close - then) / then


def daily_relative_volume(daily, sessions, upto, *, lookback: int = 20) -> float | None:
    """Today's volume over the mean of the ``lookback`` days BEFORE it.

    Today is excluded from its own baseline, exactly as in the intraday version --
    including it drags the average toward the value being tested and mutes the surge
    the feature exists to detect. Time-of-day normalisation is unnecessary here: a
    whole session is a whole session.
    """
    series = daily_series(daily, sessions, upto)
    if len(series) < lookback + 1:
        return None
    baseline = sum(b.volume for b in series[-(lookback + 1) : -1]) / lookback
    if baseline <= 0:
        return None
    return series[-1].volume / baseline


def daily_atr_pct(daily, sessions, upto) -> float | None:
    """Daily ATR(14) as a percent of the last close.

    Much larger than its intraday namesake, because Wilder's true range now spans the
    overnight gap. That is the correct measure for a hold that carries that risk.
    """
    series = daily_series(daily, sessions, upto)
    value = atr(series)
    if value is None or not series or series[-1].close <= 0:
        return None
    return 100.0 * value / series[-1].close


def daily_return_pct(daily, sessions, upto) -> float | None:
    """Close-to-close return of the most recent daily bar, as a percent.

    Feeds the cross-sectional relative-strength score. Close-to-close rather than
    open-to-close because on this horizon the overnight move is part of the return,
    not something an entry at 15:15 can sidestep.
    """
    series = daily_series(daily, sessions, upto)
    if len(series) < 2 or series[-2].close <= 0:
        return None
    return 100.0 * (series[-1].close - series[-2].close) / series[-2].close
