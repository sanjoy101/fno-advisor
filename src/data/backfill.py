"""Historical 1-minute OHLCV backfill from Fyers, resumable and validated.

Design points that matter more than the fetching:

**Resumable.** A ledger records every ``(symbol, trade_date)`` that has been settled --
written, quarantined, or confirmed empty. A restart skips them. Six months x 20 symbols
is ~2,500 symbol-days; a run that has to start over because the network blipped at
hour two is a run that never finishes.

**Validated on write, quarantined on failure.** Bad days go to ``data/quarantine/``
with a machine-readable reason file, never silently dropped and never silently kept.
Silently dropping a day makes a gap look like a holiday; silently keeping a corrupt one
poisons every backtest that spans it. Both are worse than a file you have to look at.

**Rate limited with backoff.** Every request is logged with symbol, range, attempt,
row count and duration, so a throttle or a partial day is visible after the fact.

**Read-only.** This calls the history endpoint. No order API is imported or reachable.

Data-availability boundary:
    Historical candles are stamped at bar OPEN, matching Fyers' convention and this
    project's :class:`~src.positions.models.Bar`. A bar stamped 15:29 covers
    [15:29, 15:30) and is complete. Bars outside the 09:15-15:30 session are rejected
    rather than stored, so a post-market print cannot leak into a session VWAP.
"""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol, Sequence

from src.market.calendar import (
    IST,
    SESSION_CLOSE,
    SESSION_OPEN,
    ensure_ist,
    is_trading_day,
    trading_days_between,
)
from src.positions.models import BAR_INTERVAL, Bar
from src.positions.store import PositionStore

log = logging.getLogger("fno.backfill")

__all__ = [
    "LIQUID_FNO_20",
    "LIQUID_FNO_UNIVERSE",
    "RETIRED_SYMBOLS",
    "DECISION_FIELDS",
    "DuplicateReport",
    "dedupe_bars",
    "trim_to_session",
    "SYMBOL_LISTED_FROM",
    "last_completed_session",
    "BackfillLedger",
    "RateLimiter",
    "ValidationReport",
    "validate_session_bars",
    "HistoryClient",
    "FyersHistoryClient",
    "NO_DATA_STATUS",
    "HTTP_OK",
    "Backfiller",
    "to_fyers_symbol",
]

#: The starting universe of liquid F&O names. Deliberately not the full universe --
#: inspect the data before scaling. Stored per-date in data/reference/ once Module B
#: needs a survivorship-safe universe.
#:
#: TATAMOTORS was in the original list and is GONE: ``NSE:TATAMOTORS-EQ`` returns
#: "Invalid symbol provided" (code -300) -- the Tata Motors demerger replaced it with
#: two listed entities. Both successors are carried here; Fyers serves history for
#: both back beyond the start of this dataset.
#:
#: This is exactly the survivorship trap CLAUDE.md 1.2 warns about: a ticker that
#: silently stops existing turns into a symbol with no data rather than an error you
#: notice. The backfill ledger caught it only because ALL of its requests failed.
LIQUID_FNO_UNIVERSE: tuple[str, ...] = (
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "TMPV", "TMCV", "SBIN", "AXISBANK", "MARUTI", "LT",
    "BHARTIARTL", "ITC", "KOTAKBANK", "HINDALCO", "TATASTEEL",
    "ADANIENT", "BAJFINANCE", "SUNPHARMA", "WIPRO", "ONGC",
)

#: Backwards-compatible alias. The name says 20; the list is 21 after the Tata Motors
#: split. Prefer LIQUID_FNO_UNIVERSE.
LIQUID_FNO_20 = LIQUID_FNO_UNIVERSE

#: Tickers known to be dead, mapped to their successor(s), so a stale config or an
#: old ledger entry produces an explanation instead of 7 failed requests.
RETIRED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "TATAMOTORS": ("TMPV", "TMCV"),
}

#: First date each ticker has any history. Ranges before this are SKIPPED, not
#: requested: a symbol that did not exist yet is not a transient failure, and retrying
#: it four times with exponential backoff per chunk wastes many minutes per symbol.
#:
#: TMCV listed on the Tata Motors demerger. TMPV is the renamed continuing entity and
#: carries the combined company's history from 2017, so it needs no entry here -- but
#: see the warning in LIQUID_FNO_UNIVERSE about what that history means.
SYMBOL_LISTED_FROM: dict[str, date] = {
    "TMCV": date(2025, 10, 23),
}

#: Largest tolerable hole inside a session before the day is quarantined.
MAX_INTRASESSION_GAP = timedelta(minutes=5)

#: Bars in a full 09:15-15:30 session: 375 one-minute bars (09:15 open .. 15:29 open).
FULL_SESSION_BARS = 375


def to_fyers_symbol(symbol: str, segment: str = "EQ") -> str:
    """``RELIANCE`` -> ``NSE:RELIANCE-EQ``. Idempotent if already qualified."""
    symbol = symbol.strip().upper()
    if symbol.startswith("NSE:"):
        return symbol
    index_symbols = {"NIFTY": "NSE:NIFTY50-INDEX",
                     "BANKNIFTY": "NSE:NIFTYBANK-INDEX"}
    if symbol in index_symbols:
        return index_symbols[symbol]
    return f"NSE:{symbol}-{segment}"


# --------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------


@dataclass
class LedgerEntry:
    symbol: str
    trade_date: str
    status: str  # OK | QUARANTINED | EMPTY
    rows: int
    reason: str | None
    recorded_at: str


class BackfillLedger:
    """Durable record of which (symbol, date) pairs are settled.

    **Append-only JSONL.** One line per settled symbol-day, appended immediately.
    Written after every symbol-day, not at the end of a run: a ledger flushed only on
    clean exit is useless precisely when it is needed, after a crash.

    Append rather than rewrite because the rewrite is quadratic. A six-year backfill
    settles ~32,000 symbol-days; re-serialising a ledger that grows to ~6 MB on every
    one of them is hundreds of gigabytes of writes and hours of wall clock. Appending
    a ~160-byte line is constant time.

    Later lines win, so a re-settled day simply supersedes its earlier record and the
    file doubles as an audit trail of what was settled when.
    """

    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        #: Canonical store. A legacy .json ledger is migrated on first load.
        self.path = path.with_suffix(".jsonl")
        self._legacy_path = path if path.suffix == ".json" else path.with_suffix(".json")
        self._entries: dict[tuple[str, str], LedgerEntry] = {}
        self._load()

    def _load(self) -> None:
        if self._legacy_path.exists() and not self.path.exists():
            self._migrate_legacy()
        if not self.path.exists():
            return
        bad = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = LedgerEntry(**json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    bad += 1
                    continue
                self._entries[(entry.symbol, entry.trade_date)] = entry
        if bad:
            # A crash mid-append can leave one truncated line. Skip it and carry on;
            # the day it described simply gets refetched.
            log.warning("ledger: skipped %d unreadable line(s) in %s", bad, self.path)

    def _migrate_legacy(self) -> None:
        try:
            raw = json.loads(self._legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("legacy ledger at %s unreadable; starting fresh", self._legacy_path)
            return
        entries = raw.get("entries", [])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for item in entries:
                handle.write(json.dumps(item) + "\n")
        log.info("migrated %d ledger entries from %s to %s",
                 len(entries), self._legacy_path.name, self.path.name)
        self._legacy_path.rename(self._legacy_path.with_suffix(".json.migrated"))

    def _append(self, entry: LedgerEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry)) + "\n")

    def _rewrite(self) -> None:
        """Full compaction. Only for forget(), which is rare."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for entry in self._entries.values():
                handle.write(json.dumps(asdict(entry)) + "\n")
        tmp.replace(self.path)

    def is_done(self, symbol: str, trade_date: date) -> bool:
        return (symbol.upper(), trade_date.isoformat()) in self._entries

    def record(
        self,
        symbol: str,
        trade_date: date,
        status: str,
        rows: int,
        reason: str | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        entry = LedgerEntry(
            symbol=symbol.upper(),
            trade_date=trade_date.isoformat(),
            status=status,
            rows=rows,
            reason=reason,
            recorded_at=(now or datetime.now(IST)).isoformat(),
        )
        self._entries[(entry.symbol, entry.trade_date)] = entry
        self._append(entry)

    def entries(self) -> list[LedgerEntry]:
        return sorted(self._entries.values(), key=lambda e: (e.trade_date, e.symbol))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self._entries.values():
            out[e.status] = out.get(e.status, 0) + 1
        return out

    def quarantined(self) -> list[LedgerEntry]:
        return [e for e in self.entries() if e.status == "QUARANTINED"]

    def forget(self, symbol: str, trade_date: date) -> None:
        """Drop an entry so it will be refetched. For retrying quarantined days.

        Compacts the whole file, which is fine because forgetting is rare and
        deliberate -- unlike recording, which happens tens of thousands of times.
        """
        self._entries.pop((symbol.upper(), trade_date.isoformat()), None)
        self._rewrite()


# --------------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------------


class RateLimiter:
    """Per-second and per-minute request caps, plus exponential backoff on failure.

    Fyers publishes per-second/minute/day limits; the defaults here sit well under
    them. Being throttled mid-backfill costs far more time than pacing does, and
    hammering a broker's API from a personal account is a good way to lose access.
    """

    def __init__(
        self,
        *,
        max_per_second: float = 3.0,
        # The account in use is on Fyers Standard (50 requests/minute). Keep ten
        # requests of headroom for other app activity and avoid walking into -360.
        max_per_minute: int = 40,
        sleep: Callable[[float], None] = _time.sleep,
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        self.min_interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self.max_per_minute = max_per_minute
        self._sleep = sleep
        self._clock = clock
        self._recent: list[float] = []
        self._last: float | None = None

    def acquire(self) -> float:
        """Block until a request may be made. Returns seconds actually waited."""
        waited = 0.0
        now = self._clock()

        if self._last is not None:
            gap = now - self._last
            if gap < self.min_interval:
                pause = self.min_interval - gap
                self._sleep(pause)
                waited += pause
                now = self._clock()

        self._recent = [t for t in self._recent if now - t < 60.0]
        if len(self._recent) >= self.max_per_minute:
            pause = 60.0 - (now - self._recent[0]) + 0.01
            if pause > 0:
                log.info("per-minute cap reached, pausing %.1fs", pause)
                self._sleep(pause)
                waited += pause
                now = self._clock()
                self._recent = [t for t in self._recent if now - t < 60.0]

        self._recent.append(now)
        self._last = now
        return waited

    def backoff(self, attempt: int, *, base: float = 2.0, cap: float = 60.0) -> float:
        """Sleep for an exponentially growing delay. Returns the delay used."""
        delay = min(cap, base ** attempt)
        log.warning("backing off %.1fs before attempt %d", delay, attempt + 1)
        self._sleep(delay)
        return delay


# --------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------


@dataclass
class ValidationReport:
    """What a candidate session's bars look like, and whether they are usable."""

    symbol: str
    trade_date: date
    rows: int
    problems: list[str] = field(default_factory=list)
    largest_gap_minutes: float = 0.0
    coverage_pct: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "trade_date": self.trade_date.isoformat(),
            "rows": self.rows,
            "problems": self.problems,
            "largest_gap_minutes": self.largest_gap_minutes,
            "coverage_pct": self.coverage_pct,
        }


def validate_session_bars(
    symbol: str,
    trade_date: date,
    bars: Sequence[Bar],
    *,
    max_gap: timedelta = MAX_INTRASESSION_GAP,
) -> ValidationReport:
    """Check one session's bars. Returns a report; never raises on bad data.

    Rules:
      * at least one bar
      * no duplicate timestamps
      * strictly ascending
      * every bar on ``trade_date`` and inside 09:15-15:30 IST
      * no gap greater than ``max_gap`` between consecutive bars inside the session
      * OHLC sanity: high >= max(open, close), low <= min(open, close), high >= low
      * volume >= 0

    OHLC sanity is checked here as well as in :class:`~src.positions.models.Bar`
    because a broker payload that violates it should quarantine the DAY with a reason,
    not blow up the run with a validation error on bar 214 of 375.
    """
    report = ValidationReport(symbol=symbol.upper(), trade_date=trade_date, rows=len(bars))

    if not bars:
        report.problems.append("no bars returned")
        return report

    ordered = sorted(bars, key=lambda b: b.ts)
    timestamps = [b.ts for b in ordered]

    duplicates = {t for t in timestamps if timestamps.count(t) > 1}
    if duplicates:
        sample = sorted(duplicates)[:3]
        report.problems.append(
            f"{len(duplicates)} duplicate timestamp(s), e.g. "
            f"{[t.strftime('%H:%M') for t in sample]}"
        )

    if [b.ts for b in bars] != timestamps:
        report.problems.append("bars were not in ascending time order")

    session_start = datetime.combine(trade_date, SESSION_OPEN, tzinfo=IST)
    session_end = datetime.combine(trade_date, SESSION_CLOSE, tzinfo=IST)

    off_date = [b for b in ordered if b.ts.astimezone(IST).date() != trade_date]
    if off_date:
        report.problems.append(
            f"{len(off_date)} bar(s) not on {trade_date}, e.g. {off_date[0].ts.isoformat()}"
        )

    outside = [
        b for b in ordered
        if b.ts.astimezone(IST).date() == trade_date
        and not (session_start <= b.ts < session_end)
    ]
    if outside:
        report.problems.append(
            f"{len(outside)} bar(s) outside 09:15-15:30, e.g. "
            f"{outside[0].ts.astimezone(IST).strftime('%H:%M')}"
        )

    biggest = timedelta(0)
    for prev, cur in zip(ordered, ordered[1:]):
        gap = cur.ts - prev.ts
        biggest = max(biggest, gap)
    report.largest_gap_minutes = biggest.total_seconds() / 60.0
    if biggest > max_gap:
        report.problems.append(
            f"gap of {report.largest_gap_minutes:.0f} min inside the session "
            f"(limit {max_gap.total_seconds() / 60:.0f})"
        )

    bad_ohlc = [
        b for b in ordered
        if b.high < b.low or b.high < max(b.open, b.close) or b.low > min(b.open, b.close)
    ]
    if bad_ohlc:
        b = bad_ohlc[0]
        report.problems.append(
            f"{len(bad_ohlc)} bar(s) fail OHLC sanity, e.g. {b.ts.strftime('%H:%M')} "
            f"O{b.open} H{b.high} L{b.low} C{b.close}"
        )

    negative = [b for b in ordered if b.volume < 0]
    if negative:
        report.problems.append(f"{len(negative)} bar(s) with negative volume")

    report.coverage_pct = 100.0 * len(ordered) / FULL_SESSION_BARS
    return report


# --------------------------------------------------------------------------------
# History client
# --------------------------------------------------------------------------------


class HistoryClient(Protocol):
    """Minimal history interface, so the backfiller is testable without a broker."""

    def fetch(self, symbol: str, start: date, end: date) -> list[list]:
        """Raw candles as ``[epoch_seconds, open, high, low, close, volume]``."""


#: Fyers' status field for "the request was fine, the range simply has no candles".
NO_DATA_STATUS = "no_data"

#: Fyers echoes the HTTP status in ``code``. 200 means the call SUCCEEDED, whatever the
#: ``s`` field says, so it must never be reported as an API failure.
HTTP_OK = 200


def _trimmed(response: dict, *, limit: int = 400) -> dict:
    """The response with its candle list summarised, so it can go in a log line."""
    out = {k: v for k, v in response.items() if k != "candles"}
    candles = response.get("candles")
    if candles is not None:
        out["candles"] = f"<{len(candles)} row(s)>"
    text = repr(out)
    return out if len(text) <= limit else {"truncated": text[:limit]}


class FyersHistoryClient:
    """Fyers v3 history endpoint. READ-ONLY.

    UNVERIFIED against the live API -- it has never run with real credentials, so the
    response handling is written to the documented shape and should be checked against
    an actual payload the first time it is used.
    """

    def __init__(self, access_token: str, app_id: str, *, segment: str = "EQ") -> None:
        self._token = access_token
        self._app_id = app_id
        self.segment = segment
        self._client = None

    def _api(self):
        if self._client is None:
            from fyers_apiv3 import fyersModel  # noqa: PLC0415 - optional dependency

            self._client = fyersModel.FyersModel(
                client_id=self._app_id, token=self._token, is_async=False, log_path=""
            )
        return self._client

    def fetch(self, symbol: str, start: date, end: date) -> list[list]:
        payload = {
            "symbol": to_fyers_symbol(symbol, self.segment),
            "resolution": "1",
            "date_format": "1",
            "range_from": start.isoformat(),
            "range_to": end.isoformat(),
            "cont_flag": "1",
        }
        response = self._api().history(data=payload)
        if not isinstance(response, dict):
            raise RuntimeError(
                f"Fyers history returned {type(response).__name__} for {symbol}: "
                f"request={payload!r} response={str(response)[:400]!r}"
            )
        status = response.get("s")
        code = response.get("code", "?")
        if status != "ok":
            # A range with nothing in it -- a holiday, a session that has not opened,
            # a symbol with no prints -- comes back as s="no_data" with code 200. 200 is
            # a SUCCESS code: raising here reports an outage that did not happen and
            # buries the message under an empty `message` field. The empty series is the
            # honest answer; classifying it as "closed" or "missing" is the caller's job
            # (see FyersLiveFeed.seed_history), which needs the market calendar to do it.
            if status == NO_DATA_STATUS or code == HTTP_OK:
                log.info(
                    "history returned no candles symbol=%s range=%s..%s status=%s "
                    "code=%s request=%r response=%r",
                    symbol, start, end, status, code, payload, _trimmed(response),
                )
                return []
            message = response.get("message") or "<empty message>"
            raise RuntimeError(
                f"Fyers history failed (code {code}, s={status!r}) for {symbol}: "
                f"{message} request={payload!r} response={_trimmed(response)!r}"
            )
        return response.get("candles", []) or []


#: Bar fields the indicators and exit rules actually consume. A conflict between two
#: copies of the same timestamp in ANY of these makes the day untrustworthy.
#:
#: ``open`` is deliberately absent, and that is a checked fact, not an assumption:
#: VWAP uses (H+L+C)/3, ATR uses high/low/prev-close, EMA and RSI use close, the
#: volume ratio uses volume, the exit engine's price is the last bar's close, and
#: MAE/MFE use high/low. Nothing reads ``open`` except Bar's own OHLC sanity check.
#: tests/test_backfill.py::test_no_indicator_or_rule_reads_bar_open pins this.
DECISION_FIELDS = ("high", "low", "close", "volume")


@dataclass
class DuplicateReport:
    """What deduplication found and did about it."""

    total_rows: int = 0
    unique_timestamps: int = 0
    dropped: int = 0
    #: Duplicate timestamps whose copies agreed on every field.
    identical: int = 0
    #: Duplicate timestamps whose copies disagreed somewhere.
    conflicting: int = 0
    #: Fields that disagreed, and how often.
    conflict_fields: dict[str, int] = field(default_factory=dict)

    @property
    def had_duplicates(self) -> bool:
        return self.dropped > 0

    @property
    def conflicts_decision_fields(self) -> bool:
        """True if a disagreement touched a field an indicator or rule reads."""
        return any(f in DECISION_FIELDS for f in self.conflict_fields)

    def describe(self) -> str:
        if not self.had_duplicates:
            return "no duplicate timestamps"
        parts = [
            f"{self.dropped} duplicate row(s) dropped "
            f"({self.total_rows} -> {self.unique_timestamps})",
            f"{self.identical} identical",
            f"{self.conflicting} conflicting",
        ]
        if self.conflict_fields:
            fields = ", ".join(f"{k}x{v}" for k, v in sorted(self.conflict_fields.items()))
            parts.append(f"fields: {fields}")
        return "; ".join(parts)


def trim_to_session(
    bars: Sequence[Bar], trade_date: date
) -> tuple[list[Bar], list[Bar]]:
    """Split bars into (inside the regular session, outside it).

    Returns the keepers and the discards rather than raising. Out-of-window prints are
    a boundary artifact, not a corrupt day: a single bar stamped 15:30, or a pre-open
    auction print at 09:07, says nothing about whether 09:15-15:29 is sound.

    This is what the module always claimed to do -- "bars outside the session are
    rejected rather than stored" -- but the validator was rejecting the whole DAY,
    which cost 136 otherwise-complete symbol-days across 2020-2023 over one extra bar
    apiece.

    Data-availability boundary:
        Keeps bars with ``session_open <= ts < session_close`` on ``trade_date``. The
        bar stamped exactly at the close covers [15:30, 15:31), which is after the
        session, so it goes.
    """
    start = datetime.combine(trade_date, SESSION_OPEN, tzinfo=IST)
    end = datetime.combine(trade_date, SESSION_CLOSE, tzinfo=IST)
    keep, drop = [], []
    for bar in bars:
        ts = bar.ts.astimezone(IST)
        (keep if (ts.date() == trade_date and start <= ts < end) else drop).append(bar)
    return keep, drop


def dedupe_bars(bars: Sequence[Bar]) -> tuple[list[Bar], DuplicateReport]:
    """Collapse repeated timestamps, keeping the first copy, and report what happened.

    Brokers do repeat data. Fyers returned every 2026-08-20 session twice, for every
    symbol -- 750 candles where there are 375 minutes. A single-day request reproduced
    it, so it is the vendor's store, not a request-shaping or retry artefact.

    Keeping the FIRST copy is arbitrary but deterministic, which matters more: a
    replay must give the same answer every run. Where the copies disagree, the
    disagreement is COUNTED and surfaced rather than resolved, so the caller can
    decide whether the day is still usable.

    Data-availability boundary:
        Pure function of ``bars``. Order is preserved by timestamp; no lookahead.
    """
    report = DuplicateReport(total_rows=len(bars))
    seen: dict[datetime, Bar] = {}
    for bar in sorted(bars, key=lambda b: b.ts):
        first = seen.get(bar.ts)
        if first is None:
            seen[bar.ts] = bar
            continue
        report.dropped += 1
        differing = [
            f for f in ("open", "high", "low", "close", "volume")
            if getattr(first, f) != getattr(bar, f)
        ]
        if differing:
            report.conflicting += 1
            for f in differing:
                report.conflict_fields[f] = report.conflict_fields.get(f, 0) + 1
        else:
            report.identical += 1
    report.unique_timestamps = len(seen)
    return [seen[ts] for ts in sorted(seen)], report


def last_completed_session(today: date, now: datetime | None = None) -> date:
    """The most recent trading day whose 15:30 close has already passed.

    Requesting a session that has not finished yet returns nothing and lands in the
    ledger as EMPTY, which is indistinguishable from a holiday or a dead symbol. The
    original run asked for 2026-08-21 at 00:57 that same morning and booked 19 EMPTY
    entries for a session that had not started.
    """
    now = ensure_ist(now or datetime.now(IST))
    cursor = today
    for _ in range(30):
        if is_trading_day(cursor):
            close = datetime.combine(cursor, SESSION_CLOSE, tzinfo=IST)
            if now >= close:
                return cursor
        cursor -= timedelta(days=1)
    raise RuntimeError(f"no completed session found within 30 days before {today}")


def candles_to_bars(candles: Iterable[Sequence]) -> list[Bar]:
    """Convert raw ``[epoch, o, h, l, c, v]`` rows to IST-aware Bars.

    Epochs are seconds UTC. Rows that cannot be parsed are skipped and counted by the
    caller's validation as missing, rather than crashing a 2,500-day run.
    """
    bars: list[Bar] = []
    for row in candles:
        try:
            ts = datetime.fromtimestamp(int(row[0]), tz=timezone.utc).astimezone(IST)
            bars.append(
                Bar(
                    ts=ts,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=int(row[5]),
                )
            )
        except (TypeError, ValueError, IndexError) as exc:
            log.warning("skipping unparseable candle %r: %s", row, exc)
    return bars


# --------------------------------------------------------------------------------
# Backfiller
# --------------------------------------------------------------------------------


@dataclass
class BackfillSummary:
    requested_symbol_days: int
    skipped_already_done: int = 0
    written: int = 0
    quarantined: int = 0
    empty: int = 0
    failed_requests: int = 0
    requests: int = 0
    seconds: float = 0.0
    #: Symbol-days where the vendor returned repeated timestamps.
    deduped_days: int = 0
    dropped_duplicate_rows: int = 0
    #: Sessions excluded because they had not closed yet when the run started.
    clamped_future_sessions: int = 0
    #: Sessions skipped because the ticker had not listed yet.
    skipped_pre_listing: int = 0
    #: Bars dropped for falling outside 09:15-15:30 (boundary artifacts).
    trimmed_out_of_session_bars: int = 0

    def as_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        extra = ""
        if self.deduped_days:
            extra += (
                f" | deduped {self.deduped_days} day(s), "
                f"{self.dropped_duplicate_rows} rows dropped"
            )
        if self.clamped_future_sessions:
            extra += f" | skipped {self.clamped_future_sessions} not-yet-closed session(s)"
        if self.skipped_pre_listing:
            extra += f" | skipped {self.skipped_pre_listing} pre-listing session(s)"
        if self.trimmed_out_of_session_bars:
            extra += f" | trimmed {self.trimmed_out_of_session_bars} out-of-session bar(s)"
        return (
            f"symbol-days requested {self.requested_symbol_days}, "
            f"already done {self.skipped_already_done}, written {self.written}, "
            f"quarantined {self.quarantined}, empty {self.empty} | "
            f"{self.requests} requests, {self.failed_requests} failed, "
            f"{self.seconds:.1f}s" + extra
        )


class Backfiller:
    """Fetches, validates, and stores historical 1-minute bars."""

    def __init__(
        self,
        client: HistoryClient,
        store: PositionStore,
        *,
        ledger: BackfillLedger | None = None,
        limiter: RateLimiter | None = None,
        max_days_per_request: int = 30,
        max_attempts: int = 4,
        clock: Callable[[], float] = _time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.ledger = ledger or BackfillLedger(store.root / "raw" / "_backfill_ledger.json")
        self.limiter = limiter or RateLimiter()
        self.max_days_per_request = max_days_per_request
        self.max_attempts = max_attempts
        self.clock = clock
        #: Wall clock, injected so the "has this session closed yet" cutoff is
        #: testable. The engine-side rule against reading the clock does not apply
        #: here -- a backfill genuinely needs to know what time it is now.
        self.now = now or (lambda: datetime.now(IST))
        self.quarantine_dir = store.root / "quarantine" / "bars"

    # ---- chunking --------------------------------------------------------------

    def _chunks(self, start: date, end: date) -> Iterator[tuple[date, date]]:
        """Split a range into request-sized windows.

        Fyers caps the span of a single intraday history request, so a six-month pull
        is many requests regardless. Chunking also means a failure loses one window,
        not the whole symbol.
        """
        cursor = start
        while cursor <= end:
            stop = min(end, cursor + timedelta(days=self.max_days_per_request - 1))
            yield cursor, stop
            cursor = stop + timedelta(days=1)

    def _fetch_with_retry(self, symbol: str, start: date, end: date) -> list[list]:
        """One window, with backoff. Raises after ``max_attempts``."""
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            waited = self.limiter.acquire()
            began = self.clock()
            try:
                candles = self.client.fetch(symbol, start, end)
            except Exception as exc:  # noqa: BLE001 - re-raised below after retries
                last = exc
                log.warning(
                    "request FAILED symbol=%s range=%s..%s attempt=%d/%d error=%s",
                    symbol, start, end, attempt + 1, self.max_attempts, exc,
                )
                if attempt + 1 < self.max_attempts:
                    self.limiter.backoff(attempt)
                continue
            log.info(
                "request ok symbol=%s range=%s..%s attempt=%d rows=%d "
                "wait=%.2fs elapsed=%.2fs",
                symbol, start, end, attempt + 1, len(candles), waited, self.clock() - began,
            )
            return candles
        raise RuntimeError(
            f"giving up on {symbol} {start}..{end} after {self.max_attempts} attempts: {last}"
        ) from last

    # ---- quarantine ------------------------------------------------------------

    def _quarantine(self, symbol: str, trade_date: date, bars: Sequence[Bar],
                    report: ValidationReport) -> Path:
        """Park a bad day where it can be inspected, with the reason next to it.

        Not deleted, not stored as if it were good. A day you have to go and look at is
        the only outcome that does not silently corrupt something later.
        """
        folder = (
            self.quarantine_dir
            / f"symbol={symbol.upper()}"
            / f"trade_date={trade_date:%Y-%m-%d}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "reason.json").write_text(
            json.dumps(report.as_dict(), indent=2), encoding="utf-8"
        )
        if bars:
            quarantine_store = PositionStore(self.quarantine_dir.parent)
            quarantine_store.bars_dir = self.quarantine_dir
            quarantine_store.save_bars(symbol, trade_date, bars)
        log.warning(
            "QUARANTINED symbol=%s date=%s rows=%d problems=%s",
            symbol, trade_date, report.rows, "; ".join(report.problems),
        )
        return folder

    # ---- main ------------------------------------------------------------------

    def run(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        force: bool = False,
    ) -> BackfillSummary:
        """Backfill ``symbols`` over ``[start, end]``, skipping settled symbol-days.

        Only NSE trading days are requested -- asking for a holiday wastes a call and
        returns an empty payload that looks like a data problem.
        """
        sessions = trading_days_between(start, end)
        # Drop sessions that have not closed yet. Asking for today at 09:00 books an
        # EMPTY entry that reads exactly like a holiday or a dead ticker.
        cutoff = last_completed_session(end, self.now())
        future = [d for d in sessions if d > cutoff]
        if future:
            log.info(
                "excluding %d session(s) after the last completed close (%s): %s",
                len(future), cutoff, ", ".join(str(d) for d in future),
            )
            sessions = [d for d in sessions if d <= cutoff]

        summary = BackfillSummary(requested_symbol_days=len(symbols) * len(sessions))
        summary.clamped_future_sessions = len(future)
        began = self.clock()

        log.info(
            "backfill start symbols=%d sessions=%d (%s..%s) chunk=%dd",
            len(symbols), len(sessions), start, end, self.max_days_per_request,
        )
        if not sessions:
            log.warning("no completed NSE sessions in %s..%s -- nothing to do", start, end)

        for symbol in symbols:
            symbol = symbol.strip().upper()
            if symbol in RETIRED_SYMBOLS:
                successors = ", ".join(RETIRED_SYMBOLS[symbol])
                log.error(
                    "symbol=%s is RETIRED and no longer trades under that ticker. "
                    "Use its successor(s): %s. Skipping.", symbol, successors,
                )
                continue
            listed_from = SYMBOL_LISTED_FROM.get(symbol)
            eligible = sessions
            if listed_from is not None:
                eligible = [d for d in sessions if d >= listed_from]
                if len(eligible) != len(sessions):
                    skipped = len(sessions) - len(eligible)
                    summary.skipped_pre_listing += skipped
                    log.info(
                        "symbol=%s not listed before %s -- skipping %d session(s) "
                        "rather than retrying a symbol that did not exist",
                        symbol, listed_from, skipped,
                    )

            pending = [
                d for d in eligible
                if force or not self.ledger.is_done(symbol, d)
            ]
            summary.skipped_already_done += len(eligible) - len(pending)
            if not pending:
                log.info("symbol=%s already complete, skipping", symbol)
                continue

            for chunk_start, chunk_end in self._chunks(min(pending), max(pending)):
                wanted = [d for d in pending if chunk_start <= d <= chunk_end]
                if not wanted:
                    continue
                summary.requests += 1
                try:
                    candles = self._fetch_with_retry(symbol, chunk_start, chunk_end)
                except RuntimeError as exc:
                    summary.failed_requests += 1
                    log.error("chunk abandoned: %s", exc)
                    continue

                by_date: dict[date, list[Bar]] = {d: [] for d in wanted}
                for bar in candles_to_bars(candles):
                    day = bar.ts.astimezone(IST).date()
                    if day in by_date:
                        by_date[day].append(bar)

                for day in wanted:
                    self._settle(symbol, day, by_date[day], summary)

        summary.seconds = self.clock() - began
        log.info("backfill done: %s", summary.render())
        return summary

    def _settle(
        self,
        symbol: str,
        trade_date: date,
        bars: list[Bar],
        summary: BackfillSummary,
    ) -> None:
        """Deduplicate, validate, and record one symbol-day's outcome.

        Deduplication runs BEFORE validation. A vendor repeating a session is a
        transport-layer defect, not a market event; letting it reach the duplicate
        check would quarantine a day whose actual prices are fine.

        A day is still quarantined when the repeated copies DISAGREE on a field an
        indicator or rule reads. Picking a winner there would silently choose one
        version of the market.
        """
        if not bars:
            self.ledger.record(symbol, trade_date, "EMPTY", 0, "no bars returned")
            summary.empty += 1
            log.warning("EMPTY symbol=%s date=%s", symbol, trade_date)
            return

        notes: list[str] = []

        # Trim before validating. An out-of-window print is a boundary artifact; the
        # regular session it brackets can still be perfectly sound.
        bars, outside = trim_to_session(bars, trade_date)
        if outside:
            stamps = sorted({b.ts.astimezone(IST).strftime("%H:%M") for b in outside})
            notes.append(
                f"trimmed {len(outside)} bar(s) outside 09:15-15:30 "
                f"({', '.join(stamps[:4])}{'...' if len(stamps) > 4 else ''})"
            )
            summary.trimmed_out_of_session_bars += len(outside)
            log.info("TRIMMED symbol=%s date=%s %s", symbol, trade_date, notes[-1])
        if not bars:
            self.ledger.record(symbol, trade_date, "EMPTY", 0,
                               "every bar fell outside the session window")
            summary.empty += 1
            return

        bars, dupes = dedupe_bars(bars)
        note: str | None = None
        if dupes.had_duplicates:
            summary.deduped_days += 1
            summary.dropped_duplicate_rows += dupes.dropped
            notes.append(dupes.describe())
            note = dupes.describe()
            log.warning("DUPLICATES symbol=%s date=%s %s", symbol, trade_date, note)

            if dupes.conflicts_decision_fields:
                report = validate_session_bars(symbol, trade_date, bars)
                report.problems.insert(
                    0,
                    f"duplicate timestamps disagreed on decision fields "
                    f"({', '.join(sorted(dupes.conflict_fields))}) -- {note}",
                )
                self._quarantine(symbol, trade_date, bars, report)
                self.ledger.record(
                    symbol, trade_date, "QUARANTINED", report.rows,
                    "; ".join(report.problems),
                )
                summary.quarantined += 1
                return

        report = validate_session_bars(symbol, trade_date, bars)
        if not report.ok:
            self._quarantine(symbol, trade_date, bars, report)
            self.ledger.record(
                symbol, trade_date, "QUARANTINED", report.rows, "; ".join(report.problems)
            )
            summary.quarantined += 1
            return

        combined = "; ".join(notes) or None
        self.store.save_bars(symbol, trade_date, bars)
        self.ledger.record(symbol, trade_date, "OK", report.rows, combined)
        summary.written += 1
        log.info(
            "written symbol=%s date=%s rows=%d coverage=%.0f%%%s",
            symbol, trade_date, report.rows, report.coverage_pct,
            f" [{combined}]" if combined else "",
        )
