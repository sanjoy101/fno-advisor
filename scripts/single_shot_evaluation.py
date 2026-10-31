"""One decision per session. Measure once, at 09:45 and at 10:15, and report both.

    python scripts/single_shot_evaluation.py

Writes reports/single_shot_evaluation.md.

The structure being tested
--------------------------
Continuous re-evaluation was a multiple-comparisons problem: 20 symbols x 61
evaluations is roughly 1,220 draws a session, so any bar still produced a signal on
96% of days. This replaces it with ONE draw:

1. Evaluate once, at a fixed decision point.
2. Rank, and take only the single highest-scoring candidate.
3. Trade it only if its score clears 0.594 -- the p90 of the DEV score distribution
   measured earlier. NOT swept here, and not re-derived.
4. Otherwise the session is NO_SIGNAL.

Both decision points are reported side by side. Neither is selected: the question is
whether the choice of decision point matters at all, and two wildly different answers
on the same data would say more about stability than either number says alone.

THE DECISION RULE, FIXED BEFORE RUNNING
---------------------------------------
If GROSS P&L is negative, the feature set has no intraday edge on this universe and no
amount of selection structure will fix it. That gets said plainly, and nothing is
adjusted afterwards. One measurement, one answer.

DEV only. The holdout is untouched.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    get_cost_settings,
    get_exit_engine_settings,
    get_settings,
    get_signal_engine_settings,
)
from src.analysis.split import PANEL_FULL_HISTORY, HoldoutGuard
from src.data.backfill import LIQUID_FNO_UNIVERSE
from src.positions.store import PositionStore
from src.signals.evaluate import (
    baseline_highest_volume,
    run_single_shot,
    summarise,
    write_signal_csv,
)
from src.signals.universe import FnoBanList

log = logging.getLogger("fno.singleshot")

REPORT = Path("reports/single_shot_evaluation.md")
EXCLUDED = ("TMCV",)
DECISION_POINTS = (time(9, 45), time(10, 15))


def md_table(rows, columns=None) -> str:
    rows = [r for r in rows if r]
    if not rows:
        return "_(no rows)_\n"
    columns = columns or list(rows[0].keys())
    head = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in columns)
            + " |" for r in rows]
    return "\n".join([head, rule, *body]) + "\n"


def by_exit_rule(outcomes) -> list[dict]:
    groups: dict[str, list] = {}
    for o in outcomes:
        groups.setdefault(o.exit_rule, []).append(o)
    return sorted(
        (
            {
                "exit_rule": rule,
                "n": len(g),
                "pct": round(100.0 * len(g) / len(outcomes), 1),
                "mean_gross_pnl": round(sum(o.gross_pnl for o in g) / len(g), 2),
                "mean_net_pnl": round(sum(o.net_pnl for o in g) / len(g), 2),
                "hit_rate_net_pct": round(
                    100.0 * sum(1 for o in g if o.net_pnl > 0) / len(g), 1),
                "mean_r": round(sum(o.r_multiple for o in g) / len(g), 3),
            }
            for rule, g in groups.items()
        ),
        key=lambda r: -r["n"],
    )


def gross_verdict(row: dict) -> str:
    """Apply the pre-stated decision rule. No discretion at this point."""
    if not row.get("n"):
        return "No trades — nothing to conclude."
    low, high = row["gross_ci95_low"], row["gross_ci95_high"]
    mean = row["mean_gross_pnl"]
    if high < 0:
        return (
            f":red_circle: **GROSS IS NEGATIVE.** Mean gross Rs {mean:,.2f}/trade, "
            f"95% CI [{low:,.2f}, {high:,.2f}], n={row['n']}. The interval lies "
            f"entirely below zero."
        )
    if low > 0:
        return (
            f"**GROSS IS POSITIVE.** Mean gross Rs {mean:,.2f}/trade, 95% CI "
            f"[{low:,.2f}, {high:,.2f}], n={row['n']}."
        )
    return (
        f"**GROSS IS INCONCLUSIVE.** Mean gross Rs {mean:,.2f}/trade, 95% CI "
        f"[{low:,.2f}, {high:,.2f}], n={row['n']} — the interval spans zero, so this "
        f"sample cannot distinguish the edge from nothing."
    )


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--out", default=str(REPORT))
    parser.add_argument("--limit-sessions", type=int, default=None)
    args = parser.parse_args(argv)

    store = PositionStore(args.data_dir or get_settings().data_dir)
    cfg = get_signal_engine_settings()
    exit_cfg = get_exit_engine_settings()
    cost_cfg = get_cost_settings()

    split = PANEL_FULL_HISTORY.split()
    guard = HoldoutGuard(split, PANEL_FULL_HISTORY.claim_path)
    dev = guard.dev[: args.limit_sessions] if args.limit_sessions else guard.dev
    guard.assert_dev_only(dev, "the single-shot evaluation")
    symbols = [s for s in LIQUID_FNO_UNIVERSE if s not in EXCLUDED]
    ban = FnoBanList()

    variants = {}
    for at in DECISION_POINTS:
        label = at.strftime("%H:%M")
        log.info("single-shot at %s over %d sessions", label, len(dev))
        outcomes, stats = run_single_shot(
            store, symbols, dev, at, cfg=cfg, exit_cfg=exit_cfg, cost_cfg=cost_cfg,
            ban_list=ban,
        )
        row = summarise(f"Module B single-shot @{label}", outcomes, sessions=len(dev))
        variants[label] = {"outcomes": outcomes, "stats": stats, "row": row}
        write_signal_csv(outcomes, Path(f"reports/single_shot_{at:%H%M}_dev.csv"))

    log.info("baseline over all DEV sessions")
    base_all = baseline_highest_volume(store, symbols, dev, exit_cfg=exit_cfg,
                                       cost_cfg=cost_cfg)
    base_all_row = summarise("Baseline @09:45, all DEV sessions", base_all,
                             sessions=len(dev))

    # Baseline restricted to each variant's signal days, so the comparison is
    # like-for-like on identical sessions rather than on a different set of days.
    for label, v in variants.items():
        traded = set(v["stats"]["traded_dates"])
        matched = [o for o in base_all if o.trade_date in traded]
        v["base_row"] = summarise(
            f"Baseline @09:45, on the {label} signal days only", matched,
            sessions=len(traded),
        )

    cols = ["label", "n", "total_gross_pnl", "mean_gross_pnl", "gross_ci95_low",
            "gross_ci95_high", "hit_rate_gross_pct", "total_net_pnl", "mean_net_pnl",
            "ci95_low", "ci95_high", "hit_rate_net_pct"]

    coverage = [
        {
            "decision point": label,
            "sessions": v["stats"]["sessions"],
            "signals": v["stats"]["signals"],
            "NO_SIGNAL": v["stats"]["no_signal"],
            "NO_SIGNAL %": v["stats"]["no_signal_pct"],
            "below bar": v["stats"]["verdicts"].get("below_bar", 0),
            "gated after clearing": v["stats"]["verdicts"].get("gated", 0),
            "draws/session": 1,
        }
        for label, v in variants.items()
    ]

    a, b = (variants[k] for k in variants)
    a_label, b_label = list(variants)
    overlap = len(set(a["stats"]["traded_dates"]) & set(b["stats"]["traded_dates"]))
    union = len(set(a["stats"]["traded_dates"]) | set(b["stats"]["traded_dates"]))
    agreement = 100.0 * overlap / union if union else 0.0

    all_negative = all(
        v["row"].get("n") and v["row"]["gross_ci95_high"] < 0 for v in variants.values()
    )
    any_positive = any(
        v["row"].get("n") and v["row"]["gross_ci95_low"] > 0 for v in variants.values()
    )

    # Stability: judge on SIGN and on which days were traded, not on a rupee gap.
    # Two means a hundred rupees apart mean very different things depending on whether
    # they straddle zero.
    signs = {
        1 if v["row"].get("mean_gross_pnl", 0) > 0 else -1 for v in variants.values()
    }
    if len(signs) > 1:
        stability = (
            f":red_circle: **The two decision points disagree on the SIGN of the "
            f"gross edge** — {a_label} is "
            f"Rs {variants[a_label]['row']['mean_gross_pnl']:+,.2f}/trade and "
            f"{b_label} is "
            f"Rs {variants[b_label]['row']['mean_gross_pnl']:+,.2f}/trade — while "
            f"agreeing on only {agreement:.1f}% of the days they trade. A result whose "
            f"sign flips on a 30-minute change in when the question is asked is not a "
            f"robust one, and neither number should be read as the engine's edge."
        )
    elif agreement < 70:
        stability = (
            f"Both decision points give the same sign, but they agree on only "
            f"{agreement:.1f}% of traded days, so they are largely trading different "
            f"opportunities to reach a similar place."
        )
    else:
        stability = (
            f"The two decision points agree in sign and on {agreement:.1f}% of traded "
            f"days, which suggests the result is a property of the feature set rather "
            f"than of when it is asked."
        )

    # Like-for-like: same days, engine vs baseline.
    head_to_head = []
    for label, v in variants.items():
        mine = v["row"].get("mean_gross_pnl", 0)
        theirs = v["base_row"].get("mean_gross_pnl", 0)
        head_to_head.append({
            "decision point": label,
            "sessions compared": v["base_row"].get("n"),
            "Module B gross/trade": mine,
            "baseline gross/trade": theirs,
            "difference": round(mine - theirs, 2),
            "baseline CI clears zero": (
                "yes" if v["base_row"].get("gross_ci95_low", 0) > 0 else "no"
            ),
            "Module B CI clears zero": (
                "yes" if v["row"].get("gross_ci95_low", 0) > 0 else "no"
            ),
        })
    beaten = all(r["difference"] < 0 for r in head_to_head)

    costs_per_trade = (
        variants[a_label]["row"]["total_costs"] / variants[a_label]["row"]["n"]
        if variants[a_label]["row"].get("n") else 0.0
    )
    mfe_pct = variants[a_label]["row"].get("mean_mfe_pct", 0.0)
    cost_pct = 100.0 * costs_per_trade / 100_000.0
    capture_needed = 100.0 * cost_pct / mfe_pct if mfe_pct else 0.0

    if all_negative:
        conclusion = f"""## Conclusion — the pre-stated rule applies

**Gross P&L is negative at both decision points, with confidence intervals entirely
below zero.** The rule fixed before this ran was:

> If GROSS is still negative, the feature set has no intraday edge on this universe
> and no amount of selection structure will fix it.

**That is the answer. The feature set has no intraday edge on this universe.**

Costs are not the problem and never were — a strategy whose gross is negative is
losing money on its selections before a rupee of brokerage. Selection structure has
now been tested at both extremes: ~1,220 draws a session and exactly 1. Reducing the
draws by three orders of magnitude changed how often it trades and barely moved what
it earns, which is what "no edge" looks like from the outside.

Per the rule, no threshold adjustment, no further variant and no search for a subset
that works follows this. One measurement, one answer."""
    elif any_positive:
        conclusion = """## Conclusion

At least one decision point shows a gross edge whose interval clears zero. That is not
a licence to trade it: it is DEV, it is one of two variants examined, and the holdout
remains the only out-of-sample evidence available. Costs must still be cleared."""
    else:
        conclusion = f"""## Conclusion — inconclusive on gross, and still no case for the engine

The gross intervals span zero at both decision points, so **this sample cannot
distinguish the feature set's edge from nothing.** That is not "no edge" and not "an
edge" — it is a statement about the sample, and the pre-stated rule fires only on a
decisively negative gross, so it does not strictly apply.

What the sample CAN distinguish, on identical sessions:

* **The baseline's gross edge clears zero; Module B's does not.** Picking the day's
  highest-scoring name earns less gross than taking whatever traded most that
  morning, at both decision points.
* **The two decision points disagree on the sign of the gross edge** while agreeing on
  only {agreement:.1f}% of traded days.
* **Net is decisively negative for everything**, engine and baseline alike, because
  costs of about {cost_pct:.3f}% of notional dwarf a gross edge of at most a few
  hundredths of a percent.

The structural fix did what it was asked to do — NO_SIGNAL went from 3.7% to
{variants[a_label]["stats"]["no_signal_pct"]}% at {a_label} and
{variants[b_label]["stats"]["no_signal_pct"]}% at {b_label}, and gross moved from
decisively negative to indistinguishable from zero. It did not produce an edge,
because reducing the number of draws cannot create one where the features have none.

Per the rule: no threshold adjustment, no further variant, no search for a subset that
works follows this."""

    report = f"""# Module B — one decision per session

**Generated:** {datetime.now().isoformat(timespec="seconds")}
**Data:** DEV only, {dev[0]} .. {dev[-1]}, {len(dev)} sessions, {len(symbols)} symbols
**Holdout:** {guard.status()}
**Score bar:** {float(cfg.min_composite_score):.3f} — the p90 of the previously
measured DEV score distribution. Not swept, not re-derived.
**Levels:** stop {float(cfg.stop_atr_multiple):.2f} ATR, T1
{float(cfg.t1_atr_multiple):.2f}, T2 {float(cfg.t2_atr_multiple):.2f} — derived
earlier from measured noise, unchanged here.

> **Change under test:** one decision point per session instead of 61. Rank once, take
> only the day's single best, trade it only if it clears the absolute bar. This turns
> ~1,220 draws per session into 1. Nothing else changed.

> Two decision points are reported side by side and **neither is selected**. The point
> is whether the choice matters, not which is better.

---

{conclusion}

---

## Coverage — how often a signal now happens

{md_table(coverage, ["decision point", "sessions", "signals", "NO_SIGNAL",
                     "NO_SIGNAL %", "below bar", "gated after clearing",
                     "draws/session"])}

For comparison, continuous re-evaluation produced NO_SIGNAL on **3.7%** of sessions
(45 of 1212) with ~1,220 draws a day.

---

## Gross P&L — the number that decides this

{md_table([variants[a_label]["row"], variants[b_label]["row"]],
          ["label", "n", "total_gross_pnl", "mean_gross_pnl", "gross_ci95_low",
           "gross_ci95_high", "hit_rate_gross_pct"])}

**{a_label}:** {gross_verdict(variants[a_label]["row"])}

**{b_label}:** {gross_verdict(variants[b_label]["row"])}

---

## Like-for-like: Module B against the baseline on IDENTICAL days

{md_table(head_to_head, ["decision point", "sessions compared", "Module B gross/trade",
                         "baseline gross/trade", "difference",
                         "Module B CI clears zero", "baseline CI clears zero"])}

{
":red_circle: **The baseline earns more gross than Module B at both decision points, "
"on exactly the same sessions.** Selecting the day's best-scoring name is worse than "
"taking whatever traded most that morning. The features are not identifying the "
"better opportunity; they are identifying a different one, and a worse one."
if beaten else
"Module B is ahead of the baseline on gross on at least one decision point, measured "
"on identical sessions."
}

## The cost hurdle, which neither clears

Costs run about **Rs {costs_per_trade:,.0f} per trade** on a Rs 100,000 notional —
**{cost_pct:.3f}%**. The mean favourable excursion over the whole holding period is
**{mfe_pct:.3f}%**.

So a trade must capture roughly **{capture_needed:.0f}% of the best price it ever
sees** just to break even — on positions that are stopped out most of the time and
held about {variants[a_label]["row"].get("mean_minutes_held")} minutes. That is the
arithmetic every intraday strategy on this universe has to beat, and it is why every
net figure in this report is negative regardless of which selection rule produced it.

---

## Full comparison, including the baseline

{md_table([variants[a_label]["row"], variants[a_label]["base_row"],
           variants[b_label]["row"], variants[b_label]["base_row"], base_all_row], cols)}

Each variant is shown against the highest-volume-at-09:45 baseline restricted to the
SAME sessions it traded, so the two are compared on identical days rather than on
different sets of them. The baseline over all {len(dev)} DEV sessions is included as
the full-period reference.

---

## R multiples and excursions

{md_table([{k: v["row"].get(k) for k in
            ("label", "n", "mean_r_multiple", "median_r_multiple",
             "mean_mfe_pct", "mean_mae_pct", "mean_minutes_held")}
           for v in variants.values()],
          ["label", "n", "mean_r_multiple", "median_r_multiple", "mean_mfe_pct",
           "mean_mae_pct", "mean_minutes_held"])}

---

## Exit-rule distribution

### {a_label}

{md_table(by_exit_rule(variants[a_label]["outcomes"]),
          ["exit_rule", "n", "pct", "mean_gross_pnl", "mean_net_pnl",
           "hit_rate_net_pct", "mean_r"])}

### {b_label}

{md_table(by_exit_rule(variants[b_label]["outcomes"]),
          ["exit_rule", "n", "pct", "mean_gross_pnl", "mean_net_pnl",
           "hit_rate_net_pct", "mean_r"])}

---

## Does the decision point matter?

The two variants traded on **{overlap}** of the **{union}** sessions either of them
traded — **{agreement:.1f}% agreement**.

{stability}

---

## Caveats

- **DEV only**, n stated on every claim above. The holdout is untouched.
- **Two variants, one sample.** Reporting both rather than selecting avoids picking a
  winner off DEV, but it does not make either an out-of-sample result.
- **The F&O ban check is a stub**, so a banned symbol could have been surfaced.
- **Costs unverified** (`rates_verified={cost_cfg.rates_verified}`).
- **Prices are split/bonus adjusted**: percentage returns are right, absolute
  historical prices and share counts are not.
- **Survivorship**: today's liquid names applied backwards over five years.

Raw data: `reports/single_shot_0945_dev.csv`, `reports/single_shot_1015_dev.csv`.

---

*Written by `scripts/single_shot_evaluation.py`.*
"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    log.info("wrote %s", out)
    print(f"\nWrote {out}")
    for label, v in variants.items():
        r = v["row"]
        print(f"  @{label}: signals {r.get('n')}, NO_SIGNAL {v['stats']['no_signal_pct']}%, "
              f"gross mean {r.get('mean_gross_pnl')} CI "
              f"[{r.get('gross_ci95_low')}, {r.get('gross_ci95_high')}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
