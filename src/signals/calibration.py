"""Measure the noise a correct position must survive, and size the stop from it.

The current stop sits at 1.02x the mean adverse excursion. That is not a badly chosen
number, it is a design error: a stop inside the noise floor exits on chatter whatever
the signal was worth. Sweeping its width on DEV would only find the width that fitted
DEV. The fix is to derive it.

The measurement
---------------
For every candidate entry, simulate a long with NO stop and walk forward to the 15:15
square-off:

* If price reaches T1, record the worst adverse excursion suffered BEFORE it got
  there. That is survivable noise -- a position that was RIGHT still had to sit
  through it, so any stop tighter than that would have cut a winner.
* Record the best favourable excursion either way, to judge whether a target is
  reachable at all.

Everything is expressed in ATR units at the moment of entry, which is what makes a
single multiplier meaningful across symbols and across the day.

This measurement is deliberately FORWARD-LOOKING. It has to be: "what did a winner
have to endure" is a question about what happened next. That is legitimate for
calibration and illegitimate for a feature, and the two must not be confused --
nothing here is used at decision time. It runs on DEV only, and the constant it
produces is a DEV-derived constant, which is a weaker claim than an out-of-sample one
and is reported as such.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Sequence

from src.market.calendar import IST, INTRADAY_SQUARE_OFF
from src.positions.indicators import atr_series
from src.positions.models import BAR_INTERVAL, Bar

log = logging.getLogger("fno.signals.calibration")

__all__ = [
    "Excursion",
    "TIME_BUCKETS",
    "bucket_of",
    "measure_session",
    "percentile",
    "describe",
]

#: Coarse time-of-day buckets. Intraday noise is not stationary: the first hour is a
#: different regime from the lunch lull, and pooling them would hide it.
TIME_BUCKETS: tuple[tuple[str, time, time], ...] = (
    ("09:30-10:29", time(9, 30), time(10, 29)),
    ("10:30-11:59", time(10, 30), time(11, 59)),
    ("12:00-13:29", time(12, 0), time(13, 29)),
    ("13:30-14:30", time(13, 30), time(14, 30)),
)


def bucket_of(at: datetime) -> str:
    t = at.astimezone(IST).time()
    for label, lo, hi in TIME_BUCKETS:
        if lo <= t <= hi:
            return label
    return "outside"


@dataclass(frozen=True)
class Excursion:
    """One simulated stop-less long, from entry to square-off."""

    symbol: str
    trade_date: date
    at: datetime
    bucket: str
    entry: float
    atr: float
    reached_t1: bool
    #: Worst adverse excursion in ATRs. Measured up to the T1 touch when there was
    #: one, otherwise over the whole holding window.
    mae_atr: float
    #: Best favourable excursion in ATRs over the whole window.
    mfe_atr: float
    bars_to_t1: int | None


def measure_session(
    symbol: str,
    trade_date: date,
    bars: Sequence[Bar],
    entry_times: Sequence[time],
    *,
    t1_atr_multiple: float,
    atr_period: int = 14,
    min_atr_pct: float = 0.0,
) -> list[Excursion]:
    """Simulate a stop-less long at each entry time and record its excursions.

    ``min_atr_pct`` applies the SAME minimum-ATR gate the engine applies. Without it
    the measurement includes entries the engine would never take, and a near-zero ATR
    turns the excursion ratio into nonsense -- an unfiltered first pass produced a
    maximum MAE of 58,755 ATR, which is a division artefact, not a market move. The
    percentiles survive such outliers but the means do not, and neither does anyone's
    confidence in the table.

    Data-availability boundary:
        Entry price and ATR come from the last bar COMPLETED at the entry time -- the
        same information the engine would have had. Everything after that is future
        data, used on purpose: the point is to measure what came next.

        The bar that touches T1 has its low included in the adverse excursion. Within
        one bar the order of high and low is unknowable, so this assumes the worst.
        That biases the derived stop WIDER, which is the safe direction for a stop.
    """
    import numpy as np  # noqa: PLC0415 - heavy import, calibration only

    if not bars:
        return []
    highs = np.fromiter((b.high for b in bars), dtype=float, count=len(bars))
    lows = np.fromiter((b.low for b in bars), dtype=float, count=len(bars))
    closes = np.fromiter((b.close for b in bars), dtype=float, count=len(bars))
    atrs = atr_series(bars, atr_period)

    square_off = datetime.combine(trade_date, INTRADAY_SQUARE_OFF, tzinfo=IST)
    last_idx = len(bars) - 1
    for i, bar in enumerate(bars):
        if bar.ts >= square_off:
            last_idx = i - 1
            break
    if last_idx < 1:
        return []

    # Index of the last bar completed at each entry time, found once by scanning.
    completed_by: dict[time, int] = {}
    for entry_time in entry_times:
        at = datetime.combine(trade_date, entry_time, tzinfo=IST)
        idx = -1
        for i, bar in enumerate(bars):
            if bar.ts + BAR_INTERVAL <= at:
                idx = i
            else:
                break
        completed_by[entry_time] = idx

    out: list[Excursion] = []
    for entry_time in entry_times:
        idx = completed_by[entry_time]
        if idx < 0 or idx >= last_idx:
            continue
        atr_now = atrs[idx]
        if atr_now is None or atr_now <= 0:
            continue
        entry = float(closes[idx])
        if entry <= 0 or 100.0 * atr_now / entry < min_atr_pct:
            continue

        fut_high = highs[idx + 1 : last_idx + 1]
        fut_low = lows[idx + 1 : last_idx + 1]
        if fut_high.size == 0:
            continue

        target = entry + t1_atr_multiple * atr_now
        touched = fut_high >= target
        hit_rel = int(touched.argmax()) if touched.any() else None

        if hit_rel is None:
            worst_low = float(fut_low.min())
            best_high = float(fut_high.max())
        else:
            worst_low = float(fut_low[: hit_rel + 1].min())
            best_high = float(fut_high.max())

        out.append(Excursion(
            symbol=symbol, trade_date=trade_date,
            at=datetime.combine(trade_date, entry_time, tzinfo=IST),
            bucket=bucket_of(datetime.combine(trade_date, entry_time, tzinfo=IST)),
            entry=entry, atr=atr_now,
            reached_t1=hit_rel is not None,
            mae_atr=max(0.0, (entry - worst_low) / atr_now),
            mfe_atr=max(0.0, (best_high - entry) / atr_now),
            bars_to_t1=None if hit_rel is None else hit_rel + 1,
        ))
    return out


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolated percentile. ``p`` in 0..100."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (p / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def describe(values: Sequence[float], label: str) -> dict:
    """Shape of a distribution, with n attached."""
    n = len(values)
    if not n:
        return {"group": label, "n": 0}
    return {
        "group": label,
        "n": n,
        "mean": round(sum(values) / n, 3),
        "p25": round(percentile(values, 25), 3),
        "p50": round(percentile(values, 50), 3),
        "p75": round(percentile(values, 75), 3),
        "p90": round(percentile(values, 90), 3),
        "p95": round(percentile(values, 95), 3),
        "max": round(max(values), 3),
    }
