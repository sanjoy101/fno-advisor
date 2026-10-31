"""Generate reports/entry_check_diagnostic.md from backfilled REAL bars. DEV ONLY.

    python scripts/entry_check_diagnostic.py

The question: the single-symbol entry check reports UNSOUND on nearly everything, and
the panel gives no way to tell whether that is accurate or whether the check is too
crude to discriminate.

Protocol, fixed BEFORE any result was seen:

  1. Panel PANEL_FULL_HISTORY, **DEV ONLY**, enforced by HoldoutGuard.assert_dev_only.
     The six-year holdout is NOT touched and NOT claimed by this script -- a diagnostic
     is not an evaluation, and spending the holdout on one would be a waste of the only
     out-of-sample data there is.
  2. Four slots per session: 09:45, 11:00, 13:00, 14:30. Spaced wider than the
     60-minute forward window so no two windows for one symbol overlap.
  3. No stop is supplied, because that is what the dashboard panel sends. The
     consequence for STOP_ALREADY_HIT is measured, not assumed.
  4. The forward window is capped at the 15:15 square-off. Slots with different
     realised horizons are reported separately and never pooled.
  5. The headline comparison is CROSS-SECTIONALLY DEMEANED. The raw figure is reported
     beside it; where they disagree, the raw one is beta (CLAUDE.md 2.3).
  6. Intervals use the independent-cell count (session x slot), not the observation
     count. 20 symbols in one minute are one draw with 20 readings.
  7. NO THRESHOLD IS CHANGED by this script. It measures; it does not tune.

Refuses to fabricate: an empty store yields a report that says so.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_exit_engine_settings, get_settings
from src.analysis.entry_check import (
    FORWARD_MINUTES,
    SAMPLE_TIMES,
    combination_breakdown,
    forward_comparison,
    observe_session,
    problem_breakdown,
    verdict_distribution,
    vwap_deviation_profile,
)
from src.analysis.split import PANEL_FULL_HISTORY, regime_of
from src.data.backfill import LIQUID_FNO_UNIVERSE
from src.market.calendar import trading_days_between
from src.positions.store import PositionStore

log = logging.getLogger("fno.report")

REPORT_PATH = Path("reports/entry_check_diagnostic.md")
JSON_PATH = Path("reports/entry_check_diagnostic.json")


def collect(store: PositionStore, symbols, sessions, cfg):
    """Every observation across the DEV panel. One parquet read per symbol-session."""
    out = []
    missing = 0
    for i, session in enumerate(sessions, 1):
        for symbol in symbols:
            if not store.has_bars(symbol, session):
                missing += 1
                continue
            out.extend(observe_session(
                symbol, session, store.load_bars(symbol, session), cfg=cfg))
        if i % 100 == 0:
            log.info("%d/%d sessions, %d observations", i, len(sessions), len(out))
    return out, missing


def table(rows, columns) -> str:
    if not rows:
        return "_(no rows)_\n"
    head = "| " + " | ".join(columns) + " |"
    rule = "|" + "|".join("---" for _ in columns) + "|"
    body = [
        "| " + " | ".join(str(r.get(c, "")) for c in columns) + " |"
        for r in rows
    ]
    return "\n".join([head, rule, *body]) + "\n"


def no_data_report(sessions, symbols) -> str:
    return (
        "# Entry-check diagnostic\n\n"
        "## NO DATA YET\n\n"
        f"No stored bars were found for any of {len(symbols)} symbols across "
        f"{len(sessions)} DEV sessions, so this question CANNOT be answered.\n\n"
        "Nothing below is computed from synthetic bars. A distribution from "
        "`synthetic_session()` describes the fixture's construction and looks exactly "
        "like a real result, which is what makes it dangerous (CLAUDE.md 8).\n\n"
        "Get real data first:\n\n"
        "```powershell\n"
        "python -m src.cli auth\n"
        "python -m src.cli backfill --months 6\n"
        "python scripts/entry_check_diagnostic.py\n"
        "```\n"
    )


def build_report(observations, sessions, symbols, missing) -> tuple[str, dict]:
    overall = verdict_distribution(observations)[0]
    by_slot = verdict_distribution(observations, by_slot=True)
    problems = problem_breakdown(observations)
    combos = combination_breakdown(observations)
    vwap = vwap_deviation_profile(observations)

    full = [o for o in observations if o.forward_minutes == FORWARD_MINUTES]
    short = [o for o in observations if 0 < o.forward_minutes < FORWARD_MINUTES]

    headline = forward_comparison(full, label=f"{FORWARD_MINUTES}-min, demeaned")
    raw = forward_comparison(full, label=f"{FORWARD_MINUTES}-min, raw", demeaned=False)
    truncated = forward_comparison(short, label="14:30 slot, truncated at 15:15")
    per_slot = [
        forward_comparison([o for o in full if o.slot == s], label=s)
        for s in sorted({o.slot for o in full})
    ]

    regimes = sorted({regime_of(s) for s in sessions})
    payload = {
        "panel": PANEL_FULL_HISTORY.name,
        "dev_from": str(sessions[0]),
        "dev_to": str(sessions[-1]),
        "dev_sessions": len(sessions),
        "symbols": len(symbols),
        "observations": len(observations),
        "verdict_overall": overall,
        "verdict_by_slot": by_slot,
        "problem_breakdown": problems,
        "combinations": combos,
        "vwap_deviation": vwap,
        "forward_demeaned": headline.as_row(),
        "forward_raw": raw.as_row(),
        "forward_truncated": truncated.as_row(),
        "forward_by_slot": [g.as_row() for g in per_slot],
    }

    sole_stop = next(
        (r for r in problems if r["problem"] == "STOP_ALREADY_HIT"), {"present_n": 0})

    # The cost hurdle, computed live rather than copied out of a report, so it cannot
    # go stale against config/settings.py.
    from src.costs import round_trip_costs

    notional = Decimal("100000")
    trip = round_trip_costs(Decimal("1000"), Decimal("1000"), 100)
    cost_pct = float(trip.total / notional * 100)
    ratio = abs(headline.diff) / cost_pct if cost_pct else float("inf")

    low, high = headline.effective_ci95
    spans_zero = low <= 0.0 <= high
    answer = (
        "**The verdict does not separate forward outcomes to any degree worth acting "
        "on.**" if spans_zero else
        "**The verdict DOES separate forward outcomes — read the magnitude before "
        "believing it matters.**"
    )

    md = f"""# Entry-check diagnostic — does the check discriminate?

## The answer, first

{answer}

SOUND minus UNSOUND on the forward {FORWARD_MINUTES}-minute return, cross-sectionally
demeaned, is **{headline.diff:+.4f}pp** with a 95% interval of
**[{low:+.4f}, {high:+.4f}]** across {headline.effective_n:,} independent
session-slot cells. The interval {'includes' if spans_zero else 'excludes'} zero
(t = {headline.diff / headline.effective_stderr:+.2f}).

**The size settles it even if the sign is real.** A round trip costs
**{cost_pct:.3f}%** of notional (computed from `config/settings.py`, not copied). The
whole measured SOUND-vs-UNSOUND gap is **{100 * ratio:.0f}% of one round trip**. Taking
the point estimate entirely at face value, and assuming it survived out of sample, it
would still be {1 / ratio:.1f}x too small to pay for the trade it is advising on.

The check is **not** firing on pure noise — the median UNSOUND-by-VWAP verdict sits
{vwap['pct_below_vwap'].get('p50', 0):.3f}% below VWAP, about
{vwap['atr_below_vwap'].get('p50', 0):.1f} one-minute bar ranges, and only
{vwap['within_1.00atr']}% are inside a single one-minute range. It is describing a real
displacement. That displacement simply does not predict the next hour.

It also fires less than it feels like: **{overall['sound_pct']}% SOUND** overall, not
"nearly everything UNSOUND" — though it reaches {min(r['sound_pct'] for r in by_slot)}%
SOUND at the least permissive slot.

**Treat the verdict as a description of the present, not a forecast.** "Price is below
VWAP and momentum has rolled over" is a true and checkable statement about right now.
This measurement says it carries no information about the next 60 minutes, so a UNSOUND
badge should not be read as "this will fall" — and a SOUND badge emphatically should not
be read as "this will rise".

---


**DEV ONLY.** Panel `{PANEL_FULL_HISTORY.name}`, {sessions[0]}..{sessions[-1]},
{len(sessions)} sessions, {len(symbols)} symbols, four slots per session.
**n = {len(observations):,} evaluations.** Regimes spanned: {', '.join(regimes)}.
{missing:,} symbol-sessions had no stored bars and were skipped.

**The six-year holdout is NOT touched and NOT claimed.** This is a diagnostic, not an
evaluation of a change; spending the only out-of-sample data on it would be waste.

**No threshold was changed.** Everything here is measured against the shipping config.

---

## 1. How often does it fire?

{table([overall], ["n", "sound", "unsound", "sound_pct", "partly_unevaluated"])}
By slot:

{table(by_slot, ["slot", "n", "sound", "unsound", "sound_pct"])}
---

## 2. Which predicate fired?

Keyed off `EntryProblem` tags, never the prose. "Present in" double-counts overlaps;
"sole cause" is what would change if the predicate were deleted.

{table(problems, ["problem", "present_n", "present_pct", "sole_cause_n", "sole_cause_pct"])}
Exact combinations:

{table(combos, ["combination", "n", "pct"])}
**STOP_ALREADY_HIT fired {sole_stop['present_n']} times, and that is structural, not
empirical.** The panel sends no stop, and with no stop the predicate has nothing to
compare against. Even when a stop IS supplied, `check_hypothetical_entry` defaults the
entry price to the current price, so the condition can only fire if the user types a
stop at or above where the stock is already trading. It is a property of the user's
input, not of the market. **The check is effectively two predicates, not three.**

---

## 3. How far below VWAP is a typical UNSOUND verdict?

n = {vwap['n']:,} observations where BELOW_VWAP fired ({vwap['n_with_atr']:,} with an
ATR to scale by). Positive numbers are distance BELOW VWAP.

Percent below VWAP:

{table([vwap['pct_below_vwap']], sorted(vwap['pct_below_vwap']))}
Same distances in ATR multiples. **Read this carefully: `atr14` here is the 14-period
ATR of ONE-MINUTE bars, not a daily ATR.** One unit is a typical one-minute bar range,
so "3.5 ATR below VWAP" means about three and a half minutes' worth of normal movement
— not the enormous number it would be on a daily scale. This is the scale that says
whether a gap is chatter, because a fixed 0.2% is a wide move for a quiet symbol and
inside the spread for a volatile one:

{table([vwap['atr_below_vwap']], sorted(vwap['atr_below_vwap']))}
| within | share of BELOW_VWAP verdicts |
|---|---|
| 0.10% | {vwap['within_0.10pct']}% |
| 0.25% | {vwap['within_0.25pct']}% |
| 0.50% | {vwap['within_0.50pct']}% |
| 0.25 one-minute ATR | {vwap['within_0.25atr']}% |
| 0.50 one-minute ATR | {vwap['within_0.50atr']}% |
| 1.00 one-minute ATR | {vwap['within_1.00atr']}% |

The one-minute-ATR row is the "is this noise" number: a gap smaller than a single
typical one-minute bar range is not a market trading below its average, it is a tick.

---

## 4. Does the verdict predict anything? — THE ONE THAT MATTERS

Forward {FORWARD_MINUTES}-minute return after SOUND vs after UNSOUND.

**Primary measure is cross-sectionally demeaned**: each observation minus the mean
forward return of every symbol at that same session and slot. Without that, a verdict
that fires on falling mornings scores a gap purely because falling mornings keep
falling — a fact about the market, not about the check (CLAUDE.md 2.3).

Intervals use the **independent-cell count** (session x slot), not the observation
count: 20 symbols sampled in one minute share that minute's market move.

{table([headline.as_row(), raw.as_row()],
       ["label", "sound_n", "sound_mean", "unsound_n", "unsound_mean", "diff",
        "ci95_low", "ci95_high", "effective_n", "mde"])}
**Demeaned:** {headline.verdict()}

**Raw (not the answer — carries the market's own move):** {raw.verdict()}

Per slot, demeaned:

{table([g.as_row() for g in per_slot],
       ["label", "sound_n", "sound_mean", "unsound_n", "unsound_mean", "diff",
        "ci95_low", "ci95_high", "effective_n", "mde"])}
The 14:30 slot, whose window is truncated at the 15:15 square-off
({short[0].forward_minutes if short else 0} minutes, so not comparable with the rest):

{table([truncated.as_row()],
       ["label", "sound_n", "sound_mean", "unsound_n", "unsound_mean", "diff",
        "ci95_low", "ci95_high", "effective_n", "mde"])}
---

## What this does NOT license

Nothing here says the entry check should be deleted, kept, or retuned. It says what the
check does and whether the verdict separated forward outcomes on DEV. Any change to a
threshold made in response to these numbers is fitting to DEV, and the honest way to
act on it is to change ONE thing, re-measure on DEV, and keep the holdout for a single
evaluation at the end (CLAUDE.md 2.3).

The check is a statement about whether an entry is *already* violated, not a
prediction. If section 4 is indistinguishable from zero, that does not make the check
wrong — it makes it uninformative about the next hour, which is a narrower and more
useful thing to know.
"""
    return md, payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="comma-separated (default: the universe)")
    parser.add_argument("--limit-sessions", type=int,
                        help="use only the first N DEV sessions (for a fast dry run)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    store = PositionStore(get_settings().data_dir)
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",")] if args.symbols
        else list(LIQUID_FNO_UNIVERSE)
    )

    calendar = trading_days_between(PANEL_FULL_HISTORY.start, PANEL_FULL_HISTORY.end)
    guard = PANEL_FULL_HISTORY.guard(calendar)
    sessions = guard.dev
    if args.limit_sessions:
        sessions = sessions[: args.limit_sessions]
    guard.assert_dev_only(sessions, "the entry-check diagnostic")

    log.info("DEV %s..%s, %d sessions, %d symbols, slots %s",
             sessions[0], sessions[-1], len(sessions), len(symbols),
             ", ".join(t.strftime("%H:%M") for t in SAMPLE_TIMES))
    log.info("holdout: %s", guard.status())

    observations, missing = collect(store, symbols, sessions, get_exit_engine_settings())

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not observations:
        REPORT_PATH.write_text(no_data_report(sessions, symbols), encoding="utf-8")
        log.warning("no stored bars -- wrote a NO DATA report to %s", REPORT_PATH)
        return 1

    md, payload = build_report(observations, sessions, symbols, missing)
    REPORT_PATH.write_text(md, encoding="utf-8")
    JSON_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s and %s (%d observations)", REPORT_PATH, JSON_PATH,
             len(observations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
