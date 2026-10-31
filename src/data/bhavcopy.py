"""NSE F&O daily bhavcopy: the source of record for expiry dates and open interest.

Why this exists
---------------

Fyers' ``history`` endpoint returns six-field OHLCV candles and **no open interest at
any resolution**, so OI cannot come from there. The exchange's own end-of-day
derivatives file carries, for every contract on every trading day: the contract's
expiry date, its OHLC, settlement price, volume, **open interest and change in open
interest**.

That makes it three things at once, all of which the futures work needs:

1. **Daily OI per contract** -- the only OI available historically. Not 1-minute.
2. **The authoritative expiry calendar.** Every row states its contract's expiry, from
   the exchange, on the day it traded. This is stronger than probing candles: it is
   the exchange's own record rather than an inference from data presence.
3. **Daily futures OHLC per contract**, which is enough to compute roll gap sizes
   without any broker API.

Two file formats
----------------

NSE moved to the UDiFF format in July 2024. Both are handled and normalised to one
shape; :func:`parse` picks by header, not by date, because the cutover date is not
worth trusting when the file itself says which it is.

* pre-UDiFF: ``INSTRUMENT, SYMBOL, EXPIRY_DT, ..., OPEN_INT, CHG_IN_OI, TIMESTAMP``
* UDiFF:     ``TradDt, FinInstrmTp, TckrSymb, XpryDt, ..., OpnIntrst, ChngInOpnIntrst``

Politeness
----------

This scrapes a public exchange endpoint, so it identifies honestly, rate-limits, and
caches aggressively. **The raw zip is persisted before anything parses it**, so a
parser change can be replayed without re-fetching -- the same rule the equity backfill
follows. Nothing here re-requests a date already on disk unless asked.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterator

log = logging.getLogger("fno.bhavcopy")

__all__ = [
    "BhavRow",
    "BhavcopyStore",
    "MISSING",
    "parse",
    "udiff_url",
    "legacy_url",
]

#: Sentinel recorded in the ledger for a date the exchange has no file for. Distinct
#: from "not fetched yet" -- an absent file is a fact about the date, and conflating
#: the two is how a holiday becomes indistinguishable from a failed download.
MISSING = "MISSING"

_UA = (
    "fno-advisor/1.0 (personal research tool; contact via repo owner) "
    "python-urllib"
)

#: Instrument type codes for FUTURES on a single stock, in each file format.
STOCK_FUTURE_TYPES = {"FUTSTK", "STF"}
INDEX_FUTURE_TYPES = {"FUTIDX", "IDF"}
STOCK_OPTION_TYPES = {"OPTSTK", "STO"}
INDEX_OPTION_TYPES = {"OPTIDX", "IDO"}


@dataclass(frozen=True)
class BhavRow:
    """One contract on one trading day, normalised across both file formats."""

    trade_date: date
    symbol: str
    instrument: str
    expiry: date
    open: float
    high: float
    low: float
    close: float
    settle: float
    volume: int
    open_interest: int
    change_in_oi: int
    #: 0 for futures. Options carry a real strike, and without it an option row is
    #: unidentifiable -- there are ~200 RELIANCE option rows on a single day.
    strike: float = 0.0
    #: "CE" / "PE" for options, "" for futures.
    option_type: str = ""

    @property
    def is_stock_future(self) -> bool:
        return self.instrument in STOCK_FUTURE_TYPES

    @property
    def is_option(self) -> bool:
        return self.instrument in STOCK_OPTION_TYPES | INDEX_OPTION_TYPES


def udiff_url(day: date) -> str:
    return ("https://nsearchives.nseindia.com/content/fo/"
            f"BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip")


def legacy_url(day: date) -> str:
    """Pre-UDiFF path: /2020/JAN/fo01JAN2020bhav.csv.zip (month name upper-case)."""
    mon = f"{day:%b}".upper()
    return ("https://nsearchives.nseindia.com/content/historical/DERIVATIVES/"
            f"{day:%Y}/{mon}/fo{day:%d}{mon}{day:%Y}bhav.csv.zip")


def _parse_date(text: str) -> date:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date in bhavcopy: {text!r}")


def _num(text: str, cast=float, default=0):
    text = (text or "").strip()
    if not text or text == "-":
        return default
    try:
        return cast(float(text))
    except ValueError:
        return default


def parse(raw: bytes, *, instruments: set[str] | None = None) -> list[BhavRow]:
    """Normalise either bhavcopy format into :class:`BhavRow` records.

    Chooses by header rather than by date: the file states which format it is, and the
    UDiFF cutover date is not worth hardcoding when the answer is in the bytes.

    Args:
        instruments: keep only these instrument-type codes (e.g. ``STOCK_FUTURE_TYPES``).
            Filtering HERE rather than in the caller matters at scale: a day's file
            holds ~31,000 rows of which ~400 are stock futures, so across the full
            2020-2026 archive the difference is ~1,000,000 objects built instead of
            ~51,000,000.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise ValueError("bhavcopy zip contains no CSV")
        with zf.open(members[0]) as fh:
            text = io.TextIOWrapper(fh, "utf-8", errors="replace")
            reader = csv.DictReader(text)
            fields = {(f or "").strip() for f in (reader.fieldnames or [])}
            if "OpnIntrst" in fields:
                return list(_parse_udiff(reader, instruments))
            if "OPEN_INT" in fields:
                return list(_parse_legacy(reader, instruments))
            raise ValueError(f"unrecognised bhavcopy header: {sorted(fields)[:12]}")


def _parse_udiff(reader: csv.DictReader,
                 instruments: set[str] | None = None) -> Iterator[BhavRow]:
    for r in reader:
        if instruments is not None and (r.get("FinInstrmTp") or "").strip().upper() \
                not in instruments:
            continue
        yield BhavRow(
            trade_date=_parse_date(r["TradDt"]),
            symbol=(r["TckrSymb"] or "").strip().upper(),
            instrument=(r["FinInstrmTp"] or "").strip().upper(),
            expiry=_parse_date(r["XpryDt"]),
            open=_num(r["OpnPric"]), high=_num(r["HghPric"]),
            low=_num(r["LwPric"]), close=_num(r["ClsPric"]),
            settle=_num(r.get("SttlmPric", "")),
            volume=_num(r.get("TtlTradgVol", ""), int),
            open_interest=_num(r.get("OpnIntrst", ""), int),
            change_in_oi=_num(r.get("ChngInOpnIntrst", ""), int),
            strike=_num(r.get("StrkPric", "")),
            option_type=(r.get("OptnTp") or "").strip().upper(),
        )


def _parse_legacy(reader: csv.DictReader,
                  instruments: set[str] | None = None) -> Iterator[BhavRow]:
    for r in reader:
        if instruments is not None and (r.get("INSTRUMENT") or "").strip().upper() \
                not in instruments:
            continue
        yield BhavRow(
            trade_date=_parse_date(r["TIMESTAMP"]),
            symbol=(r["SYMBOL"] or "").strip().upper(),
            instrument=(r["INSTRUMENT"] or "").strip().upper(),
            expiry=_parse_date(r["EXPIRY_DT"]),
            open=_num(r["OPEN"]), high=_num(r["HIGH"]),
            low=_num(r["LOW"]), close=_num(r["CLOSE"]),
            settle=_num(r.get("SETTLE_PR", "")),
            volume=_num(r.get("CONTRACTS", ""), int),
            open_interest=_num(r.get("OPEN_INT", ""), int),
            change_in_oi=_num(r.get("CHG_IN_OI", ""), int),
            strike=_num(r.get("STRIKE_PR", "")),
            # Legacy files write "XX" for futures rather than leaving it blank.
            option_type=(lambda t: "" if t in ("XX", "") else t)(
                (r.get("OPTION_TYP") or "").strip().upper()),
        )


@dataclass
class BhavcopyStore:
    """Immutable on-disk cache of raw bhavcopy zips, plus a resumable ledger.

    Raw bytes are written exactly as fetched and never rewritten. The ledger records
    OK / MISSING / an error string per date, append-only in spirit, so an interrupted
    run resumes rather than restarting.
    """

    root: Path
    min_interval_seconds: float = 1.2
    _last_request: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.raw_dir = self.root / "raw" / "bhavcopy"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.raw_dir / "_ledger.json"

    # ---- ledger ---------------------------------------------------------------

    def _ledger(self) -> dict[str, str]:
        if not self.ledger_path.exists():
            return {}
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("bhavcopy ledger unreadable; treating as empty")
            return {}

    def _record(self, day: date, status: str) -> None:
        ledger = self._ledger()
        ledger[day.isoformat()] = status
        self.ledger_path.write_text(json.dumps(ledger, indent=0, sort_keys=True),
                                    encoding="utf-8")

    def status(self, day: date) -> str | None:
        return self._ledger().get(day.isoformat())

    def settled(self) -> dict[str, str]:
        return self._ledger()

    # ---- fetch ----------------------------------------------------------------

    def path_for(self, day: date) -> Path:
        return self.raw_dir / f"{day:%Y}" / f"fo_{day:%Y%m%d}.zip"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url: str) -> bytes | None:
        """Fetch one URL. None on 404 (the file genuinely is not there)."""
        self._throttle()
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return None
            raise

    def fetch(self, day: date, *, force: bool = False) -> bytes | None:
        """Fetch and cache one day. Returns raw bytes, or None if the file is absent.

        Tries the UDiFF path first for dates from mid-2024 and the legacy path first
        before that, then falls back to the other. Both are attempted because the
        cutover was not instantaneous across the archive.
        """
        target = self.path_for(day)
        if target.exists() and not force:
            return target.read_bytes()

        prefer_udiff = day >= date(2024, 7, 1)
        urls = ([udiff_url(day), legacy_url(day)] if prefer_udiff
                else [legacy_url(day), udiff_url(day)])

        for url in urls:
            raw = self._get(url)
            if raw is None:
                continue
            if not raw.startswith(b"PK"):  # an HTML error page, not a zip
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            self._record(day, "OK")
            return raw

        self._record(day, MISSING)
        return None

    def load(self, day: date, *,
             instruments: set[str] | None = None) -> list[BhavRow] | None:
        """Parsed rows for one cached day, or None if that day is not on disk."""
        raw = self.path_for(day).read_bytes() if self.path_for(day).exists() else None
        if raw is None:
            return None
        return parse(raw, instruments=instruments)
