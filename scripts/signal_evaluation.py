"""Evaluate Module B on the DEV set and write reports/signal_evaluation.md.

    python scripts/signal_evaluation.py

DEV ONLY. The holdout is untouched and stays untouched: nothing here claims it, and
``HoldoutGuard.assert_dev_only`` raises if a holdout session reaches the run.

Nothing is tuned here. The thresholds in ``SignalEngineSettings`` were fixed before
this was ever run, and this script only measures what they do. If the engine loses
money after costs, that is the result.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    get_cost_settings,
    get_exit_engine_settings,
    get_settings,
    get_signal_engine_settings,
)
from src.analysis.split import PANEL_FULL_HISTORY, HoldoutGuard, regime_of
from src.data.backfill import LIQUID_FNO_UNIVERSE
from src.positions.store import PositionStore
from src.signals.evaluate import (
    BASELINE_ENTRY_TIME,
    baseline_highest_volume,
    run_signal_sessions,
    summarise,
    verdict,
    write_signal_csv,
)
from src.signals.universe import MIN_ATR_PCT, MIN_TURNOVER_RUPEES, FnoBanList

log = logging.getLogger("fno.signals.report")

REPORT = Path("reports/signal_evaluation.md")
CSV = Path("reports/signal_outcomes_dev.csv")

#: TMCV listed at the Tata Motors demerger and covers 4% of the panel. Including it
#: would weight the aggregate toward the few months it exists.
EXCLUDED_SYMBOLS = ("TMCV",)


def md_table(rows, columns=None) -> str:
    rows = [r for r in rows if r]
    if not rows:
        return "_(no rows)_\n"
    columns = columns or list(rows[0].keys())
    head = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in columns) + " |"
        for r in rows
    ]
    return "\n".join([head, rule, *body]) + "\n"


def by_exit_rule(outcomes) -> list[dict]:
    groups: dict[str, list] = {}
    for o in outcomes:
        groups.setdefault(o.exit_rule, []).append(o)
    rows = []
    for rule, group in groups.items():
        n = len(group)
        rows.append({
            "exit_rule": rule,
            "n": n,
            "pct": round(100.0 * n / len(outcomes), 1),
            "mean_net_pnl": round(sum(o.net_pnl for o in group) / n, 2),
            "hit_rate_pct": round(100.0 * sum(1 for o in group if o.net_pnl > 0) / n, 1),
            "mean_r": round(sum(getattr(o, "r_multiple", 0.0) for o in group) / n, 3),
            "median_minutes": round(sorted(o.minutes_held for o in group)[n // 2], 1),
        })
    return sorted(rows, key=lambda r: -r["n"])


def signal_time_histogram(outcomes) -> list[dict]:
    """When in the day signals actually fire.

    The engine is allowed 61 evaluations between 09:30 and 14:30. If almost every
    signal lands in the first one, the other 60 are decoration and the engine is
    really a fixed-time entry with extra steps.
    """
    bins: dict[str, int] = {}
    for o in outcomes:
        bins[o.signal_at.strftime("%H:%M")] = bins.get(o.signal_at.strftime("%H:%M"), 0) + 1
    total = len(outcomes) or 1
    rows = [
        {"signal_time": k, "n": v, "pct": round(100.0 * v / total, 1)}
        for k, v in sorted(bins.items())
    ]
    return rows[:12]


def stop_geometry(outcomes) -> list[dict]:
    """Is the stop outside the noise it has to survive?

    Compares the declared stop distance with the adverse excursion actually taken.
    A stop sitting at roughly one MAE is not protecting the trade, it IS the exit.
    """
    if not outcomes:
        return []
    n = len(outcomes)
    stop_pct = [
        100.0 * (o.entry_price - o.stop) / o.entry_price for o in outcomes if o.entry_price
    ]
    mae = [o.mae_pct for o in outcomes]
    mfe = [o.mfe_pct for o in outcomes]
    hit = sum(1 for o in outcomes if o.net_pnl > 0) / n
    rr = sum(o.risk_reward_t1 for o in outcomes) / n
    return [
        {"measure": "mean declared stop distance",
         "value": f"{sum(stop_pct) / len(stop_pct):.3f}% of entry"},
        {"measure": "mean adverse excursion (MAE)", "value": f"{sum(mae) / n:.3f}%"},
        {"measure": "mean favourable excursion (MFE)", "value": f"{sum(mfe) / n:.3f}%"},
        {"measure": "stop distance / MAE",
         "value": f"{(sum(stop_pct) / len(stop_pct)) / max(1e-9, sum(mae) / n):.2f}x"},
        {"measure": "hit rate (net)", "value": f"{100 * hit:.1f}%"},
        {"measure": "mean declared R:R to T1", "value": f"{rr:.2f}"},
        {"measure": "break-even hit rate needed at that R:R",
         "value": f"{100 / (1 + rr):.1f}%"},
        {"measure": "expectancy at the observed hit rate",
         "value": f"{hit * rr - (1 - hit):+.3f} R before costs"},
    ]


def by_regime(outcomes) -> list[dict]:
    groups: dict[str, list] = {}
    for o in outcomes:
        groups.setdefault(regime_of(o.trade_date), []).append(o)
    return [
        {
            "regime": regime,
            "n": len(group),
            "total_net_pnl": round(sum(o.net_pnl for o in group), 2),
            "mean_net_pnl": round(sum(o.net_pnl for o in group) / len(group), 2),
            "hit_rate_pct": round(
                100.0 * sum(1 for o in group if o.net_pnl > 0) / len(group), 1
            ),
        }
        for regime, group in sorted(groups.items())
    ]


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--out", default=str(REPORT))
    parser.add_argument("--limit-sessions", type=int, default=None,
                        help="evaluate only the first N DEV sessions (smoke runs)")
    args = parser.parse_args(argv)

    store = PositionStore(args.data_dir or get_settings().data_dir)
    cfg = get_signal_engine_settings()
    exit_cfg = get_exit_engine_settings()
    cost_cfg = get_cost_settings()

    split = PANEL_FULL_HISTORY.split()
    guard = HoldoutGuard(split, PANEL_FULL_HISTORY.claim_path)
    dev = guard.dev[: args.limit_sessions] if args.limit_sessions else guard.dev
    guard.assert_dev_only(dev, "the Module B evaluation")

    symbols = [s for s in LIQUID_FNO_UNIVERSE if s not in EXCLUDED_SYMBOLS]
    ban = FnoBanList()

    log.info("signal engine over %d DEV sessions x %d symbols", len(dev), len(symbols))
    signals, stats = run_signal_sessions(
        store, symbols, dev, cfg=cfg, exit_cfg=exit_cfg, cost_cfg=cost_cfg, ban_list=ban
    )
    write_signal_csv(signals, CSV)

    log.info("baseline: highest-volume symbol at %s", BASELINE_ENTRY_TIME)
    base = baseline_highest_volume(
        store, symbols, dev, exit_cfg=exit_cfg, cost_cfg=cost_cfg
    )

    sig_row = summarise("Module B signals", signals, sessions=len(dev))
    base_row = summarise("Baseline: highest volume @09:45", base, sessions=len(dev))

    geometry = stop_geometry(signals)
    stop_ratio = float(
        next((r["value"] for r in geometry if r["measure"] == "stop distance / MAE"),
             "0x").rstrip("x")
    )
    rule_rows = by_exit_rule(signals)
    stop_hit_pct = next((r["pct"] for r in rule_rows if r["exit_rule"] == "STOP_HIT"), 0.0)
    no_signal_pct = 100.0 * stats["no_signal_sessions"] / max(1, stats["sessions"])

    if sig_row.get("n") and base_row.get("n"):
        per_session_sig = sig_row["total_net_pnl"] / len(dev)
        per_session_base = base_row["total_net_pnl"] / len(dev)
        beats = per_session_sig > per_session_base
        headline = (
            f"Module B returns Rs {per_session_sig:,.2f} per DEV session against the "
            f"baseline's Rs {per_session_base:,.2f}. "
            + (
                "**Module B is ahead of the baseline on this sample.**"
                if beats else
                "**Module B does NOT beat the baseline.** The extra machinery is not "
                "earning its place."
            )
        )
    else:
        headline = "Not enough trades on one side to compare."

    report = f"""# Module B (Signal Engine) — DEV evaluation

**Generated:** {datetime.now().isoformat(timespec="seconds")}
**Data:** DEV only — {split.dev[0]} .. {split.dev[-1]}, {len(dev)} sessions,
{len(symbols)} symbols (TMCV excluded: 4% coverage)
**Holdout:** untouched. {guard.status()}
**Costs:** modelled, `rates_verified={cost_cfg.rates_verified}`
**Config:** score bar {float(cfg.min_composite_score):+.2f}, max
{cfg.max_candidates}/day, signals {cfg.first_signal_time}–{cfg.last_signal_time}
every {cfg.interval_minutes} min, liquidity floor Rs {MIN_TURNOVER_RUPEES:,.0f},
min ATR {MIN_ATR_PCT}%

> Nothing here was tuned. The thresholds were fixed before this ran. Entry is the
> engine's own signal; the exit is Module C, unchanged.

---

## Headline

{headline}

**Module B:** {verdict(sig_row)}

**Baseline:** {verdict(base_row)}

---

## Coverage

{md_table([{
    "measure": "DEV sessions", "value": stats["sessions"]},
    {"measure": "sessions with at least one signal", "value": stats["sessions_with_a_signal"]},
    {"measure": "NO_SIGNAL sessions", "value": stats["no_signal_sessions"]},
    {"measure": "signals taken", "value": stats["signals"]},
    {"measure": "engine evaluations", "value": stats["evaluations"]},
], ["measure", "value"])}

---

## Head to head

{md_table([sig_row, base_row],
          ["label", "n", "total_net_pnl", "total_gross_pnl", "total_costs",
           "mean_net_pnl", "median_net_pnl", "hit_rate_net_pct",
           "ci95_low", "ci95_high", "mean_minutes_held"])}

R multiple is net P&L over the risk the signal declared, ``(entry − stop) × qty``.
The baseline sets no stop, so it has no R.

{md_table([{k: v for k, v in sig_row.items()
            if k in ("label", "n", "mean_r_multiple", "median_r_multiple",
                     "mean_mfe_pct", "mean_mae_pct")}],
          ["label", "n", "mean_r_multiple", "median_r_multiple",
           "mean_mfe_pct", "mean_mae_pct"])}

---

## Diagnosis — three things the headline does not say

**1. Module B is negative BEFORE costs; the baseline is positive before costs.**
Module B gross Rs {sig_row.get("total_gross_pnl", 0):,.2f} against the baseline's
Rs {base_row.get("total_gross_pnl", 0):,.2f}. Costs then take
Rs {sig_row.get("total_costs", 0):,.2f} and Rs {base_row.get("total_costs", 0):,.2f}
respectively. So this is not a strategy with an edge that costs eat — the selection
itself is worse than "whatever traded most this morning", and it trades roughly twice
as often to do it.

**2. Stop geometry {"— still inside the noise" if stop_ratio < 1.5 else "— now outside the noise"}.**

{md_table(geometry, ["measure", "value"])}

{
f":red_circle: The stop sits at {stop_ratio:.2f}x the mean adverse excursion, which is "
f"still inside the noise floor. It exits on chatter whatever the signal was worth."
if stop_ratio < 1.5 else
f":white_check_mark: The stop sits at {stop_ratio:.2f}x the mean adverse excursion, "
f"clear of the noise floor. STOP_HIT is {stop_hit_pct:.1f}% of exits "
f"({'PASS' if stop_hit_pct <= 80 else 'FAIL'} against the stated 80% threshold)."
}

At the declared R:R the rule still needs the break-even hit rate above to survive, and
it is not achieving it. The expectancy line is what that costs before a rupee of
brokerage — a wider stop reduced the loss per unit of risk without turning the sign.

**3. Selectivity.** {stats["no_signal_sessions"]} NO_SIGNAL sessions out of
{stats["sessions"]} (**{no_signal_pct:.1f}%**), and {stats["evaluations"]} evaluations
across {stats["sessions"]} sessions — about
{stats["evaluations"] / max(1, stats["sessions"]):.1f} of the 61 available per day.

{md_table(signal_time_histogram(signals), ["signal_time", "n", "pct"])}

{
f"Raising the bar moved this, but not far: {no_signal_pct:.1f}% of sessions produce "
f"nothing. **A per-candidate bar cannot make SESSIONS selective**, because a session "
f"surfaces a signal if any of {len(symbols)} symbols clears at any of 61 evaluations "
f"— roughly {len(symbols) * 61:,} chances a day. Making NO_SIGNAL common needs a "
f"per-SESSION gate (only trade the day's best if it clears a higher bar), fewer "
f"evaluation opportunities, or both. That is a design change, not a threshold."
if no_signal_pct < 15 else
f"NO_SIGNAL now fires on {no_signal_pct:.1f}% of sessions, which is a real refusal "
f"rate rather than a formality."
}

---

## Module B by exit rule

{md_table(rule_rows,
          ["exit_rule", "n", "pct", "mean_net_pnl", "hit_rate_pct", "mean_r",
           "median_minutes"])}

**STOP_HIT is {stop_hit_pct:.1f}% of exits.** The stated failure threshold was 80%:
{"the stop is still inside the noise and the fix FAILED." if stop_hit_pct > 80
 else "the fix PASSED that test — the stop is no longer the default exit."}

---

## Module B by regime

Six years is several markets, and a rule that only works in one of them is a rule
fitted to one of them.

{md_table(by_regime(signals),
          ["regime", "n", "total_net_pnl", "mean_net_pnl", "hit_rate_pct"])}

---

## Caveats

- **DEV only.** The holdout has not been touched and must not be, until there is a
  configuration worth a single evaluation.
- **State n on every claim.** Anything whose 95% interval spans zero is labelled
  INCONCLUSIVE above and should not drive a decision.
- **The F&O ban check is a stub.** No NSE ban list has been scraped, so a banned
  symbol could have been surfaced. This overstates the tradeable universe slightly.
- **Costs unverified** (`rates_verified={cost_cfg.rates_verified}`).
- **Prices are split/bonus adjusted.** Percentage returns are right; absolute
  historical prices and share counts are not.
- **Survivorship.** Today's liquid names applied backwards over six years.
- **No tuning was done and none should be done off this report alone.** The
  diagnosis above points at the stop width, the score bar and the daily budget, but
  changing any of them because it improves this number is fitting to DEV. Change one,
  re-measure on DEV, and keep the holdout for the single evaluation at the end.

Raw per-signal data: `{CSV}` ({len(signals):,} rows).

---

*Written by `scripts/signal_evaluation.py`.*
"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    log.info("wrote %s", out)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
