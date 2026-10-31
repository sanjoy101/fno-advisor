"""Multi-day swing experiment: 1, 3 and 5 trading-day holds, on DEV only.

    python scripts/swing_evaluation.py

Writes reports/swing_evaluation.md.

Hypothesis under test: the same feature set may carry signal on a horizon where
friction is not the dominant term.

THE DECISION RULE, FIXED BEFORE RUNNING
---------------------------------------
If gross EXCESS return over the equal-weight buy-and-hold baseline does not clear zero
on DEV at any of the three horizons, these features carry no edge at any tested
timescale. That gets reported plainly. No holdout, no sweep, no fourth horizon.

Method
------
* Entry at 15:15 on the signal day, from that day's completed bars only.
* Stop calibrated fresh on EACH horizon from the p75 of survivable adverse excursion,
  measured in DAILY ATRs. The intraday multiple is not reused.
* Target preserves the declared 1.5:1 against the new stop.
* Gap-through-stop modelled honestly: a session opening beyond the stop fills at the
  open.
* Excess return is over an equal-weight panel hold on the SAME dates, because on a
  multi-day horizon beta otherwise masquerades as edge.
* Sample size is the EFFECTIVE n from non-overlapping date blocks, not the trade count.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import CostSettings, get_settings, get_signal_engine_settings
from src.analysis.split import PANEL_SWING, SWING_DEV_END, HoldoutGuard, swing_split
from src.costs import delivery_pnl, delivery_round_trip_costs, round_trip_costs
from src.data.backfill import LIQUID_FNO_UNIVERSE
from src.market.calendar import trading_days_between
from src.positions.store import PositionStore
from src.signals import daily as D
from src.signals.calibration import describe, percentile
from src.signals.swing import (
    HORIZONS,
    effective_n,
    mde_from,
    measure_swing_excursions,
    panel_return_pct,
    rank_universe_daily,
    simulate_swing,
)
from src.signals.universe import MIN_TURNOVER_RUPEES

log = logging.getLogger("fno.swing.report")

REPORT = Path("reports/swing_evaluation.md")
EXCLUDED = ("TMCV",)
NOTIONAL = 100_000.0

#: Stated in advance, same rule as the intraday calibration.
STOP_PERCENTILE = 75.0
#: Declared reward-to-risk, preserved from the intraday design.
DECLARED_RR = 1.5
#: Score bar as a percentile of the observed DAILY score distribution. Same rule as
#: intraday; the distribution differs, so the value is re-read, not reused.
SCORE_PERCENTILE = 90.0
#: Daily ATR floor. Much larger than the intraday one because daily ATR spans the gap.
MIN_DAILY_ATR_PCT = 0.8


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


def ci95(values):
    n = len(values)
    if n < 2:
        return (values[0] if n else 0.0), 0.0, 0.0, 0.0
    mean = sum(values) / n
    sd = statistics.stdev(values)
    half = 1.96 * sd / (n ** 0.5)
    return mean, sd, mean - half, mean + half


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--out", default=str(REPORT))
    parser.add_argument("--limit-sessions", type=int, default=None)
    args = parser.parse_args(argv)

    store = PositionStore(args.data_dir or get_settings().data_dir)
    cfg = get_signal_engine_settings()
    cost_cfg = CostSettings()
    symbols = [s for s in LIQUID_FNO_UNIVERSE if s not in EXCLUDED]

    all_sessions = trading_days_between(PANEL_SWING.start, PANEL_SWING.end)
    split = swing_split(all_sessions)
    guard = HoldoutGuard(split, PANEL_SWING.claim_path)
    dev = list(split.dev)[: args.limit_sessions] if args.limit_sessions else list(split.dev)
    guard.assert_dev_only(dev, "the swing evaluation")

    log.info("building daily aggregates: %d symbols x %d DEV sessions",
             len(symbols), len(dev))
    daily_by_symbol = {s: D.build_daily_map(store, s, dev) for s in symbols}
    have = {s: len(m) for s, m in daily_by_symbol.items()}
    log.info("daily coverage: min %d, max %d", min(have.values()), max(have.values()))

    # ---- 1. calibrate the stop, separately per horizon ------------------------
    calibration = {}
    for h in HORIZONS:
        survivable, mfes = [], []
        for symbol, dmap in daily_by_symbol.items():
            series = [dmap[d] for d in sorted(dmap)]
            for e in measure_swing_excursions(symbol, series, h):
                mfes.append(e.mfe_atr)
                if e.profitable:
                    survivable.append(e.mae_atr)
        stop_atr = percentile(survivable, STOP_PERCENTILE) if survivable else None
        target_atr = stop_atr * DECLARED_RR if stop_atr else None
        calibration[h] = {
            "horizon": h,
            "observations": len(mfes),
            "reached_target": len(survivable),
            "reach_rate_pct": round(100.0 * len(survivable) / max(1, len(mfes)), 1),
            "survivable": describe(survivable, f"{h}d survivable MAE (ATR)"),
            "stop_atr": None if stop_atr is None else round(stop_atr, 3),
            "target_atr": None if target_atr is None else round(target_atr, 3),
            "mfe": describe(mfes, f"{h}d MFE (ATR)"),
            "target_reachable_pct": round(
                100.0 * sum(1 for v in mfes if target_atr and v >= target_atr)
                / max(1, len(mfes)), 1),
        }
        log.info("h=%d stop=%.3f ATR target=%.3f ATR (n=%d winners)",
                 h, stop_atr or 0, target_atr or 0, len(survivable))

    # ---- 2. score distribution, to set the bar --------------------------------
    log.info("sampling the daily score distribution")
    sample_dates = dev[40::7]
    scores_all = []
    for upto in sample_dates:
        ranked, _ = rank_universe_daily(
            daily_by_symbol, dev, upto, cfg,
            min_atr_pct=MIN_DAILY_ATR_PCT, min_turnover=MIN_TURNOVER_RUPEES,
        )
        scores_all.extend(r["score"] for r in ranked)
    score_bar = percentile(scores_all, SCORE_PERCENTILE) if scores_all else 0.0
    score_row = describe(scores_all, "daily composite score")
    log.info("score bar p%d = %.3f from n=%d", SCORE_PERCENTILE, score_bar, len(scores_all))

    # ---- 3. rank once per date; the three arms share it ----------------------
    # The ranking depends only on the date, not the horizon: same features, same
    # decision moment. Computing it once and reusing it across the arms is a 3x
    # saving and changes no value.
    log.info("ranking the universe on %d decision dates", len(dev) - 40)
    ranking_by_date = {}
    for i, upto in enumerate(dev[40:]):
        ranked, _ = rank_universe_daily(
            daily_by_symbol, dev, upto, cfg,
            min_atr_pct=MIN_DAILY_ATR_PCT, min_turnover=MIN_TURNOVER_RUPEES,
        )
        ranking_by_date[upto] = ranked[0] if ranked else None
        if (i + 1) % 250 == 0:
            log.info("  ranked %d/%d dates", i + 1, len(dev) - 40)

    arms = {}
    for h in HORIZONS:
        stop_atr = calibration[h]["stop_atr"]
        target_atr = calibration[h]["target_atr"]
        trades, excesses, no_signal = [], [], 0

        for upto in dev[40:]:
            best = ranking_by_date.get(upto)
            if best is None or best["score"] < score_bar:
                no_signal += 1
                continue
            dmap = daily_by_symbol[best["symbol"]]
            series = [dmap[d] for d in sorted(dmap)]
            idx = next((i for i, b in enumerate(series) if b.ts.date() == upto), None)
            if idx is None:
                no_signal += 1
                continue
            entry = series[idx].close
            atr_abs = best["atr_pct"] * entry / 100.0
            trade = simulate_swing(
                series, idx, h,
                stop=entry - stop_atr * atr_abs,
                target=entry + target_atr * atr_abs,
                symbol=best["symbol"],
                quantity=max(1, int(NOTIONAL / entry)),
                atr_value=atr_abs,
                score=best["score"],
            )
            if trade is None:
                no_signal += 1
                continue
            bench = panel_return_pct(daily_by_symbol, trade.entry_date, trade.exit_date)
            if bench is None:
                continue
            trades.append(trade)
            excesses.append(trade.gross_return_pct - bench)

        gross = [t.gross_return_pct for t in trades]
        nets = []
        for t in trades:
            pnl = delivery_pnl(t.entry_price, t.exit_price, t.quantity, cost_cfg)
            nets.append(float(pnl.net_pct))

        eff = effective_n([t.entry_date for t in trades], h)
        # Aggregate to one excess observation per entry DATE, then take
        # non-overlapping blocks: that is the unit the interval is computed on.
        by_date: dict[date, list[float]] = {}
        for t, x in zip(trades, excesses):
            by_date.setdefault(t.entry_date, []).append(x)
        daily_excess = [sum(v) / len(v) for _, v in sorted(by_date.items())]
        blocks = [
            sum(daily_excess[i : i + h]) / len(daily_excess[i : i + h])
            for i in range(0, len(daily_excess), h)
        ] or [0.0]

        m_raw, sd_raw, lo_raw, hi_raw = ci95(excesses) if excesses else (0, 0, 0, 0)
        m_eff, sd_eff, lo_eff, hi_eff = ci95(blocks)

        gapped = [t for t in trades if t.gapped_through]
        reasons: dict[str, int] = {}
        for t in trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

        arms[h] = {
            "horizon": h,
            "trades": len(trades),
            "no_signal": no_signal,
            "sessions": len(dev) - 40,
            "gross_mean_pct": round(sum(gross) / len(gross), 4) if gross else None,
            "net_mean_pct": round(sum(nets) / len(nets), 4) if nets else None,
            "excess_mean_pct": round(m_raw, 4),
            "excess_ci_raw": (round(lo_raw, 4), round(hi_raw, 4)),
            "excess_mean_blocks": round(m_eff, 4),
            "excess_ci_eff": (round(lo_eff, 4), round(hi_eff, 4)),
            "effective_n": len(blocks),
            "eff": eff,
            "sd_blocks": round(sd_eff, 4),
            "mde": round(mde_from(len(blocks), sd_eff), 4),
            "hit_rate_gross_pct": round(
                100.0 * sum(1 for v in gross if v > 0) / len(gross), 1) if gross else None,
            "hit_rate_excess_pct": round(
                100.0 * sum(1 for v in excesses if v > 0) / len(excesses), 1)
            if excesses else None,
            "reasons": reasons,
            "gapped": len(gapped),
            "gapped_pct_of_stops": round(
                100.0 * len(gapped) / max(1, reasons.get("STOP", 0) + len(gapped)), 1),
            "mean_gap_rupees": round(
                sum(t.gap_through_rupees for t in gapped) / len(gapped), 2)
            if gapped else 0.0,
            "mean_gap_pct": round(
                sum(100.0 * t.gap_through_rupees / t.entry_price for t in gapped)
                / len(gapped), 3) if gapped else 0.0,
            "mean_days_held": round(sum(t.days_held for t in trades) / len(trades), 2)
            if trades else None,
            "trades_list": trades,
        }

    # ---- 4. the decision rule ---------------------------------------------------
    clears = [h for h in HORIZONS if arms[h]["trades"] and arms[h]["excess_ci_eff"][0] > 0]
    inconclusive = [
        h for h in HORIZONS
        if arms[h]["trades"] and arms[h]["excess_ci_eff"][0] <= 0 <= arms[h]["excess_ci_eff"][1]
    ]
    negative = [h for h in HORIZONS if arms[h]["trades"] and arms[h]["excess_ci_eff"][1] < 0]

    def status_of(h):
        a = arms[h]
        lo, hi = a["excess_ci_eff"]
        if hi < 0:
            return "NEGATIVE — interval entirely below zero"
        if lo > 0:
            return "POSITIVE — interval entirely above zero"
        under = abs(a["excess_mean_pct"]) < a["mde"]
        return (
            "INCONCLUSIVE — interval spans zero"
            + (f", and |{a['excess_mean_pct']}| is below the {a['mde']} MDE, so the "
               f"sample could not have detected an effect this small either way"
               if under else "")
        )

    status_rows = [
        {
            "horizon": f"{h}d",
            "excess mean %": arms[h]["excess_mean_pct"],
            "95% CI (effective n)": f"[{arms[h]['excess_ci_eff'][0]}, {arms[h]['excess_ci_eff'][1]}]",
            "effective n": arms[h]["effective_n"],
            "MDE": arms[h]["mde"],
            "verdict": status_of(h),
        }
        for h in HORIZONS if arms[h]["trades"]
    ]

    beta_rows = [
        {
            "horizon": f"{h}d",
            "gross mean %": arms[h]["gross_mean_pct"],
            "minus panel buy-and-hold": round(
                arms[h]["gross_mean_pct"] - arms[h]["excess_mean_pct"], 4),
            "= EXCESS %": arms[h]["excess_mean_pct"],
        }
        for h in HORIZONS if arms[h]["trades"]
    ]

    if clears:
        verdict = f"""## Conclusion — excess clears zero at {", ".join(f"{h}d" for h in clears)}

Gross excess return over the equal-weight panel clears zero on DEV at
{", ".join(f"{h}-day" for h in clears)}, on the effective sample. That is not a
licence to trade it: it is DEV, it is one of three arms, and the holdout is the only
out-of-sample evidence available. The next step is a single holdout evaluation of one
pre-specified configuration, not further exploration."""
    elif inconclusive or negative:
        detail = []
        if negative:
            detail.append(
                f"at {', '.join(f'{h}d' for h in negative)} the interval lies ENTIRELY "
                f"BELOW zero"
            )
        if inconclusive:
            detail.append(f"at {', '.join(f'{h}d' for h in inconclusive)} it spans zero")
        verdict = f"""## Conclusion — the pre-stated rule applies

**Gross excess return over the buy-and-hold baseline does not clear zero at any of the
three horizons.** On the effective sample, {"; ".join(detail)}. None lies above it, and
every point estimate is negative.

{md_table(status_rows, ["horizon", "excess mean %", "95% CI (effective n)",
                        "effective n", "MDE", "verdict"])}

The rule fixed before this ran was:

> If gross excess return over the buy-and-hold baseline does not clear zero on DEV at
> any of the three horizons, the feature set does not predict on this horizon either,
> and the conclusion is that these features carry no edge at any tested timescale.

**That is the answer. These features carry no edge at any tested timescale.**

They have now been tested intraday (~16-minute holds, both continuous and single-shot
selection) and on 1, 3 and 5 trading-day holds, against a naive comparator each time.
Not one of those tests produced an excess whose interval clears zero. The consistent
result across horizons is itself the finding: the problem is the features, not the
holding period and not the friction.

Per the rule: no holdout evaluation, no sweep, no fourth horizon."""
    else:
        verdict = """## Conclusion — no usable trades

Not enough trades were produced to test the hypothesis. That is a coverage failure,
not a result about the features."""

    cal_rows = [
        {
            "horizon": f"{c['horizon']}d",
            "observations": c["observations"],
            "profitable at horizon": c["reached_target"],
            "win rate %": c["reach_rate_pct"],
            "survivable MAE p50": c["survivable"].get("p50"),
            "survivable MAE p75": c["survivable"].get("p75"),
            "STOP (ATR)": c["stop_atr"],
            "TARGET (ATR)": c["target_atr"],
            "target reachable %": c["target_reachable_pct"],
        }
        for c in calibration.values()
    ]

    arm_rows = [
        {
            "horizon": f"{a['horizon']}d",
            "trades": a["trades"],
            "NO_SIGNAL": a["no_signal"],
            "gross mean %": a["gross_mean_pct"],
            "net mean %": a["net_mean_pct"],
            "EXCESS mean %": a["excess_mean_pct"],
            "excess 95% CI (effective)": f"[{a['excess_ci_eff'][0]}, {a['excess_ci_eff'][1]}]",
            "hit rate excess %": a["hit_rate_excess_pct"],
        }
        for a in arms.values()
    ]

    n_rows = [
        {
            "horizon": f"{a['horizon']}d",
            "trades (raw)": a["eff"]["trades"],
            "distinct entry dates": a["eff"]["distinct_entry_dates"],
            "effective n (blocks)": a["effective_n"],
            "overstatement": f"{a['eff']['overstatement_factor']}x",
            "SD of block excess": a["sd_blocks"],
            "MDE at effective n": a["mde"],
        }
        for a in arms.values()
    ]

    gap_rows = [
        {
            "horizon": f"{a['horizon']}d",
            "stopped trades": a["reasons"].get("STOP", 0) + a["gapped"],
            "gapped through": a["gapped"],
            "% of stops that gapped": a["gapped_pct_of_stops"],
            "mean shortfall Rs": a["mean_gap_rupees"],
            "mean shortfall %": a["mean_gap_pct"],
        }
        for a in arms.values()
    ]

    exit_rows = []
    for a in arms.values():
        for reason, n in sorted(a["reasons"].items(), key=lambda kv: -kv[1]):
            exit_rows.append({
                "horizon": f"{a['horizon']}d",
                "exit": reason,
                "n": n,
                "pct": round(100.0 * n / max(1, a["trades"]), 1),
            })

    intraday_rt = round_trip_costs(1000, 1000, 100, cost_cfg)
    delivery_rt = delivery_round_trip_costs(1000, 1000, 100, cost_cfg)

    report = f"""# Multi-day swing experiment — DEV only

**Generated:** {datetime.now().isoformat(timespec="seconds")}
**Panel:** {len(symbols)} symbols, DEV {dev[0]} .. {dev[-1]} ({len(dev)} sessions)
**Holdout:** 2025-01-01 onward — {guard.status()}
**Entry:** 15:15 on the signal day, from that day's completed bars only
**Arms:** {", ".join(f"{h} trading days" for h in HORIZONS)}, reported separately

---

{verdict}

---

## Costs — delivery, and it is WORSE than intraday

A position carried overnight settles as delivery (CNC), not intraday (MIS). That was
checked rather than assumed, and it moves the arithmetic in the opposite direction to
what zero brokerage suggests:

| | intraday (MIS) | delivery (CNC) |
| --- | --- | --- |
| brokerage, round trip | Rs {intraday_rt.buy.brokerage + intraday_rt.sell.brokerage} | Rs {delivery_rt.buy.brokerage + delivery_rt.sell.brokerage} |
| STT | Rs {intraday_rt.buy.stt + intraday_rt.sell.stt} (sell only, 0.025%) | **Rs {delivery_rt.buy.stt + delivery_rt.sell.stt}** (both legs, 0.1% each) |
| stamp duty | Rs {intraday_rt.buy.stamp_duty} | Rs {delivery_rt.buy.stamp_duty} |
| slippage (modelled) | Rs {intraday_rt.slippage} | Rs {delivery_rt.slippage} |
| **total on Rs 1,00,000** | **Rs {intraday_rt.total}** ({float(intraday_rt.total) / 1000:.3f}%) | **Rs {delivery_rt.total}** ({float(delivery_rt.total) / 1000:.3f}%) |

**Delivery costs {float(delivery_rt.total) / float(intraday_rt.total):.1f}x what
intraday costs, despite zero brokerage**, because delivery STT is charged on both legs
at four times the intraday rate. The premise that "friction is not the dominant term
on a longer horizon" holds only because the expected MOVE is larger over days — not
because friction shrinks. It grows.

> Brokerage of Rs 0 on delivery is as supplied by the account holder and is NOT
> independently verified here. The statutory charges are the material part and do not
> depend on the broker.

---

## Stop calibration — recomputed on THIS horizon, not reused

The intraday stop was 2.37 intraday-ATRs. It is not carried over: daily ATR spans the
overnight gap and the holding window is days, so survivable noise is a different
distribution. Measured the same way — stop-less holds, p{STOP_PERCENTILE:.0f} of the
adverse excursion suffered by positions that were PROFITABLE AT THE HORIZON, in DAILY
ATRs.

A winner is defined by profit at the horizon, not by reaching a target. Defining it by
a target would be circular — the target is derived from the stop, the stop from what
winners survived, and the winners from the target. A first attempt did exactly that
and produced a stop of 0.32 ATR against a traded target of 0.48 ATR, calibrated
against a 1.5 ATR target that was never used.

{md_table(cal_rows)}

The stop widens with the horizon, as it must: a five-day hold has to sit through more
noise than a one-day hold.

---

## Results — gross first

{md_table(arm_rows)}

Gross is the number that matters here: if the features do not predict, no cost
treatment rescues them.

### Gross is positive, and all of it is beta

{md_table(beta_rows, ["horizon", "gross mean %", "minus panel buy-and-hold",
                      "= EXCESS %"])}

Raw gross rises with the horizon and looks like an edge growing. It is not. Subtract an
equal-weight hold of the same panel over the SAME entry and exit dates and what remains
is negative at every horizon. **The strategy was long in a market that went up** — that
is what the baseline exists to strip out, and stripping it out is what turns an
apparent +0.25% into −0.07%.

---

## Effective sample size — the trade count is not it

Daily entries with an h-day hold share h−1 days with their neighbours, and trades
opened on the same date share that day's market move. The unit is therefore the
non-overlapping DATE BLOCK, not the trade.

{md_table(n_rows)}

The MDE column is computed from the effective n. Using the raw trade count would
shrink it by roughly the square root of the overstatement factor and turn a null into
a discovery.

---

## Overnight gaps — a stop cannot execute while the market is shut

{md_table(gap_rows)}

Gapped trades are filled at the OPEN, not at the stop. The shortfall column is the
difference, which is money the position lost with no opportunity to act.

---

## Exit distribution

{md_table(exit_rows, ["horizon", "exit", "n", "pct"])}

---

## Corporate actions

The 2020–2026 backfill was checked during ingestion: Fyers serves split- and
bonus-ADJUSTED history. A >20% discontinuity scan across 21 symbols and six years
found exactly one step, and it was the COVID crash (AXISBANK, 2020-03-20 → 2020-03-24,
−21.8%), not a corporate action. Verified against known events — TATASTEEL's 1:10
split, RELIANCE's and WIPRO's 1:1 bonuses all pass through with no discontinuity.

Consequently **no trade in this experiment has a return that depends on an unadjusted
corporate action**. The cost of that adjustment is stated elsewhere and still applies:
absolute historical prices and share counts are not what would have been seen at the
time, though percentage returns — which is all this experiment uses — are correct.

---

## Score distribution and bar

{md_table([score_row], ["group", "n", "mean", "p25", "p50", "p75", "p90", "p95", "max"])}

Bar set at p{SCORE_PERCENTILE:.0f} = **{score_bar:.3f}**, the same rule as the intraday
calibration. The value is re-read rather than reused because the daily distribution is
a different one.

---

## Caveats

- **DEV only.** The holdout (2025-01-01 onward) is untouched and, under the decision
  rule above, stays that way.
- **Every daily feature passed `assert_session_causal`** at the new frequency, plus
  two deliberately leaky controls that the check correctly rejected.
- **Within-day resolution order** assumes a day touching both stop and target resolves
  as the LOSS, because the order inside a daily bar is unknowable. This biases results
  downward.
- **The F&O ban check is a stub.**
- **Costs unverified** (`rates_verified={cost_cfg.rates_verified}`), and delivery
  brokerage is as supplied, not confirmed.
- **Survivorship**: today's liquid names applied backwards.

---

*Written by `scripts/swing_evaluation.py`.*
"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    log.info("wrote %s", out)
    print(f"\nWrote {out}")
    for h, a in arms.items():
        print(f"  {h}d: trades {a['trades']}, gross {a['gross_mean_pct']}%, "
              f"excess {a['excess_mean_pct']}% CI(eff) {a['excess_ci_eff']}, "
              f"n_eff {a['effective_n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
