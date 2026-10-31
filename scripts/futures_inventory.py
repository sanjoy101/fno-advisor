"""Inventory of the F&O data actually on disk. Report BEFORE anything is built on it.

    python scripts/futures_inventory.py
    python scripts/futures_inventory.py --report reports/futures_inventory.md

Covers what the bhavcopy backfill provides, which is everything except 1-minute bars:
the expiry calendar derived from the exchange's own daily file, per-underlying
symbol-day counts, every roll with its measured gap, daily OI coverage, EMPTY days per
date, and a comparison of futures history against the spot history already stored.

It reports; it does not fix, adjust or interpolate anything.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from src.data.backfill import LIQUID_FNO_UNIVERSE  # noqa: E402
from src.data.bhavcopy import STOCK_FUTURE_TYPES, BhavcopyStore  # noqa: E402
from src.data.futures import (  # noqa: E402
    ROLL_DAYS_BEFORE_EXPIRY,
    build_continuous,
    expiry_calendar,
)


def spot_coverage(data_dir: Path) -> dict[str, tuple[int, date, date]]:
    """(symbol-days, first, last) of stored 1-minute EQUITY bars, from the layout."""
    out = {}
    bars = data_dir / "raw" / "bars"
    if not bars.exists():
        return out
    for sym_dir in bars.glob("symbol=*"):
        days = sorted(d.name.split("=", 1)[1] for d in sym_dir.glob("trade_date=*"))
        if days:
            out[sym_dir.name.split("=", 1)[1]] = (
                len(days), date.fromisoformat(days[0]), date.fromisoformat(days[-1]))
    return out


def load_all(store: BhavcopyStore, universe: set[str]) -> dict[date, list]:
    """Stock-future rows for the universe only.

    Filtered inside the parser, not after: a day's file is ~31,000 rows of which ~400
    are stock futures, so parsing everything and discarding built ~51 million objects
    across the archive and did not finish in ten minutes.
    """
    rows_by_date: dict[date, list] = {}
    for path in sorted((store.raw_dir).rglob("fo_*.zip")):
        day = date.fromisoformat(
            f"{path.stem[3:7]}-{path.stem[7:9]}-{path.stem[9:11]}")
        try:
            rows = store.load(day, instruments=STOCK_FUTURE_TYPES) or []
            rows_by_date[day] = [r for r in rows if r.symbol in universe]
        except Exception as exc:  # noqa: BLE001 - reported, never silently dropped
            print(f"  !! unreadable {path.name}: {type(exc).__name__}: {exc}")
    return rows_by_date


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=None)
    p.add_argument("--report", help="also write markdown here")
    args = p.parse_args(argv)

    data_dir = Path(args.data_dir or get_settings().data_dir)
    store = BhavcopyStore(data_dir)
    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    say("# F&O data inventory")
    say()
    rows_by_date = load_all(store, set(LIQUID_FNO_UNIVERSE))
    if not rows_by_date:
        say("No bhavcopy data on disk. Run scripts/bhavcopy_backfill.py first.")
        return 1

    days = sorted(rows_by_date)
    say(f"Bhavcopy files parsed : {len(days)}")
    say(f"Date range            : {days[0]} .. {days[-1]}")
    say(f"Roll rule             : roll {ROLL_DAYS_BEFORE_EXPIRY} trading day(s) "
        f"before expiry (contract's last day is the session before its expiry)")
    say()

    universe = list(LIQUID_FNO_UNIVERSE)
    calendar = expiry_calendar(rows_by_date)

    # ---- duplicates / integrity ------------------------------------------------
    say("## Integrity")
    say()
    dupes = 0
    for day, rows in rows_by_date.items():
        seen = Counter((r.symbol, r.expiry) for r in rows)
        dupes += sum(c - 1 for c in seen.values() if c > 1)
    say(f"- duplicate (date, symbol, expiry) rows: **{dupes}**"
        + ("" if dupes else "  — none, so no quarantine was needed"))
    say(f"- dates out of order in the archive: **0** (files are read date-sorted)")
    say()

    # ---- EMPTY days per date ---------------------------------------------------
    say("## EMPTY days per date (universe underlyings with no futures row)")
    say()
    empty_by_date: dict[date, list[str]] = {}
    for day, rows in rows_by_date.items():
        present = {r.symbol for r in rows}
        absent = [s for s in universe if s not in present]
        if absent:
            empty_by_date[day] = absent
    if not empty_by_date:
        say("None. Every universe underlying has a futures row on every stored date.")
    else:
        say(f"{len(empty_by_date)} dates have at least one underlying missing.")
        say()
        say("| date | n missing | symbols |")
        say("|---|---:|---|")
        for day in sorted(empty_by_date)[:40]:
            syms = empty_by_date[day]
            say(f"| {day} | {len(syms)} | {', '.join(syms[:8])}"
                f"{' …' if len(syms) > 8 else ''} |")
        if len(empty_by_date) > 40:
            say(f"| … | | {len(empty_by_date) - 40} more dates not listed |")
    say()

    # ---- per-underlying coverage ------------------------------------------------
    say("## Coverage per underlying")
    say()
    spot = spot_coverage(data_dir)
    say("| underlying | fut days | fut first | fut last | spot days | spot first | "
        "shortfall |")
    say("|---|---:|---|---|---:|---|---|")
    shortfalls = []
    for sym in universe:
        fdays = sorted(d for d, rows in rows_by_date.items()
                       if any(r.symbol == sym for r in rows))
        s = spot.get(sym)
        if not fdays:
            say(f"| {sym} | **0** | — | — | {s[0] if s else 0} | "
                f"{s[1] if s else '—'} | **NO FUTURES AT ALL** |")
            shortfalls.append((sym, "no futures listed"))
            continue
        note = ""
        if s:
            gap = (fdays[0] - s[1]).days
            if gap > 30:
                note = f"**futures start {gap}d later**"
                shortfalls.append((sym, f"futures history starts {gap} days after spot"))
            elif s[0] - len(fdays) > 30:
                note = f"{s[0] - len(fdays)} fewer days"
        say(f"| {sym} | {len(fdays)} | {fdays[0]} | {fdays[-1]} | "
            f"{s[0] if s else 0} | {s[1] if s else '—'} | {note} |")
    say()

    # ---- expiry calendar ---------------------------------------------------------
    say("## Expiry calendar (from the exchange's own daily file)")
    say()
    ref = calendar.get("RELIANCE", [])
    traded = set(days)
    weekday_counts = Counter(e.strftime("%A") for e in ref if e <= days[-1])
    say(f"Distinct RELIANCE expiries observed: **{len(ref)}**")
    say()
    say("Expiry weekday by era — NSE changed this more than once, which is why the")
    say("calendar is derived rather than assumed:")
    say()
    say("| weekday | count |")
    say("|---|---:|")
    for wd, n in weekday_counts.most_common():
        say(f"| {wd} | {n} |")
    say()
    non_trading = [e for e in ref if e <= days[-1] and e not in traded]
    say(f"Expiry dates that are NOT in the traded-day set: **{len(non_trading)}**"
        + (f" — {', '.join(str(d) for d in non_trading[:10])}" if non_trading else
           " (every expiry fell on a session the exchange ran)"))
    say()

    # ---- rolls -------------------------------------------------------------------
    say("## Rolls and gap sizes")
    say()
    say(f"Roll rule: **{ROLL_DAYS_BEFORE_EXPIRY} trading day(s) before expiry**. "
        f"Prices below are RAW; nothing is back-adjusted.")
    say()
    all_gaps = []
    per_symbol_rolls = {}
    for sym in universe:
        bars, rolls = build_continuous(rows_by_date, sym)
        per_symbol_rolls[sym] = (bars, rolls)
        all_gaps.extend(abs(r.gap_pct) for r in rolls if r.gap_pct == r.gap_pct)
    say("| underlying | continuous days | rolls | mean abs gap % | max abs gap % |")
    say("|---|---:|---:|---:|---:|")
    for sym in universe:
        bars, rolls = per_symbol_rolls[sym]
        gaps = [abs(r.gap_pct) for r in rolls if r.gap_pct == r.gap_pct]
        say(f"| {sym} | {len(bars)} | {len(rolls)} | "
            f"{statistics.mean(gaps):.3f} | {max(gaps):.3f} |" if gaps
            else f"| {sym} | {len(bars)} | 0 | — | — |")
    say()
    if all_gaps:
        say(f"Across all underlyings: **{len(all_gaps)} rolls**, mean absolute gap "
            f"**{statistics.mean(all_gaps):.3f}%**, median "
            f"{statistics.median(all_gaps):.3f}%, max {max(all_gaps):.3f}%.")
        say()
        say("A roll gap is a real price discontinuity between two different")
        say("instruments. Any return computed across a roll boundary without")
        say("accounting for it is measuring the roll, not the market.")
    say()

    # ---- every roll for one reference underlying ---------------------------------
    bars, rolls = per_symbol_rolls.get("RELIANCE", ([], []))
    if rolls:
        say("### Every roll date, RELIANCE (reference)")
        say()
        say("| roll date | from expiry | to expiry | from close | to close | gap | gap % |")
        say("|---|---|---|---:|---:|---:|---:|")
        for r in rolls:
            say(f"| {r.on_date} | {r.from_expiry} | {r.to_expiry} | {r.from_close:.2f} "
                f"| {r.to_close:.2f} | {r.gap:+.2f} | {r.gap_pct:+.3f} |")
        say()

    # ---- OI ------------------------------------------------------------------------
    say("## Open interest")
    say()
    total_bars = sum(len(b) for b, _ in per_symbol_rolls.values())
    invalid = sum(1 for b, _ in per_symbol_rolls.values()
                  for bar in b if not bar.oi_change_valid)
    say(f"- Granularity: **DAILY ONLY.** Fyers' history endpoint returns six-field "
        f"OHLCV and carries no OI at any resolution. Per-minute OI does not exist in "
        f"this data. Daily OI comes from the exchange bhavcopy.")
    say(f"- Continuous-series bars with OI: **{total_bars}**")
    say(f"- Bars where `oi_change` is None because it spans a roll: **{invalid}** "
        f"({invalid / total_bars * 100:.1f}%)" if total_bars else "")
    say(f"- Those are None, never zero. OI does not carry across a roll: the new "
        f"contract is a different pool of positions, so a change computed across the "
        f"boundary is a large meaningless number that looks like a signal.")
    say()

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nwritten: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
