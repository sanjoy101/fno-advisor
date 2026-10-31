"""Evaluate Module B: run the signal engine across sessions and score the results.

Each signal is handed straight to Module C's replay driver, so the exit is decided by
the same rules that would run live. Nothing here decides when to sell.

Two disciplines carried over from the momentum analysis:

* **Every P&L figure is net of costs**, and the gross sits beside it.
* **The baseline is reported whether or not it flatters the engine.** "Buy the
  highest-volume stock at 09:45" is a rule a person could follow with no engine at
  all. If the signal engine cannot beat it, that is the finding.

R multiple is net P&L over the risk the signal itself declared -- ``(entry - stop) x
quantity``. Measuring against the engine's own stated risk is the only way the number
means anything: a strategy can post a good average R by quietly widening its stops.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Sequence

from config.settings import (
    CostSettings,
    ExitEngineSettings,
    SignalEngineSettings,
    get_cost_settings,
    get_exit_engine_settings,
    get_signal_engine_settings,
)
from src.positions.replay import DEFAULT_NOTIONAL, SessionOutcome, replay_one_session
from src.signals.engine import generate_signals, signal_times
from src.signals.features import volume_profile_for as F_volume_profile_for
from src.signals.universe import FnoBanList, UniverseFilters

log = logging.getLogger("fno.signals.eval")

__all__ = [
    "BarCache",
    "SignalOutcome",
    "SingleShotResult",
    "run_single_shot",
    "run_single_shot_session",
    "run_signal_session",
    "run_signal_sessions",
    "baseline_highest_volume",
    "summarise",
    "write_signal_csv",
    "BASELINE_ENTRY_TIME",
]

#: The baseline enters at a fixed clock time, as the momentum analysis did.
BASELINE_ENTRY_TIME = time(9, 45)


#: Sessions of bars to keep in memory. The volume baseline needs a 20-session
#: trailing window and sessions are processed in date order, so a slightly larger
#: window holds everything the next day will ask for.
CACHE_SESSIONS = 28


class BarCache:
    """Bounded, order-aware memo for bar loads. Purely a speed wrapper.

    Two separate re-read problems, and the second is much the worse:

    * The engine re-ranks up to 61 times a day, re-reading the same 20 files each time.
    * The time-of-day volume baseline needs the 20 PRIOR sessions, so every session
      re-reads 400 files that the previous session had already read. Over 1,212
      sessions that is roughly half a million parquet reads for 24,000 distinct files.

    Evicting whole sessions, oldest first, is what makes this safe: a run walks
    forward in date order, so the sessions dropped are ones no later day will ask for.
    Bounding it matters -- an unbounded cache would hold 9 million Bar objects.

    Changes no value the engine sees.
    """

    def __init__(self, store, max_sessions: int = CACHE_SESSIONS):
        self._store = store
        self._bars: dict[tuple[str, date], list] = {}
        self._has: dict[tuple[str, date], bool] = {}
        self._order: list[date] = []
        self._max = max_sessions
        self.root = getattr(store, "root", None)

    def _touch(self, trade_date: date) -> None:
        if trade_date in self._order:
            return
        self._order.append(trade_date)
        while len(self._order) > self._max:
            evicted = self._order.pop(0)
            for key in [k for k in self._bars if k[1] == evicted]:
                del self._bars[key]

    def has_bars(self, symbol: str, trade_date: date) -> bool:
        key = (symbol, trade_date)
        if key not in self._has:
            self._has[key] = self._store.has_bars(symbol, trade_date)
        return self._has[key]

    def load_bars(self, symbol: str, trade_date: date) -> list:
        key = (symbol, trade_date)
        if key not in self._bars:
            self._touch(trade_date)
            self._bars[key] = self._store.load_bars(symbol, trade_date)
        return self._bars[key]


@dataclass
class SignalOutcome:
    """One signal, from the moment it fired to the moment Module C closed it."""

    trade_date: date
    symbol: str
    signal_at: datetime
    score: float
    sector: str
    entry_price: float
    stop: float
    t1: float
    t2: float
    risk_reward_t1: float
    reason: str

    quantity: int
    exit_state: str
    exit_rule: str
    exit_time: datetime | None
    exit_price: float | None
    gross_pnl: float
    net_pnl: float
    gross_pct: float
    net_pct: float
    costs: float
    mae_pct: float
    mfe_pct: float
    minutes_held: float
    r_multiple: float

    def as_row(self) -> dict:
        row = asdict(self)
        row["trade_date"] = self.trade_date.isoformat()
        row["signal_at"] = self.signal_at.isoformat()
        row["exit_time"] = None if self.exit_time is None else self.exit_time.isoformat()
        return row


def _to_outcome(candidate, outcome: SessionOutcome) -> SignalOutcome:
    risk_per_share = float(candidate.entry_mid - candidate.stop)
    risk_amount = risk_per_share * outcome.quantity
    return SignalOutcome(
        trade_date=candidate.trade_date,
        symbol=candidate.symbol,
        signal_at=candidate.asof,
        score=candidate.score,
        sector=candidate.sector,
        entry_price=outcome.entry_price,
        stop=float(candidate.stop),
        t1=float(candidate.t1),
        t2=float(candidate.t2),
        risk_reward_t1=candidate.risk_reward_t1,
        reason=candidate.reason(),
        quantity=outcome.quantity,
        exit_state=outcome.exit_state,
        exit_rule=outcome.exit_rule,
        exit_time=outcome.exit_time,
        exit_price=outcome.exit_price,
        gross_pnl=outcome.gross_pnl,
        net_pnl=outcome.net_pnl,
        gross_pct=outcome.gross_pct,
        net_pct=outcome.net_pct,
        costs=outcome.costs,
        mae_pct=outcome.mae_pct,
        mfe_pct=outcome.mfe_pct,
        minutes_held=outcome.minutes_held,
        r_multiple=(outcome.net_pnl / risk_amount) if risk_amount > 0 else 0.0,
    )


def run_signal_session(
    store,
    symbols: Sequence[str],
    trade_date: date,
    all_sessions: Sequence[date],
    *,
    cfg: SignalEngineSettings | None = None,
    exit_cfg: ExitEngineSettings | None = None,
    cost_cfg: CostSettings | None = None,
    notional: Decimal = DEFAULT_NOTIONAL,
    ban_list: FnoBanList | None = None,
    filters: UniverseFilters | None = None,
    cache: "BarCache | None" = None,
) -> tuple[list[SignalOutcome], int]:
    """One session: walk the signal times, take positions, let Module C exit them.

    Returns ``(outcomes, evaluations)``. A session with no signal returns an empty
    list, which is a result and not a gap.

    ``max_candidates`` is treated as a DAILY budget, not a per-evaluation one. The
    engine re-ranks every five minutes; without a daily cap a trending day would open
    a position at 09:30 and another sixty times after it, which is not what "up to 2
    candidates" means to someone placing the orders by hand.
    """
    cfg = cfg or get_signal_engine_settings()
    exit_cfg = exit_cfg or get_exit_engine_settings()
    cost_cfg = cost_cfg or get_cost_settings()

    cache = cache if cache is not None else BarCache(store)
    # One volume baseline per symbol per session, not one per evaluation. It depends
    # only on prior sessions, so it cannot change during the day. Built through the
    # cache so consecutive sessions reuse the 19 of 20 files they share.
    profiles = {
        symbol: F_volume_profile_for(
            cache, symbol, all_sessions, trade_date,
            lookback=cfg.volume_profile_lookback,
        )
        for symbol in symbols
        if cache.has_bars(symbol, trade_date)
    }

    outcomes: list[SignalOutcome] = []
    taken: set[str] = set()
    evaluations = 0

    for at in signal_times(trade_date, cfg):
        if len(taken) >= cfg.max_candidates:
            break
        decision = generate_signals(
            cache, symbols, trade_date, at, all_sessions,
            cfg=cfg, exit_cfg=exit_cfg, ban_list=ban_list, filters=filters,
            profiles=profiles,
        )
        evaluations += 1
        for candidate in decision.candidates:
            if candidate.symbol in taken or len(taken) >= cfg.max_candidates:
                continue
            bars = cache.load_bars(candidate.symbol, trade_date)
            outcome = replay_one_session(
                candidate.symbol, trade_date, bars,
                entry_time=at.time(), notional=notional,
                engine_cfg=exit_cfg, cost_cfg=cost_cfg,
                stop_loss=candidate.stop, target=candidate.t1,
            )
            if outcome is None:
                continue
            taken.add(candidate.symbol)
            outcomes.append(_to_outcome(candidate, outcome))
    return outcomes, evaluations


def run_signal_sessions(
    store,
    symbols: Sequence[str],
    sessions: Sequence[date],
    **kw,
) -> tuple[list[SignalOutcome], dict]:
    """Run every session and return the outcomes plus coverage statistics.

    One cache spans the whole run. Sessions are walked in date order so the trailing
    volume window is already resident by the time the next day asks for it.
    """
    outcomes: list[SignalOutcome] = []
    signal_days = 0
    evaluations = 0
    cache = BarCache(store)
    for trade_date in sorted(sessions):
        day_outcomes, evals = run_signal_session(
            store, symbols, trade_date, sessions, cache=cache, **kw
        )
        evaluations += evals
        if day_outcomes:
            signal_days += 1
        outcomes.extend(day_outcomes)
    stats = {
        "sessions": len(sessions),
        "sessions_with_a_signal": signal_days,
        "no_signal_sessions": len(sessions) - signal_days,
        "signals": len(outcomes),
        "evaluations": evaluations,
    }
    log.info("signal run: %s", stats)
    return outcomes, stats


# --------------------------------------------------------------------------------
# Single-shot: one decision point per session
# --------------------------------------------------------------------------------


@dataclass
class SingleShotResult:
    """What one session's single decision produced, including the case for nothing."""

    trade_date: date
    at: datetime
    top_symbol: str | None
    top_score: float | None
    #: traded | below_bar | gated | no_scores
    verdict: str
    detail: str = ""
    outcome: SignalOutcome | None = None


def run_single_shot_session(
    store,
    symbols: Sequence[str],
    trade_date: date,
    all_sessions: Sequence[date],
    at_time: time,
    *,
    cfg: SignalEngineSettings | None = None,
    exit_cfg: ExitEngineSettings | None = None,
    cost_cfg: CostSettings | None = None,
    notional: Decimal = DEFAULT_NOTIONAL,
    ban_list: FnoBanList | None = None,
    filters: UniverseFilters | None = None,
    cache: "BarCache | None" = None,
) -> SingleShotResult:
    """Evaluate ONCE, take only the day's single best candidate, or nothing.

    Continuous re-evaluation was a multiple-comparisons problem, not a threshold
    problem: 20 symbols x 61 evaluations is roughly 1,220 draws a session, so any bar
    still yielded a signal on 96% of days. This is one draw.

    "The single highest-scoring one" is taken literally. If the top-scoring symbol is
    then rejected by a downstream gate the session is NO_SIGNAL -- it does NOT fall
    through to the second-best, because walking down the ranking is how one draw
    silently becomes several. That case is recorded separately as ``gated`` so it can
    be told apart from "nothing cleared the bar".
    """
    from src.market.calendar import IST

    cfg = cfg or get_signal_engine_settings()
    cache = cache if cache is not None else BarCache(store)
    at = datetime.combine(trade_date, at_time, tzinfo=IST)

    profiles = {
        symbol: F_volume_profile_for(
            cache, symbol, all_sessions, trade_date,
            lookback=cfg.volume_profile_lookback,
        )
        for symbol in symbols
        if cache.has_bars(symbol, trade_date)
    }
    decision = generate_signals(
        cache, symbols, trade_date, at, all_sessions,
        cfg=cfg, exit_cfg=exit_cfg, ban_list=ban_list, filters=filters,
        profiles=profiles,
    )

    if not decision.all_scores:
        return SingleShotResult(trade_date, at, None, None, "no_scores",
                                "no symbol produced a composite score")

    top_symbol, top_score = max(decision.all_scores, key=lambda kv: (kv[1], kv[0]))
    bar = float(cfg.min_composite_score)
    if top_score < bar:
        return SingleShotResult(
            trade_date, at, top_symbol, top_score, "below_bar",
            f"best of the day scored {top_score:+.3f}, below the {bar:+.3f} bar",
        )

    candidate = next((c for c in decision.candidates if c.symbol == top_symbol), None)
    if candidate is None:
        why = next((r.detail for r in decision.rejections if r.symbol == top_symbol),
                   "rejected downstream of the score")
        return SingleShotResult(trade_date, at, top_symbol, top_score, "gated", why)

    outcome = replay_one_session(
        candidate.symbol, trade_date, cache.load_bars(candidate.symbol, trade_date),
        entry_time=at_time, notional=notional,
        engine_cfg=exit_cfg, cost_cfg=cost_cfg,
        stop_loss=candidate.stop, target=candidate.t1,
    )
    if outcome is None:
        return SingleShotResult(trade_date, at, top_symbol, top_score, "gated",
                                "no completed bar at the decision point")
    return SingleShotResult(trade_date, at, top_symbol, top_score, "traded",
                            candidate.reason(), _to_outcome(candidate, outcome))


def run_single_shot(
    store,
    symbols: Sequence[str],
    sessions: Sequence[date],
    at_time: time,
    **kw,
) -> tuple[list[SignalOutcome], dict]:
    """One decision per session across the whole set."""
    cache = BarCache(store)
    results: list[SingleShotResult] = []
    for trade_date in sorted(sessions):
        results.append(run_single_shot_session(
            store, symbols, trade_date, sessions, at_time, cache=cache, **kw
        ))

    outcomes = [r.outcome for r in results if r.outcome is not None]
    verdicts: dict[str, int] = {}
    for r in results:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
    stats = {
        "decision_point": at_time.strftime("%H:%M"),
        "sessions": len(sessions),
        "signals": len(outcomes),
        "no_signal": len(sessions) - len(outcomes),
        "no_signal_pct": round(100.0 * (len(sessions) - len(outcomes)) / max(1, len(sessions)), 1),
        "verdicts": verdicts,
        "traded_dates": [r.trade_date for r in results if r.outcome is not None],
        "draws_per_session": 1,
    }
    log.info("single-shot @%s: %s", at_time.strftime("%H:%M"), stats["verdicts"])
    return outcomes, stats


# --------------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------------


def baseline_highest_volume(
    store,
    symbols: Sequence[str],
    sessions: Sequence[date],
    *,
    at: time = BASELINE_ENTRY_TIME,
    notional: Decimal = DEFAULT_NOTIONAL,
    exit_cfg: ExitEngineSettings | None = None,
    cost_cfg: CostSettings | None = None,
) -> list[SessionOutcome]:
    """Buy the highest-volume stock at 09:45 every day. One position per session.

    The rule to beat. It needs no engine, no features and no score -- if Module B
    cannot beat it after costs then Module B is not earning its complexity, and that
    must be reported rather than buried.

    Data-availability boundary:
        "Highest volume" is cumulative volume from the session open to the last bar
        completed at ``at``. Ranking on FULL-day volume would be look-ahead, and would
        also be a much better rule than anyone could actually follow.
    """
    from src.positions.models import completed_bars
    from src.market.calendar import IST

    exit_cfg = exit_cfg or get_exit_engine_settings()
    cost_cfg = cost_cfg or get_cost_settings()
    out: list[SessionOutcome] = []

    for trade_date in sessions:
        decision_at = datetime.combine(trade_date, at, tzinfo=IST)
        best, best_volume = None, -1
        for symbol in symbols:
            if not store.has_bars(symbol, trade_date):
                continue
            done = completed_bars(store.load_bars(symbol, trade_date), decision_at)
            volume = sum(b.volume for b in done)
            if volume > best_volume:
                best, best_volume = symbol, volume
        if best is None:
            continue
        outcome = replay_one_session(
            best, trade_date, store.load_bars(best, trade_date),
            entry_time=at, notional=notional,
            engine_cfg=exit_cfg, cost_cfg=cost_cfg,
        )
        if outcome is not None:
            out.append(outcome)
    return out


# --------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------


def _mean_ci(values: Sequence[float]) -> tuple[float, float, float, float]:
    """(mean, sd, ci_low, ci_high) at 95%, normal approximation."""
    n = len(values)
    if not n:
        return 0.0, 0.0, 0.0, 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    sd = variance ** 0.5
    stderr = sd / (n ** 0.5) if n > 1 else 0.0
    return mean, sd, mean - 1.96 * stderr, mean + 1.96 * stderr


def summarise(label: str, outcomes: Sequence, *, sessions: int | None = None) -> dict:
    """Headline numbers, with ``n`` attached to every one of them.

    GROSS gets its own confidence interval alongside net. If gross is negative the
    strategy has no edge to defend and costs are beside the point, so that number
    needs to carry its own uncertainty rather than being read off a total.
    """
    n = len(outcomes)
    if not n:
        return {"label": label, "n": 0, "note": "no trades"}
    net = [o.net_pnl for o in outcomes]
    gross = [o.gross_pnl for o in outcomes]
    wins = [v for v in net if v > 0]
    mean_net, sd, _, _ = _mean_ci(net)
    mean_gross, sd_gross, g_low, g_high = _mean_ci(gross)
    stderr = sd / (n ** 0.5) if n > 1 else 0.0
    row = {
        "label": label,
        "n": n,
        "sessions": sessions,
        "total_net_pnl": round(sum(net), 2),
        "total_gross_pnl": round(sum(gross), 2),
        "total_costs": round(sum(o.costs for o in outcomes), 2),
        "mean_gross_pnl": round(mean_gross, 2),
        "gross_ci95_low": round(g_low, 2),
        "gross_ci95_high": round(g_high, 2),
        "hit_rate_gross_pct": round(100.0 * sum(1 for v in gross if v > 0) / n, 2),
        "mean_net_pnl": round(mean_net, 2),
        "median_net_pnl": round(median(net), 2),
        "sd_net_pnl": round(sd, 2),
        "hit_rate_net_pct": round(100.0 * len(wins) / n, 2),
        "mean_mfe_pct": round(sum(o.mfe_pct for o in outcomes) / n, 4),
        "mean_mae_pct": round(sum(o.mae_pct for o in outcomes) / n, 4),
        "mean_minutes_held": round(sum(o.minutes_held for o in outcomes) / n, 1),
        "ci95_low": round(mean_net - 1.96 * stderr, 2),
        "ci95_high": round(mean_net + 1.96 * stderr, 2),
    }
    if hasattr(outcomes[0], "r_multiple"):
        r = [o.r_multiple for o in outcomes]
        row["mean_r_multiple"] = round(sum(r) / n, 3)
        row["median_r_multiple"] = round(median(r), 3)
    return row


def verdict(row: dict) -> str:
    """A reading that refuses to call a null result a win."""
    if not row.get("n"):
        return "No trades. Nothing to conclude."
    low, high = row["ci95_low"], row["ci95_high"]
    if low <= 0 <= high:
        return (
            f"INCONCLUSIVE: mean net Rs {row['mean_net_pnl']:,.2f}/trade, 95% CI "
            f"[{low:,.2f}, {high:,.2f}] spans zero at n={row['n']}. This sample "
            f"cannot distinguish this from breaking even."
        )
    direction = "PROFITABLE" if row["mean_net_pnl"] > 0 else "LOSS-MAKING"
    return (
        f"{direction} after costs: mean net Rs {row['mean_net_pnl']:,.2f}/trade, "
        f"95% CI [{low:,.2f}, {high:,.2f}], n={row['n']}."
    )


def write_signal_csv(outcomes: Sequence[SignalOutcome], path: Path | str) -> Path:
    import pandas as pd  # noqa: PLC0415 - heavy import, analysis only

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([o.as_row() for o in outcomes]) if outcomes else pd.DataFrame()
    frame.to_csv(path, index=False)
    return path
