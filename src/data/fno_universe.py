"""Dated NSE F&O stock universe imported from the documented FYERS symbol master.

The downloaded file is a daily instrument master. It is stored locally with its
date and source so a live subscription never depends on a network fetch during app
startup, and an old or malformed file cannot silently replace the last good copy.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from src.market.calendar import IST, ensure_ist, now_ist

log = logging.getLogger("fno.universe")

FYERS_NSE_FO_MASTER = "https://public.fyers.in/sym_details/NSE_FO.csv"
INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY")
# NSE_FO includes options on indices alongside stock options. Only NIFTY and
# BANKNIFTY have explicit index-symbol mappings in this app; keep the other index
# underlyings out of the stock scanner rather than requesting them as ``-EQ``.
NON_EQUITY_UNDERLYINGS = frozenset({
    "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI",
})
MINIMUM_STOCKS_IN_MASTER = 50
MAXIMUM_STOCKS_IN_MASTER = 250
UNIVERSE_FILENAME = "fno_universe.json"


@dataclass(frozen=True)
class UniverseSnapshot:
    as_of: date
    fetched_at: datetime
    symbols: tuple[str, ...]
    indices: tuple[str, ...]
    source: str
    sha256: str

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            "as_of": self.as_of.isoformat(),
            "fetched_at": ensure_ist(self.fetched_at).isoformat(),
            "symbols": list(self.symbols),
            "indices": list(self.indices),
            "source": self.source,
            "sha256": self.sha256,
        }


def _expiry(value: str) -> date | None:
    value = (value or "").strip()
    # FYERS currently publishes this field as an epoch timestamp in seconds,
    # despite documenting it as a date string. Resolve it in IST so contracts
    # expiring near UTC midnight retain their exchange calendar date.
    try:
        epoch = float(value)
        if epoch >= 100_000_000:
            if epoch >= 100_000_000_000:  # tolerate millisecond epoch variants
                epoch /= 1000
            return datetime.fromtimestamp(epoch, tz=IST).date()
    except (ValueError, OverflowError, OSError):
        pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%d%b%Y", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _rows(raw: bytes) -> list[list[str]]:
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def parse_fyers_fo_master(raw: bytes, *, as_of: date) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return active stock underlyings and the two index context symbols.

    The parser accepts either a header row or the documented FYERS positional
    columns. Only rows with a CE/PE option contract whose expiry has not passed are
    used, which prevents expired-only names from remaining in the live universe.
    """
    rows = _rows(raw)
    if not rows:
        raise ValueError("FYERS NSE F&O symbol master is empty")
    normal = [cell.strip().casefold() for cell in rows[0]]
    header_names = {"underlying symbol", "expiry date", "option type"}
    if header_names.issubset(set(normal)):
        indexes = (normal.index("underlying symbol"), normal.index("expiry date"),
                   normal.index("option type"))
        data = rows[1:]
    else:
        # FYERS' published file-header order: expiry date at 8, underlying at 13,
        # option type at 16. See the Symbol Master File documentation.
        indexes = (13, 8, 16)
        data = rows

    stocks, indices = set(), set()
    for row in data:
        if len(row) <= max(indexes):
            continue
        symbol = row[indexes[0]].strip().upper()
        expiry = _expiry(row[indexes[1]])
        option_type = row[indexes[2]].strip().upper()
        if not symbol or option_type not in {"CE", "PE"} or expiry is None or expiry < as_of:
            continue
        if symbol in INDEX_SYMBOLS:
            indices.add(symbol)
        elif symbol in NON_EQUITY_UNDERLYINGS:
            continue
        elif symbol.replace("&", "").replace("-", "").isalnum():
            stocks.add(symbol)

    stocks_out = tuple(sorted(stocks))
    indices_out = tuple(s for s in INDEX_SYMBOLS if s in indices)
    if len(stocks_out) < MINIMUM_STOCKS_IN_MASTER:
        raise ValueError(
            f"parsed only {len(stocks_out)} active stock underlyings; refusing to "
            "treat a partial or changed file layout as the F&O universe"
        )
    if len(stocks_out) > MAXIMUM_STOCKS_IN_MASTER:
        raise ValueError(
            f"parsed {len(stocks_out)} stock underlyings, above the configured "
            f"safety cap of {MAXIMUM_STOCKS_IN_MASTER}"
        )
    return stocks_out, indices_out


def download_snapshot(*, as_of: date | None = None,
                      opener=urllib.request.urlopen) -> UniverseSnapshot:
    """Fetch and validate a daily FYERS symbol master without storing raw contracts."""
    as_of = as_of or now_ist().date()
    request = urllib.request.Request(
        FYERS_NSE_FO_MASTER,
        headers={"User-Agent": "fno-advisor/1.0 (read-only symbol master refresh)"},
    )
    with opener(request, timeout=30) as response:
        raw = response.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("FYERS symbol master exceeds the 32 MB safety limit")
    stocks, indices = parse_fyers_fo_master(raw, as_of=as_of)
    return UniverseSnapshot(
        as_of=as_of, fetched_at=now_ist(), symbols=stocks, indices=indices,
        source=FYERS_NSE_FO_MASTER, sha256=hashlib.sha256(raw).hexdigest(),
    )


def snapshot_path(data_root: Path | str) -> Path:
    return Path(data_root) / "reference" / UNIVERSE_FILENAME


def save_snapshot(data_root: Path | str, snapshot: UniverseSnapshot) -> Path:
    """Atomically replace the reference only after its full parse succeeded."""
    path = snapshot_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(snapshot.as_dict(), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def load_snapshot(data_root: Path | str, *, today: date | None = None,
                  max_age_days: int = 7) -> UniverseSnapshot | None:
    """Load only a well-formed, recent snapshot; otherwise return None."""
    path = snapshot_path(data_root)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            return None
        as_of = date.fromisoformat(raw["as_of"])
        today = today or now_ist().date()
        age = (today - as_of).days
        symbols = tuple(sorted({str(s).strip().upper() for s in raw["symbols"] if str(s).strip()}))
        symbols = tuple(s for s in symbols if s not in NON_EQUITY_UNDERLYINGS)
        indices = tuple(s for s in INDEX_SYMBOLS if s in set(raw.get("indices", [])))
        if age < 0 or age > max_age_days or len(symbols) < MINIMUM_STOCKS_IN_MASTER:
            return None
        if len(symbols) > MAXIMUM_STOCKS_IN_MASTER:
            return None
        return UniverseSnapshot(
            as_of=as_of, fetched_at=ensure_ist(datetime.fromisoformat(raw["fetched_at"])),
            symbols=symbols, indices=indices, source=str(raw["source"]),
            sha256=str(raw["sha256"]),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        log.warning("F&O universe reference is unreadable or stale: %s", path)
        return None


def refresh_snapshot(data_root: Path | str, *, as_of: date | None = None,
                     opener=urllib.request.urlopen) -> UniverseSnapshot:
    """Fetch first, validate, then atomically persist; failure preserves old data."""
    snapshot = download_snapshot(as_of=as_of, opener=opener)
    save_snapshot(data_root, snapshot)
    return snapshot
