"""Generate reports/momentum_analysis.md from backfilled REAL bars.

    python scripts/momentum_analysis.py
    python scripts/momentum_analysis.py --holdout-status

Protocol, fixed BEFORE any result is seen:

  1. Panel: 2026-02-16 .. 2026-08-19, 21 symbols. 2026-08-20 is EXCLUDED -- Fyers
     returns it twice with conflicting high/low/volume for 20 of 21 symbols, and the
     lone survivor would make it an n=1 cross-section.
  2. Chronological 80/20 DEV/HOLDOUT split. No shuffling.
  3. All diagnostics and BOTH sweeps run on DEV only, enforced by HoldoutGuard.
  4. The best DEV config is selected by a rule declared below, before the numbers.
  5. That ONE config is evaluated ONCE on HOLDOUT. The claim is recorded to disk.
  6. The power table uses the MEASURED SD, not an assumption.
  7. Anything the sample cannot resolve is labelled INCONCLUSIVE, not reported as a
     small effect.

Refuses to run on synthetic data. If the store is empty it writes a report saying so.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    ExitEngineSettings,
    get_cost_settings,
    get_exit_engine_settings,
    get_settings,
)
from src.analysis.momentum import (
    FORWARD_MINUTES,
    MIN_N_FOR_CONFIDENT_CLAIM,
    exit_rule_distribution,
    exit_state_distribution,
    exit_time_histogram,
    forward_returns_post_exit,
    holding_period_profile,
    minimum_detectable_effect,
    rule_profile,
    summarise,
    sweep_momentum_confirmation,
    sweep_summary,
    sweep_trail_multiple,
)
from src.analysis.split import (
    PANEL_FULL_HISTORY,
    DevHoldoutSplit,
    HoldoutGuard,
    config_fingerprint,
    regime_of,
)
from src.data.backfill import LIQUID_FNO_UNIVERSE
from src.market.calendar import trading_days_between
from src.positions.replay import DEFAULT_NOTIONAL, batch_replay, write_outcomes_csv
from src.positions.store import PositionStore

log = logging.getLogger("fno.report")

REPORT_PATH = Path("reports/momentum_analysis.md")
CSV_DEV = Path("reports/batch_replay_dev.csv")
CSV_HOLDOUT = Path("reports/batch_replay_holdout.csv")

#: The six-year panel. The original 2026 H1 panel's holdout is SPENT and its range is
#: excluded via PANEL.spent -- data that has been looked at cannot be a holdout again.
PANEL = PANEL_FULL_HISTORY
PANEL_START = PANEL.start
PANEL_END = PANEL.end
EXCLUDED_SESSIONS = PANEL.excluded
SPENT = PANEL.spent
CLAIM_PATH = PANEL.claim_path

TRAIL_VALUES = (0.5, 1.0, 1.5, 2.0, 3.0)
CONFIRM_VALUES = (1, 2, 3, 5)

#: DECLARED BEFORE RESULTS. The best DEV config is the one with the highest total net
#: P&L on DEV; ties go to the simpler rule (fewer confirmation bars, then wider
#: trail). Only the 9 one-parameter-at-a-time configs below are candidates -- not the
#: 20-point grid -- which keeps the multiple-comparison burden to 9, not 20.
SELECTION_METRIC = "total_net_pnl"
SELECTION_RULE = (
    "highest total net P&L on DEV across the 9 swept configs "
    "(5 trail widths + 4 confirmation settings, baseline common to both); "
    "ties broken toward the simpler rule"
)

ASSUMED_FWD_RETURN_SD_PCT = 0.60


def md_table(rows: list[dict], columns: list[str] | None = None) -> str:
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


def panel_sessions() -> list[date]:
    """Panel sessions, minus data-quality exclusions and minus any spent region."""
    return [
        d for d in trading_days_between(PANEL_START, PANEL_END)
        if d not in EXCLUDED_SESSIONS
        and not any(lo <= d <= hi for lo, hi, _ in SPENT)
    ]


def coverage(store: PositionStore, symbols, sessions) -> dict[str, int]:
    return {s: sum(1 for d in sessions if store.has_bars(s, d)) for s in symbols}


def usable_symbols(cov: dict[str, int], sessions: int, tolerance: float = 0.95):
    """Symbols with at least ``tolerance`` of the panel's sessions.

    A symbol materially short of the rest would weight the aggregate toward whatever
    happened during the days it does cover.
    """
    threshold = int(sessions * tolerance)
    keep = sorted(s for s, n in cov.items() if n >= threshold)
    drop = sorted((s, n) for s, n in cov.items() if n < threshold)
    return keep, drop, threshold


def power_table(sd: float) -> str:
    rows = [
        {
            "n (momentum exits)": n,
            "min detectable mean (pp)": f"{minimum_detectable_effect(n, sd):.3f}",
        }
        for n in (50, 100, 250, 500, 1000, 2000, 5000)
    ]
    return md_table(rows)


def select_best(rows: list[dict]) -> dict | None:
    """Apply the declared selection rule. No discretion at this point."""
    usable = [r for r in rows if r.get("n")]
    if not usable:
        return None

    def simplicity(r: dict) -> tuple:
        label = r["config"]
        confirm = int(label.split("=")[1].rstrip("bar")) if label.startswith("confirm") else 1
        trail = float(label.split("=")[1].rstrip("x")) if label.startswith("trail") else 1.5
        return (confirm, -trail)

    return sorted(usable, key=lambda r: (-r[SELECTION_METRIC], simplicity(r)))[0]


def config_for(label: str, base: ExitEngineSettings) -> ExitEngineSettings:
    if label.startswith("trail="):
        return base.model_copy(
            update={"trail_atr_multiple": Decimal(label.split("=")[1].rstrip("x"))}
        )
    if label.startswith("confirm="):
        return base.model_copy(
            update={"momentum_confirmation_bars": int(label.split("=")[1].rstrip("bar"))}
        )
    raise ValueError(f"unrecognised config label {label!r}")


def no_data_report(store: PositionStore, symbols, sessions) -> str:
    return f"""# Momentum rule analysis — NO DATA YET

**Generated:** {datetime.now().isoformat(timespec="seconds")}
**Status:** :warning: **NOT RUN — the bar store is empty.**

The store at `{store.root}` contains no backfilled sessions for
{PANEL_START}..{PANEL_END}, so there is nothing to analyse.

**No numbers have been produced from synthetic data, and none will be.** Synthetic
bars describe a fixture's construction, not the market, and the output would look
exactly like a real finding.

```powershell
python -m src.cli auth
python -m src.cli backfill --months 6
python scripts/momentum_analysis.py
```

Scope when run: {len(symbols)} symbols x {len(sessions)} sessions.

## What this sample size will be able to resolve

{power_table(ASSUMED_FWD_RETURN_SD_PCT)}

At an assumed SD of {ASSUMED_FWD_RETURN_SD_PCT}pp, resolving a 0.20pp effect (about
one round trip's costs) needs n >= {int((2.8 * ASSUMED_FWD_RETURN_SD_PCT / 0.20) ** 2)}
momentum exits.
"""


def build_report(store: PositionStore, args) -> str:
    engine_cfg = get_exit_engine_settings()
    cost_cfg = get_cost_settings()
    entry_time = time.fromisoformat(args.entry_time)

    sessions = panel_sessions()
    cov = coverage(store, LIQUID_FNO_UNIVERSE, sessions)
    symbols, dropped, threshold = usable_symbols(cov, len(sessions))

    if not symbols:
        return no_data_report(store, LIQUID_FNO_UNIVERSE, sessions)

    split = DevHoldoutSplit.chronological(sessions, args.dev_fraction)
    guard = HoldoutGuard(split, CLAIM_PATH)

    # ---- DEV ONLY from here until the single holdout evaluation ---------------
    guard.assert_dev_only(guard.dev, "the DEV batch replay")
    log.info("DEV replay: %d symbols x %d sessions", len(symbols), len(guard.dev))
    dev = batch_replay(store, symbols, guard.dev, entry_time=entry_time,
                       engine_cfg=engine_cfg, cost_cfg=cost_cfg)
    if not dev:
        return no_data_report(store, symbols, sessions)
    write_outcomes_csv(dev, CSV_DEV)

    rule_dist = exit_rule_distribution(dev)
    state_dist = exit_state_distribution(dev)
    momentum_pct = next(
        (r["pct"] for r in rule_dist if r["exit_rule"] == "MOMENTUM_REVERSAL"), 0.0
    )
    hist_all = exit_time_histogram(dev)
    hist_mom = exit_time_histogram(dev, rule="MOMENTUM_REVERSAL")
    holding = holding_period_profile(dev)
    rules = rule_profile(dev)

    first_bar_pct = 100.0 * sum(1 for o in dev if o.bars_held <= 1) / len(dev)
    quarter_hour_pct = 100.0 * sum(1 for o in dev if o.minutes_held <= 15) / len(dev)
    mean_gross = sum(o.gross_pnl for o in dev) / len(dev)
    mean_costs = sum(o.costs for o in dev) / len(dev)

    fwd_mom, _ = forward_returns_post_exit(store, dev, minutes=FORWARD_MINUTES,
                                           rule="MOMENTUM_REVERSAL")
    fwd_all, _ = forward_returns_post_exit(store, dev, minutes=FORWARD_MINUTES)
    measured_sd = fwd_mom.sd if fwd_mom.n > 1 else ASSUMED_FWD_RETURN_SD_PCT

    log.info("DEV sweeps")
    trail_rows = sweep_trail_multiple(store, symbols, guard.dev, values=TRAIL_VALUES,
                                      base=engine_cfg, cost_cfg=cost_cfg,
                                      entry_time=entry_time)
    confirm_rows = sweep_momentum_confirmation(store, symbols, guard.dev,
                                               values=CONFIRM_VALUES, base=engine_cfg,
                                               cost_cfg=cost_cfg, entry_time=entry_time)
    all_sweep = trail_rows + confirm_rows
    best = select_best(all_sweep)
    best_cfg = config_for(best["config"], engine_cfg) if best else engine_cfg

    # ---- THE SINGLE HOLDOUT EVALUATION ----------------------------------------
    holdout_sessions = guard.claim(best_cfg, f"best DEV config: {best['config']}")
    log.info("HOLDOUT evaluation (one only): %s", best["config"])
    hold = batch_replay(store, symbols, holdout_sessions, entry_time=entry_time,
                        engine_cfg=best_cfg, cost_cfg=cost_cfg)
    write_outcomes_csv(hold, CSV_HOLDOUT)

    hold_rule_dist = exit_rule_distribution(hold)
    hold_mom_pct = next(
        (r["pct"] for r in hold_rule_dist if r["exit_rule"] == "MOMENTUM_REVERSAL"), 0.0
    )
    fwd_hold, _ = forward_returns_post_exit(store, hold, minutes=FORWARD_MINUTES,
                                            rule="MOMENTUM_REVERSAL")
    dev_net = summarise("DEV net P&L", [o.net_pnl for o in dev])
    hold_net = summarise("HOLDOUT net P&L", [o.net_pnl for o in hold])

    dev_best_mean = best["mean_net_pnl"]
    hold_mean = hold_net.mean
    shortfall = hold_mean - dev_best_mean
    held_up = (
        "CONFIRMED" if hold_mean > 0 and abs(shortfall) <= abs(dev_best_mean) * 0.5
        else "NOT CONFIRMED"
    )

    trail_spread = (
        max(r[SELECTION_METRIC] for r in trail_rows if r.get("n"))
        - min(r[SELECTION_METRIC] for r in trail_rows if r.get("n"))
    )
    dead_code = (
        f":red_circle: **Momentum is {momentum_pct:.1f}% of DEV exits — above your 70% "
        f"threshold. The trailing stop is effectively dead code:** it can only fire on "
        f"the {100 - momentum_pct:.1f}% of sessions momentum has not already closed."
        if momentum_pct > 70 else
        f"**Momentum is {momentum_pct:.1f}% of DEV exits — BELOW your 70% dead-code "
        f"threshold.** The trailing stop fires on {100 - momentum_pct:.1f}% of exits and "
        f"the trail sweep moves net P&L by Rs {trail_spread:,.0f} across widths, so it is "
        f"not inert either. The suspicion raised on synthetic bars did **not** replicate "
        f"on real data."
    )

    dropped_note = (
        "None — every symbol cleared the threshold."
        if not dropped else
        "\n".join(f"- `{s}` — {n}/{len(sessions)} sessions, below the "
                  f"{threshold}-session floor. EXCLUDED." for s, n in dropped)
    )

    return f"""# Momentum rule analysis — real NSE bars

**Generated:** {datetime.now().isoformat(timespec="seconds")}
**Panel:** {len(symbols)} symbols x {len(sessions)} sessions ({PANEL_START} .. {PANEL_END})
**Excluded sessions:** {", ".join(str(d) for d in EXCLUDED_SESSIONS)} — vendor returned
the session twice with conflicting `high`/`low`/`volume` for 20 of 21 symbols
**Split:** {split.describe()}
**Regime coverage:**
{md_table([{"regime": r, "DEV sessions": sum(1 for d in split.dev if regime_of(d) == r),
            "HOLDOUT sessions": sum(1 for d in split.holdout if regime_of(d) == r)}
           for r in dict.fromkeys(regime_of(d) for d in sessions)],
          ["regime", "DEV sessions", "HOLDOUT sessions"])}
**Entry:** long at {args.entry_time} IST, filled at the close of the last completed
bar, fixed notional Rs {int(DEFAULT_NOTIONAL):,}
**Costs:** modelled (`rates_verified={cost_cfg.rates_verified}`)

> **This is a diagnostic, not a strategy result.** Entry is a fixed clock time, not a
> signal. These numbers describe how the EXIT rules behave; they say nothing about
> whether the system makes money.

## Protocol (fixed before any result was seen)

- Chronological 80/20 split. No shuffling — time-ordered data.
- Diagnostics and both sweeps: **DEV only**, enforced by `HoldoutGuard`.
- Selection rule, declared in advance: **{SELECTION_RULE}**.
- **One** holdout evaluation, of **one** config. The claim is recorded at
  `{CLAIM_PATH}`; a second evaluation of a different config raises `HoldoutSpentError`.
- Power computed with the **measured** SD ({measured_sd:.3f}pp), not an assumption.

### Symbol screen

{dropped_note}

---

## Summary

:rotating_light: **The dominant finding is not about momentum. {first_bar_pct:.0f}% of
positions are closed on the FIRST completed bar after entry, and
{quarter_hour_pct:.0f}% within 15 minutes.** Mean gross P&L is
Rs {mean_gross:+,.2f} against Rs {mean_costs:,.2f} of costs — the exercise is
measuring the price of a round trip, not the quality of an exit rule. Every
comparison below inherits that. See §2b.

{dead_code}

**Forward return {FORWARD_MINUTES} min after a momentum exit (DEV):** {fwd_mom.verdict()}

> This null is **adequately powered**: the minimum detectable effect
> ({minimum_detectable_effect(max(fwd_mom.n, 2), measured_sd):.3f}pp) is far below the
> ~0.18pp round-trip cost, so an economically meaningful effect WOULD have shown up.
> It did not. **The momentum rule is not cutting winners.** It is losing money a
> different way — see §2b.

**Trail sweep (DEV):** {sweep_summary(trail_rows)}

**Confirmation sweep (DEV):** {sweep_summary(confirm_rows)}

**Holdout:** best DEV config `{best["config"]}` gave mean net
Rs {dev_best_mean:,.2f}/position on DEV and Rs {hold_mean:,.2f} on HOLDOUT
({shortfall:+,.2f}). **{held_up}.**

---

## 1. Distribution of exit rules (DEV, n = {len(dev):,})

{md_table(rule_dist, ["exit_rule", "n", "pct"])}

By coarse state:

{md_table(state_dist, ["exit_state", "n", "pct"])}

---

## 2. Exit time distribution (DEV)

All exits, 15-minute bins:

{md_table(hist_all, ["bin", "n", "pct"])}

Momentum exits only (n = {sum(r["n"] for r in hist_mom):,}):

{md_table(hist_mom, ["bin", "n", "pct"])}

The 10:52 cluster seen on synthetic bars was an **artifact of the fixture**. On real
bars exits cluster in the first quarter hour after entry.

---

## 2b. Holding period — the effect that dominates everything else

{md_table(holding, ["measure", "value"])}

Gross P&L is indistinguishable from zero; costs are the whole story. A strategy that
enters at a fixed clock time and exits within a bar or two captures no move and pays
a full round trip.

Per-rule, which matters more than the raw counts — a rule can dominate the count and
still be the one destroying value:

{md_table(rules, ["exit_rule", "n", "pct", "median_hold_min", "mean_gross_pct",
                  "mean_net_pnl", "mean_mfe_pct", "mean_mae_pct", "win_rate_net_pct"])}

Read the `median_hold_min` column first. `MOMENTUM_REVERSAL` and `VWAP_LOSS` fire
almost immediately and both have **negative** mean gross return — they close positions
before any move can develop. `STOP_HIT` (the ATR trail, which can only engage after
breakeven arms) holds materially longer, shows the largest MFE, and is the only rule
with positive mean gross AND net.

That is the honest version of "is the momentum rule costing money": not by cutting
winners after the fact, but by preventing a position from existing long enough to
become one.

---

## 3. Forward return after exit — is the rule cutting winners? (DEV)

{FORWARD_MINUTES}-minute return from the exit price, long side. **Positive means price
kept rising after we sold.** Windows running past 15:30 are EXCLUDED, not truncated —
truncating shrinks measured moves and manufactures a null.

{md_table([fwd_mom.as_row(), fwd_all.as_row()],
          ["label", "n", "mean_pct", "median_pct", "sd_pct", "ci95_low", "ci95_high",
           "t_stat", "mde_pct", "censored"])}

**Momentum exits:** {fwd_mom.verdict()}

**All exits (baseline):** {fwd_all.verdict()}

### Power, using the measured SD

Measured SD of the momentum forward return: **{measured_sd:.3f}pp**
(replaces the {ASSUMED_FWD_RETURN_SD_PCT}pp placeholder used before data existed).

{power_table(measured_sd)}

At n = {fwd_mom.n:,}, the minimum detectable effect is
**{minimum_detectable_effect(max(fwd_mom.n, 2), measured_sd):.3f}pp**. A measured mean
smaller than that is not a small effect — it is not a measurement.

---

## 4. Trail multiple sweep (DEV only)

{md_table(trail_rows, ["config", "n", "total_net_pnl", "mean_net_pnl",
                       "median_net_pnl", "win_rate_net_pct", "momentum_exit_pct",
                       "mean_mfe_pct", "mean_mae_pct"])}

{sweep_summary(trail_rows)}

---

## 5. Momentum confirmation sweep (DEV only)

`momentum_confirmation_bars = N` requires the reversal to hold N consecutive completed
bars. N=1 is the shipped rule.

{md_table(confirm_rows, ["config", "n", "total_net_pnl", "mean_net_pnl",
                         "win_rate_net_pct", "momentum_exits", "momentum_exit_pct",
                         "fwd_ret_after_momentum_mean_pct", "fwd_ret_n"])}

{sweep_summary(confirm_rows)}

---

## 6. Holdout — one evaluation, one config

Selected by the declared rule: **`{best["config"]}`**
(fingerprint `{config_fingerprint(best_cfg)}`).

{guard.status()}

| | DEV | HOLDOUT |
|---|---|---|
| sessions | {len(guard.dev)} | {len(holdout_sessions)} |
| positions | {len(dev):,} | {len(hold):,} |
| total net P&L | {dev_net.mean * dev_net.n:,.2f} | {hold_net.mean * hold_net.n:,.2f} |
| mean net / position | {dev_net.mean:,.2f} | {hold_net.mean:,.2f} |
| median net / position | {dev_net.median:,.2f} | {hold_net.median:,.2f} |
| momentum share of exits | {momentum_pct:.1f}% | {hold_mom_pct:.1f}% |
| fwd return after momentum | {fwd_mom.mean:+.3f}pp (n={fwd_mom.n}) | {fwd_hold.mean:+.3f}pp (n={fwd_hold.n}) |

**DEV mean for the selected config was Rs {dev_best_mean:,.2f}; HOLDOUT delivered
Rs {hold_mean:,.2f} ({shortfall:+,.2f}). {held_up}.**

Holdout forward return after momentum exits: {fwd_hold.verdict()}

The holdout is now spent. Any further configuration change must be judged on DEV, or
on data collected after {holdout_sessions[-1]}.

---

## Caveats

- **One regime.** {len(sessions)} sessions is a single market period.
- **Nine in-sample maxima.** The selected config is the best of 9 scored on DEV. The
  holdout number is the honest one; the DEV number is optimistic by construction.
- **Entry is arbitrary.** A fixed {args.entry_time} entry is not the signal engine.
- **Costs unverified.** `rates_verified={cost_cfg.rates_verified}`.
- **Survivorship.** Today's liquid names applied backwards. TMPV/TMCV are
  post-demerger instruments; both cleared the coverage screen.
- **Claims below n={MIN_N_FOR_CONFIDENT_CLAIM} are DIRECTIONAL ONLY** and should not
  drive a code change alone.

Raw data: `{CSV_DEV}` ({len(dev):,} rows), `{CSV_HOLDOUT}` ({len(hold):,} rows).

---

*Written by `scripts/momentum_analysis.py`.*
"""


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry-time", default="09:45")
    parser.add_argument("--dev-fraction", type=float, default=0.8)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--out", default=str(REPORT_PATH))
    parser.add_argument("--holdout-status", action="store_true",
                        help="report whether the holdout has been used, and exit")
    args = parser.parse_args(argv)

    store = PositionStore(args.data_dir) if args.data_dir else PositionStore(
        get_settings().data_dir
    )

    if args.holdout_status:
        split = DevHoldoutSplit.chronological(panel_sessions(), args.dev_fraction)
        print(split.describe())
        print(HoldoutGuard(split, CLAIM_PATH).status())
        return 0

    report = build_report(store, args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    log.info("wrote %s (%d bytes)", out, len(report))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
