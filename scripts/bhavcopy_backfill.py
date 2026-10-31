"""Fetch the NSE F&O daily bhavcopy for every trading day in a range.

Resumable and polite: one request at a time, throttled, raw zips cached immutably,
and a ledger so an interrupted run picks up where it stopped.

    python scripts/bhavcopy_backfill.py --start 2020-01-01 --end 2026-08-21

Only days the calendar says the exchange traded are requested. That both avoids
pointless 404s on weekends and turns the run into a **calendar cross-check**: a day
the calendar calls a trading day but which has no bhavcopy is a discrepancy worth
seeing, and it is reported rather than silently skipped.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from src.data.bhavcopy import MISSING, BhavcopyStore  # noqa: E402
from src.market.calendar import is_trading_day  # noqa: E402

log = logging.getLogger("fno.bhavcopy.backfill")


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default=None, help="default: yesterday")
    p.add_argument("--data-dir", default=None)
    p.add_argument("--interval", type=float, default=1.2,
                   help="minimum seconds between requests (be polite)")
    p.add_argument("--force", action="store_true", help="refetch days already cached")
    args = p.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    store = BhavcopyStore(args.data_dir or get_settings().data_dir,
                          min_interval_seconds=args.interval)

    days = []
    day = start
    while day <= end:
        if is_trading_day(day):
            days.append(day)
        day += timedelta(days=1)

    log.info("%d trading days in %s..%s", len(days), start, end)

    fetched = cached = missing = 0
    for i, day in enumerate(days, 1):
        if store.path_for(day).exists() and not args.force:
            cached += 1
            continue
        raw = store.fetch(day, force=args.force)
        if raw is None:
            missing += 1
            log.warning("MISSING %s (calendar says it traded)", day)
        else:
            fetched += 1
        if i % 50 == 0:
            log.info("%d/%d  fetched=%d cached=%d missing=%d",
                     i, len(days), fetched, cached, missing)

    ledger = store.settled()
    absent = sorted(d for d, s in ledger.items() if s == MISSING)
    print()
    print(f"trading days requested : {len(days)}")
    print(f"newly fetched          : {fetched}")
    print(f"already cached         : {cached}")
    print(f"MISSING (no file)      : {missing}")
    if absent:
        print()
        print("Dates the calendar calls trading days but NSE has no bhavcopy for.")
        print("Each is either a calendar error or an exchange archive gap -- look:")
        for d in absent:
            print(f"  {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
