"""ELEVATED_EVENT_RISK: an exclusion filter, not a signal.

WHAT THIS IS
------------

A symbol is marked ``ELEVATED_EVENT_RISK`` when a material corporate announcement
landed since the previous close, or when one is scheduled for the session. The claim it
makes is narrow and entirely negative:

    **Price is being driven by information rather than by flow, so normal technical
    reasoning does not apply to this symbol today.**

That is the whole claim. It says nothing about direction, nothing about magnitude, and
nothing about whether the day will be profitable. **No claim is made that this improves
returns.** It identifies days on which the model has no business having an opinion.

WHY IT LIVES IN ``src/market/`` AND NOT ``src/signals/``
---------------------------------------------------------

Because it is a property of the market's state, like a holiday or a ban period, not a
view about a stock. Putting it beside the signal engine would invite the next person to
give it a score and rank on it. There is no score here and the type system will not
carry one: :data:`RISK_VALUES` has exactly three members, they are unordered, and
nothing in this module returns a number.

THREE THINGS THIS DELIBERATELY DOES NOT DO
-------------------------------------------

* **No sentiment scoring.** Subjects are classified into categories that describe WHAT
  KIND of event occurred, never whether it is good or bad. :data:`MATERIAL_CATEGORIES`
  contains ``RATING_CHANGE`` and not ``RATING_UPGRADE`` for exactly this reason -- the
  moment a category carries a direction, someone will trade it.
* **No direction inference.** Nothing reads the body of a filing.
* **No historical backfill.** See :mod:`src.data.announcements`. Retroactively edited
  filings make historical news unusable for backtesting, and neither the poison harness
  nor ``assert_session_causal`` can detect that class of leak. The data is
  forward-collected only, and no function here will accept a date it has no
  contemporaneous observations for -- it returns UNKNOWN instead.

THE THIRD STATE IS THE IMPORTANT ONE
-------------------------------------

``UNKNOWN`` is not a shade of ``CLEAR``. A collector that has not run, an outage at
NSE, or a REPLAY of a date that predates collection all mean *we do not know whether
something happened*, and rendering that as CLEAR is how a filter silently stops
filtering. It must be visually and structurally distinct, which is the same lesson the
dashboard learned when a page with no data rendered as ``LIVE`` (CLAUDE.md 2.4).

Staleness is measured against :data:`MAX_POLL_AGE`, and it is measured from the last
SUCCESSFUL poll -- including one that found nothing, which is why
``AnnouncementStore.record_empty_poll`` exists. "We looked and there was nothing" and
"we have not looked" are different facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Sequence

from src.market.calendar import (
    IST,
    SESSION_CLOSE,
    ensure_ist,
    previous_trading_day,
)

__all__ = [
    "ELEVATED",
    "CLEAR",
    "UNKNOWN",
    "RISK_VALUES",
    "CATEGORY_RESULTS",
    "CATEGORY_BOARD_MEETING",
    "CATEGORY_RATING_CHANGE",
    "CATEGORY_MERGER_ACQUISITION",
    "CATEGORY_REGULATORY_ACTION",
    "MATERIAL_CATEGORIES",
    "CATEGORY_KEYWORDS",
    "MAX_POLL_AGE",
    "SCHEDULED_LOOKAHEAD_SESSIONS",
    "FORBIDDEN_DIRECTIONAL_TERMS",
    "STATE_FRESH",
    "STATE_STALE",
    "STATE_UNAVAILABLE",
    "SymbolEventRisk",
    "EventRiskReport",
    "classify",
    "is_material",
    "event_window",
    "assess",
    "unclassified_subjects",
    "StoreEventRiskSource",
]

#: The three risk values. Exactly three, and deliberately NOT ordered: there is no
#: "more elevated". A filter answers yes / no / do-not-know.
ELEVATED = "ELEVATED_EVENT_RISK"
CLEAR = "CLEAR"
UNKNOWN = "UNKNOWN"
RISK_VALUES: tuple[str, ...] = (ELEVATED, CLEAR, UNKNOWN)

CATEGORY_RESULTS = "RESULTS"
CATEGORY_BOARD_MEETING = "BOARD_MEETING"
CATEGORY_RATING_CHANGE = "RATING_CHANGE"
CATEGORY_MERGER_ACQUISITION = "MERGER_ACQUISITION"
CATEGORY_REGULATORY_ACTION = "REGULATORY_ACTION"

#: The five material categories, as specified. Each names a KIND of event. None of them
#: names a direction, and none may -- see :data:`FORBIDDEN_DIRECTIONAL_TERMS`.
MATERIAL_CATEGORIES: tuple[str, ...] = (
    CATEGORY_RESULTS,
    CATEGORY_BOARD_MEETING,
    CATEGORY_RATING_CHANGE,
    CATEGORY_MERGER_ACQUISITION,
    CATEGORY_REGULATORY_ACTION,
)

#: Lower-case substrings matched against NSE's own subject line.
#:
#: Matching is done on ``desc`` (announcements) and ``purpose`` (calendar) rather than
#: on the free-text attachment summary. Those two are CONTROLLED VOCABULARIES the
#: exchange assigns, so the table below is auditable against a finite list; the free
#: text is not, and matching it would produce false positives nobody could review.
#:
#: The table was built from the vocabulary actually observed in the live feed, not
#: guessed. Subjects that match nothing are reported by
#: :func:`unclassified_subjects` so gaps are visible and can be closed deliberately,
#: rather than silently rendering a symbol CLEAR.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    CATEGORY_RESULTS: (
        "financial result", "financial results", "quarterly result",
        "audited result", "unaudited result", "annual result",
    ),
    CATEGORY_BOARD_MEETING: (
        "board meeting",
    ),
    CATEGORY_RATING_CHANGE: (
        "credit rating", "rating revision", "rating action",
    ),
    CATEGORY_MERGER_ACQUISITION: (
        "acquisition", "amalgamation", "merger", "demerger",
        "scheme of arrangement", "takeover", "slump sale", "divestment",
        "open offer",
    ),
    CATEGORY_REGULATORY_ACTION: (
        "action(s) taken", "action(s) initiated", "actions taken",
        "actions initiated", "orders passed", "insolvency", "sebi",
        "penalty", "penalties", "show cause", "adjudicating", "adjudication",
        "prosecution", "litigation", "search and seizure", "raid",
    ),
}

#: Terms a category name may never contain. A category that carries a direction is a
#: sentiment score with extra steps. Enforced by a test, not by convention.
FORBIDDEN_DIRECTIONAL_TERMS: tuple[str, ...] = (
    "upgrade", "downgrade", "positive", "negative", "bullish", "bearish",
    "good", "bad", "beat", "miss", "sentiment", "score", "buy", "sell",
    "strong", "weak",
)

#: How old the newest successful poll may be before the answer becomes UNKNOWN.
#: Thirty minutes: long enough that a collector on a 15-minute schedule tolerates one
#: missed run, short enough that a morning's filings cannot hide behind it.
MAX_POLL_AGE = timedelta(minutes=30)

#: Scheduled events are flagged only on the session they fall on. Zero, not because
#: tomorrow's results are irrelevant, but because this filter answers "does normal
#: reasoning apply TODAY" and a wider window would flag most of the panel through
#: earnings season, which is how a filter becomes noise and gets ignored.
SCHEDULED_LOOKAHEAD_SESSIONS = 0

STATE_FRESH = "FRESH"
STATE_STALE = "STALE"
STATE_UNAVAILABLE = "UNAVAILABLE"


def classify(subject: str) -> tuple[str, ...]:
    """Material categories a subject line falls into. Empty tuple means not material.

    A subject may match more than one category -- "Outcome of Board Meeting -
    Financial Results" is both -- and all matches are returned. Order follows
    :data:`MATERIAL_CATEGORIES` so the result is deterministic.
    """
    text = " ".join((subject or "").split()).lower()
    if not text:
        return ()
    return tuple(
        category for category in MATERIAL_CATEGORIES
        if any(kw in text for kw in CATEGORY_KEYWORDS[category])
    )


def is_material(subject: str) -> bool:
    return bool(classify(subject))


def unclassified_subjects(subjects: Iterable[str]) -> dict[str, int]:
    """Subjects matching no category, with counts. For review, not for logic.

    The keyword table can only be as good as the vocabulary it was built against, and
    NSE adds subjects. This makes the gap visible: anything here is currently being
    treated as immaterial, and if something in the list should be flagged the table
    needs widening deliberately.
    """
    out: dict[str, int] = {}
    for subject in subjects:
        text = " ".join((subject or "").split())
        if text and not classify(text):
            out[text] = out.get(text, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def event_window(trade_date: date, now: datetime) -> tuple[datetime, datetime]:
    """The interval an announcement must fall in to count as "landed".

    From the PREVIOUS session's close to ``now``. Information released after the last
    close has not been traded on yet and is exactly what the first hour is digesting;
    starting the window at today's open would miss every overnight filing, which is
    when most of them arrive.
    """
    now = ensure_ist(now)
    previous = previous_trading_day(trade_date)
    start = datetime.combine(previous, SESSION_CLOSE, tzinfo=IST)
    return start, now


@dataclass(frozen=True)
class SymbolEventRisk:
    """One symbol's verdict. Carries no number and no direction."""

    symbol: str
    risk: str
    categories: tuple[str, ...]
    reason: str
    #: Compact descriptions of what triggered the flag, for the UI. Subject lines only.
    items: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def elevated(self) -> bool:
        return self.risk == ELEVATED

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "event_risk": self.risk,
            "categories": list(self.categories),
            "reason": self.reason,
            "items": list(self.items),
        }


@dataclass(frozen=True)
class EventRiskReport:
    """The whole panel's event-risk state at one moment."""

    asof: datetime
    trade_date: date
    data_state: str
    reason: str
    symbols: tuple[SymbolEventRisk, ...]
    last_poll_at: datetime | None = None
    window_start: datetime | None = None

    def for_symbol(self, symbol: str) -> SymbolEventRisk | None:
        symbol = symbol.strip().upper()
        return next((s for s in self.symbols if s.symbol == symbol), None)

    def risk_of(self, symbol: str) -> str:
        """The verdict for one symbol. UNKNOWN when we have nothing on it.

        Never CLEAR by omission: a symbol we hold no information about is one we cannot
        clear, and defaulting the other way is how the filter silently stops filtering.
        """
        found = self.for_symbol(symbol)
        return found.risk if found else UNKNOWN

    @property
    def elevated_symbols(self) -> tuple[str, ...]:
        return tuple(s.symbol for s in self.symbols if s.elevated)

    def as_dict(self) -> dict:
        return {
            "asof": self.asof.isoformat(),
            "trade_date": self.trade_date.isoformat(),
            "data_state": self.data_state,
            "reason": self.reason,
            "last_poll_at": None if self.last_poll_at is None
            else self.last_poll_at.isoformat(),
            "window_start": None if self.window_start is None
            else self.window_start.isoformat(),
            "poll_age_seconds": (
                None if self.last_poll_at is None
                else round((self.asof - self.last_poll_at).total_seconds(), 1)
            ),
            "max_poll_age_seconds": MAX_POLL_AGE.total_seconds(),
            "symbols": [s.as_dict() for s in self.symbols],
            "elevated": list(self.elevated_symbols),
            "material_categories": list(MATERIAL_CATEGORIES),
            "is_ranking": False,
            "disclaimer": (
                "ELEVATED_EVENT_RISK is an EXCLUSION FILTER, not a signal. It means a "
                "material announcement landed or is scheduled, so price is being driven "
                "by information rather than flow and normal technical reasoning does "
                "not apply. It says NOTHING about direction, carries no score, and no "
                "claim is made that acting on it improves returns. UNKNOWN is not "
                "CLEAR -- it means we do not know."
            ),
        }

    @classmethod
    def unavailable(cls, symbols: Sequence[str], trade_date: date,
                    now: datetime, reason: str) -> "EventRiskReport":
        """Every symbol UNKNOWN, with the reason stated. Never CLEAR."""
        now = ensure_ist(now)
        return cls(
            asof=now, trade_date=trade_date, data_state=STATE_UNAVAILABLE,
            reason=reason,
            symbols=tuple(
                SymbolEventRisk(symbol=s.strip().upper(), risk=UNKNOWN,
                                categories=(), reason=reason)
                for s in sorted({x.strip().upper() for x in symbols})
            ),
        )


def assess(
    observations: Mapping[str, object],
    symbols: Sequence[str],
    trade_date: date,
    now: datetime,
    *,
    last_poll_at: datetime | None,
    lookahead_sessions: int = SCHEDULED_LOOKAHEAD_SESSIONS,
    max_poll_age: timedelta = MAX_POLL_AGE,
) -> EventRiskReport:
    """Verdict per symbol from forward-collected observations.

    Data-availability boundary:
        An announcement counts as landed when EITHER our own ``first_seen_at`` OR the
        exchange's ``source_timestamp`` falls inside :func:`event_window`. The union is
        deliberate and it is the cautious direction for an exclusion filter: a revised
        source timestamp or a late-observed filing should both still flag the symbol.
        ``first_seen_at`` is our clock and cannot be moved by a later edit at the
        source; ``source_timestamp`` can be, which is why it is never used alone.

    ``last_poll_at`` older than ``max_poll_age`` makes the whole report STALE and every
    symbol UNKNOWN. A stale collector cannot distinguish "nothing happened" from "we
    stopped looking", and only one of those is safe to trade on.
    """
    now = ensure_ist(now)
    wanted = sorted({s.strip().upper() for s in symbols})

    if last_poll_at is None:
        return EventRiskReport.unavailable(
            wanted, trade_date, now,
            "No announcement poll has ever completed. Event risk is forward-collected "
            "only -- run `python -m src.cli collect-announcements` (or the scheduled "
            "collector) before relying on this panel.",
        )

    last_poll_at = ensure_ist(last_poll_at)
    age = now - last_poll_at
    if age > max_poll_age:
        stale_reason = (
            f"The last successful announcement poll was {age.total_seconds() / 60:.0f} "
            f"minutes ago at {last_poll_at:%Y-%m-%d %H:%M}, older than the "
            f"{max_poll_age.total_seconds() / 60:.0f}-minute limit. Nothing can be "
            f"cleared on data this old."
        )
        report = EventRiskReport.unavailable(wanted, trade_date, now, stale_reason)
        return EventRiskReport(
            asof=report.asof, trade_date=trade_date, data_state=STATE_STALE,
            reason=stale_reason, symbols=report.symbols, last_poll_at=last_poll_at,
        )

    start, end = event_window(trade_date, now)
    horizon = _scheduled_horizon(trade_date, lookahead_sessions)

    per_symbol: list[SymbolEventRisk] = []
    for symbol in wanted:
        categories: set[str] = set()
        items: list[dict] = []

        for obs in observations.values():
            if getattr(obs, "symbol", "") != symbol:
                continue
            subject = getattr(obs, "subject", "")
            matched = classify(subject)
            scheduled_for = getattr(obs, "scheduled_for", None)

            if scheduled_for is not None:
                if scheduled_for not in horizon:
                    continue
                # Every event-calendar row IS a board-meeting intimation, so the
                # category floor is BOARD_MEETING even when the stated purpose matches
                # nothing else.
                matched = tuple(sorted(set(matched) | {CATEGORY_BOARD_MEETING}))
                items.append({
                    "subject": subject,
                    "categories": list(matched),
                    "kind": "SCHEDULED",
                    "scheduled_for": scheduled_for.isoformat(),
                    "first_seen_at": _iso(getattr(obs, "first_seen_at", None)),
                    "revised": bool(getattr(obs, "revised", False)),
                })
                categories.update(matched)
                continue

            if not matched:
                continue
            first_seen = getattr(obs, "first_seen_at", None)
            source_ts = getattr(obs, "source_timestamp", None)
            landed = any(
                stamp is not None and start <= ensure_ist(stamp) <= end
                for stamp in (first_seen, source_ts)
            )
            if not landed:
                continue
            items.append({
                "subject": subject,
                "categories": list(matched),
                "kind": "LANDED",
                "first_seen_at": _iso(first_seen),
                "source_timestamp": _iso(source_ts),
                "revised": bool(getattr(obs, "revised", False)),
            })
            categories.update(matched)

        if categories:
            ordered = tuple(c for c in MATERIAL_CATEGORIES if c in categories)
            per_symbol.append(SymbolEventRisk(
                symbol=symbol, risk=ELEVATED, categories=ordered,
                reason=(
                    f"{', '.join(ordered)} — price is being driven by information "
                    f"rather than flow. Normal technical reasoning does not apply to "
                    f"{symbol} today. This says nothing about direction."
                ),
                items=tuple(items),
            ))
        else:
            per_symbol.append(SymbolEventRisk(
                symbol=symbol, risk=CLEAR, categories=(),
                reason=(
                    f"No material announcement observed for {symbol} since "
                    f"{start:%Y-%m-%d %H:%M}, and none scheduled today. CLEAR is about "
                    f"the absence of an event, not about the trade being sound."
                ),
            ))

    return EventRiskReport(
        asof=now, trade_date=trade_date, data_state=STATE_FRESH,
        reason=(
            f"Last poll {age.total_seconds() / 60:.0f} minutes ago at "
            f"{last_poll_at:%H:%M}. Window opens {start:%Y-%m-%d %H:%M}."
        ),
        symbols=tuple(per_symbol), last_poll_at=last_poll_at, window_start=start,
    )


class StoreEventRiskSource:
    """Reads the forward-collected ledger and assesses it. Never touches the network.

    A dashboard request must not block on NSE. Collection is a separate scheduled job
    (``scripts/collect_announcements.py``); this only reads what that job has already
    written, so an NSE outage degrades the panel to STALE rather than hanging the page.

    The ledger is re-parsed only when the file changes. The runtime broadcasts on every
    tick, and re-reading an append-only JSONL once a second would grow into real work
    over a session for an answer that changes every fifteen minutes.
    """

    def __init__(self, store, *, lookahead_sessions: int = SCHEDULED_LOOKAHEAD_SESSIONS,
                 max_poll_age: timedelta = MAX_POLL_AGE) -> None:
        self.store = store
        self.lookahead_sessions = lookahead_sessions
        self.max_poll_age = max_poll_age
        self._stamp: tuple[int, int] | None = None
        self._cache: tuple[dict, datetime | None] = ({}, None)

    def _ledger_stamp(self) -> tuple[int, int]:
        path = self.store.ledger_path
        if not path.exists():
            return (0, 0)
        stat = path.stat()
        return (stat.st_size, int(stat.st_mtime_ns))

    def _load(self) -> tuple[dict, datetime | None]:
        stamp = self._ledger_stamp()
        if stamp != self._stamp:
            self._cache = (self.store.observations(), self.store.last_poll_at())
            self._stamp = stamp
        return self._cache

    def assess(self, symbols: Sequence[str], trade_date: date,
               now: datetime) -> EventRiskReport:
        observations, last_poll = self._load()
        return assess(
            observations, symbols, trade_date, now,
            last_poll_at=last_poll,
            lookahead_sessions=self.lookahead_sessions,
            max_poll_age=self.max_poll_age,
        )


def _scheduled_horizon(trade_date: date, lookahead_sessions: int) -> set[date]:
    """Sessions a scheduled event may fall on and still be flagged."""
    from src.market.calendar import next_trading_day  # local: avoids an import cycle

    out = {trade_date}
    cursor = trade_date
    for _ in range(max(0, lookahead_sessions)):
        cursor = next_trading_day(cursor)
        out.add(cursor)
    return out


def _iso(value) -> str | None:
    return None if value is None else ensure_ist(value).isoformat()
