"""Backfill the daily India VIX series from NSE's public index-close file.

    python scripts/india_vix_backfill.py --start 2020-01-01 --end 2026-08-21
    python scripts/india_vix_backfill.py --status

**No broker token is needed.** This scrapes a public exchange endpoint, so it can run
without the daily 2FA step -- see :mod:`src.data.india_vix` for why India VIX comes
from here rather than from the F&O bhavcopy (it is an index, not a contract, and is
absent from that file on every sampled date) or from Fyers.

Resumable: a date already on disk is never re-requested. Interrupting is safe.

Only trading days are requested. A non-trading day has no index file, so asking for
one would book a MISSING that means "holiday" and is indistinguishable from a genuine
publication failure -- the exact confusion CLAUDE.md 4 forbids between a holiday and
an empty result.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from src.data.india_vix import MISSING, VixStore  # noqa: E402
from src.market.calendar import is_trading_day  # noqa: E402


def sessions(start: date, end: date) -> list[date]:
    out, cursor = [], start
    while cursor <= end:
        if is_trading_day(cursor):
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--data-dir", default=None)
    p.add_argument("--status", action="store_true",
                   help="summarise what is already on disk; fetch nothing")
    args = p.parse_args(argv)

    data_dir = Path(args.data_dir or get_settings().data_dir)
    store = VixStore(data_dir)

    if args.status:
        ledger = store.settled()
        rows = store.load_all()
        counts: dict[str, int] = {}
        for status in ledger.values():
            counts[status] = counts.get(status, 0) + 1
        print(f"ledger entries : {len(ledger)}")
        for status, n in sorted(counts.items()):
            print(f"  {status:<12} {n}")
        if rows:
            days = sorted(rows)
            closes = [rows[d].close for d in days]
            print(f"parsed rows    : {len(rows)}")
            print(f"range          : {days[0]} .. {days[-1]}")
            print(f"close min/max  : {min(closes):.4f} / {max(closes):.4f}")
        else:
            print("parsed rows    : 0 (nothing fetched yet)")
        return 0

    start = date.fromisoformat(args.start)
    end = (date.fromisoformat(args.end) if args.end
           else date.today() - timedelta(days=1))

    wanted = sessions(start, end)
    todo = [d for d in wanted if not store.path_for(d).exists()]
    print(f"{len(wanted)} trading days in range; {len(todo)} not yet on disk")

    ok = missing = no_row = failed = 0
    for i, day in enumerate(todo, 1):
        try:
            row = store.fetch(day)
        except Exception as exc:  # noqa: BLE001 - recorded, run continues
            failed += 1
            print(f"  !! {day}: {type(exc).__name__}: {exc}")
            continue
        if row is None:
            if store.status(day) == MISSING:
                missing += 1
            else:
                no_row += 1
        else:
            ok += 1
        if i % 100 == 0:
            print(f"  {i}/{len(todo)}  ok={ok} missing={missing} no_row={no_row}")

    print(f"\nok={ok} missing={missing} no_vix_row={no_row} failed={failed}")
    if missing:
        print("MISSING dates are trading days the exchange published no index file "
              "for. Investigate before treating them as absent volatility data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
