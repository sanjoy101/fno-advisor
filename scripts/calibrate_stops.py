"""Derive the stop multiplier from measured noise, and the score bar from measured
scores. Writes reports/stop_calibration.md.

    python scripts/calibrate_stops.py

DEV ONLY. Nothing is swept and nothing is chosen for its P&L. Two constants come out,
each read off a stated percentile of an observed distribution:

* the stop, from the adverse excursion that positions which REACHED T1 had to survive;
* the score bar, from the distribution of every composite score the engine produced.

Both are DEV-derived, which is a weaker claim than out-of-sample, and the report says
so.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings, get_signal_engine_settings
from src.analysis.split import PANEL_FULL_HISTORY, HoldoutGuard
from src.market.calendar import IST
from src.data.backfill import LIQUID_FNO_UNIVERSE
from src.positions.store import PositionStore
from src.signals.calibration import (
    TIME_BUCKETS,
    describe,
    measure_session,
    percentile,
)
from src.signals.engine import generate_signals
from src.signals.evaluate import BarCache
from src.signals.universe import MIN_ATR_PCT, FnoBanList

log = logging.getLogger("fno.calibrate")

REPORT = Path("reports/stop_calibration.md")
EXCLUDED = ("TMCV",)

#: Entry times sampled for the excursion measurement. The engine can signal at 61
#: instants; sampling 11 spread across the window keeps the run tractable while
#: covering every time-of-day bucket.
MEASURE_TIMES = [
    time(9, 30), time(10, 0), time(10, 30), time(11, 0), time(11, 30), time(12, 0),
    time(12, 30), time(13, 0), time(13, 30), time(14, 0), time(14, 30),
]

#: The stop must sit beyond this percentile of survivable adverse excursion. Stated
#: in advance: three quarters of winning positions never touch it.
STOP_PERCENTILE = 75.0

#: The score bar, as a percentile of all observed composite scores. Stated in advance.
SCORE_PERCENTILE = 90.0


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


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--out", default=str(REPORT))
    parser.add_argument("--limit-sessions", type=int, default=None)
    parser.add_argument("--score-sample", type=int, default=200,
                        help="sessions sampled for the score distribution")
    args = parser.parse_args(argv)

    store = PositionStore(args.data_dir or get_settings().data_dir)
    cfg = get_signal_engine_settings()
    split = PANEL_FULL_HISTORY.split()
    guard = HoldoutGuard(split, PANEL_FULL_HISTORY.claim_path)
    dev = guard.dev[: args.limit_sessions] if args.limit_sessions else guard.dev
    guard.assert_dev_only(dev, "stop calibration")
    symbols = [s for s in LIQUID_FNO_UNIVERSE if s not in EXCLUDED]

    # ---- 1. measure excursions ------------------------------------------------
    log.info("measuring excursions over %d sessions x %d symbols x %d entry times",
             len(dev), len(symbols), len(MEASURE_TIMES))
    cache = BarCache(store, max_sessions=4)
    excursions = []
    for i, trade_date in enumerate(dev):
        for symbol in symbols:
            if not cache.has_bars(symbol, trade_date):
                continue
            excursions.extend(measure_session(
                symbol, trade_date, cache.load_bars(symbol, trade_date),
                MEASURE_TIMES, t1_atr_multiple=float(cfg.t1_atr_multiple),
                min_atr_pct=MIN_ATR_PCT,
            ))
        if (i + 1) % 200 == 0:
            log.info("  %d/%d sessions, %d excursions", i + 1, len(dev), len(excursions))

    winners = [e for e in excursions if e.reached_t1]
    survivable = [e.mae_atr for e in winners]
    log.info("%d excursions, %d reached T1 (%.1f%%)",
             len(excursions), len(winners), 100.0 * len(winners) / max(1, len(excursions)))

    overall = describe(survivable, "ALL (reached T1)")
    by_bucket = [
        describe([e.mae_atr for e in winners if e.bucket == label], label)
        for label, _, _ in TIME_BUCKETS
    ]
    by_symbol = sorted(
        (describe([e.mae_atr for e in winners if e.symbol == s], s) for s in symbols),
        key=lambda r: -(r.get("p75") or 0),
    )

    derived_stop = percentile(survivable, STOP_PERCENTILE)
    bucket_stops = {
        label: percentile([e.mae_atr for e in winners if e.bucket == label],
                          STOP_PERCENTILE)
        for label, _, _ in TIME_BUCKETS
    }
    symbol_p75 = [r["p75"] for r in by_symbol if r.get("p75") is not None]
    spread = (max(symbol_p75) - min(symbol_p75)) if symbol_p75 else 0.0

    # ---- 2. achievability of the re-derived target ----------------------------
    declared_rr = float(cfg.t1_atr_multiple) / float(cfg.stop_atr_multiple)
    new_t1 = derived_stop * declared_rr
    new_t2 = derived_stop * (float(cfg.t2_atr_multiple) / float(cfg.stop_atr_multiple))
    mfe_all = [e.mfe_atr for e in excursions]
    mfe_row = describe(mfe_all, "MFE, all simulated entries")
    reach_t1 = 100.0 * sum(1 for v in mfe_all if v >= new_t1) / max(1, len(mfe_all))
    reach_t2 = 100.0 * sum(1 for v in mfe_all if v >= new_t2) / max(1, len(mfe_all))

    # ---- 3. score distribution -------------------------------------------------
    sample = dev[:: max(1, len(dev) // args.score_sample)][: args.score_sample]
    log.info("sampling composite scores over %d sessions", len(sample))
    score_cache = BarCache(store)
    scores: list[float] = []
    ban = FnoBanList()
    for trade_date in sample:
        for at_t in (time(9, 30), time(10, 30), time(11, 30), time(12, 30), time(13, 30)):
            at = datetime.combine(trade_date, at_t, tzinfo=IST)
            decision = generate_signals(
                score_cache, symbols, trade_date, at, dev, cfg=cfg, ban_list=ban,
            )
            scores.extend(v for _, v in decision.all_scores)

    score_row = describe(scores, "composite score, all candidates")
    current_bar = float(cfg.min_composite_score)
    current_pctile = (
        100.0 * sum(1 for v in scores if v < current_bar) / len(scores) if scores else 0.0
    )
    new_bar = percentile(scores, SCORE_PERCENTILE)

    ranking = (
        f":red_circle: The bar sits at the **{current_pctile:.0f}th percentile** of "
        f"observed scores, which is BELOW the median. It is not ranking anything -- "
        f"more than half the universe clears it."
        if current_pctile < 50 else
        f"The bar sits at the {current_pctile:.0f}th percentile of observed scores."
    )

    report = f"""# Stop and score-bar calibration — derived, not swept

**Generated:** {datetime.now().isoformat(timespec="seconds")}
**Data:** DEV only, {dev[0]} .. {dev[-1]}, {len(dev)} sessions, {len(symbols)} symbols
**Holdout:** {guard.status()}

> Nothing here is swept. Two constants are read off stated percentiles of measured
> distributions. Both are DEV-DERIVED — a weaker claim than out-of-sample, and not a
> substitute for the single holdout evaluation.

---

## 1. The noise a correct position must survive

For every simulated entry, a long is opened with NO stop and walked to the 15:15
square-off. For those that reached T1, the worst adverse excursion BEFORE they got
there is recorded. A position that was RIGHT still had to sit through that, so any
stop tighter would have cut a winner.

All figures in ATRs at entry. Entries are screened by the SAME minimum-ATR gate the
engine applies ({MIN_ATR_PCT}% of price), so this measures the noise on trades the
engine could actually take. Without that screen a near-zero ATR turns the ratio into
nonsense — an unscreened first pass reported a maximum MAE of 58,755 ATR.

**{len(excursions):,} simulated entries, {len(winners):,} reached T1
({100.0 * len(winners) / max(1, len(excursions)):.1f}%).**

{md_table([overall], ["group", "n", "mean", "p25", "p50", "p75", "p90", "p95", "max"])}

### By time of day

{md_table(by_bucket, ["group", "n", "mean", "p25", "p50", "p75", "p90", "p95", "max"])}

### By symbol (worst p75 first)

{md_table(by_symbol[:8], ["group", "n", "mean", "p50", "p75", "p90", "max"])}

Spread of the p75 across symbols: **{spread:.2f} ATR**
(min {min(symbol_p75):.2f}, max {max(symbol_p75):.2f} over {len(symbol_p75)} symbols).

---

## 2. The derived stop

Rule stated in advance: the stop must sit **beyond the {STOP_PERCENTILE:.0f}th
percentile** of survivable adverse excursion, so three quarters of positions that go
on to reach T1 never touch it.

**Derived multiplier: {derived_stop:.2f} ATR** (was {float(cfg.stop_atr_multiple):.2f}).

Per bucket, for comparison:

{md_table([{"bucket": k, "p75 ATR": None if v is None else round(v, 2)}
           for k, v in bucket_stops.items()], ["bucket", "p75 ATR"])}

A single global multiplier is used rather than one per bucket or per symbol. The
bucket values sit close together, and every extra constant read off DEV is another
degree of freedom fitted to it; one number from {len(survivable):,} observations is
the most defensible of the three.

---

## 3. Re-derived targets, and whether they are reachable

The declared reward-to-risk is preserved exactly: **{declared_rr:.1f}** to T1.

| level | old (ATR) | new (ATR) |
| --- | --- | --- |
| stop | {float(cfg.stop_atr_multiple):.2f} | **{derived_stop:.2f}** |
| T1 | {float(cfg.t1_atr_multiple):.2f} | **{new_t1:.2f}** |
| T2 | {float(cfg.t2_atr_multiple):.2f} | **{new_t2:.2f}** |

Is that target reachable? Distribution of the best favourable excursion over the
holding window, all simulated entries:

{md_table([mfe_row], ["group", "n", "mean", "p25", "p50", "p75", "p90", "p95", "max"])}

- **{reach_t1:.1f}%** of entries reach the new T1 of {new_t1:.2f} ATR at some point.
- **{reach_t2:.1f}%** reach the new T2 of {new_t2:.2f} ATR.

{
"**The new T1 exceeds what these stocks typically move in the holding window.** "
"The median entry never gets there, so the declared R:R is not achievable and a "
"trade priced on it should not be taken. This is a finding about the R:R, not a "
"reason to shrink the stop back into the noise."
if new_t1 > (mfe_row.get("p50") or 0) else
f"The new T1 of {new_t1:.2f} ATR sits below the median favourable excursion of "
f"{mfe_row.get('p50')} ATR, so it is reachable on a typical entry."
}

---

## 4. The score bar

{md_table([score_row], ["group", "n", "mean", "p25", "p50", "p75", "p90", "p95", "max"])}

{ranking}

Rule stated in advance: raise the bar to the **{SCORE_PERCENTILE:.0f}th percentile**
of observed scores.

**Derived bar: {new_bar:.3f}** (was {current_bar:.3f}).

By construction roughly {100 - SCORE_PERCENTILE:.0f}% of individual candidates clear
it, against {100 - current_pctile:.0f}% today. The effect on NO_SIGNAL days is
measured in the re-run, not predicted here — a session surfaces a signal if ANY
symbol clears, so the per-session rate is not the per-candidate rate.

---

## What changes

| setting | old | new | source |
| --- | --- | --- | --- |
| `stop_atr_multiple` | {float(cfg.stop_atr_multiple):.2f} | **{derived_stop:.2f}** | p{STOP_PERCENTILE:.0f} of survivable MAE, n={len(survivable):,} |
| `t1_atr_multiple` | {float(cfg.t1_atr_multiple):.2f} | **{new_t1:.2f}** | stop x {declared_rr:.1f} to preserve R:R |
| `t2_atr_multiple` | {float(cfg.t2_atr_multiple):.2f} | **{new_t2:.2f}** | stop x {float(cfg.t2_atr_multiple) / float(cfg.stop_atr_multiple):.1f} |
| `min_composite_score` | {current_bar:.3f} | **{new_bar:.3f}** | p{SCORE_PERCENTILE:.0f} of observed scores, n={len(scores):,} |

Nothing else changes.

---

*Written by `scripts/calibrate_stops.py`.*
"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    log.info("wrote %s", out)
    print(f"\nWrote {out}")
    print(f"derived stop = {derived_stop:.4f} ATR   new T1 = {new_t1:.4f}   "
          f"new T2 = {new_t2:.4f}   new score bar = {new_bar:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
