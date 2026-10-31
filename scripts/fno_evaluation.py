"""F&O positioning features on a 1-trading-day horizon, DEV only.

    python scripts/fno_evaluation.py

Writes reports/fno_evaluation.md.

THE DECISION RULE, FIXED BEFORE RUNNING
---------------------------------------
Recorded in ``reports/.fno_preregistration.json``, which this script verifies against
the live constants before it computes anything:

> If gross EXCESS return over the equal-weight buy-and-hold comparator does not clear
> zero on DEV, F&O features carry no edge either, and the conclusion is that this
> approach is exhausted. No sweeps, no additional horizons, no search for a subset that
> works.

Method
------
* One decision point per session, at 15:15. The single highest-scoring eligible symbol,
  or none. No fall-through to second best.
* Absolute score bar of +0.25 on the [-1, +1] composite, stated in advance. A second
  arm with no bar at all is reported alongside as a POWER REFERENCE, so a null cannot
  hide behind a small sample.
* Every F&O feature is strictly ``trade_date < decision date``: the bhavcopy and the
  index-close file for session D both publish after the close of D.
* Stop calibrated fresh on this horizon from the p75 of survivable adverse excursion in
  daily ATRs; target preserves 1.5:1.
* Excess is over an equal-weight panel hold on the SAME dates.
* Sample size is the effective n from non-overlapping date blocks.
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import statistics
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import CostSettings, get_settings  # noqa: E402
from src.analysis.split import PANEL_SWING, HoldoutGuard, swing_split  # noqa: E402
from src.costs import delivery_pnl, delivery_round_trip_costs  # noqa: E402
from src.data.backfill import LIQUID_FNO_UNIVERSE  # noqa: E402
from src.data.bhavcopy import STOCK_FUTURE_TYPES, BhavcopyStore  # noqa: E402
from src.data.india_vix import VixStore  # noqa: E402
from src.market.calendar import trading_days_between  # noqa: E402
from src.positions.models import Bar  # noqa: E402
from src.positions.store import PositionStore  # noqa: E402
from src.signals import daily as D  # noqa: E402
from src.signals import fno_features as F  # noqa: E402
from src.signals import fno_scoring as S  # noqa: E402
from src.signals.calibration import describe, percentile  # noqa: E402
from src.signals.swing import (  # noqa: E402
    effective_n,
    mde_from,
    measure_swing_excursions,
    panel_return_pct,
    simulate_swing,
)
from src.signals.universe import MIN_TURNOVER_RUPEES  # noqa: E402

log = logging.getLogger("fno.evaluation")

REPORT = Path("reports/fno_evaluation.md")
PREREG = Path("reports/.fno_preregistration.json")

#: No futures listed at all. Not a coverage gap that could be filled -- there is no
#: TMCV futures contract in the exchange's own file on any date in the panel.
NO_FUTURES = ("TMCV",)
#: Futures begin 2025-10-24, entirely outside DEV, so it contributes nothing here.
OUTSIDE_DEV = ("TMPV",)

HORIZON = 1
NOTIONAL = 100_000.0
STOP_PERCENTILE = 75.0
DECLARED_RR = 1.5
MIN_DAILY_ATR_PCT = 0.8
#: Sessions of history before the first decision. Set to MIN_VIX_HISTORY so that every
#: one of the nine components is potentially available from the first decision date --
#: otherwise the composite early in the panel is an average over a different set of
#: components than later, and the absolute bar would not mean the same thing throughout.
WARMUP = F.MIN_VIX_HISTORY


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


def verify_preregistration() -> dict:
    """Refuse to run if the live constants drifted from what was registered.

    The absolute score bar only means anything if the scale it sits on was fixed before
    any P&L existed. This is the structural version of that promise: a tuned constant
    stops the experiment rather than quietly changing what the number means.
    """
    if not PREREG.exists():
        raise SystemExit(f"missing pre-registration: {PREREG}")
    prereg = json.loads(PREREG.read_text(encoding="utf-8"))
    c = prereg["scoring_constants"]
    mismatches = []
    if c["weights"] != S.WEIGHTS:
        mismatches.append(f"weights: registered {c['weights']} vs live {S.WEIGHTS}")
    live_sat = {str(k): v for k, v in S.OI_SATURATION_PCT.items()}
    if c["oi_saturation_pct"] != live_sat:
        mismatches.append(f"oi_saturation_pct: {c['oi_saturation_pct']} vs {live_sat}")
    for key, live in (("carry_centre_pct", S.CARRY_CENTRE_PCT),
                      ("carry_span_pct", S.CARRY_SPAN_PCT),
                      ("basis_roc_saturation_pp", S.BASIS_ROC_SATURATION_PP),
                      ("vix_change_saturation_pct", S.VIX_CHANGE_SATURATION_PCT),
                      ("rollover_saturation", S.ROLLOVER_SATURATION)):
        if c[key] != live:
            mismatches.append(f"{key}: registered {c[key]} vs live {live}")
    if mismatches:
        raise SystemExit(
            "The live scoring constants no longer match the pre-registration:\n  "
            + "\n  ".join(mismatches)
            + "\n\nThe pre-stated score bar is only meaningful against the registered "
              "scale. Change the registration deliberately and say why in the report, "
              "or restore the constants."
        )
    return prereg


# --------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------


def load_spot(store: PositionStore, symbols, sessions):
    """One pass over the stored 1-minute bars, producing both daily views.

    Two different closes are needed and they are NOT interchangeable:

    * the 15:15 aggregate, which is the decision-time view every existing daily feature
      and the entry price use;
    * the FULL-session close, which is the only thing comparable with the bhavcopy
      futures close for the basis. Pairing a 15:30 futures close with a 15:15 spot price
      would put fifteen minutes of spot drift into a quantity whose whole range is about
      0.3%.

    Both describe session D and both are available to a decision on D+1, which is all
    the F&O features ever ask for.
    """
    daily: dict[str, dict[date, Bar]] = {}
    full_close: dict[str, dict[date, float]] = {}
    for i, symbol in enumerate(symbols, 1):
        dmap: dict[date, Bar] = {}
        fmap: dict[date, float] = {}
        for day in sessions:
            if not store.has_bars(symbol, day):
                continue
            bars = store.load_bars(symbol, day)
            if not bars:
                continue
            aggregated = D.aggregate_daily(bars, day)
            if aggregated is not None:
                dmap[day] = aggregated
            fmap[day] = bars[-1].close
        daily[symbol] = dmap
        full_close[symbol] = fmap
        log.info("  spot %2d/%d %-11s %d sessions", i, len(symbols), symbol, len(dmap))
    return daily, full_close


def load_bhavcopy(store: BhavcopyStore, universe, sessions):
    """Stock-future rows for the universe, for the panel's sessions only.

    Filtered inside the parser: a day's file is ~31,000 rows of which ~400 are stock
    futures, and building the rest would dominate the run.
    """
    wanted = set(sessions)
    rows_by_date: dict[date, list] = {}
    paths = sorted(store.raw_dir.rglob("fo_*.zip"))
    for i, path in enumerate(paths, 1):
        day = date.fromisoformat(f"{path.stem[3:7]}-{path.stem[7:9]}-{path.stem[9:11]}")
        if day not in wanted:
            continue
        try:
            rows = store.load(day, instruments=STOCK_FUTURE_TYPES) or []
        except Exception as exc:  # noqa: BLE001 - reported, never silently dropped
            log.warning("unreadable %s: %s: %s", path.name, type(exc).__name__, exc)
            continue
        rows_by_date[day] = [r for r in rows if r.symbol in universe]
        if i % 250 == 0:
            log.info("  bhavcopy %d/%d files scanned", i, len(paths))
    return rows_by_date


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default=None)
    p.add_argument("--out", default=str(REPORT))
    p.add_argument("--cache", default=None,
                   help="pickle path for the loaded panel, to make reruns cheap")
    p.add_argument("--limit-sessions", type=int, default=None,
                   help="SMOKE TEST ONLY. Truncates DEV, which makes the result a "
                        "different experiment from the pre-registered one.")
    args = p.parse_args(argv)

    prereg = verify_preregistration()
    log.info("pre-registration verified: %s", prereg["experiment"])

    data_dir = Path(args.data_dir or get_settings().data_dir)
    cost_cfg = CostSettings()
    symbols = [s for s in LIQUID_FNO_UNIVERSE
               if s not in NO_FUTURES and s not in OUTSIDE_DEV]

    all_sessions = trading_days_between(PANEL_SWING.start, PANEL_SWING.end)
    split = swing_split(all_sessions)
    guard = HoldoutGuard(split, PANEL_SWING.claim_path)
    dev = list(split.dev)[:args.limit_sessions] if args.limit_sessions else list(split.dev)
    guard.assert_dev_only(dev, "the F&O feature evaluation")
    log.info("DEV %s .. %s (%d sessions), %d symbols", dev[0], dev[-1], len(dev),
             len(symbols))

    # The cache deliberately holds only the two expensive loads. India VIX is a few
    # seconds of CSV parsing and is always re-read, so a cache written while the
    # backfill was still running cannot silently freeze a partial VIX series into a
    # result -- which would show up as missing components, not as an error.
    cache = Path(args.cache) if args.cache else None
    if cache and cache.exists():
        log.info("loading cached panel from %s", cache)
        daily, full_close, rows_by_date = pickle.loads(cache.read_bytes())
    else:
        log.info("loading spot bars")
        daily, full_close = load_spot(PositionStore(data_dir), symbols, dev)
        log.info("loading bhavcopy")
        rows_by_date = load_bhavcopy(BhavcopyStore(data_dir), set(symbols), dev)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(pickle.dumps((daily, full_close, rows_by_date)))
    log.info("loading India VIX")
    vix = {d: r.close for d, r in VixStore(data_dir).load_all().items()}
    log.info("bhavcopy dates %d, VIX sessions %d", len(rows_by_date), len(vix))

    vix_dev = {d: v for d, v in vix.items() if d <= dev[-1]}
    missing_vix = [d for d in dev if d not in vix_dev]

    # ---- build the F&O records -------------------------------------------------
    log.info("building F&O daily records")
    records = {s: F.build_fno_daily(rows_by_date, s, full_close.get(s, {}))
               for s in symbols}
    for s in symbols:
        log.info("  fno %-11s %d sessions", s, len(records[s]))

    # ---- integrity: the price-scale snap, and why each session was dropped ------
    from src.data.futures import ROLL_DAYS_BEFORE_EXPIRY, active_expiry, expiry_calendar

    scale_rows = []
    for s in symbols:
        fut_dates = {d for d in dev
                     if any(r.symbol == s for r in rows_by_date.get(d, []))}
        spot_dates = set(full_close.get(s, {}))
        calendar = expiry_calendar(rows_by_date).get(s, [])
        no_spot = no_contract = unresolved = 0
        for day in sorted(fut_dates & spot_dates):
            if day in records[s]:
                continue
            expiry = active_expiry(day, calendar, ROLL_DAYS_BEFORE_EXPIRY)
            near = next((r for r in rows_by_date[day]
                         if r.symbol == s and r.expiry == expiry), None)
            if expiry is None or near is None:
                no_contract += 1
            elif F.price_scale_factor(near.close, full_close[s][day]) is None:
                unresolved += 1
        no_spot = len(fut_dates - spot_dates)
        median_basis = F.median_basis_pct(records[s])
        plausible = F.basis_is_plausible(records[s])
        scale_rows.append({
            "symbol": s,
            "built": len(records[s]),
            "no spot bars": no_spot,
            "near contract absent": no_contract,
            "scale unresolved": unresolved,
            "factors": ", ".join(f"{f:g}" for f in sorted(
                {r.scale_factor for r in records[s].values()})),
            "ex-dates in DEV": ", ".join(
                str(d) for d in sorted(d for d, r in records[s].items()
                                       if r.is_scale_change)) or "—",
            "median basis %": None if median_basis is None else round(median_basis, 3),
            "basis usable": "yes" if plausible else "**NO**",
        })

    basis_usable = {s: F.basis_is_plausible(records[s]) for s in symbols}
    suppressed = sorted(s for s, ok in basis_usable.items() if not ok)
    if suppressed:
        log.warning("basis components suppressed for %s (residual price-scale offset)",
                    ", ".join(suppressed))

    # ---- calibrate the stop on THIS horizon ------------------------------------
    log.info("calibrating the stop at horizon=%d", HORIZON)
    survivable, mfes = [], []
    for symbol, dmap in daily.items():
        series = [dmap[d] for d in sorted(dmap)]
        for e in measure_swing_excursions(symbol, series, HORIZON):
            mfes.append(e.mfe_atr)
            if e.profitable:
                survivable.append(e.mae_atr)
    stop_atr = percentile(survivable, STOP_PERCENTILE)
    target_atr = stop_atr * DECLARED_RR
    log.info("stop=%.3f ATR target=%.3f ATR (n=%d profitable of %d)",
             stop_atr, target_atr, len(survivable), len(mfes))

    # ---- rank once per decision date -------------------------------------------
    decision_dates = dev[WARMUP:]
    log.info("scoring %d decision dates", len(decision_dates))
    top_by_date: dict[date, S.FnoCandidate | None] = {}
    availability = Counter()
    state_counts = Counter()
    scored_total = 0
    all_scores: list[float] = []

    for i, upto in enumerate(decision_dates):
        eligible = []
        for symbol in symbols:
            dmap = daily.get(symbol) or {}
            series = D.daily_series(dmap, dev, upto)
            if not series:
                continue
            atr_pct = D.daily_atr_pct(dmap, dev, upto)
            close = series[-1].close
            if (atr_pct is None or atr_pct < MIN_DAILY_ATR_PCT
                    or close * series[-1].volume < MIN_TURNOVER_RUPEES):
                continue
            eligible.append(symbol)

        ranked = S.rank_universe_fno(records, vix_dev, dev, upto, eligible=eligible,
                                     basis_usable=basis_usable)
        top_by_date[upto] = ranked[0] if ranked else None
        scored_total += len(ranked)
        all_scores.extend(c.score for c in ranked)
        for cand in ranked:
            for comp in cand.components:
                if comp.available:
                    availability[comp.name] += 1
            state = cand.component("oi_price_state_1d")
            if state and state.available:
                state_counts[state.detail] += 1
        if (i + 1) % 250 == 0:
            log.info("  scored %d/%d dates", i + 1, len(decision_dates))

    score_shape = describe(all_scores, "F&O composite score")

    # ---- the two arms -----------------------------------------------------------
    def run_arm(bar: float | None) -> dict:
        trades, excesses, no_signal, below_bar = [], [], 0, 0
        for upto in decision_dates:
            best = top_by_date.get(upto)
            if best is None:
                no_signal += 1
                continue
            if bar is not None and best.score < bar:
                below_bar += 1
                no_signal += 1
                continue
            dmap = daily[best.symbol]
            series = [dmap[d] for d in sorted(dmap)]
            idx = next((j for j, b in enumerate(series) if b.ts.date() == upto), None)
            if idx is None:
                no_signal += 1
                continue
            entry = series[idx].close
            atr_pct = D.daily_atr_pct(dmap, dev, upto)
            if atr_pct is None:
                no_signal += 1
                continue
            atr_abs = atr_pct * entry / 100.0
            trade = simulate_swing(
                series, idx, HORIZON,
                stop=entry - stop_atr * atr_abs,
                target=entry + target_atr * atr_abs,
                symbol=best.symbol,
                quantity=max(1, int(NOTIONAL / entry)),
                atr_value=atr_abs,
                score=best.score,
            )
            if trade is None:
                no_signal += 1
                continue
            bench = panel_return_pct(daily, trade.entry_date, trade.exit_date)
            if bench is None:
                no_signal += 1
                continue
            trades.append(trade)
            excesses.append(trade.gross_return_pct - bench)

        gross = [t.gross_return_pct for t in trades]
        nets = [float(delivery_pnl(t.entry_price, t.exit_price, t.quantity,
                                   cost_cfg).net_pct) for t in trades]
        eff = effective_n([t.entry_date for t in trades], HORIZON)
        by_date: dict[date, list[float]] = {}
        for t, x in zip(trades, excesses):
            by_date.setdefault(t.entry_date, []).append(x)
        blocks = [sum(v) / len(v) for _, v in sorted(by_date.items())] or [0.0]

        m_raw, _, lo_raw, hi_raw = ci95(excesses) if excesses else (0, 0, 0, 0)
        m_eff, sd_eff, lo_eff, hi_eff = ci95(blocks)
        reasons = Counter(t.exit_reason for t in trades)
        gapped = [t for t in trades if t.gapped_through]

        return {
            "bar": bar,
            "trades": len(trades),
            "no_signal": no_signal,
            "below_bar": below_bar,
            "sessions": len(decision_dates),
            "gross_mean_pct": round(sum(gross) / len(gross), 4) if gross else None,
            "net_mean_pct": round(sum(nets) / len(nets), 4) if nets else None,
            "excess_mean_pct": round(m_eff, 4),
            "excess_ci_raw": (round(lo_raw, 4), round(hi_raw, 4)),
            "excess_ci_eff": (round(lo_eff, 4), round(hi_eff, 4)),
            "effective_n": len(blocks),
            "eff": eff,
            "sd_blocks": round(sd_eff, 4),
            "mde": round(mde_from(len(blocks), sd_eff), 4),
            "hit_rate_gross_pct": round(100.0 * sum(1 for v in gross if v > 0)
                                        / len(gross), 1) if gross else None,
            "hit_rate_excess_pct": round(100.0 * sum(1 for v in excesses if v > 0)
                                         / len(excesses), 1) if excesses else None,
            "reasons": reasons,
            "gapped": len(gapped),
            "gapped_pct_of_stops": round(100.0 * len(gapped)
                                         / max(1, reasons.get("STOP", 0) + len(gapped)), 1),
            "mean_gap_pct": round(sum(100.0 * t.gap_through_rupees / t.entry_price
                                      for t in gapped) / len(gapped), 3) if gapped else 0.0,
            "panel_mean_pct": round(
                (sum(gross) / len(gross)) - m_raw, 4) if gross else None,
        }

    primary_bar = prereg["score_bar"]["primary"]
    log.info("running the primary arm (bar=%.2f)", primary_bar)
    primary = run_arm(primary_bar)
    log.info("running the power-reference arm (no bar)")
    power = run_arm(None)
    log.info("primary: trades=%d excess=%s CI=%s",
             primary["trades"], primary["excess_mean_pct"], primary["excess_ci_eff"])
    log.info("power  : trades=%d excess=%s CI=%s",
             power["trades"], power["excess_mean_pct"], power["excess_ci_eff"])

    # ---- the decision rule -------------------------------------------------------
    def status_of(arm) -> str:
        if not arm["trades"]:
            return "NO TRADES — coverage failure, not a result"
        lo, hi = arm["excess_ci_eff"]
        if lo > 0:
            return "CLEARS ZERO — interval entirely above zero"
        if hi < 0:
            return "NEGATIVE — interval entirely below zero"
        under = abs(arm["excess_mean_pct"]) < arm["mde"]
        return ("INCONCLUSIVE — interval spans zero"
                + (f"; |{arm['excess_mean_pct']}| is below the {arm['mde']} MDE, so the "
                   f"sample could not have detected an effect this small either way"
                   if under else ""))

    clears = primary["trades"] and primary["excess_ci_eff"][0] > 0

    arm_rows = [
        {
            "arm": name,
            "score bar": "—" if a["bar"] is None else f"{a['bar']:+.2f}",
            "trades": a["trades"],
            "NO_SIGNAL": a["no_signal"],
            "gross mean %": a["gross_mean_pct"],
            "net mean %": a["net_mean_pct"],
            "panel comparator %": a["panel_mean_pct"],
            "EXCESS mean %": a["excess_mean_pct"],
            "excess 95% CI (effective n)":
                f"[{a['excess_ci_eff'][0]}, {a['excess_ci_eff'][1]}]",
            "effective n": a["effective_n"],
            "MDE": a["mde"],
            "verdict": status_of(a),
        }
        for name, a in (("primary (pre-stated bar)", primary),
                        ("power reference (no bar)", power))
    ]

    if clears:
        verdict = f"""## Conclusion — gross excess clears zero on DEV

The pre-stated primary arm produced a mean gross excess of
**{primary['excess_mean_pct']}%** over the equal-weight panel, 95% CI
[{primary['excess_ci_eff'][0]}, {primary['excess_ci_eff'][1]}] on
n_eff = {primary['effective_n']}. The interval lies above zero.

That is not a licence to trade it. It is DEV, the constants were fixed in advance but
the feature SET was not, and the holdout is the only out-of-sample evidence available.
The next step is a single holdout evaluation of exactly this configuration — not
further exploration."""
    else:
        lo, hi = primary["excess_ci_eff"]
        plo, phi = power["excess_ci_eff"]
        verdict = f"""## Conclusion — the pre-stated rule applies

**Gross excess return over the equal-weight buy-and-hold comparator does not clear zero
on DEV.**

On the pre-stated arm the mean gross excess is **{primary['excess_mean_pct']}%**,
95% CI **[{lo}, {hi}]** on n_eff = {primary['effective_n']}. On the power-reference arm,
which takes the day's best candidate every session and so carries the largest sample the
design can produce, it is **{power['excess_mean_pct']}%**, 95% CI **[{plo}, {phi}]** on
n_eff = {power['effective_n']}. Neither lower bound is above zero.

{md_table(arm_rows, ["arm", "trades", "EXCESS mean %",
                     "excess 95% CI (effective n)", "effective n", "MDE", "verdict"])}

The rule fixed before this ran, recorded in `reports/.fno_preregistration.json`, was:

> If gross excess return over the buy-and-hold comparator does not clear zero on DEV,
> F&O features carry no edge either, and the conclusion is that this approach is
> exhausted. Report that plainly. No sweeps, no additional horizons, no search for a
> subset that works.

**That is the answer. The F&O features carry no edge on this horizon, and this approach
is exhausted.**

Per the rule: no sweep, no second horizon, no subset search, and **no holdout
evaluation**. The swing holdout (2025-01-01 onward) is untouched and stays that way."""

    # ---- supporting tables -------------------------------------------------------
    cal_row = {
        "horizon": f"{HORIZON}d",
        "stop-less holds measured": len(mfes),
        "profitable at horizon": len(survivable),
        "win rate %": round(100.0 * len(survivable) / max(1, len(mfes)), 1),
        "survivable MAE p50": describe(survivable, "")["p50"],
        "survivable MAE p75": describe(survivable, "")["p75"],
        "STOP (daily ATR)": round(stop_atr, 3),
        "TARGET (daily ATR)": round(target_atr, 3),
        "target reachable %": round(
            100.0 * sum(1 for v in mfes if v >= target_atr) / max(1, len(mfes)), 1),
    }

    avail_rows = [
        {
            "component": name,
            "weight": S.WEIGHTS[name],
            "measured": availability.get(name, 0),
            "of scored symbol-days": scored_total,
            "availability %": round(100.0 * availability.get(name, 0)
                                    / max(1, scored_total), 1),
        }
        for name in S.WEIGHTS
    ]

    state_rows = [
        {"state": k, "n": v, "share %": round(100.0 * v / max(1, sum(state_counts.values())), 1)}
        for k, v in state_counts.most_common()
    ]

    exit_rows = [
        {"arm": name, "exit": reason, "n": n,
         "pct": round(100.0 * n / max(1, a["trades"]), 1)}
        for name, a in (("primary", primary), ("power reference", power))
        for reason, n in a["reasons"].most_common()
    ]

    delivery_rt = delivery_round_trip_costs(1000, 1000, 100, cost_cfg)
    # Computed from THIS experiment's DEV list, not Panel.regime_coverage(), which
    # would re-split at the 0.8 fraction instead of the swing panel's calendar boundary
    # and report regime counts for a window this run never touched.
    from src.analysis.split import REGIMES, regime_of  # noqa: PLC0415
    dev_set = set(dev)
    regimes = [
        {"regime": label, "dev_sessions": sum(1 for d in dev_set if regime_of(d) == label)}
        for label, _, _ in REGIMES
        if any(regime_of(d) == label for d in dev_set)
    ]

    report = f"""# F&O positioning features on a 1-day horizon — DEV only

**Generated:** {datetime.now().isoformat(timespec="seconds")}
**Pre-registration:** `reports/.fno_preregistration.json` — verified against the live
constants before this ran; the script refuses to execute if they have drifted.
**Panel:** {len(symbols)} symbols, DEV {dev[0]} .. {dev[-1]} ({len(dev)} sessions),
{len(decision_dates)} decision dates after a {WARMUP}-session warmup
**Holdout:** 2025-01-01 onward — {guard.status()}
**Horizon:** {HORIZON} trading day, entry 15:15, one decision point per session

---

{verdict}

---

## What was actually computable, and what was not

The request asked for OI change over **1, 5, 15 and 30 minutes**. That does not exist in
this data and could not be manufactured:

- Fyers' history endpoint returns six-field OHLCV and carries **no open interest at any
  resolution**. OI comes from the exchange's end-of-day derivatives bhavcopy, which is
  daily by construction.
- India VIX comes from NSE's daily index-close file — one OHLC row per session. There is
  no intraday India VIX here at any resolution.
- Futures prices are daily bhavcopy closes. With no intraday futures bars there is no
  intraday basis either.

So **every lookback in this experiment is in trading days, not minutes.** Resampling a
daily row into minutes would have manufactured a series the exchange never published.
The features built were:

| requested | built | note |
| --- | --- | --- |
| OI change 1/5/15/30 min | OI change 1/5/15/30 **trading days**, absolute and % | daily is the finest resolution that exists |
| OI change day-over-day | 1-day change, near-month and total | |
| OI–price divergence, four states | `LONG_BUILDUP` / `SHORT_BUILDUP` / `SHORT_COVERING` / `LONG_UNWINDING` | price leg from adjusted spot, not raw futures |
| futures basis, absolute and % of spot | `basis_pct`, plus an annualised carry rate | annualised because raw basis decays mechanically into expiry |
| basis rate of change | `basis_roc_pp`, one-session change in points | None across a roll |
| India VIX level, change, 1-year percentile | all three | percentile needs {F.MIN_VIX_HISTORY} sessions of history |
| rollover % near expiry | `rollover_ratio` and `rollover_relative` | relative to the same-days-to-expiry norm over prior cycles |

---

## Three discontinuities, each of which would have produced a fake signal

All three are handled explicitly, and all three yield `None` rather than zero. A zero
reads as "no change", which is the opposite of "not comparable".

### 1. The contract roll

OI does not carry across a roll — the new contract is a different pool of positions.
Measured on this data, RELIANCE's near-month OI moves **+292%** on its 2024-10-31 roll.
Near-month OI change and basis rate-of-change are therefore `None` across a roll.

Total OI, summed over every listed futures expiry, *is* continuous through a roll, and
that is what the 5/15/30-day windows are built from — a monthly contract does not live
long enough for a 30-day window to fit inside one.

### 2. Corporate actions — the trap that is not the roll

NSE restates F&O lot size at a split or bonus, so OI counted in units jumps by the
action's factor on the ex-date. Measured here:

| underlying | ex-date | near-month OI move | total OI move |
| --- | --- | --- | --- |
| RELIANCE 1:1 bonus | 2024-10-28 | +73.9% | +99.2% |
| WIPRO 1:1 bonus | 2024-12-03 | +88.9% | +90.2% |

Neither is a position build. Every OI window spanning an ex-date is invalidated, exactly
as a roll is.

### 3. The price-scale mismatch between the two sources — silent, and fatal to the basis

Fyers serves spot history **split/bonus ADJUSTED**; the bhavcopy serves futures **RAW**.
Before its bonus, RELIANCE's futures close divided by its stored spot close is **2.007**,
not 1.003. TATASTEEL's is **10.0**. A basis computed as `futures − spot` on those two
numbers is not a mispriced carry, it is a 100% error that would dwarf every real signal
in the panel — and nothing about it looks wrong on inspection.

`price_scale_factor` recovers the factor by snapping that ratio to the nearest plausible
corporate-action factor, and returns `None` when nothing fits within {F.MAX_SCALE_RESIDUAL_PCT}%.
This is only safe because the quantity being measured is small and the candidates are far
apart — a real basis is well under 5% while the closest candidate factors are 20% apart —
and that premise is asserted in the tests rather than assumed.

**Price CHANGES are taken from the adjusted spot series, never from raw futures**, so no
four-state classification can be flipped by an ex-date.

### 3b. Demergers defeat the snap, and one symbol in this panel proves it

The snap works because splits and bonuses are simple rationals. **Demergers are not.**
Their adjustment factor is a value ratio — an arbitrary number — so it lands *inside* the
snap's {F.MAX_SCALE_RESIDUAL_PCT}% tolerance, is absorbed into a factor of 1.0, and
re-emerges as a constant fake premium indistinguishable from an expensive stock to carry.

An audit of the resulting basis distributions caught it. **ADANIENT's futures/spot ratio
is 1.0350 in every one of the twenty quarters from 2020Q1 to 2024Q4** — including 2023Q1,
when the stock lost roughly seventy percent on the Hindenburg report. A carry premium
cannot hold flat through that. SBIN's ratio over the same years sits at 1.002 and moves
around, which is what a real basis does.

Unchecked, this mattered: at ~3.5% of spot the annualised carry clamps `basis_carry` to
+1.0 on essentially every session, adding up to {2.0 / sum(S.WEIGHTS.values()):.3f} to
ADANIENT's composite for free and systematically over-selecting it.

Widening the candidate factors to catch 1.035 would be the wrong fix — it would destroy
the 20% separation that makes the snap unambiguous, after which a *genuine* 3% basis
would be silently snapped away. Instead `basis_is_plausible` refuses the two basis
components for any symbol whose MEDIAN basis exceeds {F.BASIS_PLAUSIBILITY_PCT}% of spot
(three times fair monthly carry at a ~6% annual rate). They drop out with their weight
through the ordinary "could not be measured" path. The tell is persistence, not size: a
real basis mean-reverts to carry and collapses into every expiry.

**Suppressed in this run: {", ".join(suppressed) if suppressed else "none"}.**

#### This experiment was run twice, and both results are recorded

The ADANIENT contamination was found by auditing the basis distributions **after** a
first full run, so there are two results and hiding either would be dishonest:

| run | primary excess % | primary 95% CI | power-arm excess % | power 95% CI |
| --- | --- | --- | --- | --- |
| 1 — ADANIENT basis contaminated | −0.0637 | [−0.1476, +0.0201] | −0.0878 | [−0.1633, −0.0124] |
| 2 — basis suppressed for ADANIENT | {primary['excess_mean_pct']} | [{primary['excess_ci_eff'][0]}, {primary['excess_ci_eff'][1]}] | {power['excess_mean_pct']} | [{power['excess_ci_eff'][0]}, {power['excess_ci_eff'][1]}] |

Nothing about the pre-registration changed between them: same horizon, same bar, same
weights, same saturation constants, same decision rule. The only change was refusing a
feature that was measuring a demerger adjustment rather than a market price.

The correction's DIRECTION was predictable before it was applied — removing a constant
positive bias on one symbol can only reduce how often that symbol is selected — and it
made the result **more** negative, not less. The first run's power arm was already below
zero. This is a fix that cost the strategy, not one that rescued it.

### What the snap found across the panel

{md_table(scale_rows, ["symbol", "built", "no spot bars", "near contract absent",
                       "scale unresolved", "factors", "ex-dates in DEV",
                       "median basis %", "basis usable"])}

Reading the drop columns:

- **no spot bars** — sessions with a futures row but no stored equity bars. These are the
  circuit-breaker halt days quarantined by the equity backfill (2020-03-13, 2020-03-23,
  2022-03-07), which is correct data of a halted market being correctly withheld.
- **near contract absent** — sessions where the roll rule selects an expiry that has no
  row in the exchange file that day. Almost all of these fall in 2023-05-25 → 2023-06-29,
  around the one expiry NSE moved off its scheduled date (2023-06-29 was Bakri Id). The
  session is skipped rather than filled from a neighbouring contract: a fabricated bar in
  a price series is worse than a hole in it.
- **scale unresolved** — no candidate factor fit within {F.MAX_SCALE_RESIDUAL_PCT}%.

---

## Causality

Every F&O feature is strictly `trade_date < decision date`. The bhavcopy for session D
and the index-close file for session D both publish **after the close of D**, so a
decision at 15:15 on D — thirteen minutes before the close that produces them — may use
D-1 at the newest. A daily row looks like a property of the day it is stamped with, which
is exactly what makes `<=` the natural mistake here.

This is a **cross-session** leak shape, and the bar-level poison harness cannot see it:
every session in a wrong date list is genuinely complete, so poisoning intraday bars
proves nothing. `assert_session_causal` is the only thing that covers it, and in
`tests/test_fno_features.py` it is applied to:

- every public feature in `src.signals.fno_features`, at four decision dates each;
- the assembled composite `score_symbol_fno`, because a leak can live in the wiring
  rather than in any one feature;
- **two deliberately leaky controls that the check must reject** — one using `<=`
  instead of `<` (the publication-lag bug), one reading `sessions[-1]`. A check that
  never fails is not evidence.

A coverage test fails if a new feature is added to `__all__` without a causality test,
and a non-vacuity test fails if the fixture is so flat that a leak could hide in it.

Feature availability across the {scored_total:,} scored symbol-days:

{md_table(avail_rows)}

---

## Stop calibration — recomputed on this horizon

Stop-less holds over the whole DEV panel; the stop is the p{STOP_PERCENTILE:.0f} of the
adverse excursion suffered by positions that were **profitable at the horizon**, in daily
ATRs. A winner is defined by profit at the horizon rather than by reaching a target,
because the target is derived from the stop and defining winners by it would be circular.

{md_table([cal_row])}

---

## Results

{md_table(arm_rows, ["arm", "score bar", "trades", "NO_SIGNAL", "gross mean %",
                     "net mean %", "panel comparator %", "EXCESS mean %",
                     "excess 95% CI (effective n)", "effective n", "MDE"])}

Gross is the number the decision rule turns on: if the features do not predict, no cost
treatment rescues them. Net is reported beside it throughout, per CLAUDE.md 1.4.

**The comparator column is the point.** Beta is not edge: a long-only rule in a market
that rose makes money for reasons that have nothing to do with open interest. Excess is
gross minus an equal-weight hold of the same panel over the *same entry and exit dates*.

### The power-reference arm exists so a null cannot hide behind a small sample

The pre-stated bar of {primary_bar:+.2f} was fixed from the scale, not from the data, and
it was always possible it would leave too few trades to conclude anything. The
no-bar arm was declared in advance for exactly that reason: it takes the day's best
candidate every session, giving the largest sample this design can produce. Both are
reported whatever they show.

---

## Score distribution

{md_table([score_shape], ["group", "n", "mean", "p25", "p50", "p75", "p90", "p95", "max"])}

The bar of **{primary_bar:+.2f}** was set from the [-1, +1] scale before any P&L existed —
zero means the weighted evidence is balanced, and the bar asks for a quarter of the way
from balanced to unanimous. It is **not** a percentile of this distribution. Had it been,
it would have been chosen after seeing the data it was meant to filter.

### OI–price state distribution across scored symbol-days

{md_table(state_rows)}

Frequencies only. Per-state P&L is deliberately **not** reported: the pre-registration
forbids searching for a subset that works, and a table of returns broken out by state is
the most direct invitation to do it.

---

## Exit distribution

{md_table(exit_rows, ["arm", "exit", "n", "pct"])}

Gapped through the stop: {primary['gapped']} of the primary arm's stops
({primary['gapped_pct_of_stops']}%), mean shortfall {primary['mean_gap_pct']}% of entry.
A stop cannot execute while the market is shut, so those fill at the OPEN.

---

## Costs — delivery, because the position is held overnight

Round trip on Rs 1,00,000: **Rs {delivery_rt.total}**
({float(delivery_rt.total) / 1000:.3f}%), of which STT is
Rs {delivery_rt.buy.stt + delivery_rt.sell.stt} charged on **both** legs at 0.1%, against
intraday's 0.025% on the sell only. Delivery costs more than intraday, not less, despite
zero brokerage.

> Cost rates are unverified against actual contract notes
> (`rates_verified={cost_cfg.rates_verified}`), and delivery brokerage is as supplied by
> the account holder. The statutory charges are the material part and do not depend on
> the broker. The decision rule turns on GROSS excess, so this does not affect the
> conclusion.

---

## Caveats

- **DEV only.** The swing holdout (2025-01-01 onward) is untouched and, under the
  decision rule, stays that way.
- **Regime coverage of the panel:** {", ".join(f"{r['regime']} {r['dev_sessions']} DEV" for r in regimes)}.
- **India VIX sessions missing from DEV:** {len(missing_vix)}. Where VIX is absent the
  two market-wide components are unavailable and drop out of the composite with their
  weight, rather than being counted as neutral.
- **The feature set was not pre-registered, only the constants and the rule.** Nine
  components chosen from domain convention is itself a design choice with freedom in it.
  What is protected is that no constant was tuned against an outcome.
- **Survivorship**: today's liquid names applied backwards. TMCV is excluded because no
  futures contract on it exists in the exchange file on any date; TMPV because its
  futures begin 2025-10-24, outside DEV entirely.
- **Within-day resolution order** assumes a day touching both stop and target resolves as
  the LOSS, which biases results downward.
- **The F&O ban check is a stub.** A symbol in ban has no new-position capacity and
  should not be rankable; that gate is not yet implemented.
- **Prices are split/bonus adjusted**, so percentage returns are correct but absolute
  prices and share counts are not what would have been seen at the time.

---

*Written by `scripts/fno_evaluation.py`.*
"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    log.info("wrote %s", out)
    print(f"\nWrote {out}")
    for name, a in (("primary", primary), ("power  ", power)):
        print(f"  {name}: trades {a['trades']}, gross {a['gross_mean_pct']}%, "
              f"net {a['net_mean_pct']}%, excess {a['excess_mean_pct']}% "
              f"CI(eff) {a['excess_ci_eff']}, n_eff {a['effective_n']}")
    print(f"  verdict: {'CLEARS ZERO' if clears else 'DOES NOT CLEAR ZERO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
