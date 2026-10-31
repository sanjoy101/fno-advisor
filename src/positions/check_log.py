"""A scoreboard of entry checks already made, and what happened after them.

WHAT THIS IS
------------

Every ``check-entry`` evaluation is appended to a ledger with the verdict and the price
at the moment of the check. Later, once a fixed horizon has elapsed, the outcome is
SETTLED against the price then and the pair is frozen. The dashboard renders the result
as a log of the past.

The purpose is one question and one question only: **does the SOUND/UNSOUND verdict have
any relationship to what happened next?** :func:`summarise_verdicts` answers it with a
mean, an n and an interval, and refuses to answer while the sample is too small.

WHAT THIS IS NOT, AND THE GUARDS THAT KEEP IT THAT WAY
------------------------------------------------------

A table of symbols with a return column beside each one is a buy list unless something
structural stops it being one. Module B's ranking failed evaluation at every horizon
tested (CLAUDE.md 2.3), so a ranking reintroduced here by accident would put a
known-negative result on screen as a recommendation.

* **Ordered by CHECK TIME, most recent first, and by nothing else.** :func:`entries`
  sorts on ``checked_at`` descending. There is no parameter to change that, because a
  parameter is all it takes.
* **No sort-by-return, anywhere.** Not as an argument, not as a column header, not in the
  frontend. A descending return column IS a buy list whatever the header says.
* **SOUND and UNSOUND are always returned together.** There is no verdict filter. Showing
  only the SOUND rows would turn a scoreboard into a track record.
* **No aggregate "best" view.** The only aggregate is the SOUND-vs-UNSOUND comparison,
  which is a test of the check, not a league table of symbols.
* **The change column is past tense** -- "change since you checked" -- because the number
  describes something that already happened and is not a forecast of anything.

INDEPENDENCE, AND WHY THE OBVIOUS n IS WRONG
--------------------------------------------

One press of "check all" evaluates every subscribed symbol in the same minute. Those are
one draw with N readings, not N draws: they share that minute's market move, exactly the
cross-sectional correlation the swing experiment had to correct for (CLAUDE.md 2.3).
Counting them as N independent observations would shrink the interval by ~sqrt(N) and
manufacture a finding out of a single quiet afternoon. :func:`summarise_verdicts` counts
independent cells as distinct (trade_date, minute) pairs and widens accordingly.

Data-availability boundary:
    ``price_at_check`` is the last COMPLETED bar's close at the moment of the check --
    the same price the verdict was computed from, never a later one. Settlement reads a
    price at or after ``settle_at`` and is written once; a settled row never changes
    again. An unsettled row is marked LIVE and is EXCLUDED from the summary, so a number
    that is still moving can never be averaged into a claim.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from src.analysis.entry_check import GroupDiff
from src.analysis.momentum import MIN_N_FOR_ANY_CLAIM, summarise
from src.market.calendar import IST, ensure_ist, square_off_time

log = logging.getLogger("fno.checklog")

__all__ = [
    "SETTLE_MINUTES",
    "MIN_N_FOR_A_CLAIM",
    "CheckRecord",
    "CheckEntry",
    "CheckLog",
    "settle_time",
    "summarise_verdicts",
    "VerdictComparison",
]

#: How long after a check its outcome is frozen. Matches the forward window the DEV
#: diagnostic used (src/analysis/entry_check.py), so the dashboard's accumulating sample
#: and the six-year measurement are answering the same question.
SETTLE_MINUTES = 60

#: Per-group observations below which no claim is made at all. Same bar the offline
#: analysis uses; there is no separate, laxer standard for the live dashboard.
MIN_N_FOR_A_CLAIM = MIN_N_FOR_ANY_CLAIM


def settle_time(checked_at: datetime, trade_date: date) -> datetime:
    """When a check's outcome is frozen.

    Capped at the 15:15 intraday square-off: a 60-minute hold from 14:30 would finish at
    15:30, which this system could never have held, and crediting the check with a move
    nobody could have taken would be inventing the result.
    """
    checked_at = ensure_ist(checked_at)
    return min(checked_at + timedelta(minutes=SETTLE_MINUTES),
               square_off_time(trade_date))


@dataclass(frozen=True)
class CheckRecord:
    """One entry-check evaluation, as it was made."""

    check_id: str
    symbol: str
    trade_date: date
    checked_at: datetime
    #: "SOUND" | "UNSOUND" | "NO_VERDICT"
    verdict: str
    reason: str
    price_at_check: float | None
    #: "SINGLE" (one symbol, asked deliberately) or "BULK" (a check-all sweep).
    source: str = "SINGLE"

    def as_dict(self) -> dict:
        return {
            "kind": "CHECK",
            "check_id": self.check_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date.isoformat(),
            "checked_at": self.checked_at.isoformat(),
            "verdict": self.verdict,
            "reason": self.reason,
            "price_at_check": self.price_at_check,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, row: dict) -> "CheckRecord":
        return cls(
            check_id=row["check_id"],
            symbol=row["symbol"],
            trade_date=date.fromisoformat(row["trade_date"]),
            checked_at=ensure_ist(datetime.fromisoformat(row["checked_at"])),
            verdict=row["verdict"],
            reason=row.get("reason", ""),
            price_at_check=row.get("price_at_check"),
            source=row.get("source", "SINGLE"),
        )


@dataclass
class CheckEntry:
    """A record plus its outcome, settled or still running."""

    record: CheckRecord
    settled_price: float | None = None
    settled_at: datetime | None = None
    #: Set when the outcome can never be determined -- a past session with no stored
    #: bars for that symbol. UNRESOLVED is a third state and it is NOT "still running":
    #: leaving it as LIVE would show a finished check as though it were in flight, the
    #: same failure as rendering UNKNOWN event risk as CLEAR.
    unresolved_reason: str | None = None
    #: Filled in for the CURRENT session only, from the live feed. Never persisted.
    live_price: float | None = None
    live_at: datetime | None = None

    @property
    def settled(self) -> bool:
        return self.settled_price is not None

    @property
    def unresolved(self) -> bool:
        return not self.settled and self.unresolved_reason is not None

    @property
    def state(self) -> str:
        return "SETTLED" if self.settled else "UNRESOLVED" if self.unresolved else "LIVE"

    @property
    def outcome_price(self) -> float | None:
        return self.settled_price if self.settled else self.live_price

    @property
    def outcome_at(self) -> datetime | None:
        return self.settled_at if self.settled else self.live_at

    @property
    def change_pct(self) -> float | None:
        """Change since the check, in percent. PAST TENSE -- it already happened."""
        start, end = self.record.price_at_check, self.outcome_price
        if start is None or end is None or start <= 0:
            return None
        return 100.0 * (end - start) / start

    def as_dict(self) -> dict:
        """The row the dashboard renders.

        Deliberately carries no score, rank, rating or 'best' field, and no ordering
        hint of any kind. The caller cannot reorder these rows from the payload because
        there is nothing in it to order them by except the timestamp.
        """
        return {
            "check_id": self.record.check_id,
            "symbol": self.record.symbol,
            "trade_date": self.record.trade_date.isoformat(),
            "checked_at": self.record.checked_at.isoformat(),
            "verdict": self.record.verdict,
            "reason": self.record.reason,
            "price_at_check": self.record.price_at_check,
            "source": self.record.source,
            "outcome_state": self.state,
            "unresolved_reason": self.unresolved_reason,
            "price_now": self.outcome_price,
            "price_now_at": None if self.outcome_at is None else self.outcome_at.isoformat(),
            "change_since_you_checked_pct": self.change_pct,
            "settles_at": settle_time(
                self.record.checked_at, self.record.trade_date).isoformat(),
        }


@dataclass
class VerdictComparison:
    """Mean change after SOUND versus after UNSOUND, and whether to believe it."""

    diff: GroupDiff
    live_excluded: int

    @property
    def conclusive(self) -> bool:
        """True only when the sample is big enough AND the interval clears zero."""
        if min(self.diff.sound.n, self.diff.unsound.n) < MIN_N_FOR_A_CLAIM:
            return False
        low, high = self.diff.effective_ci95
        return not (low <= 0.0 <= high)

    @property
    def label(self) -> str:
        if min(self.diff.sound.n, self.diff.unsound.n) < MIN_N_FOR_A_CLAIM:
            return "INCONCLUSIVE"
        return "DISTINGUISHABLE" if self.conclusive else "INDISTINGUISHABLE"

    def headline(self) -> str:
        """One sentence, in the words the sample actually supports."""
        s, u = self.diff.sound, self.diff.unsound
        need = MIN_N_FOR_A_CLAIM
        if min(s.n, u.n) < need:
            return (
                f"INCONCLUSIVE — {s.n} settled SOUND and {u.n} settled UNSOUND checks. "
                f"At least {need} of each are needed before this sample can say "
                f"anything at all."
            )
        low, high = self.diff.effective_ci95
        if self.conclusive:
            direction = "higher" if self.diff.diff > 0 else "LOWER"
            return (
                f"SOUND checks were followed by {direction} changes than UNSOUND ones: "
                f"{self.diff.diff:+.3f}pp, 95% CI [{low:+.3f}, {high:+.3f}], "
                f"on {self.diff.effective_n} independent minutes."
            )
        return (
            f"INDISTINGUISHABLE — SOUND minus UNSOUND is {self.diff.diff:+.3f}pp with a "
            f"95% CI of [{low:+.3f}, {high:+.3f}] on {self.diff.effective_n} independent "
            f"minutes, which includes zero. On this evidence the verdict is decoration: "
            f"it is not telling you what happens next."
        )

    def as_dict(self) -> dict:
        low, high = self.diff.effective_ci95
        return {
            "label": self.label,
            "conclusive": self.conclusive,
            "headline": self.headline(),
            "sound_n": self.diff.sound.n,
            "sound_mean_pct": round(self.diff.sound.mean, 4),
            "unsound_n": self.diff.unsound.n,
            "unsound_mean_pct": round(self.diff.unsound.mean, 4),
            "difference_pp": round(self.diff.diff, 4),
            "ci95_low": round(low, 4),
            "ci95_high": round(high, 4),
            "independent_minutes": self.diff.effective_n,
            "minimum_n_each": MIN_N_FOR_A_CLAIM,
            "live_excluded": self.live_excluded,
            "settle_minutes": SETTLE_MINUTES,
        }


def summarise_verdicts(entries: Sequence[CheckEntry]) -> VerdictComparison:
    """Mean change after SOUND vs after UNSOUND, over SETTLED entries only.

    Unsettled rows are excluded and counted, never averaged: a number that is still
    moving is not an outcome yet, and mixing the two would let a good hour on one open
    check drag the whole claim.

    The interval is computed on independent MINUTES rather than on rows. A single
    check-all sweep produces one row per symbol in the same minute; those rows share
    that minute's market move and are one draw, not many.
    """
    sound, unsound = [], []
    cells: set[tuple[date, str]] = set()
    live = 0

    for entry in entries:
        if not entry.settled:
            live += 1
            continue
        change = entry.change_pct
        if change is None:
            continue
        if entry.record.verdict == "SOUND":
            sound.append(change)
        elif entry.record.verdict == "UNSOUND":
            unsound.append(change)
        else:
            continue
        cells.add((entry.record.trade_date,
                   entry.record.checked_at.strftime("%H:%M")))

    return VerdictComparison(
        diff=GroupDiff(
            label="change since you checked",
            sound=summarise("SOUND", sound),
            unsound=summarise("UNSOUND", unsound),
            effective_n=len(cells),
        ),
        live_excluded=live,
    )


@dataclass
class CheckLog:
    """Append-only ledger of entry checks and their settled outcomes.

    Append-only for the same reason the announcements ledger is: a settled outcome that
    can be rewritten is a scoreboard that can be edited after the fact, and the whole
    value of this file is that it was written before the answer was known.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.dir = self.root / "checks"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "entry_checks.jsonl"

    # ---- writing ----------------------------------------------------------------

    def _append(self, row: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def record(self, record: CheckRecord) -> CheckRecord:
        """Log one evaluation. Creates no position and places nothing."""
        self._append(record.as_dict())
        return record

    def settle(self, check_id: str, price: float, at: datetime) -> None:
        """Freeze one check's outcome. Written once; later calls are ignored on read."""
        self._append({
            "kind": "SETTLE",
            "check_id": check_id,
            "settled_price": price,
            "settled_at": ensure_ist(at).isoformat(),
        })

    def mark_unresolved(self, check_id: str, reason: str) -> None:
        """Record that this check's outcome can never be determined.

        Not a settlement and not a failure to try: it is the honest third answer, and it
        keeps a finished check from sitting on the page forever looking like one that is
        still running.
        """
        self._append({"kind": "UNRESOLVED", "check_id": check_id, "reason": reason})

    # ---- reading ----------------------------------------------------------------

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line from an interrupted write. Skip it loudly rather
                # than failing the whole panel -- but never silently rewrite the file.
                log.warning("skipping unparseable line in %s", self.path)
        return out

    def entries(self, trade_date: date | None = None) -> list[CheckEntry]:
        """Every logged check, MOST RECENT CHECK FIRST.

        The order is by ``checked_at`` descending and there is deliberately no argument
        to change it. Sorting this list by outcome would turn a record of what you did
        into a list of what to buy, and an optional parameter is all that would take.
        """
        records: dict[str, CheckEntry] = {}
        settlements: dict[str, tuple[float, datetime]] = {}
        unresolved: dict[str, str] = {}

        for row in self._rows():
            kind = row.get("kind")
            if kind == "CHECK":
                record = CheckRecord.from_dict(row)
                records.setdefault(record.check_id, CheckEntry(record=record))
            elif kind == "SETTLE":
                # FIRST settlement wins. The outcome was frozen when it was frozen; a
                # later line must not be able to move it.
                settlements.setdefault(
                    row["check_id"],
                    (row["settled_price"],
                     ensure_ist(datetime.fromisoformat(row["settled_at"]))),
                )
            elif kind == "UNRESOLVED":
                unresolved.setdefault(row["check_id"], row.get("reason", ""))

        for check_id, (price, at) in settlements.items():
            entry = records.get(check_id)
            if entry is not None:
                entry.settled_price, entry.settled_at = price, at
        for check_id, reason in unresolved.items():
            entry = records.get(check_id)
            # A settlement always wins over an unresolved marker: knowing the answer
            # beats having recorded that we did not.
            if entry is not None and not entry.settled:
                entry.unresolved_reason = reason

        out = list(records.values())
        if trade_date is not None:
            out = [e for e in out if e.record.trade_date == trade_date]
        return sorted(out, key=lambda e: e.record.checked_at, reverse=True)

    def unsettled_due(self, now: datetime) -> list[CheckEntry]:
        """Checks whose settle time has passed and which have no outcome yet."""
        now = ensure_ist(now)
        return [
            e for e in self.entries()
            if not e.settled and not e.unresolved
            and settle_time(e.record.checked_at, e.record.trade_date) <= now
        ]
