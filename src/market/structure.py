"""Descriptive intraday market structure from completed OHLCV bars.

These calculations describe price action already observed. They do not predict
returns, rank symbols, or produce entry recommendations. Callers must pass only
completed bars (``completed_bars`` is the shared data-availability boundary).
"""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from typing import Sequence

from src.market.calendar import ensure_ist
from src.positions.models import BAR_INTERVAL, Bar

INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY"})

__all__ = ["INDEX_SYMBOLS", "market_observation", "market_structure", "resample_bars",
           "scanner_observation", "sector_breadth"]


def resample_bars(bars: Sequence[Bar], minutes: int, asof) -> list[Bar]:
    """Aggregate complete one-minute bars into complete 1/5/15-minute candles.

    A group with a missing minute is omitted rather than filled with synthetic data.
    The final bucket is omitted until its close time, so no chart can expose a forming
    multi-minute candle.
    """
    if minutes not in (1, 5, 15):
        raise ValueError("chart interval must be 1, 5 or 15 minutes")
    asof = ensure_ist(asof)
    if minutes == 1:
        return [bar for bar in bars if bar.close_time <= asof]
    buckets: dict[object, list[Bar]] = defaultdict(list)
    for bar in bars:
        minute = (bar.ts.minute // minutes) * minutes
        start = bar.ts.replace(minute=minute, second=0, microsecond=0)
        buckets[start].append(bar)
    aggregated = []
    expected = int(minutes / (BAR_INTERVAL.total_seconds() / 60))
    for start, group in sorted(buckets.items()):
        group.sort(key=lambda bar: bar.ts)
        if start + (minutes * BAR_INTERVAL) > asof or len(group) != expected:
            continue
        if any(right.ts - left.ts != BAR_INTERVAL for left, right in zip(group, group[1:])):
            continue
        aggregated.append(Bar(ts=start, open=group[0].open, high=max(b.high for b in group),
                              low=min(b.low for b in group), close=group[-1].close,
                              volume=sum(b.volume for b in group)))
    return aggregated


def _atr(bars: Sequence[Bar], end: int, period: int = 14) -> float | None:
    """Mean true range over bars strictly before ``end``."""
    start = max(1, end - period)
    ranges = [
        max(bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close))
        for i in range(start, end)
    ]
    return sum(ranges) / len(ranges) if ranges else None


def _pivots(bars: Sequence[Bar], wing: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs, lows = [], []
    for i in range(wing, len(bars) - wing):
        high_window = [b.high for b in bars[i - wing:i + wing + 1]]
        low_window = [b.low for b in bars[i - wing:i + wing + 1]]
        if bars[i].high == max(high_window) and high_window.count(bars[i].high) == 1:
            highs.append((i, bars[i].high))
        if bars[i].low == min(low_window) and low_window.count(bars[i].low) == 1:
            lows.append((i, bars[i].low))
    return highs, lows


def _equal_levels(pivots: list[tuple[int, float]], tolerance: float) -> list[dict]:
    levels: list[list[tuple[int, float]]] = []
    for pivot in pivots:
        bucket = next((g for g in levels if abs(g[0][1] - pivot[1]) <= tolerance), None)
        if bucket is None:
            levels.append([pivot])
        else:
            bucket.append(pivot)
    return [
        {"price": round(sum(p for _, p in group) / len(group), 4),
         "touches": len(group), "first_bar": group[0][0], "last_bar": group[-1][0]}
        for group in levels if len(group) >= 2
    ]


def market_structure(bars: Sequence[Bar], *, max_zones: int = 6) -> dict:
    """Find confirmed swing, equal-price and displacement zones.

    A candidate order block is the last opposite-colour candle in the three bars
    before a completed displacement candle whose range is at least 1.5 prior ATRs
    and which closes through the most recent confirmed swing. The zone is the full
    high-low of that opposite candle. Only a mechanical pattern is reported; it is
    not evidence of institutional orders or a forecast.
    """
    if len(bars) < 7:
        return {"state": "INSUFFICIENT_BARS", "swing_highs": [], "swing_lows": [],
                "equal_highs": [], "equal_lows": [], "order_blocks": []}
    highs, lows = _pivots(bars)
    tail_highs, tail_lows = highs[-12:], lows[-12:]
    typical = bars[-1].close
    atr = _atr(bars, len(bars))
    tolerance = max(typical * 0.001, (atr or 0) * 0.15)
    zones = []
    high_cursor = low_cursor = 0
    last_high = last_low = None
    for i in range(3, len(bars)):
        # A pivot centered at i-3 is the newest one whose two right-hand bars
        # existed before candidate candle i. Use the globally computed pivot list
        # with a causal cursor instead of recalculating every prefix of the session.
        confirmed_through = i - 3
        while high_cursor < len(highs) and highs[high_cursor][0] <= confirmed_through:
            last_high = highs[high_cursor][1]
            high_cursor += 1
        while low_cursor < len(lows) and lows[low_cursor][0] <= confirmed_through:
            last_low = lows[low_cursor][1]
            low_cursor += 1
        prior_atr = _atr(bars, i)
        if prior_atr is None or prior_atr <= 0:
            continue
        candle = bars[i]
        move = candle.high - candle.low
        if move < 1.5 * prior_atr:
            continue
        # The cursor includes only pivots whose right wing completed before i, so
        # future bars cannot retrospectively validate an earlier breakout.
        direction = ("BULLISH" if candle.close > candle.open and last_high is not None
                     and candle.close > last_high else
                     "BEARISH" if candle.close < candle.open and last_low is not None
                     and candle.close < last_low else None)
        if direction is None:
            continue
        opposite = next((j for j in range(i - 1, max(-1, i - 4), -1)
                         if ((bars[j].close < bars[j].open) if direction == "BULLISH"
                             else (bars[j].close > bars[j].open))), None)
        if opposite is None:
            continue
        block = bars[opposite]
        zones.append({"direction": direction, "low": block.low, "high": block.high,
                      "formed_at": block.ts.isoformat(),
                      "confirmed_at": candle.close_time.isoformat(),
                      "displacement_atr": round(move / prior_atr, 2),
                      "status": "POTENTIAL_ZONE"})
    return {
        "state": "AVAILABLE",
        "swing_highs": [{"bar": i, "price": p} for i, p in tail_highs],
        "swing_lows": [{"bar": i, "price": p} for i, p in tail_lows],
        "equal_highs": _equal_levels(tail_highs, tolerance),
        "equal_lows": _equal_levels(tail_lows, tolerance),
        "order_blocks": zones[-max_zones:],
        "rules": {"pivot_wing_bars": 2, "equal_level_tolerance": round(tolerance, 4),
                  "displacement_min_atr": 1.5, "zone_source": "last opposite candle"},
    }


def market_observation(symbol: str, bars: Sequence[Bar], *, stale: bool,
                       last_data_at: str | None) -> dict:
    """One alphabetically ordered row, measured from the earliest available bar."""
    if not bars:
        return {"symbol": symbol, "state": "NO_DATA", "stale": stale,
                "last_data_at": last_data_at}
    first, last = bars[0], bars[-1]
    change = (last.close - first.open) / first.open * 100 if first.open else None
    state = "UP_FROM_BASELINE" if change and change > 0 else "DOWN_FROM_BASELINE" if change and change < 0 else "AT_BASELINE"
    volume = sum(b.volume for b in bars)
    return {
        "symbol": symbol, "state": state, "stale": stale,
        "last_data_at": last_data_at, "last_price": last.close,
        "baseline_price": first.open, "baseline_at": first.ts.isoformat(),
        "change_from_baseline_pct": round(change, 3) if change is not None else None,
        "session_high": max(b.high for b in bars), "session_low": min(b.low for b in bars),
        "volume": volume, "bars": len(bars),
    }


def scanner_observation(bars: Sequence[Bar], *, choppy_efficiency: float = 0.20) -> dict:
    """Describe completed-bar direction, path efficiency, and recent volume activity.

    Data-availability boundary:
        Uses only the supplied completed bars, in chronological order. Price change
        is measured from the first available bar's open to the latest close. Recent
        volume is the mean of the latest five bars versus the mean of up to the prior
        fifteen bars. At least five bars are required in each volume window. This is
        an intraday comparison, not a multi-day or time-of-day-normalized baseline.
    """
    if len(bars) < 10:
        return {"eligible": False, "reason": "NEED_MORE_BARS", "score": None,
                "direction": None, "volume_ratio": None, "efficiency": None}
    recent = bars[-5:]
    prior = bars[max(0, len(bars) - 20):-5]
    if len(prior) < 5 or fmean(b.volume for b in prior) <= 0:
        return {"eligible": False, "reason": "VOLUME_BASELINE_UNAVAILABLE", "score": None,
                "direction": None, "volume_ratio": None, "efficiency": None}
    volume_ratio = fmean(b.volume for b in recent) / fmean(b.volume for b in prior)
    first, last = bars[0], bars[-1]
    net = last.close - first.open
    path = abs(first.close - first.open) + sum(
        abs(right.close - left.close) for left, right in zip(bars, bars[1:])
    )
    efficiency = min(1.0, abs(net) / path) if path > 0 else 0.0
    change_pct = net / first.open * 100 if first.open else 0.0
    direction = "BULLISH" if net > 0 else "BEARISH" if net < 0 else None
    if direction is None:
        return {"eligible": False, "reason": "NO_DIRECTIONAL_CHANGE", "score": 0.0,
                "direction": None, "volume_ratio": round(volume_ratio, 2),
                "efficiency": round(efficiency, 3), "change_pct": round(change_pct, 3)}
    if efficiency < choppy_efficiency:
        return {"eligible": False, "reason": "CHOPPY_PATH", "score": 0.0,
                "direction": direction, "volume_ratio": round(volume_ratio, 2),
                "efficiency": round(efficiency, 3), "change_pct": round(change_pct, 3)}
    score = abs(change_pct) * min(volume_ratio, 5.0) * efficiency
    return {"eligible": True, "reason": None, "score": round(score, 4),
            "direction": direction, "volume_ratio": round(volume_ratio, 2),
            "efficiency": round(efficiency, 3), "change_pct": round(change_pct, 3)}


def sector_breadth(rows: Sequence[dict], sectors: dict[str, str]) -> list[dict]:
    """Descriptive advancing/declining counts; unknown sectors stay explicit."""
    groups: dict[str, dict[str, int]] = defaultdict(lambda: {"up": 0, "down": 0, "flat": 0, "no_data": 0})
    for row in rows:
        sector = sectors.get(row["symbol"], "UNKNOWN")
        state = row.get("state")
        key = "up" if state == "UP_FROM_BASELINE" else "down" if state == "DOWN_FROM_BASELINE" else "flat" if state == "AT_BASELINE" else "no_data"
        groups[sector][key] += 1
    return [{"sector": sector, **counts} for sector, counts in sorted(groups.items())]
