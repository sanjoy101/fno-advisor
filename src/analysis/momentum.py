"""Diagnostics for the exit rules, especially the momentum rule.

The question this exists to answer: **is the momentum rule cutting winners early?**

The test is a forward return. For every position the momentum rule closed, look at
what price did over the next 60 minutes. If the mean forward return is reliably
POSITIVE, the rule sold into continued upside and is costing money. If it is negative
or indistinguishable from zero, the rule is doing its job or at least not hurting.

Two disciplines are enforced in code rather than left to the writer:

* **Every statistic carries its N.** :class:`Stat` cannot be constructed without one,
  and :meth:`Stat.verdict` refuses to call anything significant when the sample is too
  small to support it.
* **Effect sizes are compared to what the sample could detect.**
  :func:`minimum_detectable_effect` says what the smallest finding this N could
  reliably see is. A measured effect below that is not a small effect -- it is no
  measurement at all, and must be reported as such.

Sweeps vary one config value and re-run the whole batch. They are DIAGNOSTIC. Picking
the best-scoring value off a sweep on one sample is how you fit noise; see the caveat
in :func:`sweep_summary`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Sequence

from config.settings import CostSettings, ExitEngineSettings
from src.market.calendar import IST
from src.positions.models import completed_bars
from src.positions.replay import (
    DEFAULT_ENTRY_TIME,
    DEFAULT_NOTIONAL,
    SessionOutcome,
    batch_replay,
)

log = logging.getLogger("fno.analysis")

__all__ = [
    "Stat",
    "summarise",
    "minimum_detectable_effect",
    "exit_rule_distribution",
    "exit_state_distribution",
    "holding_period_profile",
    "rule_profile",
    "exit_time_histogram",
    "forward_returns_post_exit",
    "sweep_trail_multiple",
    "sweep_momentum_confirmation",
    "sweep_summary",
    "FORWARD_MINUTES",
]

#: How far past an exit to measure the forward return.
FORWARD_MINUTES = 60

#: Below this many observations, no claim about a mean is made at all.
MIN_N_FOR_ANY_CLAIM = 30
#: Below this, a claim is reported as directional only, never as established.
MIN_N_FOR_CONFIDENT_CLAIM = 200


@dataclass(frozen=True)
class Stat:
    """A mean with everything needed to judge whether to believe it.

    There is no constructor that omits ``n``. That is deliberate: a mean forward
    return of +0.14% means something completely different at n=12 and n=1,700, and the
    two must never be reportable in the same shape.
    """

    label: str
    n: int
    mean: float
    median: float
    sd: float
    #: Observations dropped because the forward window ran past the session end.
    censored: int = 0

    @property
    def stderr(self) -> float:
        return self.sd / math.sqrt(self.n) if self.n > 1 and self.sd > 0 else 0.0

    @property
    def t_stat(self) -> float:
        return self.mean / self.stderr if self.stderr > 0 else 0.0

    @property
    def ci95(self) -> tuple[float, float]:
        """Normal-approximation 95% interval. Fine at these N; wrong below ~30."""
        half = 1.96 * self.stderr
        return self.mean - half, self.mean + half

    @property
    def mde(self) -> float:
        """Smallest effect this sample could detect at alpha=0.05, power=0.80."""
        return minimum_detectable_effect(self.n, self.sd)

    def verdict(self) -> str:
        """A plain-language reading that refuses to overclaim on a small sample."""
        if self.n < MIN_N_FOR_ANY_CLAIM:
            return (
                f"NO CLAIM POSSIBLE (n={self.n}, need >={MIN_N_FOR_ANY_CLAIM}). "
                f"The point estimate is shown for completeness only."
            )
        low, high = self.ci95
        crosses_zero = low <= 0.0 <= high
        if crosses_zero:
            return (
                f"NOT DISTINGUISHABLE FROM ZERO (95% CI [{low:+.3f}, {high:+.3f}], "
                f"n={self.n}). This sample can only detect effects of "
                f"+/-{self.mde:.3f}pp or larger."
            )
        direction = "POSITIVE" if self.mean > 0 else "NEGATIVE"
        strength = (
            "DIRECTIONAL ONLY" if self.n < MIN_N_FOR_CONFIDENT_CLAIM else "SUPPORTED"
        )
        return (
            f"{direction}, {strength} (mean {self.mean:+.3f}pp, 95% CI "
            f"[{low:+.3f}, {high:+.3f}], t={self.t_stat:+.2f}, n={self.n})"
        )

    def as_row(self) -> dict:
        low, high = self.ci95
        return {
            "label": self.label,
            "n": self.n,
            "mean_pct": round(self.mean, 4),
            "median_pct": round(self.median, 4),
            "sd_pct": round(self.sd, 4),
            "ci95_low": round(low, 4),
            "ci95_high": round(high, 4),
            "t_stat": round(self.t_stat, 3),
            "mde_pct": round(self.mde, 4),
            "censored": self.censored,
        }


def summarise(label: str, values: Sequence[float], *, censored: int = 0) -> Stat:
    """Build a :class:`Stat` from raw observations."""
    n = len(values)
    if n == 0:
        return Stat(label=label, n=0, mean=0.0, median=0.0, sd=0.0, censored=censored)
    ordered = sorted(values)
    mean = sum(values) / n
    median = (
        ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    )
    variance = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    return Stat(label, n, mean, median, math.sqrt(variance), censored)


def minimum_detectable_effect(
    n: int, sd: float, *, alpha_z: float = 1.96, power_z: float = 0.84
) -> float:
    """Smallest true mean this sample could reliably detect.

    ``(z_alpha/2 + z_power) * sd / sqrt(n)``, i.e. alpha=0.05 two-sided, power=0.80.

    Use it to keep a null result honest: "we measured +0.02% but could only have
    detected +0.11%" is a statement about the sample, not about the rule.
    """
    if n < 2 or sd <= 0:
        return float("inf")
    return (alpha_z + power_z) * sd / math.sqrt(n)


# --------------------------------------------------------------------------------
# 1. Exit rule distribution
# --------------------------------------------------------------------------------


def exit_rule_distribution(outcomes: Sequence[SessionOutcome]) -> list[dict]:
    """Count and share of each exit rule.

    The stated decision threshold: if MOMENTUM_REVERSAL is >70% of exits, the trailing
    stop is effectively dead code -- it can only fire on the minority of sessions
    momentum has not already closed.
    """
    total = len(outcomes)
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.exit_rule] = counts.get(outcome.exit_rule, 0) + 1
    rows = [
        {
            "exit_rule": rule,
            "n": n,
            "pct": round(100.0 * n / total, 2) if total else 0.0,
        }
        for rule, n in counts.items()
    ]
    return sorted(rows, key=lambda r: -r["n"])


def exit_state_distribution(outcomes: Sequence[SessionOutcome]) -> list[dict]:
    """Same, grouped by the coarser PositionState."""
    total = len(outcomes)
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.exit_state] = counts.get(outcome.exit_state, 0) + 1
    return sorted(
        (
            {"exit_state": s, "n": n, "pct": round(100.0 * n / total, 2) if total else 0.0}
            for s, n in counts.items()
        ),
        key=lambda r: -r["n"],
    )


# --------------------------------------------------------------------------------
# 2. Exit time histogram
# --------------------------------------------------------------------------------


def holding_period_profile(outcomes: Sequence[SessionOutcome]) -> list[dict]:
    """How long positions actually survive, and where the P&L comes from.

    Worth computing before anything else. If most positions are closed within a bar
    or two of entry, then no exit rule is really being tested -- the aggregate is
    measuring the cost of a round trip, and every downstream comparison inherits that.
    """
    n = len(outcomes)
    if not n:
        return []
    held = sorted(o.minutes_held for o in outcomes)

    def pct(v: float) -> float:
        return round(100.0 * v / n, 2)

    def quantile(q: float) -> float:
        return held[min(n - 1, int(q * n))]

    return [
        {"measure": "positions", "value": n},
        {"measure": "exited on the first completed bar",
         "value": f"{sum(1 for o in outcomes if o.bars_held <= 1)} "
                  f"({pct(sum(1 for o in outcomes if o.bars_held <= 1))}%)"},
        {"measure": "held <= 5 min",
         "value": f"{sum(1 for o in outcomes if o.minutes_held <= 5)} "
                  f"({pct(sum(1 for o in outcomes if o.minutes_held <= 5))}%)"},
        {"measure": "held <= 15 min",
         "value": f"{sum(1 for o in outcomes if o.minutes_held <= 15)} "
                  f"({pct(sum(1 for o in outcomes if o.minutes_held <= 15))}%)"},
        {"measure": "held >= 60 min",
         "value": f"{sum(1 for o in outcomes if o.minutes_held >= 60)} "
                  f"({pct(sum(1 for o in outcomes if o.minutes_held >= 60))}%)"},
        {"measure": "median hold (min)", "value": quantile(0.50)},
        {"measure": "p90 hold (min)", "value": quantile(0.90)},
        {"measure": "max hold (min)", "value": held[-1]},
        {"measure": "mean GROSS P&L (Rs)",
         "value": round(sum(o.gross_pnl for o in outcomes) / n, 2)},
        {"measure": "mean costs (Rs)", "value": round(sum(o.costs for o in outcomes) / n, 2)},
        {"measure": "mean NET P&L (Rs)",
         "value": round(sum(o.net_pnl for o in outcomes) / n, 2)},
        {"measure": "win rate, gross",
         "value": f"{pct(sum(1 for o in outcomes if o.gross_pnl > 0))}%"},
        {"measure": "win rate, net",
         "value": f"{pct(sum(1 for o in outcomes if o.net_pnl > 0))}%"},
    ]


def rule_profile(outcomes: Sequence[SessionOutcome]) -> list[dict]:
    """Per-rule holding time, gross/net P&L and excursions.

    Separates "which rule fires most" from "which rule fires WELL". A rule can
    dominate the count and still be the one destroying value.
    """
    groups: dict[str, list[SessionOutcome]] = {}
    for o in outcomes:
        groups.setdefault(o.exit_rule, []).append(o)
    rows = []
    for rule, group in groups.items():
        k = len(group)
        held = sorted(o.minutes_held for o in group)
        rows.append({
            "exit_rule": rule,
            "n": k,
            "pct": round(100.0 * k / len(outcomes), 1),
            "median_hold_min": held[k // 2],
            "mean_gross_pct": round(sum(o.gross_pct for o in group) / k, 4),
            "mean_net_pnl": round(sum(o.net_pnl for o in group) / k, 2),
            "mean_mfe_pct": round(sum(o.mfe_pct for o in group) / k, 4),
            "mean_mae_pct": round(sum(o.mae_pct for o in group) / k, 4),
            "win_rate_net_pct": round(100.0 * sum(1 for o in group if o.net_pnl > 0) / k, 1),
        })
    return sorted(rows, key=lambda r: -r["n"])


def exit_time_histogram(
    outcomes: Sequence[SessionOutcome],
    *,
    bin_minutes: int = 15,
    rule: str | None = None,
) -> list[dict]:
    """Distribution of exit times, binned.

    Answers whether a clustered exit time is a property of the rules or an artifact of
    whatever data was used. A single dominant bin on synthetic data means nothing; the
    same on 2,000 real sessions is a finding.
    """
    selected = [o for o in outcomes if rule is None or o.exit_rule == rule]
    total = len(selected)
    bins: dict[str, int] = {}
    for outcome in selected:
        if outcome.exit_time is None:
            continue
        local = outcome.exit_time.astimezone(IST)
        minute = (local.hour * 60 + local.minute) // bin_minutes * bin_minutes
        label = f"{minute // 60:02d}:{minute % 60:02d}"
        bins[label] = bins.get(label, 0) + 1
    return [
        {"bin": label, "n": n, "pct": round(100.0 * n / total, 2) if total else 0.0}
        for label, n in sorted(bins.items())
    ]


# --------------------------------------------------------------------------------
# 3. Forward return after exit -- the actual question
# --------------------------------------------------------------------------------


def forward_price(
    bars: Sequence,
    exit_time: datetime,
    minutes: int = FORWARD_MINUTES,
) -> tuple[float | None, bool]:
    """Price ``minutes`` after ``exit_time``, and whether the window was censored.

    Returns ``(price, censored)``. Censored means the forward window ran past the last
    bar of the session, so the observation must be excluded rather than truncated to
    the close -- truncating would systematically shrink the measured move and bias the
    result toward "no effect".

    Data-availability boundary:
        Uses the last bar completed at ``exit_time + minutes``. This is a
        FORWARD-looking measurement made deliberately, in analysis only. It must never
        be used by the engine, which is why it lives here and not in exit_engine.
    """
    target = exit_time + timedelta(minutes=minutes)
    if not bars:
        return None, True
    last_bar_close = max(b.ts for b in bars) + timedelta(minutes=1)
    if target > last_bar_close:
        return None, True
    known = completed_bars(bars, target)
    if not known:
        return None, True
    return known[-1].close, False


def forward_returns_post_exit(
    store,
    outcomes: Sequence[SessionOutcome],
    *,
    minutes: int = FORWARD_MINUTES,
    rule: str | None = None,
) -> tuple[Stat, list[dict]]:
    """Forward return over ``minutes`` after each exit, from the LONG side.

    A positive value means price kept rising after the exit, i.e. the rule sold into
    continued upside. That is the signature of cutting winners early.

    Returns the aggregate :class:`Stat` and the per-observation rows.
    """
    selected = [o for o in outcomes if rule is None or o.exit_rule == rule]
    values: list[float] = []
    rows: list[dict] = []
    censored = 0

    bars_cache: dict[tuple[str, date], list] = {}
    for outcome in selected:
        if outcome.exit_time is None or outcome.exit_price is None:
            censored += 1
            continue
        key = (outcome.symbol, outcome.trade_date)
        if key not in bars_cache:
            bars_cache[key] = store.load_bars(outcome.symbol, outcome.trade_date)
        price, was_censored = forward_price(bars_cache[key], outcome.exit_time, minutes)
        if was_censored or price is None:
            censored += 1
            continue
        pct = 100.0 * (price - outcome.exit_price) / outcome.exit_price
        values.append(pct)
        rows.append(
            {
                "symbol": outcome.symbol,
                "trade_date": outcome.trade_date.isoformat(),
                "exit_rule": outcome.exit_rule,
                "exit_time": outcome.exit_time.astimezone(IST).strftime("%H:%M"),
                "exit_price": round(outcome.exit_price, 2),
                "forward_price": round(price, 2),
                "forward_return_pct": round(pct, 4),
            }
        )

    label = f"forward return {minutes}min after {rule or 'any'} exit"
    return summarise(label, values, censored=censored), rows


# --------------------------------------------------------------------------------
# 4 & 5. Sweeps
# --------------------------------------------------------------------------------


def _sweep(
    store,
    symbols: Sequence[str],
    trade_dates: Sequence[date],
    configs: list[tuple[str, ExitEngineSettings]],
    *,
    cost_cfg: CostSettings | None = None,
    entry_time=DEFAULT_ENTRY_TIME,
    notional: Decimal = DEFAULT_NOTIONAL,
    forward_minutes: int = FORWARD_MINUTES,
) -> list[dict]:
    """Re-run the whole batch under each config and summarise."""
    rows: list[dict] = []
    for label, cfg in configs:
        outcomes = batch_replay(
            store, symbols, trade_dates,
            entry_time=entry_time, notional=notional,
            engine_cfg=cfg, cost_cfg=cost_cfg,
        )
        if not outcomes:
            rows.append({"config": label, "n": 0, "note": "no outcomes"})
            continue

        net = [o.net_pnl for o in outcomes]
        gross = [o.gross_pnl for o in outcomes]
        net_stat = summarise(f"net P&L {label}", net)
        momentum = [o for o in outcomes if o.exit_rule == "MOMENTUM_REVERSAL"]
        fwd, _ = forward_returns_post_exit(
            store, outcomes, minutes=forward_minutes, rule="MOMENTUM_REVERSAL"
        )

        rows.append(
            {
                "config": label,
                "n": len(outcomes),
                "total_net_pnl": round(sum(net), 2),
                "total_gross_pnl": round(sum(gross), 2),
                "mean_net_pnl": round(net_stat.mean, 2),
                "median_net_pnl": round(net_stat.median, 2),
                "sd_net_pnl": round(net_stat.sd, 2),
                "win_rate_net_pct": round(100.0 * sum(1 for v in net if v > 0) / len(net), 2),
                "momentum_exits": len(momentum),
                "momentum_exit_pct": round(100.0 * len(momentum) / len(outcomes), 2),
                "fwd_ret_after_momentum_mean_pct": round(fwd.mean, 4) if fwd.n else None,
                "fwd_ret_n": fwd.n,
                "mean_mfe_pct": round(sum(o.mfe_pct for o in outcomes) / len(outcomes), 4),
                "mean_mae_pct": round(sum(o.mae_pct for o in outcomes) / len(outcomes), 4),
            }
        )
    return rows


def sweep_trail_multiple(
    store,
    symbols: Sequence[str],
    trade_dates: Sequence[date],
    values: Sequence[float] = (0.5, 1.0, 1.5, 2.0, 3.0),
    *,
    base: ExitEngineSettings | None = None,
    **kw,
) -> list[dict]:
    """Net P&L across trailing-stop widths.

    If every row is identical, the trail genuinely never binds -- another rule closes
    the position first, and widening or tightening it changes nothing.
    """
    base = base or ExitEngineSettings()
    configs = [
        (f"trail={v}x", base.model_copy(update={"trail_atr_multiple": Decimal(str(v))}))
        for v in values
    ]
    return _sweep(store, symbols, trade_dates, configs, **kw)


def sweep_momentum_confirmation(
    store,
    symbols: Sequence[str],
    trade_dates: Sequence[date],
    values: Sequence[int] = (1, 2, 3, 5),
    *,
    base: ExitEngineSettings | None = None,
    **kw,
) -> list[dict]:
    """Net P&L and post-exit forward return across confirmation-bar requirements.

    N=1 is the current rule. Higher N demands the reversal persist before acting. The
    diagnostic reading: if forward return after a momentum exit falls toward zero as N
    rises, the single-bar rule was firing on noise.
    """
    base = base or ExitEngineSettings()
    configs = [
        (f"confirm={n}bar", base.model_copy(update={"momentum_confirmation_bars": n}))
        for n in values
    ]
    return _sweep(store, symbols, trade_dates, configs, **kw)


def sweep_summary(rows: Sequence[dict], metric: str = "total_net_pnl") -> str:
    """A guarded reading of a sweep. Never says "use this value".

    A sweep is one sample. The best-scoring value on one sample is the best-scoring
    value on one sample, not the right setting -- separating those requires held-out
    data, which is a later session's job.
    """
    usable = [r for r in rows if r.get("n")]
    if not usable:
        return "No outcomes at any setting -- nothing to compare."
    values = [r[metric] for r in usable]
    spread = max(values) - min(values)
    best = max(usable, key=lambda r: r[metric])
    if spread == 0:
        return (
            f"IDENTICAL at every setting ({metric} = {values[0]:,.2f}, "
            f"n={usable[0]['n']}). The parameter is inert on this sample -- another "
            f"rule decides the exit first."
        )
    relative = abs(spread) / max(1.0, abs(sum(values) / len(values)))
    return (
        f"Range {min(values):,.2f} to {max(values):,.2f} (spread {spread:,.2f}, "
        f"{relative:.1%} of mean). Best on THIS sample: {best['config']}. "
        f"That is an in-sample maximum, not a recommendation -- confirming it needs "
        f"held-out data."
    )
