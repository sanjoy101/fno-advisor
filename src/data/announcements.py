"""NSE corporate announcements and the board-meeting calendar. FORWARD-COLLECT ONLY.

WHY THERE IS NO HISTORICAL BACKFILL, AND WHY THERE NEVER WILL BE
-----------------------------------------------------------------

The announcements endpoint will happily serve a symbol's entire filing history --
``?symbol=RELIANCE`` returns 3,336 rows going back to 2006. **That capability is
deliberately not used, and this module offers no way to reach it.**

Corporate filings are revised. A company files, then files a corrigendum; NSE restates
the subject line; an attachment is replaced. What the endpoint returns for a past date
is what the record looks like TODAY, not what was visible to a trader at 10:15 that
morning. Pulling that history and treating each row's ``an_dt`` as the moment the
information existed would embed a look-ahead that:

* is invisible to :mod:`src.analysis.lookahead` -- the bar-level harness varies bars
  within a series, and nothing about the bars changes;
* is invisible to ``assert_session_causal`` -- every session in the date list is
  genuinely complete, and the leak lives in the CONTENT of a row rather than in which
  rows are visible;
* would be undetectable after the fact, because the pre-revision text no longer exists
  anywhere to compare against.

There is no harness in this repo that can catch it, so the only safe posture is to
never create the data. Everything here records what was observable AT THE MOMENT IT WAS
OBSERVED, by our own clock, and nothing else.

``first_seen_at`` IS THE ONLY TRUSTWORTHY TIMESTAMP
----------------------------------------------------

Every observation is appended to a JSONL ledger with ``observed_at``, taken from
:func:`~src.market.calendar.now_ist` at the moment of the fetch.
:meth:`AnnouncementStore.observations` derives ``first_seen_at`` as the EARLIEST
``observed_at`` for each announcement key, and that value never changes once written.

The exchange's own ``an_dt`` is stored too, because discarding what the source said
would make the record unauditable -- but it is a claim by a mutable source, not
evidence. A revision shows up as a second ledger line for the same key with different
content, which is exactly the signal that the source changed its mind.

WHAT THIS IS FOR
----------------

An EXCLUSION FILTER. See :mod:`src.market.event_risk`. It identifies symbols where
price is being driven by information rather than by flow, so that normal technical
reasoning can be marked as not applicable. **Nothing here scores sentiment, infers
direction, or claims to improve returns.**

COLLECTION SHAPE
----------------

Announcements are pulled with ``from_date`` and ``to_date`` both set to the session
being collected. That matters: the unparameterised endpoint returns only the **20 most
recent rows across all equities**, which on a busy morning is a few minutes of the
market. Polling that would silently miss filings between polls -- and for an exclusion
filter a silent miss is the dangerous direction, because it renders a risky symbol as
CLEAR. The dated form returns the whole session so far.

Politeness: honest User-Agent, throttled, cookies bootstrapped from the public
homepage, and **the raw JSON is persisted before anything parses it**, so a parser
change replays from disk without re-fetching. Same rules as :mod:`src.data.bhavcopy`.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Iterable

from src.market.calendar import IST, ensure_ist, now_ist

log = logging.getLogger("fno.announcements")

__all__ = [
    "ANNOUNCEMENTS_URL",
    "EVENT_CALENDAR_URL",
    "Observation",
    "AnnouncementStore",
    "NseAnnouncementClient",
    "parse_announcements",
    "parse_event_calendar",
    "announcement_key",
    "calendar_key",
]

_UA = ("fno-advisor/1.0 (personal research tool; contact via repo owner) "
       "python-urllib")

_HOME = "https://www.nseindia.com/"
ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
EVENT_CALENDAR_URL = "https://www.nseindia.com/api/event-calendar"

_ANN_REFERER = ("https://www.nseindia.com/companies-listing/"
                "corporate-filings-announcements")
_CAL_REFERER = ("https://www.nseindia.com/companies-listing/"
                "corporate-filings-event-calendar")

#: Source tags, recorded on every observation so the two feeds stay distinguishable.
SOURCE_ANNOUNCEMENT = "announcement"
SOURCE_CALENDAR = "calendar"


@dataclass(frozen=True)
class Observation:
    """One announcement or scheduled event, as WE saw it, when we saw it.

    ``first_seen_at`` is our own clock at the first fetch that returned this key. It is
    the only timestamp safe to reason about causally; see the module docstring.

    ``source_timestamp`` is what the exchange said, parsed where possible. It is
    recorded for audit and **must not** be used to decide what was knowable when.
    """

    key: str
    symbol: str
    #: NSE's own subject/category for an announcement, or ``purpose`` for a calendar
    #: entry. A controlled vocabulary, which is why materiality is classified from it
    #: rather than from free text.
    subject: str
    source: str
    first_seen_at: datetime
    source_timestamp: datetime | None
    #: Scheduled date, for calendar entries only.
    scheduled_for: date | None
    #: True when a later fetch returned different content under the same key -- i.e.
    #: the source revised it. Surfaced rather than smoothed over.
    revised: bool = False
    detail: str = ""


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def announcement_key(row: dict) -> str:
    """Stable identity for an announcement row.

    Prefers NSE's ``seq_id``. Falls back to symbol + timestamp + subject, which is
    stable across a content revision -- that is the point, so a revision lands on the
    SAME key and is visible as one rather than appearing to be a new filing.
    """
    seq = _norm(str(row.get("seq_id") or ""))
    if seq:
        return f"ann:{seq}"
    return (f"ann:{_norm(row.get('symbol')).upper()}|{_norm(row.get('an_dt'))}"
            f"|{_norm(row.get('desc')).lower()}")


def calendar_key(row: dict) -> str:
    return (f"cal:{_norm(row.get('symbol')).upper()}|{_norm(row.get('date'))}"
            f"|{_norm(row.get('purpose')).lower()}")


def _parse_dt(text: str | None) -> datetime | None:
    text = _norm(text)
    if not text:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _parse_date(text: str | None) -> date | None:
    dt = _parse_dt(text)
    return dt.date() if dt else None


def _rows_of(raw: bytes) -> list[dict] | None:
    """Rows from either payload shape, or None if it is neither.

    NSE serves a bare list today and has served ``{"data": [...]}`` elsewhere, so both
    are accepted. Anything else returns None so the caller raises a clear error rather
    than an ``AttributeError`` from deep inside a parse -- an unexpected payload shape
    means the feed changed, and that has to be legible.
    """
    data = json.loads(raw.decode("utf-8", errors="replace"))
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("data", [])
    else:
        return None
    return rows if isinstance(rows, list) else None


def parse_announcements(raw: bytes, *, symbols: Iterable[str] | None = None) -> list[dict]:
    """Rows from an announcements payload, optionally narrowed to a symbol set.

    Returns raw dicts. Interpretation belongs to :mod:`src.market.event_risk`; this
    module's job is to fetch honestly and store immutably.
    """
    rows = _rows_of(raw)
    if rows is None:
        raise ValueError("unrecognised announcements payload")
    if symbols is None:
        return rows
    wanted = {s.strip().upper() for s in symbols}
    return [r for r in rows if _norm(r.get("symbol")).upper() in wanted]


def parse_event_calendar(raw: bytes, *,
                         symbols: Iterable[str] | None = None) -> list[dict]:
    rows = _rows_of(raw)
    if rows is None:
        raise ValueError("unrecognised event-calendar payload")
    if symbols is None:
        return rows
    wanted = {s.strip().upper() for s in symbols}
    return [r for r in rows if _norm(r.get("symbol")).upper() in wanted]


class NseAnnouncementClient:
    """Fetches the two public feeds. Bootstraps cookies; throttles; never authenticates.

    The endpoints reject a bare request without the cookies NSE's homepage sets, so the
    homepage is fetched once per client and the jar reused. That is a session, not a
    login: no credentials are involved and none could be.
    """

    def __init__(self, *, min_interval_seconds: float = 1.5, timeout: float = 60.0) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.timeout = timeout
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self._last_request = 0.0
        self._bootstrapped = False

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url: str, referer: str) -> bytes:
        self._throttle()
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Referer": referer,
        })
        with self._opener.open(req, timeout=self.timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return raw

    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        self._get(_HOME, _HOME)
        self._bootstrapped = True

    def fetch_announcements(self, day: date) -> bytes:
        """Raw payload for ONE session. Both date bounds are ``day``.

        Never call this with a range spanning past sessions. The dated form exists here
        to make a poll COMPLETE for the current session, not to reach backwards -- see
        the module docstring on why historical filings are unusable.
        """
        self.bootstrap()
        stamp = f"{day:%d-%m-%Y}"
        url = (f"{ANNOUNCEMENTS_URL}?index=equities"
               f"&from_date={stamp}&to_date={stamp}")
        return self._get(url, _ANN_REFERER)

    def fetch_event_calendar(self) -> bytes:
        """Raw payload of upcoming board meetings. The endpoint decides the window."""
        self.bootstrap()
        return self._get(EVENT_CALENDAR_URL, _CAL_REFERER)


@dataclass
class AnnouncementStore:
    """Immutable raw payloads plus an append-only observation ledger.

    Nothing is ever rewritten. A revision is a new ledger line under the same key, and
    ``first_seen_at`` is derived as the earliest ``observed_at`` for that key -- so a
    later edit by the source cannot move an observation earlier in time.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.raw_dir = self.root / "raw" / "announcements"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.raw_dir / "_observed.jsonl"

    # ---- raw payloads ----------------------------------------------------------

    def save_raw(self, kind: str, day: date, raw: bytes, observed_at: datetime) -> Path:
        """Persist a payload exactly as fetched, before anything parses it."""
        target = (self.raw_dir / f"{day:%Y}" / f"{day:%Y-%m-%d}"
                  / f"{kind}_{observed_at:%H%M%S}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return target

    # ---- ledger ----------------------------------------------------------------

    def append(self, rows: Iterable[dict]) -> int:
        """Append observation lines. Append-only; never compacted, never rewritten."""
        lines = [json.dumps(r, sort_keys=True, default=str) for r in rows]
        if not lines:
            return 0
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return len(lines)

    def _raw_ledger(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        out = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("unreadable ledger line skipped: %.80s", line)
        return out

    def record(
        self,
        *,
        source: str,
        rows: Iterable[dict],
        observed_at: datetime,
    ) -> dict:
        """Write one poll's rows to the ledger and report what was new.

        Returns counts of new / repeated / revised keys. A repeat writes a line too:
        the ledger is a log of observations, not a set of announcements, and knowing
        that a poll saw the same thing again is what distinguishes "nothing happened"
        from "we stopped looking".
        """
        observed_at = ensure_ist(observed_at)
        known = {r["key"]: r for r in self._raw_ledger()}
        new = repeated = revised = 0
        lines = []
        for row in rows:
            key = row["key"]
            prior = known.get(key)
            was_revised = bool(prior and prior.get("fingerprint") != row.get("fingerprint"))
            if prior is None:
                new += 1
            elif was_revised:
                revised += 1
            else:
                repeated += 1
            lines.append({**row, "observed_at": observed_at.isoformat(),
                          "source": source, "revised": was_revised})
        self.append(lines)
        return {"new": new, "repeated": repeated, "revised": revised,
                "written": len(lines)}

    def observations(self) -> dict[str, Observation]:
        """Every known announcement, keyed, with ``first_seen_at`` = earliest sighting.

        Data-availability boundary:
            NONE APPLIED here -- this is the whole stored ledger. Consumers narrow it
            by ``first_seen_at``, which is OUR clock and therefore cannot be moved by a
            later edit at the source. :func:`src.market.event_risk.assess` is the only
            intended consumer and applies the window.
        """
        by_key: dict[str, Observation] = {}
        for row in self._raw_ledger():
            key = row.get("key")
            if not key:
                continue
            observed = _iso(row.get("observed_at"))
            if observed is None:
                continue
            prior = by_key.get(key)
            first_seen = min(observed, prior.first_seen_at) if prior else observed
            by_key[key] = Observation(
                key=key,
                symbol=_norm(row.get("symbol")).upper(),
                subject=_norm(row.get("subject")),
                source=row.get("source", SOURCE_ANNOUNCEMENT),
                first_seen_at=first_seen,
                source_timestamp=_iso(row.get("source_timestamp")),
                scheduled_for=(date.fromisoformat(row["scheduled_for"])
                               if row.get("scheduled_for") else None),
                revised=bool(row.get("revised")) or bool(prior and prior.revised),
                detail=_norm(row.get("detail")),
            )
        return by_key

    def last_poll_at(self, source: str | None = None) -> datetime | None:
        """Newest ``observed_at`` in the ledger, i.e. when we last successfully looked.

        This is what staleness is measured against. It is recorded even by a poll that
        found nothing -- see :meth:`record_empty_poll` -- because "we looked and there
        was nothing" and "we have not looked" are different facts, and treating the
        second as the first is how a symbol renders CLEAR when it is simply unobserved.
        """
        stamps = [
            _iso(r.get("observed_at")) for r in self._raw_ledger()
            if source is None or r.get("source") == source
        ]
        stamps = [s for s in stamps if s is not None]
        return max(stamps) if stamps else None

    def record_empty_poll(self, source: str, observed_at: datetime) -> None:
        """Record that a poll succeeded and returned nothing for our symbols."""
        self.append([{
            "key": "", "symbol": "", "subject": "", "source": source,
            "observed_at": ensure_ist(observed_at).isoformat(), "heartbeat": True,
        }])


def _iso(text) -> datetime | None:
    if not text:
        return None
    try:
        return ensure_ist(datetime.fromisoformat(str(text)))
    except (TypeError, ValueError):
        return None


def observation_rows(
    *,
    announcements: Iterable[dict],
    calendar: Iterable[dict],
) -> list[dict]:
    """Normalise both feeds into ledger rows. No interpretation, no classification.

    ``fingerprint`` is a hash of the fields whose change would mean the source revised
    the filing. It is what makes a revision detectable at all.
    """
    import hashlib

    rows: list[dict] = []
    for r in announcements:
        subject = _norm(r.get("desc"))
        detail = _norm(r.get("attchmntText"))
        fingerprint = hashlib.sha256(
            f"{subject}|{detail}|{_norm(r.get('attchmntFile'))}".encode("utf-8")
        ).hexdigest()[:16]
        source_ts = _parse_dt(r.get("an_dt"))
        rows.append({
            "key": announcement_key(r),
            "symbol": _norm(r.get("symbol")).upper(),
            "subject": subject,
            "detail": detail,
            "source_timestamp": None if source_ts is None else source_ts.isoformat(),
            "scheduled_for": None,
            "fingerprint": fingerprint,
        })
    for r in calendar:
        purpose = _norm(r.get("purpose"))
        detail = _norm(r.get("bm_desc"))
        scheduled = _parse_date(r.get("date"))
        fingerprint = hashlib.sha256(
            f"{purpose}|{detail}|{scheduled}".encode("utf-8")).hexdigest()[:16]
        rows.append({
            "key": calendar_key(r),
            "symbol": _norm(r.get("symbol")).upper(),
            "subject": purpose,
            "detail": detail,
            "source_timestamp": None,
            "scheduled_for": None if scheduled is None else scheduled.isoformat(),
            "fingerprint": fingerprint,
        })
    return rows


def collect_once(
    client: NseAnnouncementClient,
    store: AnnouncementStore,
    symbols: Iterable[str],
    day: date,
    *,
    now: datetime | None = None,
) -> dict:
    """One poll of both feeds. Persists raw, then records observations.

    Raw first, always: a parser change must be replayable from disk without asking NSE
    for the same data again, and a payload we failed to parse is still evidence.

    Returns a summary. Never raises on a feed being unavailable -- the caller decides
    what an outage means, and for an exclusion filter the answer is UNKNOWN rather than
    CLEAR.
    """
    observed_at = ensure_ist(now or now_ist())
    wanted = sorted({s.strip().upper() for s in symbols})
    out: dict = {"observed_at": observed_at.isoformat(), "trade_date": day.isoformat(),
                 "errors": []}

    announcements: list[dict] = []
    calendar: list[dict] = []

    try:
        raw = client.fetch_announcements(day)
        store.save_raw("ann", day, raw, observed_at)
        announcements = parse_announcements(raw, symbols=wanted)
        out["announcements_all"] = len(parse_announcements(raw))
        out["announcements_ours"] = len(announcements)
    except Exception as exc:  # noqa: BLE001 - an outage is a state, not a crash
        log.warning("announcements fetch failed: %s: %s", type(exc).__name__, exc)
        out["errors"].append(f"announcements: {type(exc).__name__}: {exc}")

    try:
        raw = client.fetch_event_calendar()
        store.save_raw("cal", day, raw, observed_at)
        calendar = parse_event_calendar(raw, symbols=wanted)
        out["calendar_all"] = len(parse_event_calendar(raw))
        out["calendar_ours"] = len(calendar)
    except Exception as exc:  # noqa: BLE001
        log.warning("event calendar fetch failed: %s: %s", type(exc).__name__, exc)
        out["errors"].append(f"calendar: {type(exc).__name__}: {exc}")

    if out["errors"] and not announcements and not calendar:
        out["recorded"] = None
        return out

    rows = observation_rows(announcements=announcements, calendar=calendar)
    ann_rows = [r for r in rows if r["key"].startswith("ann:")]
    cal_rows = [r for r in rows if r["key"].startswith("cal:")]

    summary = {"new": 0, "repeated": 0, "revised": 0, "written": 0}
    for source, subset in ((SOURCE_ANNOUNCEMENT, ann_rows), (SOURCE_CALENDAR, cal_rows)):
        if subset:
            part = store.record(source=source, rows=subset, observed_at=observed_at)
            for k in summary:
                summary[k] += part[k]
        elif f"{source}s" not in str(out["errors"]):
            # A successful poll that found nothing for our symbols still proves we
            # looked. Without this, "no news" is indistinguishable from "not collected".
            store.record_empty_poll(source, observed_at)
    out["recorded"] = summary
    return out
