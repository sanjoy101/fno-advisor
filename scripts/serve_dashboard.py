"""Serve the Module C dashboard, in REPLAY or LIVE mode.

**Prefer the CLI subcommand** -- it is the same code and matches the rest of the
interface:

    python -m src.cli dashboard --date 2026-08-19 --speed 120
    python -m src.cli dashboard --mode live

This script is kept as a thin wrapper so existing habits and any shortcuts still
work. Both front doors call :func:`src.api.serve.serve`; the wiring lives there once,
because two copies is how one of them silently starts LIVE when you asked for REPLAY.

    python scripts/serve_dashboard.py --replay 2026-08-19 --speed 120
    python scripts/serve_dashboard.py --live

Then open http://127.0.0.1:8000.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from src.api.serve import serve  # noqa: E402
from src.positions.store import PositionStore  # noqa: E402


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--replay", metavar="YYYY-MM-DD",
                      help="replay a stored session (default mode)")
    mode.add_argument("--live", action="store_true",
                      help="drive from the live Fyers feed; needs a fresh token")
    p.add_argument("--speed", type=float, default=60.0,
                   help="replay speed multiplier (60 = one minute per second)")
    p.add_argument("--symbols", help="comma-separated; default is a small liquid set")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--data-dir", default=None)
    p.add_argument("--tick-seconds", type=float, default=1.0)
    args = p.parse_args(argv)

    store = PositionStore(args.data_dir or get_settings().data_dir)
    try:
        return serve(
            store,
            mode="live" if args.live else "replay",
            trade_date=date.fromisoformat(args.replay) if args.replay else None,
            speed=args.speed,
            symbols=args.symbols,
            host=args.host,
            port=args.port,
            tick_seconds=args.tick_seconds,
        )
    except ValueError as exc:
        p.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
