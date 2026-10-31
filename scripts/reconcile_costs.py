"""Reconcile the modelled cost rates against a REAL broker contract note.

Feed it the figures off a contract note; it prints predicted vs actual vs delta,
line by line, and diagnoses *which rule* is wrong on any line that does not match.

    python scripts/reconcile_costs.py --type intraday ^
        --buy-value 100000 --sell-value 100500 ^
        --actual-brokerage 20.00 --actual-stt 25.00 --actual-exchange 5.96 ^
        --actual-sebi 0.20 --actual-stamp 3.00 --actual-ipft 0.20 ^
        --actual-gst 4.75 --actual-total 59.11 ^
        --trade-date 2026-08-19

With no ``--actual-*`` figures it prints the model's prediction alone, which is
useful for eyeballing but is **not** a reconciliation and says so.

Three rules this script exists to enforce
-----------------------------------------

**Slippage is excluded from the comparison.** A contract note has no slippage line,
because slippage is not a charge -- it is a modelled assumption about fill quality.
Comparing ``LegCosts.total`` (which includes it) against the note's TOTAL would show
a large spurious delta, and the tempting fix would be to bend a statutory rate to
absorb it. That would be two errors that cancel. The comparison is against
brokerage + statutory only.

**A rate error and a rounding error are different findings.** Contract notes round
STT and stamp duty to the nearest RUPEE; this model rounds every line to paise. A
sub-rupee delta on those lines is a rounding-rule difference, and "fixing" the rate
to absorb it would corrupt a rate that was already correct. Each mismatched line is
therefore tested against both hypotheses and labelled.

**Nothing here writes to config, and nothing here sets the verified flag.** It
reports. A human reads the report and decides. See CLAUDE.md section 1.4.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import CostSettings, get_cost_settings  # noqa: E402
from src.costs import (  # noqa: E402
    LegCosts,
    buy_leg_costs,
    delivery_buy_leg_costs,
    delivery_sell_leg_costs,
    sell_leg_costs,
)

PAISA = Decimal("0.01")
RUPEE = Decimal("1")
TOLERANCE = Decimal("0.01")  # a line matches if it is within one paisa


def _money(v: Decimal) -> Decimal:
    return v.quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass
class Line:
    """One row of the comparison."""

    name: str
    predicted: Decimal
    actual: Decimal | None
    #: Turnover this charge was levied on, for backing out the implied rate.
    base: Decimal
    #: The configured rate, where the line is a simple percentage of ``base``.
    rate: Decimal | None
    #: Name of the CostSettings field, so a correction names the thing to change.
    field: str | None

    @property
    def delta(self) -> Decimal | None:
        if self.actual is None:
            return None
        return _money(self.actual - self.predicted)

    @property
    def matches(self) -> bool:
        d = self.delta
        return d is not None and abs(d) <= TOLERANCE

    def implied_rate(self) -> Decimal | None:
        """The rate the actual figure implies, given the base it was levied on."""
        if self.actual is None or not self.base:
            return None
        return self.actual / self.base


def _fmt_rate(rate: Decimal | None) -> str:
    """Render a rate the way the exchange quotes it.

    Tiny levies (SEBI, IPFT) are published as rupees per crore, and reading
    0.000001 as a percentage is how they get mis-entered by a factor of 100.
    """
    if rate is None:
        return "--"
    pct = rate * 100
    if rate != 0 and abs(rate) < Decimal("0.00001"):
        per_crore = rate * Decimal("10000000")
        return f"{pct:.7f}%  (Rs {per_crore:.2f}/crore)"
    return f"{pct:.5f}%"


def build_lines(
    trade_type: str,
    buy_value: Decimal,
    sell_value: Decimal,
    actuals: dict[str, Decimal | None],
    cfg: CostSettings,
) -> tuple[list[Line], LegCosts, LegCosts]:
    """Predict each line for the given trade, and pair it with the actual."""
    if trade_type == "intraday":
        buy = buy_leg_costs(buy_value, 1, cfg)
        sell = sell_leg_costs(sell_value, 1, cfg)
        stt_rate, stt_base = cfg.stt_equity_intraday_sell, sell_value
        stamp_rate = cfg.stamp_duty_equity_buy
        stt_field = "stt_equity_intraday_sell"
        stamp_field = "stamp_duty_equity_buy"
    else:
        buy = delivery_buy_leg_costs(buy_value, 1, cfg)
        sell = delivery_sell_leg_costs(sell_value, 1, cfg)
        stt_rate, stt_base = cfg.stt_delivery_each_side, buy_value + sell_value
        stamp_rate = cfg.stamp_duty_delivery_buy
        stt_field = "stt_delivery_each_side"
        stamp_field = "stamp_duty_delivery_buy"

    both = buy_value + sell_value
    lines = [
        Line("Brokerage", _money(buy.brokerage + sell.brokerage), actuals["brokerage"],
             both, None, "brokerage_per_order / brokerage_per_delivery_order"),
        Line("STT", _money(buy.stt + sell.stt), actuals["stt"],
             stt_base, stt_rate, stt_field),
        Line("Exchange txn chg", _money(buy.exchange_txn + sell.exchange_txn),
             actuals["exchange"], both, cfg.exchange_txn_equity, "exchange_txn_equity"),
        Line("SEBI fees", _money(buy.sebi_fee + sell.sebi_fee), actuals["sebi"],
             both, cfg.sebi_turnover_fee, "sebi_turnover_fee"),
        Line("Stamp duty", _money(buy.stamp_duty + sell.stamp_duty), actuals["stamp"],
             buy_value, stamp_rate, stamp_field),
        Line("IPFT", _money(buy.ipft + sell.ipft), actuals["ipft"],
             both, cfg.ipft_charge, "ipft_charge"),
        Line("GST", _money(buy.gst + sell.gst), actuals["gst"],
             both, None, "gst_rate"),
    ]
    return lines, buy, sell


def diagnose(line: Line, cfg: CostSettings, buy: LegCosts, sell: LegCosts) -> list[str]:
    """Explain a mismatched line: rate wrong, rounding wrong, or base wrong."""
    notes: list[str] = []
    if line.actual is None or line.matches:
        return notes

    # Hypothesis 1: the rate is right but the note rounds to the nearest rupee.
    if line.rate is not None:
        raw = line.base * line.rate
        if abs(line.actual - raw.quantize(RUPEE, rounding=ROUND_HALF_UP)) <= TOLERANCE:
            notes.append(
                f"ROUNDING, not rate: actual == raw {raw:.4f} rounded to the nearest "
                f"RUPEE. The configured rate {_fmt_rate(line.rate)} is CORRECT. "
                f"Do not change it -- change how this line is rounded."
            )
            return notes

    # Hypothesis 2: the rate itself is wrong.
    implied = line.implied_rate()
    if implied is not None and line.rate is not None:
        notes.append(
            f"RATE: implied {_fmt_rate(implied)} vs configured {_fmt_rate(line.rate)}"
        )
        if line.rate:
            notes.append(f"       ratio actual/predicted = {line.actual / line.predicted:.4f}"
                         if line.predicted else "       predicted is zero")

    # Hypothesis 3 (GST only): the base is wrong, not the rate.
    if line.name == "GST":
        notes.extend(_diagnose_gst_base(line, cfg, buy, sell))

    if line.name == "Brokerage":
        notes.append(
            "Brokerage is a BROKER term, not a statutory rate -- it cannot be "
            "derived, only read off the note. Check both the per-order amount and "
            "the percentage-of-turnover cap, and whether the cap applies per order "
            "or per leg."
        )
    return notes


def _diagnose_gst_base(line: Line, cfg: CostSettings, buy: LegCosts,
                       sell: LegCosts) -> list[str]:
    """Find which GST base reproduces the actual figure.

    GST is 18% almost everywhere; when the GST line is off it is nearly always the
    BASE that differs, not the rate. Brokers disagree about whether the IPFT and the
    SEBI fee sit inside it.
    """
    b, s = buy, sell
    candidates = {
        "brokerage + exchange + SEBI + IPFT  (as configured)":
            (b.brokerage + b.exchange_txn + b.sebi_fee + b.ipft)
            + (s.brokerage + s.exchange_txn + s.sebi_fee + s.ipft),
        "brokerage + exchange + SEBI  (IPFT excluded)":
            (b.brokerage + b.exchange_txn + b.sebi_fee)
            + (s.brokerage + s.exchange_txn + s.sebi_fee),
        "brokerage + exchange  (SEBI and IPFT excluded)":
            (b.brokerage + b.exchange_txn) + (s.brokerage + s.exchange_txn),
        "brokerage only":
            b.brokerage + s.brokerage,
        "brokerage + exchange + SEBI + IPFT + stamp duty":
            (b.brokerage + b.exchange_txn + b.sebi_fee + b.ipft + b.stamp_duty)
            + (s.brokerage + s.exchange_txn + s.sebi_fee + s.ipft + s.stamp_duty),
    }
    out = []
    for label, base in candidates.items():
        if abs(_money(base * cfg.gst_rate) - line.actual) <= TOLERANCE:
            out.append(f"BASE: actual == 18% of [{label}] = {_money(base * cfg.gst_rate)}")
    if not out and line.actual:
        implied_base = line.actual / cfg.gst_rate
        out.append(f"No standard base reproduces it. At 18%, the implied base is "
                   f"Rs {implied_base:.2f} -- work out what sums to that.")
    return out


def render(trade_type: str, buy_value: Decimal, sell_value: Decimal,
           lines: list[Line], buy: LegCosts, sell: LegCosts,
           actual_total: Decimal | None, cfg: CostSettings,
           trade_date: str | None) -> tuple[str, bool]:
    """Build the report. Returns (text, reconciled)."""
    has_actuals = any(ln.actual is not None for ln in lines)
    w = []
    w.append("=" * 78)
    w.append("COST MODEL RECONCILIATION" if has_actuals
             else "COST MODEL PREDICTION  (no contract note supplied)")
    w.append("=" * 78)
    w.append(f"Trade type   : {trade_type}")
    w.append(f"Trade date   : {trade_date or 'NOT STATED'}")
    w.append(f"Buy value    : Rs {buy_value:,.2f}")
    w.append(f"Sell value   : Rs {sell_value:,.2f}")
    w.append(f"Config       : rates_verified={cfg.rates_verified}")
    w.append("")

    if not has_actuals:
        w.append("No --actual-* figures given, so NOTHING IS RECONCILED. This is the")
        w.append("model's prediction only. Supply the contract note figures to compare.")
        w.append("")

    head = f"{'Line':<20}{'Predicted':>13}{'Actual':>13}{'Delta':>13}   Status"
    w.append(head)
    w.append("-" * 78)

    all_match = True
    for ln in lines:
        actual = f"{ln.actual:,.2f}" if ln.actual is not None else "--"
        delta = f"{ln.delta:+,.2f}" if ln.delta is not None else "--"
        if ln.actual is None:
            status = "no actual"
            all_match = False
        elif ln.matches:
            status = "OK"
        else:
            status = "MISMATCH"
            all_match = False
        w.append(f"{ln.name:<20}{ln.predicted:>13,.2f}{actual:>13}{delta:>13}   {status}")

    w.append("-" * 78)
    charged = sum(ln.predicted for ln in lines)
    w.append(f"{'TOTAL (charges)':<20}{charged:>13,.2f}"
             f"{(f'{actual_total:,.2f}' if actual_total is not None else '--'):>13}"
             f"{(f'{actual_total - charged:+,.2f}' if actual_total is not None else '--'):>13}")
    w.append("")

    slippage = _money(buy.slippage + sell.slippage)
    w.append(f"Modelled slippage, EXCLUDED from the comparison : Rs {slippage:,.2f}")
    w.append("  A contract note has no slippage line. Slippage is an assumption about")
    w.append("  fill quality, not a charge. It still applies to every P&L figure.")
    w.append("")

    mismatched = [ln for ln in lines if ln.actual is not None and not ln.matches]
    if mismatched:
        w.append("DIAGNOSIS")
        w.append("-" * 78)
        for ln in mismatched:
            w.append(f"* {ln.name}  (config: {ln.field})")
            for note in diagnose(ln, cfg, buy, sell):
                w.append(f"    {note}")
            w.append("")

    if actual_total is not None and all_match:
        residual = _money(actual_total - charged)
        if abs(residual) > TOLERANCE:
            w.append("!! Every line matches but the TOTAL does not, by "
                     f"Rs {residual:+,.2f}.")
            w.append("   The note carries a charge this model does not have a line for.")
            w.append("   Find out what it is. Do NOT spread it across the other rates.")
            all_match = False

    w.append("=" * 78)
    if not has_actuals:
        w.append("VERDICT: nothing reconciled -- no contract note figures were supplied.")
    elif all_match:
        w.append("VERDICT: every line reconciles. COST_RATES_VERIFIED may be set true,")
        w.append(f"         recording trade date {trade_date or '(STATE IT)'}.")
    else:
        w.append("VERDICT: NOT reconciled. Correct the rules named above, re-run, and")
        w.append("         leave COST_RATES_VERIFIED=false until every line is OK.")
    w.append("=" * 78)
    return "\n".join(w), (has_actuals and all_match)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--type", dest="trade_type", required=True,
                   choices=["intraday", "delivery", "fno"],
                   help="intraday equity, delivery (CNC), or F&O")
    p.add_argument("--buy-value", type=Decimal, required=True, help="buy turnover, Rs")
    p.add_argument("--sell-value", type=Decimal, required=True, help="sell turnover, Rs")
    p.add_argument("--trade-date", help="the note's trade date, YYYY-MM-DD")
    for name in ("brokerage", "stt", "exchange", "sebi", "stamp", "ipft", "gst", "total"):
        p.add_argument(f"--actual-{name}", type=Decimal, default=None,
                       help=f"{name} as charged on the note")
    p.add_argument("--report", help="also write the report to this markdown path")
    args = p.parse_args(argv)

    if args.trade_type == "fno":
        print(
            "F&O is not implemented in src/costs.py.\n\n"
            "CostSettings carries futures/options rates but NO code consumes them, and\n"
            "assert_equity_intraday() refuses the segment on purpose. Options STT is\n"
            "levied on PREMIUM rather than notional, so reusing the equity path would\n"
            "silently understate cost.\n\n"
            "Reconciling an F&O note means BUILDING that code path first. Supply an\n"
            "equity note, or say to build the F&O model.",
            file=sys.stderr,
        )
        return 2

    if args.trade_date:
        date.fromisoformat(args.trade_date)  # fail loudly on a malformed date

    cfg = get_cost_settings()
    actuals = {
        "brokerage": args.actual_brokerage, "stt": args.actual_stt,
        "exchange": args.actual_exchange, "sebi": args.actual_sebi,
        "stamp": args.actual_stamp, "ipft": args.actual_ipft, "gst": args.actual_gst,
    }
    lines, buy, sell = build_lines(
        args.trade_type, args.buy_value, args.sell_value, actuals, cfg
    )
    text, reconciled = render(
        args.trade_type, args.buy_value, args.sell_value, lines, buy, sell,
        args.actual_total, cfg, args.trade_date,
    )
    print(text)

    if args.report:
        Path(args.report).write_text(f"```\n{text}\n```\n", encoding="utf-8")
        print(f"\nwritten: {args.report}")

    return 0 if reconciled else 1


if __name__ == "__main__":
    raise SystemExit(main())
