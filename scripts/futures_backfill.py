"""Fetch 1-minute bars for near-month FUTURES contracts. Requires a Fyers token.

    python -m src.cli auth                      # manual 2FA, once per day
    python scripts/futures_backfill.py --start 2020-01-01 --end 2026-08-21
    python scripts/futures_backfill.py --symbols RELIANCE,INFY --dry-run

**The contract list is derived from the bhavcopy expiry calendar, not from a rule.**
Run ``scripts/bhavcopy_backfill.py`` first. NSE has changed the monthly expiry weekday
more than once (last Thursday historically, last Tuesday as of 2026), so generating
contracts from "last Thursday of the month" would request instruments that never
existed and silently miss ones that did.

Each contract is fetched only over the window in which the continuous series actually
uses it -- from the session after the previous contract's roll, to the session before
its own expiry (see :data:`src.data.futures.ROLL_DAYS_BEFORE_EXPIRY`). Fetching a
contract's whole listed life would pull months of thin, far-dated quotes nobody trades.

Storage: ``data/raw/futures/symbol=X/expiry=YYYY-MM-DD/trade_date=YYYY-MM-DD/bars.parquet``
-- partitioned by CONTRACT, because two contracts on the same underlying on the same
day are different instruments and must never be merged into one file.

.. warning::
   UNVERIFIED against the live API. The futures symbol format is confirmed against the
   public NSE_FO symbol master, but no futures request has been made with real
   credentials. Check the first response by hand.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from src.data.backfill import (  # noqa: E402
    LIQUID_FNO_UNIVERSE,
    FyersHistoryClient,
    RateLimiter,
    candles_to_bars,
    dedupe_bars,
    trim_to_session,
    validate_session_bars,
)
from src.data.bhavcopy import BhavcopyStore  # noqa: E402
from src.data.futures import (  # noqa: E402
    ROLL_DAYS_BEFORE_EXPIRY,
    active_expiry,
    expiry_calendar,
    fyers_futures_symbol,
)
from src.market.calendar import is_trading_day  # noqa: E402

log = logging.getLogger("fno.futures.backfill")


def contract_windows(rows_by_date, symbols, roll_days_before):
    """(underlying, expiry) -> sorted trading dates the continuous series uses it on."""
    calendar = expiry_calendar(rows_by_date)
    windows: dict[tuple[str, date], list[date]] = defaultdict(list)
    for day in sorted(rows_by_date):
        for symbol in symbols:
            expiries = calendar.get(symbol)
            if not expiries:
                continue
            expiry = active_expiry(day, expiries, roll_days_before)
            if expiry is not None:
                windows[(symbol, expiry)].append(day)
    return windows


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--symbols", help="comma-separated; default: the liquid universe")
    p.add_argument("--data-dir", default=None)
    p.add_argument("--roll-days-before", type=int, default=ROLL_DAYS_BEFORE_EXPIRY)
    p.add_argument("--dry-run", action="store_true",
                   help="list the contracts and windows; make no API calls")
    args = p.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    data_dir = Path(args.data_dir or get_settings().data_dir)
    symbols = ([s.strip().upper() for s in args.symbols.split(",")]
               if args.symbols else list(LIQUID_FNO_UNIVERSE))

    bhav = BhavcopyStore(data_dir)
    rows_by_date = {}
    for path in sorted(bhav.raw_dir.rglob("fo_*.zip")):
        day = date.fromisoformat(f"{path.stem[3:7]}-{path.stem[7:9]}-{path.stem[9:11]}")
        if start <= day <= end:
            rows_by_date[day] = [r for r in bhav.load(day) if r.is_stock_future]
    if not rows_by_date:
        print("No bhavcopy data in range. Run scripts/bhavcopy_backfill.py first -- "
              "the contract list is derived from it, not from an expiry rule.")
        return 1

    windows = contract_windows(rows_by_date, symbols, args.roll_days_before)
    total_days = sum(len(v) for v in windows.values())
    print(f"{len(windows)} contracts, {total_days} contract-days, "
          f"{len(symbols)} underlyings, {min(rows_by_date)}..{max(rows_by_date)}")

    if args.dry_run:
        for (symbol, expiry), days in sorted(windows.items())[:25]:
            print(f"  {fyers_futures_symbol(symbol, expiry):<26} "
                  f"{days[0]} .. {days[-1]}  ({len(days)} days)")
        if len(windows) > 25:
            print(f"  … {len(windows) - 25} more contracts")
        print("\nDry run: no API calls made.")
        return 0

    from src.data.fyers_auth import require_token

    settings = get_settings()
    token = require_token(settings.fyers)
    client = FyersHistoryClient(token.access_token, settings.fyers.app_id)
    limiter = RateLimiter()

    out_root = data_dir / "raw" / "futures"
    ok = empty = quarantined = 0
    for (symbol, expiry), days in sorted(windows.items()):
        fy = fyers_futures_symbol(symbol, expiry)
        for day in days:
            if not is_trading_day(day):
                continue
            target = (out_root / f"symbol={symbol}" / f"expiry={expiry}"
                      / f"trade_date={day}" / "bars.parquet")
            if target.exists():
                continue
            limiter.wait()
            try:
                candles = client.fetch(fy, day, day)
            except Exception as exc:  # noqa: BLE001 - recorded, run continues
                log.warning("%s %s failed: %s", fy, day, exc)
                continue
            bars = candles_to_bars(candles)
            if not bars:
                empty += 1
                continue
            bars, _dupes = dedupe_bars(bars)
            bars, _out = trim_to_session(bars, day)
            report = validate_session_bars(symbol, day, bars)
            if not report.ok:
                quarantined += 1
                log.warning("QUARANTINE %s %s: %s", fy, day, report.problems)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_bars(target, bars)
            ok += 1

    print(f"\nwritten={ok} empty={empty} quarantined={quarantined}")
    return 0


def _write_bars(path: Path, bars) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist(
        [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
          "close": b.close, "volume": b.volume} for b in bars]
    )
    pq.write_table(table, path)


if __name__ == "__main__":
    raise SystemExit(main())
