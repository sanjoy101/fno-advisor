"""India VIX daily series: market-wide volatility context.

Where it comes from, and why not from the places you would look first
---------------------------------------------------------------------

**Not the F&O bhavcopy.** Probed directly: the derivatives file carries only
``STF``/``STO``/``IDF``/``IDO`` rows (and ``FUTSTK``/``OPTSTK``/``FUTIDX``/``OPTIDX``
in the pre-UDiFF era). No row on any sampled date -- 2020-01-01, 2024-07-15,
2026-08-19 -- has ``VIX`` in its ticker. India VIX is an INDEX, not a contract, so it
was never going to be in a derivatives contract file.

**Not the Fyers history endpoint, as things stand.** Fyers does list
``NSE:INDIAVIX-INDEX`` in its own websocket symbol map, so the symbol is real, but
whether ``history`` serves it has NOT been verified against the live API and it would
cost a daily 2FA token to find out. This module therefore uses a source that needs no
credentials at all, which also means the series can be rebuilt on any machine without
a broker account.

**The source used: NSE's public daily index-close file.**
``ind_close_all_DDMMYYYY.csv`` carries one row per NSE index for that session, with
open/high/low/close. India VIX appears as the row named exactly ``India VIX``.
Confirmed present at both ends of the panel range:

* ``2020-01-01`` -> ``India VIX,01-01-2020,11.6675,11.73,11.085,11.6``  (77 indices)
* ``2026-08-19`` -> ``India VIX,19-08-2026,11.3875,11.725,10.06,11.32`` (164 indices)

GRANULARITY IS DAILY, AND THAT IS THE ONLY THING THIS FILE OFFERS
-----------------------------------------------------------------

One OHLC row per session. There is no intraday India VIX here at any resolution. If an
intraday VIX is ever needed it must come from a different source and be labelled as
such -- do not resample this daily row into minutes and call it intraday, which would
manufacture a series the exchange never published.

DATA-AVAILABILITY BOUNDARY (CLAUDE.md 1.2)
-------------------------------------------

The file for session ``D`` publishes **after the close of D**. Its open/high/low/close
are therefore *not* available for an intraday decision on D. For a decision at any time
on D, the newest usable row is **D-1**. :func:`asof_row` enforces this with a strict
``<`` on the trade date and is the only accessor that should be used for a feature;
:meth:`VixStore.load_all` returns the raw series and does not know what you intend to
do with it.

This is the same trap as the daily bhavcopy in CLAUDE.md 1.2, and it is easy to get
wrong because a daily row *looks* like a property of the day it is stamped with.

THREE FILES CARRY A US-ORDER DATE STAMP, AND ONE OF THEM OVERWRITES A GOOD SESSION
-----------------------------------------------------------------------------------

Three files in 2020-01-01 .. 2026-08-19 write their ``Index Date`` as **MM-DD-YYYY**
while every other file in the archive uses DD-MM-YYYY:

==============================  =================  ====================
file (the session requested)    internal stamp     parses as
==============================  =================  ====================
``ind_close_all_06042023.csv``  ``04-06-2023``     2023-06-04
``ind_close_all_10042023.csv``  ``04-10-2023``     2023-10-04
``ind_close_all_11042023.csv``  ``04-11-2023``     2023-11-04
==============================  =================  ====================

**The DATA is right; only the stamp is misformatted.** Keyed by filename the April 2023
VIX series runs 12.59, 12.41, 11.80, 12.27, 11.98, 12.27 with no discontinuity, and the
genuine ``ind_close_all_04102023.csv`` -- fetched from the 4-October URL, stamped
consistently -- closes at 11.66, a different number from the 12.27 in the April file.

Keying on the internal date, as :meth:`VixStore.load_all` originally did, therefore did
real damage. ``ind_close_all_10042023.csv`` collided with the genuine 4-October file and
sorted later, so it **won**: 2023-10-04 silently took April's volatility (12.27 instead
of 11.66) and 2023-04-10 vanished from the series. 1640 files became 1639 rows and
nothing said so.

:meth:`VixStore.load_all` now keys every row by the date in its **filename**, which is
the URL the session was requested at and is unambiguous, and warns when the internal
stamp disagrees. Nothing is discarded: all three sessions keep their data and the
4-October session keeps its own.

Note what makes this hard to notice. A swap is only detectable when the day is 12 or
less AND differs from the month; otherwise ``%d-%m-%Y`` either raises or, worse, parses
to the correct date by coincidence. There may be other US-order files in the archive
that happen to be undetectable -- keying by filename makes that harmless rather than
something to hunt for.

Politeness
----------

Public exchange endpoint: honest User-Agent, throttled, and the **raw CSV is persisted
before anything parses it**, so a parser change replays from disk without re-fetching.
Nothing re-requests a date already on disk unless asked. Same rules as
:mod:`src.data.bhavcopy`; the small fetch/throttle helpers are deliberately duplicated
rather than hoisted out of that module, because it is backfilled and working and a
refactor of it buys nothing here.

The stored file is the WHOLE index file, not just the VIX line. It is the raw payload
as fetched (CLAUDE.md 3.5), and it costs ~17 KB a day.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("fno.india_vix")

__all__ = [
    "MISSING",
    "VIX_INDEX_NAME",
    "VixRow",
    "VixStore",
    "asof_row",
    "date_from_filename",
    "index_close_url",
    "parse_index_close",
]

#: Recorded in the ledger for a date the exchange published no file for. Distinct from
#: "not fetched yet": an absent file is a fact about the date, and conflating the two
#: makes a holiday indistinguishable from a failed download.
MISSING = "MISSING"

#: The exact index name in the file. Matched case-insensitively after whitespace
#: collapse, but not fuzzily -- "India VIX" must not silently match some future
#: "India VIX 30" that NSE may add alongside it.
VIX_INDEX_NAME = "india vix"

_UA = ("fno-advisor/1.0 (personal research tool; contact via repo owner) "
       "python-urllib")


@dataclass(frozen=True)
class VixRow:
    """One session's India VIX. Daily only -- there is no intraday field here."""

    trade_date: date
    open: float
    high: float
    low: float
    close: float


def index_close_url(day: date) -> str:
    return ("https://nsearchives.nseindia.com/content/indices/"
            f"ind_close_all_{day:%d%m%Y}.csv")


def _num(text: str) -> float:
    text = (text or "").strip().replace(",", "")
    if not text or text == "-":
        return float("nan")
    return float(text)


def _parse_date(text: str) -> date:
    text = (text or "").strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable index-file date: {text!r}")


def parse_index_close(raw: bytes) -> VixRow | None:
    """Pull the India VIX row out of a daily index-close CSV.

    Returns None when the file parses but carries no India VIX row. That is reported
    by the caller rather than raising, because it is a fact about the date -- but it
    is NOT silently treated as "no data": :meth:`VixStore.fetch` records it distinctly.
    """
    text = io.StringIO(raw.decode("utf-8", errors="replace"))
    reader = csv.DictReader(text)
    fields = {(f or "").strip() for f in (reader.fieldnames or [])}
    if "Index Name" not in fields:
        raise ValueError(f"unrecognised index-close header: {sorted(fields)[:8]}")
    for r in reader:
        name = " ".join((r.get("Index Name") or "").split()).lower()
        if name != VIX_INDEX_NAME:
            continue
        return VixRow(
            trade_date=_parse_date(r.get("Index Date", "")),
            open=_num(r.get("Open Index Value", "")),
            high=_num(r.get("High Index Value", "")),
            low=_num(r.get("Low Index Value", "")),
            close=_num(r.get("Closing Index Value", "")),
        )
    return None


def date_from_filename(name: str) -> date | None:
    """The session an ``ind_close_all_DDMMYYYY.csv`` file was fetched for.

    Used only to cross-check a file against its own contents. None when the name does
    not carry a parseable date, so an unexpected filename is ignored by the check rather
    than causing the file to be discarded.
    """
    stem = Path(name).stem
    prefix = "ind_close_all_"
    if not stem.startswith(prefix):
        return None
    digits = stem[len(prefix):]
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return datetime.strptime(digits, "%d%m%Y").date()
    except ValueError:
        return None


def asof_row(rows: dict[date, VixRow], asof: date) -> VixRow | None:
    """Newest India VIX row usable for a decision made on ``asof``.

    Data-availability boundary:
        Strictly ``trade_date < asof``. The file for session D publishes AFTER the
        close of D, so D's own row is not available to any decision taken during D.
        Returns None when nothing precedes ``asof``, rather than reaching forward.

    The strict ``<`` is the whole point of this function. ``<=`` would hand an
    intraday decision the closing volatility of the session it is still inside.
    """
    usable = [d for d in rows if d < asof]
    return rows[max(usable)] if usable else None


@dataclass
class VixStore:
    """Immutable on-disk cache of raw NSE index-close files, plus a resumable ledger.

    Raw bytes are written exactly as fetched and never rewritten. The ledger records
    OK / MISSING / NO_VIX_ROW / an error string per date, so an interrupted run
    resumes instead of restarting.
    """

    root: Path
    min_interval_seconds: float = 1.2
    _last_request: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.raw_dir = self.root / "raw" / "india_vix"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.raw_dir / "_ledger.json"

    # ---- ledger ---------------------------------------------------------------

    def _ledger(self) -> dict[str, str]:
        if not self.ledger_path.exists():
            return {}
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("india vix ledger unreadable; treating as empty")
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
        return self.raw_dir / f"{day:%Y}" / f"ind_close_all_{day:%d%m%Y}.csv"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url: str) -> bytes | None:
        """Fetch one URL. None when the file genuinely is not there (403/404)."""
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

    def fetch(self, day: date, *, force: bool = False) -> VixRow | None:
        """Fetch and cache one session. Returns the parsed row, or None if absent.

        A file that exists but carries no India VIX row is recorded as
        ``NO_VIX_ROW``, not as MISSING. Those are different facts: one says the
        exchange published nothing for the date, the other says it published a file
        that does not mention India VIX. Collapsing them would hide a schema change.
        """
        target = self.path_for(day)
        if target.exists() and not force:
            return parse_index_close(target.read_bytes())

        raw = self._get(index_close_url(day))
        if raw is None or raw[:1] == b"<":  # absent, or an HTML error page
            self._record(day, MISSING)
            return None

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        row = parse_index_close(raw)
        self._record(day, "OK" if row is not None else "NO_VIX_ROW")
        return row

    def load(self, day: date) -> VixRow | None:
        """Parsed row for one cached session, or None if that date is not on disk."""
        target = self.path_for(day)
        return parse_index_close(target.read_bytes()) if target.exists() else None

    def load_all(self) -> dict[date, VixRow]:
        """Every cached session's row, keyed by trade date.

        Data-availability boundary:
            NONE APPLIED. This is the raw stored series, including the row stamped
            with today's date if it has been fetched. Any feature must go through
            :func:`asof_row`, which applies the strict ``trade_date < asof`` rule.

        **Rows are keyed by the date in the FILENAME**, which is the URL the session was
        requested at, not by the ``Index Date`` column. Three files in the archive write
        that column in US order -- see the module docstring -- and one of them collided
        with a genuine session and overwrote it. The filename is unambiguous; the column
        demonstrably is not.

        A disagreement is logged rather than silently absorbed, because a NEW one would
        mean something about the feed has changed.
        """
        out: dict[date, VixRow] = {}
        for path in sorted(self.raw_dir.rglob("ind_close_all_*.csv")):
            try:
                row = parse_index_close(path.read_bytes())
            except ValueError as exc:
                log.warning("unreadable index file %s: %s", path.name, exc)
                continue
            if row is None:
                continue
            stamped = date_from_filename(path.name)
            if stamped is None:
                log.warning("index file %s has no parseable date in its name; keyed by "
                            "its internal stamp %s", path.name, row.trade_date)
                out[row.trade_date] = row
                continue
            if stamped != row.trade_date:
                log.warning(
                    "index file %s carries an internal Index Date of %s, which is not "
                    "the session it was fetched for; keyed by the filename",
                    path.name, row.trade_date,
                )
                row = VixRow(trade_date=stamped, open=row.open, high=row.high,
                             low=row.low, close=row.close)
            out[stamped] = row
        return out
