"""Multi-day swing arm: entry at 15:15, hold 1/3/5 trading days, ATR stop and target.

Three things here that the intraday version did not have to model, all of which can
flatter a result if handled carelessly:

**Gap-through-stop.** A stop cannot execute overnight. If a session opens beyond the
stop the fill is the OPEN, not the stop, and the difference is a real loss the
backtest must wear. Assuming stop-price fills on gap days is the single most common
way a swing backtest invents money.

**Delivery costs.** An overnight hold settles CNC, where STT is 0.1% on BOTH legs
against intraday's 0.025% sell-only. Friction per trade roughly doubles even at zero
brokerage.

**Overlapping windows.** Daily entries with a five-day hold produce trades that share
four days out of five. The raw trade count is not the sample size and must not be used
as one -- see :func:`effective_n`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date
from typing import Mapping, Sequence

from src.positions.indicators import atr
from src.positions.models import Bar
from src.signals import daily as D

log = logging.getLogger("fno.swing")

__all__ = [
    "HORIZONS",
    "SwingTrade",
    "simulate_swing",
    "measure_swing_excursions",
    "SwingExcursion",
    "score_symbol_daily",
    "rank_universe_daily",
    "panel_return_pct",
    "effective_n",
    "mde_from",
]

#: The three holding periods, reported as separate arms and never pooled.
HORIZONS: tuple[int, ...] = (1, 3, 5)


@dataclass(frozen=True)
class SwingExcursion:
    """One stop-less multi-day hold, for calibrating the stop on this horizon."""

    symbol: str
    entry_date: date
    horizon: int
    entry: float
    atr: float
    #: True when the position was in profit at the END of the horizon. Used instead of
    #: "reached a target" to define a winner, because the target is derived FROM this
    #: measurement and defining winners by it would be circular.
    profitable: bool
    mae_atr: float
    mfe_atr: float


@dataclass
class SwingTrade:
    """One swing position from 15:15 entry to exit, with gap handling recorded."""

    symbol: str
    horizon: int
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    stop: float
    target: float
    atr: float
    quantity: int
    #: STOP | STOP_GAP | TARGET | TARGET_GAP | TIME
    exit_reason: str
    days_held: int
    mae_pct: float
    mfe_pct: float
    #: How far past the stop the session opened, in rupees. Zero unless STOP_GAP.
    gap_through_rupees: float = 0.0
    score: float = 0.0

    @property
    def gross_return_pct(self) -> float:
        return 100.0 * (self.exit_price - self.entry_price) / self.entry_price

    @property
    def gapped_through(self) -> bool:
        return self.exit_reason == "STOP_GAP"

    def as_row(self) -> dict:
        row = asdict(self)
        row["entry_date"] = self.entry_date.isoformat()
        row["exit_date"] = self.exit_date.isoformat()
        row["gross_return_pct"] = round(self.gross_return_pct, 4)
        return row


def simulate_swing(
    series: Sequence[Bar],
    entry_index: int,
    horizon: int,
    stop: float,
    target: float,
    *,
    symbol: str = "",
    quantity: int = 1,
    atr_value: float = 0.0,
    score: float = 0.0,
) -> SwingTrade | None:
    """Walk a daily series forward from ``entry_index`` and resolve the position.

    Resolution order within a day, and why:

    1. **The OPEN against the stop.** This is the only price known to be tradeable at
       that moment. If it is at or beyond the stop, the fill is the open -- a stop
       cannot execute while the market is shut.
    2. **The OPEN against the target**, for the same reason in the favourable
       direction.
    3. **The day's low against the stop**, filled at the stop.
    4. **The day's high against the target**, filled at the target.

    Step 3 precedes step 4 deliberately. Within a single daily bar the order of the
    high and the low is unknowable, so a day that touched both is resolved as the
    LOSS. That biases results downward, which is the honest direction for a backtest.

    Data-availability boundary:
        Reads ``series[entry_index + 1 : entry_index + 1 + horizon]``. The entry bar
        itself supplies only its close, which is the 15:15 price the entry is taken at.
    """
    if entry_index < 0 or entry_index >= len(series) - 1:
        return None
    entry_bar = series[entry_index]
    entry_price = entry_bar.close
    if entry_price <= 0:
        return None

    forward = series[entry_index + 1 : entry_index + 1 + horizon]
    if not forward:
        return None

    worst = entry_price
    best = entry_price
    exit_price = forward[-1].close
    exit_bar = forward[-1]
    reason = "TIME"
    gap_through = 0.0
    days_held = len(forward)

    for i, bar in enumerate(forward, start=1):
        worst = min(worst, bar.low)
        best = max(best, bar.high)

        if bar.open <= stop:
            exit_price, exit_bar, reason = bar.open, bar, "STOP_GAP"
            gap_through = stop - bar.open
            days_held = i
            break
        if bar.open >= target:
            exit_price, exit_bar, reason = bar.open, bar, "TARGET_GAP"
            days_held = i
            break
        if bar.low <= stop:
            exit_price, exit_bar, reason = stop, bar, "STOP"
            days_held = i
            break
        if bar.high >= target:
            exit_price, exit_bar, reason = target, bar, "TARGET"
            days_held = i
            break

    return SwingTrade(
        symbol=symbol,
        horizon=horizon,
        entry_date=entry_bar.ts.date(),
        exit_date=exit_bar.ts.date(),
        entry_price=entry_price,
        exit_price=exit_price,
        stop=stop,
        target=target,
        atr=atr_value,
        quantity=quantity,
        exit_reason=reason,
        days_held=days_held,
        mae_pct=100.0 * max(0.0, entry_price - worst) / entry_price,
        mfe_pct=100.0 * max(0.0, best - entry_price) / entry_price,
        gap_through_rupees=max(0.0, gap_through),
        score=score,
    )


def measure_swing_excursions(
    symbol: str,
    series: Sequence[Bar],
    horizon: int,
    *,
    min_history: int = 25,
) -> list[SwingExcursion]:
    """Stop-less holds over the whole series, to calibrate the stop on THIS horizon.

    A "winner" is a position **in profit at the end of the horizon**, not one that
    reached some target. Defining it by a target would be circular: the target is
    derived from the stop, the stop is derived from what winners survived, and the
    winners would be defined by the target. The first attempt here did exactly that
    and produced a stop of 0.32 ATR alongside a traded target of 0.48 ATR — calibrated
    against a 1.5 ATR target that was never used. Profit-at-horizon is independent of
    both and answers the question directly: what drawdown did a position that
    ultimately worked have to sit through?

    Deliberately forward-looking, as the intraday calibration was. Legitimate for
    calibration, illegitimate as a feature, and used only for the former.

    The intraday multiple is NOT reused. Daily ATR spans the overnight gap and the
    window is days rather than minutes, so survivable noise here is a different
    distribution and has to be measured as one.
    """
    out: list[SwingExcursion] = []
    for i in range(min_history, len(series) - 1):
        window = series[: i + 1]
        atr_value = atr(window)
        if atr_value is None or atr_value <= 0:
            continue
        entry = series[i].close
        if entry <= 0:
            continue
        forward = series[i + 1 : i + 1 + horizon]
        if not forward:
            continue

        worst = min(entry, min(b.low for b in forward))
        best = max(entry, max(b.high for b in forward))
        out.append(SwingExcursion(
            symbol=symbol,
            entry_date=series[i].ts.date(),
            horizon=horizon,
            entry=entry,
            atr=atr_value,
            profitable=forward[-1].close > entry,
            mae_atr=max(0.0, entry - worst) / atr_value,
            mfe_atr=max(0.0, best - entry) / atr_value,
        ))
    return out


def score_symbol_daily(
    daily: Mapping[date, Bar],
    sessions: Sequence[date],
    upto: date,
    panel_median_return: float | None,
    cfg,
):
    """Composite score for one symbol at 15:15 on ``upto``, from daily features.

    The SAME nine components and the SAME weights as the intraday engine, fed daily
    analogues: the prior 5-day range in place of the opening range, a 20-day SMA in
    place of session VWAP, daily volume against its 20-day mean in place of the
    time-of-day baseline. Reusing the scoring functions unchanged is deliberate --
    the hypothesis is that these features carry signal on a longer horizon, and
    changing their shaping at the same time would confound the test.
    """
    from src.signals.scoring import (
        composite,
        score_ema_trend,
        score_or_breakout,
        score_or_hold,
        score_relative_strength,
        score_roc,
        score_rsi,
        score_volume,
        score_vwap_persistence,
        score_vwap_trend,
    )

    breakout = D.range_breakout_atr(daily, sessions, upto)
    state = None if breakout is None else (
        "ABOVE" if breakout > 0 else "BELOW" if breakout < 0 else "INSIDE"
    )
    components = (
        score_or_breakout(state, breakout, cfg),
        score_or_hold(D.range_breakout_held(daily, sessions, upto), cfg),
        score_ema_trend(D.daily_ema_alignment_pct(daily, sessions, upto), cfg),
        score_vwap_trend(D.close_vs_sma_pct(daily, sessions, upto), cfg),
        score_vwap_persistence(D.days_above_sma(daily, sessions, upto), cfg),
        score_rsi(D.daily_rsi(daily, sessions, upto), cfg),
        score_roc(D.daily_roc_pct(daily, sessions, upto), cfg),
        score_volume(D.daily_relative_volume(daily, sessions, upto), cfg),
        score_relative_strength(
            D.daily_return_pct(daily, sessions, upto), panel_median_return, cfg
        ),
    )
    return composite(components), components


def rank_universe_daily(
    daily_by_symbol: Mapping[str, Mapping[date, Bar]],
    sessions: Sequence[date],
    upto: date,
    cfg,
    *,
    min_atr_pct: float = 0.0,
    min_turnover: float = 0.0,
):
    """Score every symbol at ``upto`` and return them ranked, best first.

    The panel median return is computed first, across the symbols that survive the
    hard gates, so relative strength is measured against the tradeable universe rather
    than against names that were never candidates.
    """
    eligible: dict[str, dict] = {}
    for symbol, daily in daily_by_symbol.items():
        series = D.daily_series(daily, sessions, upto)
        if not series:
            continue
        atr_pct = D.daily_atr_pct(daily, sessions, upto)
        close = series[-1].close
        turnover = close * series[-1].volume
        if atr_pct is None or atr_pct < min_atr_pct or turnover < min_turnover:
            continue
        eligible[symbol] = {
            "atr_pct": atr_pct,
            "close": close,
            "ret": D.daily_return_pct(daily, sessions, upto),
        }

    if not eligible:
        return [], None

    returns = [v["ret"] for v in eligible.values() if v["ret"] is not None]
    panel_median = sorted(returns)[len(returns) // 2] if returns else None

    ranked = []
    for symbol, info in eligible.items():
        score, components = score_symbol_daily(
            daily_by_symbol[symbol], sessions, upto, panel_median, cfg
        )
        if score is None:
            continue
        ranked.append({
            "symbol": symbol,
            "score": score,
            "components": components,
            "atr_pct": info["atr_pct"],
            "close": info["close"],
        })
    ranked.sort(key=lambda r: (-r["score"], r["symbol"]))
    return ranked, panel_median


def panel_return_pct(
    daily_by_symbol: Mapping[str, Mapping[date, Bar]],
    entry_date: date,
    exit_date: date,
) -> float | None:
    """Equal-weight buy-and-hold return of the whole panel over the SAME dates.

    The honest comparator on a multi-day horizon. Without it beta masquerades as edge:
    a long-only rule in a rising market makes money for reasons that have nothing to do
    with the features, and every number looks like a discovery.

    Both legs use the 15:15 close, matching how the trade is entered.
    """
    if exit_date <= entry_date:
        return 0.0
    returns = []
    for series in daily_by_symbol.values():
        a, b = series.get(entry_date), series.get(exit_date)
        if a is None or b is None or a.close <= 0:
            continue
        returns.append(100.0 * (b.close - a.close) / a.close)
    return sum(returns) / len(returns) if returns else None


# --------------------------------------------------------------------------------
# Effective sample size
# --------------------------------------------------------------------------------


def effective_n(
    entry_dates: Sequence[date],
    horizon: int,
) -> dict:
    """How many INDEPENDENT observations a set of overlapping trades really contains.

    Daily entries with an ``h``-day hold share ``h - 1`` days of their window with
    their neighbours, so consecutive trades are not independent draws. Two separate
    dependencies are at work and both shrink the sample:

    * **Time overlap.** Only every ``h``-th entry date starts a fresh window, so the
      count of independent time blocks is ``distinct_dates / h``.
    * **Cross-section.** Trades opened on the same date share that day's market move.
      Reporting excess-over-panel removes most of it, but they are still one draw on
      the day, not several -- so the date, not the trade, is the unit.

    Taking both together, the conservative unit is the non-overlapping DATE BLOCK. On
    ~1,240 sessions a 5-day hold leaves roughly 248 of them, not 1,240, and certainly
    not the raw trade count.
    """
    distinct = sorted(set(entry_dates))
    n_trades = len(entry_dates)
    n_dates = len(distinct)
    blocks = max(1, n_dates // horizon) if n_dates else 0
    return {
        "trades": n_trades,
        "distinct_entry_dates": n_dates,
        "horizon_days": horizon,
        "effective_n": blocks,
        "overstatement_factor": round(n_trades / blocks, 1) if blocks else None,
    }


def mde_from(n: int, sd: float, *, alpha_z: float = 1.96, power_z: float = 0.84) -> float:
    """Smallest true mean detectable at alpha=0.05, power=0.80, for a given n.

    Fed the EFFECTIVE n, never the trade count. Using the trade count would understate
    the detectable effect by the square root of the overstatement factor and turn a
    null result into a false discovery.
    """
    import math

    if n < 2 or sd <= 0:
        return float("inf")
    return (alpha_z + power_z) * sd / math.sqrt(n)
