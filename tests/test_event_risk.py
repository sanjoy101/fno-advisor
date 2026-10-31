"""ELEVATED_EVENT_RISK: it must stay an exclusion filter and must never clear by default.

Two properties carry the whole feature, and both are structural rather than behavioural
because both fail silently:

1. **UNKNOWN is never rendered as CLEAR.** A collector that has not run, an NSE outage,
   or a REPLAY of a past session all mean "we do not know". Defaulting the other way is
   how a filter stops filtering with nothing on screen to say so.
2. **It annotates and never decides.** A position under elevated event risk still gets
   its normal exit advice and still gets FORCE_EXIT at square-off. Suppressing advice on
   a news day would be the worst possible reading of "normal technical reasoning does
   not apply".
"""

from __future__ import annotations

import inspect
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.runtime import Mode, SessionRuntime, build_replay_runtime
from src.data.announcements import AnnouncementStore, observation_rows
from src.market import event_risk as ER
from src.market.calendar import IST
from src.positions.feed import MultiSymbolReplayFeed
from src.positions.store import PositionStore
from tests.conftest import TRADE_DATE, flat_series, ist, make_series

SYMS = ["RELIANCE", "INFY"]


def at(h: int, m: int, s: int = 0, d: date = TRADE_DATE) -> datetime:
    return datetime.combine(d, time(h, m, s), tzinfo=IST)


class Obs:
    """A stand-in for src.data.announcements.Observation, built where a test wants it."""

    def __init__(self, symbol, subject, first_seen_at=None, source_timestamp=None,
                 scheduled_for=None, revised=False):
        self.symbol = symbol
        self.subject = subject
        self.first_seen_at = first_seen_at
        self.source_timestamp = source_timestamp
        self.scheduled_for = scheduled_for
        self.revised = revised


def assess(observations, *, now=None, last_poll_at=None, symbols=SYMS,
           trade_date=TRADE_DATE, **kw):
    now = now or at(10, 0)
    return ER.assess(
        {str(i): o for i, o in enumerate(observations)},
        symbols, trade_date, now,
        last_poll_at=last_poll_at if last_poll_at is not None else now,
        **kw,
    )


# --------------------------------------------------------------------------------
# Classification, against the vocabulary the live feed actually uses
# --------------------------------------------------------------------------------

#: Observed in a real three-day pull of the NSE announcements feed on 2026-08-23. Kept
#: here as a fixture of the SUBJECT VOCABULARY -- no filing content, no dates, nothing
#: that could serve as historical news data.
REAL_MATERIAL = [
    ("Outcome of Board Meeting", ER.CATEGORY_BOARD_MEETING),
    ("Board Meeting Intimation", ER.CATEGORY_BOARD_MEETING),
    ("Credit Rating", ER.CATEGORY_RATING_CHANGE),
    ("Credit Rating- New", ER.CATEGORY_RATING_CHANGE),
    ("Credit Rating- Revision", ER.CATEGORY_RATING_CHANGE),
    ("Acquisition", ER.CATEGORY_MERGER_ACQUISITION),
    ("Scheme of Arrangement", ER.CATEGORY_MERGER_ACQUISITION),
    ("Action(s) taken or orders passed", ER.CATEGORY_REGULATORY_ACTION),
    ("Action(s) initiated or orders passed", ER.CATEGORY_REGULATORY_ACTION),
    ("Corporate Insolvency Resolution Process", ER.CATEGORY_REGULATORY_ACTION),
    ("Pendency of Litigation(s)/dispute(s) or the outcome impacting the Company",
     ER.CATEGORY_REGULATORY_ACTION),
    ("Reply to Clarification- Financial results", ER.CATEGORY_RESULTS),
    ("Financial Results", ER.CATEGORY_RESULTS),
]

#: Also observed, and deliberately NOT material. Flagging these would fire on most of
#: the panel most days, and a filter that always fires is one nobody reads.
REAL_IMMATERIAL = [
    "Copy of Newspaper Publication",
    "Shareholders meeting",
    "General Updates",
    "Analysts/Institutional Investor Meet/Con. Call Updates",
    "Updates",
    "Record Date",
    "Press Release",
    "Investor Presentation",
    "ESOP/ESOS/ESPS",
    "Appointment",
    "Resignation",
    "Allotment of Securities",
    "Spurt in Volume",
    "Price movement",
    "Trading Plan under PIT",
    "Corrigendum",
    "Name Change",
]


@pytest.mark.parametrize("subject,expected", REAL_MATERIAL,
                         ids=[s for s, _ in REAL_MATERIAL])
def test_real_material_subjects_are_classified(subject, expected):
    assert expected in ER.classify(subject), ER.classify(subject)
    assert ER.is_material(subject)


@pytest.mark.parametrize("subject", REAL_IMMATERIAL)
def test_real_immaterial_subjects_are_not_flagged(subject):
    assert ER.classify(subject) == ()
    assert not ER.is_material(subject)


def test_a_subject_can_match_more_than_one_category_deterministically():
    got = ER.classify("Outcome of Board Meeting - Financial Results")
    assert got == (ER.CATEGORY_RESULTS, ER.CATEGORY_BOARD_MEETING)
    assert got == ER.classify("outcome of BOARD MEETING - financial results")


def test_an_empty_subject_is_not_material():
    assert ER.classify("") == () and ER.classify(None) == ()


def test_unclassified_subjects_reports_the_gap_for_review():
    """A gap in the keyword table renders a symbol CLEAR, so it must be visible."""
    got = ER.unclassified_subjects(
        ["Financial Results", "Some Brand New Subject", "Some Brand New Subject"])
    assert got == {"Some Brand New Subject": 2}


# --------------------------------------------------------------------------------
# No direction, no score. This is the line the feature must not cross.
# --------------------------------------------------------------------------------


def test_no_category_name_carries_a_direction():
    """RATING_CHANGE, never RATING_UPGRADE. A directional category is a sentiment score."""
    for category in ER.MATERIAL_CATEGORIES:
        lowered = category.lower()
        for term in ER.FORBIDDEN_DIRECTIONAL_TERMS:
            assert term not in lowered, f"{category} encodes a direction via {term!r}"


def test_there_are_exactly_three_risk_values_and_they_are_not_ordered():
    assert set(ER.RISK_VALUES) == {ER.ELEVATED, ER.CLEAR, ER.UNKNOWN}
    assert len(ER.RISK_VALUES) == 3
    # No severity scale to sort on, and nothing that would give one.
    module_names = [n.lower() for n in dir(ER)]
    for banned in ("severity", "score", "rank", "weight", "confidence", "sentiment"):
        assert not any(banned in n for n in module_names), banned


def test_the_payload_contains_no_ranking_or_sentiment_field():
    report = assess([Obs("RELIANCE", "Financial Results", first_seen_at=at(9, 0))])
    banned = {
        "score", "rank", "ranking", "rating", "confidence", "probability",
        "sentiment", "direction", "signal", "conviction", "best", "top",
        "recommended", "preferred", "priority", "weight", "percentile", "strength",
        "expected_return", "edge", "grade", "bullish", "bearish",
    }

    def walk(node, path="root"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in banned, f"{key!r} at {path}"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    payload = report.as_dict()
    walk(payload)
    assert payload["is_ranking"] is False
    assert json.dumps(payload)  # serialisable as-is, no custom encoder needed


def test_no_numeric_risk_is_produced_anywhere():
    report = assess([Obs("RELIANCE", "Acquisition", first_seen_at=at(9, 0))])
    for row in report.symbols:
        assert isinstance(row.risk, str) and row.risk in ER.RISK_VALUES
        assert all(isinstance(c, str) for c in row.categories)


def test_the_disclaimer_says_what_it_is_and_is_not():
    text = assess([]).as_dict()["disclaimer"].lower()
    assert "not a signal" in text
    assert "direction" in text
    assert "unknown is not" in text and "clear" in text
    assert "improves returns" in text


# --------------------------------------------------------------------------------
# UNKNOWN is never CLEAR
# --------------------------------------------------------------------------------


def test_no_poll_ever_means_unavailable_and_every_symbol_unknown():
    report = ER.assess({}, SYMS, TRADE_DATE, at(10, 0), last_poll_at=None)
    assert report.data_state == ER.STATE_UNAVAILABLE
    assert {s.risk for s in report.symbols} == {ER.UNKNOWN}
    assert "forward-collected" in report.reason


def test_a_stale_poll_makes_everything_unknown_not_clear():
    report = assess([], now=at(10, 0), last_poll_at=at(9, 25))  # 35 minutes
    assert report.data_state == ER.STATE_STALE
    assert {s.risk for s in report.symbols} == {ER.UNKNOWN}
    assert "older than" in report.reason


def test_a_poll_inside_the_limit_is_fresh():
    report = assess([], now=at(10, 0), last_poll_at=at(9, 40))  # 20 minutes
    assert report.data_state == ER.STATE_FRESH
    assert {s.risk for s in report.symbols} == {ER.CLEAR}


def test_the_staleness_boundary_is_where_it_says_it_is():
    inside = assess([], now=at(10, 0), last_poll_at=at(10, 0) - ER.MAX_POLL_AGE)
    outside = assess([], now=at(10, 0),
                     last_poll_at=at(10, 0) - ER.MAX_POLL_AGE - timedelta(seconds=1))
    assert inside.data_state == ER.STATE_FRESH
    assert outside.data_state == ER.STATE_STALE


def test_a_symbol_the_report_does_not_mention_is_unknown():
    report = assess([])
    assert report.risk_of("SOMETHING_ELSE") == ER.UNKNOWN
    assert report.risk_of("RELIANCE") == ER.CLEAR


def test_clear_says_what_it_does_and_does_not_mean():
    report = assess([])
    reason = report.for_symbol("RELIANCE").reason.lower()
    assert "no material announcement" in reason
    assert "not about the trade being sound" in reason


# --------------------------------------------------------------------------------
# The window, and which timestamp decides
# --------------------------------------------------------------------------------


def test_the_window_opens_at_the_previous_sessions_close():
    start, end = ER.event_window(TRADE_DATE, at(10, 0))
    assert start.date() < TRADE_DATE
    assert start.timetz().replace(tzinfo=None) == time(15, 30)
    assert end == at(10, 0)


def test_an_overnight_announcement_is_flagged():
    """Most filings arrive after the close; a window starting at today's open misses them."""
    start, _ = ER.event_window(TRADE_DATE, at(10, 0))
    overnight = start + timedelta(hours=2)
    report = assess([Obs("RELIANCE", "Financial Results", first_seen_at=overnight)])
    assert report.risk_of("RELIANCE") == ER.ELEVATED


def test_an_announcement_from_before_the_window_is_not_flagged():
    start, _ = ER.event_window(TRADE_DATE, at(10, 0))
    old = start - timedelta(days=3)
    report = assess([Obs("RELIANCE", "Financial Results", first_seen_at=old)])
    assert report.risk_of("RELIANCE") == ER.CLEAR


def test_either_timestamp_inside_the_window_flags_it():
    """The union is the cautious direction for an exclusion filter.

    A filing observed late, or one whose source timestamp was revised, should still
    flag. first_seen_at is ours and cannot be moved; source_timestamp can be, which is
    why it is never trusted alone -- but ignoring it entirely would miss a filing that
    landed before our first poll of the day.
    """
    start, _ = ER.event_window(TRADE_DATE, at(10, 0))
    old = start - timedelta(days=3)
    inside = start + timedelta(hours=1)

    by_first_seen = assess([Obs("RELIANCE", "Acquisition",
                                first_seen_at=inside, source_timestamp=old)])
    by_source = assess([Obs("RELIANCE", "Acquisition",
                            first_seen_at=old, source_timestamp=inside)])
    assert by_first_seen.risk_of("RELIANCE") == ER.ELEVATED
    assert by_source.risk_of("RELIANCE") == ER.ELEVATED


def test_an_immaterial_announcement_inside_the_window_does_not_flag():
    start, _ = ER.event_window(TRADE_DATE, at(10, 0))
    report = assess([Obs("RELIANCE", "Copy of Newspaper Publication",
                         first_seen_at=start + timedelta(hours=1))])
    assert report.risk_of("RELIANCE") == ER.CLEAR


def test_only_the_announcing_symbol_is_flagged():
    start, _ = ER.event_window(TRADE_DATE, at(10, 0))
    report = assess([Obs("RELIANCE", "Financial Results",
                         first_seen_at=start + timedelta(hours=1))])
    assert report.risk_of("RELIANCE") == ER.ELEVATED
    assert report.risk_of("INFY") == ER.CLEAR
    assert report.elevated_symbols == ("RELIANCE",)


# --------------------------------------------------------------------------------
# Scheduled events
# --------------------------------------------------------------------------------


def test_an_event_scheduled_today_is_flagged_as_a_board_meeting():
    report = assess([Obs("INFY", "Financial Results", scheduled_for=TRADE_DATE)])
    row = report.for_symbol("INFY")
    assert row.risk == ER.ELEVATED
    assert ER.CATEGORY_BOARD_MEETING in row.categories
    assert ER.CATEGORY_RESULTS in row.categories
    assert row.items[0]["kind"] == "SCHEDULED"


def test_a_board_meeting_with_an_unrecognised_purpose_still_flags():
    """Every calendar row IS a board-meeting intimation, whatever the stated purpose."""
    report = assess([Obs("INFY", "Other business matters", scheduled_for=TRADE_DATE)])
    assert report.risk_of("INFY") == ER.ELEVATED
    assert report.for_symbol("INFY").categories == (ER.CATEGORY_BOARD_MEETING,)


def test_an_event_scheduled_for_another_day_is_not_flagged_today():
    report = assess([Obs("INFY", "Financial Results",
                         scheduled_for=TRADE_DATE + timedelta(days=7))])
    assert report.risk_of("INFY") == ER.CLEAR


def test_the_lookahead_is_zero_sessions_by_default():
    assert ER.SCHEDULED_LOOKAHEAD_SESSIONS == 0


# --------------------------------------------------------------------------------
# StoreEventRiskSource
# --------------------------------------------------------------------------------


def test_the_store_source_reads_the_ledger_and_never_fetches(tmp_path):
    store = AnnouncementStore(tmp_path)
    start, _ = ER.event_window(TRADE_DATE, at(10, 0))
    rows = observation_rows(
        announcements=[{"symbol": "RELIANCE", "desc": "Credit Rating- Revision",
                        "seq_id": "7", "an_dt": f"{start:%d-%b-%Y %H:%M:%S}",
                        "attchmntText": "x"}],
        calendar=[],
    )
    store.record(source="announcement", rows=rows, observed_at=at(9, 30))

    source = ER.StoreEventRiskSource(store)
    report = source.assess(SYMS, TRADE_DATE, at(10, 0))
    assert report.data_state == ER.STATE_FRESH
    assert report.risk_of("RELIANCE") == ER.ELEVATED
    assert ER.CATEGORY_RATING_CHANGE in report.for_symbol("RELIANCE").categories
    # No network client is constructed anywhere in the read path.
    assert "NseAnnouncementClient" not in inspect.getsource(ER.StoreEventRiskSource)


def test_the_store_source_reparses_only_when_the_ledger_changes(tmp_path):
    store = AnnouncementStore(tmp_path)
    store.record_empty_poll("announcement", at(9, 30))
    source = ER.StoreEventRiskSource(store)

    calls = {"n": 0}
    original = store.observations

    def counting():
        calls["n"] += 1
        return original()

    store.observations = counting
    source.assess(SYMS, TRADE_DATE, at(10, 0))
    source.assess(SYMS, TRADE_DATE, at(10, 1))
    assert calls["n"] == 1, "the ledger was re-parsed with no change on disk"

    store.record_empty_poll("announcement", at(10, 2))
    source.assess(SYMS, TRADE_DATE, at(10, 3))
    assert calls["n"] == 2, "a ledger change did not invalidate the cache"


# --------------------------------------------------------------------------------
# Runtime and API
# --------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return PositionStore(tmp_path)


def live_runtime(store, *, event_risk_source=None, symbols=SYMS):
    """A LIVE-mode runtime over stored bars.

    LIVE because SessionRuntime.event_risk short-circuits REPLAY to UNKNOWN by design,
    so the ELEVATED path can only be exercised on the live branch. The feed is still a
    replay feed -- the same injection rule the live-feed tests follow.
    """
    bars = {s: flat_series(1000.0, 60) for s in symbols}
    for s, series in bars.items():
        store.save_bars(s, TRADE_DATE, series)
    feed = MultiSymbolReplayFeed(dict(bars))
    feed.advance_to(ist(11, 0))
    return SessionRuntime(
        mode=Mode.LIVE, store=store, symbols=list(symbols), trade_date=TRADE_DATE,
        feed=feed, tick_seconds=0.01, event_risk_source=event_risk_source,
    )


class FakeSource:
    def __init__(self, report=None, error=None):
        self.report = report
        self.error = error
        self.calls = 0

    def assess(self, symbols, trade_date, now):
        self.calls += 1
        if self.error:
            raise self.error
        return self.report


def elevated_report(now=None, symbols=SYMS):
    now = now or at(10, 0)
    start, _ = ER.event_window(TRADE_DATE, now)
    return ER.assess(
        {"a": Obs("RELIANCE", "Financial Results",
                  first_seen_at=start + timedelta(hours=1))},
        symbols, TRADE_DATE, now, last_poll_at=now,
    )


def test_replay_always_reports_unknown_and_says_why(store):
    for symbol in SYMS:
        store.save_bars(symbol, TRADE_DATE, flat_series(1000.0, 60))
    runtime = build_replay_runtime(store, SYMS, TRADE_DATE, speed=60.0, tick_seconds=0.01)
    report = runtime.event_risk()
    assert report.data_state == ER.STATE_UNAVAILABLE
    assert {s.risk for s in report.symbols} == {ER.UNKNOWN}
    assert "forward-collected" in report.reason
    assert "never will be" in report.reason


def test_replay_ignores_an_injected_source_entirely(store):
    """Structural: a replay must not consult a source built from today's ledger."""
    for symbol in SYMS:
        store.save_bars(symbol, TRADE_DATE, flat_series(1000.0, 60))
    runtime = build_replay_runtime(store, SYMS, TRADE_DATE, speed=60.0, tick_seconds=0.01)
    source = FakeSource(report=elevated_report())
    runtime.event_risk_source = source
    assert runtime.event_risk().data_state == ER.STATE_UNAVAILABLE
    assert source.calls == 0


def test_no_source_configured_means_unknown_not_clear(store):
    runtime = live_runtime(store, event_risk_source=None)
    report = runtime.event_risk()
    assert {s.risk for s in report.symbols} == {ER.UNKNOWN}
    assert "No announcements source" in report.reason


def test_a_broken_source_yields_unknown_rather_than_clearing(store):
    runtime = live_runtime(store, event_risk_source=FakeSource(
        error=RuntimeError("ledger on fire")))
    report = runtime.event_risk()
    assert {s.risk for s in report.symbols} == {ER.UNKNOWN}
    assert "ledger on fire" in report.reason


def test_position_cards_carry_the_flag(store):
    runtime = live_runtime(store, event_risk_source=FakeSource(elevated_report()))
    runtime.add_position("RELIANCE", 10, Decimal("1000.00"), entry_time=ist(9, 30))
    runtime.add_position("INFY", 10, Decimal("1000.00"), entry_time=ist(9, 30))
    cards = {c["symbol"]: c for c in runtime.evaluate_all()}
    assert cards["RELIANCE"]["event_risk"]["risk"] == ER.ELEVATED
    assert cards["INFY"]["event_risk"]["risk"] == ER.CLEAR
    assert ER.CATEGORY_RESULTS in cards["RELIANCE"]["event_risk"]["categories"]


def test_check_entry_carries_the_flag(store):
    runtime = live_runtime(store, event_risk_source=FakeSource(elevated_report()))
    assert runtime.check_entry("RELIANCE")["event_risk"]["risk"] == ER.ELEVATED
    assert runtime.check_entry("INFY")["event_risk"]["risk"] == ER.CLEAR


def test_check_entry_all_assesses_once_and_matches_the_single_check(store):
    source = FakeSource(elevated_report())
    runtime = live_runtime(store, event_risk_source=source)
    bulk = {r["symbol"]: r for r in runtime.check_entry_all()["results"]}
    assert source.calls == 1, "event risk was assessed once per symbol instead of once"
    for symbol in SYMS:
        assert bulk[symbol] == runtime.check_entry(symbol)


# --------------------------------------------------------------------------------
# THE SAFETY PROPERTY: this annotates, it never decides
# --------------------------------------------------------------------------------


def test_event_risk_never_changes_advice(store, monkeypatch):
    """Byte-identical advice with and without an elevated flag.

    Suppressing or altering advice on a news day is the worst available reading of
    "normal technical reasoning does not apply": the one thing you certainly still need
    on a day the stock is gapping on news is to be told to be flat by 15:15.

    The clock is frozen so the comparison can be exact. Without that, LIVE mode's
    ``minutes_held`` and ``price_age_seconds`` drift between the two runs and the
    assertion would have to be loosened to a field subset -- which is precisely where a
    real difference could hide.
    """
    frozen = ist(11, 0)
    monkeypatch.setattr("src.api.runtime.now_ist", lambda: frozen)

    def advice_without_annotation(source):
        runtime = live_runtime(store, event_risk_source=source)
        runtime.add_position("RELIANCE", 10, Decimal("1000.00"),
                             entry_time=ist(9, 30), position_id="fixed-id")
        cards = runtime.evaluate_all()
        for card in cards:
            card.pop("event_risk", None)
        return cards

    plain = advice_without_annotation(None)
    for position in store.list_positions(TRADE_DATE, status="OPEN"):
        store.close_position(position.position_id, TRADE_DATE, Decimal("1000"), frozen)
    flagged = advice_without_annotation(FakeSource(elevated_report()))

    assert plain, "the fixture produced no advice, so this proves nothing"
    assert plain == flagged, "the event-risk flag altered the advice"


def test_the_exit_engine_does_not_know_event_risk_exists():
    """Structural. The engine must not grow a dependency it could branch on."""
    import src.positions.exit_engine as engine

    source = inspect.getsource(engine)
    for term in ("event_risk", "ELEVATED_EVENT_RISK", "announcements"):
        assert term not in source, f"the exit engine references {term!r}"


def test_force_exit_still_fires_under_elevated_event_risk(store):
    """The loudest advice must survive the flag. Pinned because it is the whole risk."""
    runtime = live_runtime(store, event_risk_source=FakeSource(elevated_report()))
    runtime.add_position("RELIANCE", 10, Decimal("1000.00"), entry_time=ist(9, 30))
    runtime.feed.advance_to(ist(15, 20))
    # LIVE mode reads the wall clock, so drive evaluate() through the engine directly
    # with an explicit post-square-off time -- the same injection the engine is built for.
    from src.positions.exit_engine import evaluate

    position = runtime.open_positions()[0]
    advice = evaluate(position, runtime._runtime_for(position),
                      runtime.feed.bars("RELIANCE"), ist(15, 20),
                      runtime.feed.last_tick_at("RELIANCE"))
    assert advice.state.value == "FORCE_EXIT"


def test_the_api_exposes_event_risk_and_the_page_can_read_it(store):
    runtime = live_runtime(store, event_risk_source=FakeSource(elevated_report()))
    with TestClient(create_app(runtime, run_loop=False)) as client:
        body = client.get("/event-risk").json()
        assert body["data_state"] == ER.STATE_FRESH
        assert body["elevated"] == ["RELIANCE"]
        assert body["is_ranking"] is False

        positions = client.get("/positions").json()
        assert positions["event_risk"]["elevated"] == ["RELIANCE"]

        rows = client.get("/check-entry-all").json()["results"]
        assert {r["symbol"]: r["event_risk"]["risk"] for r in rows} == {
            "RELIANCE": ER.ELEVATED, "INFY": ER.CLEAR}


def test_the_snapshot_carries_event_risk_for_the_websocket(store):
    runtime = live_runtime(store, event_risk_source=FakeSource(elevated_report()))
    snap = runtime.snapshot()
    assert snap["event_risk"]["elevated"] == ["RELIANCE"]


def test_there_is_no_route_that_predicts_from_announcement_content(store):
    runtime = live_runtime(store, event_risk_source=FakeSource(elevated_report()))
    app = create_app(runtime, run_loop=False)
    paths = {r.path for r in app.routes}
    for banned in ("/sentiment", "/news", "/predictions", "/signals", "/scores"):
        assert banned not in paths


# --------------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------------


def test_the_page_says_it_is_a_filter_and_that_unknown_is_not_clear():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
        encoding="utf-8")
    lowered = html.lower()
    assert "exclusion filter, not a" in lowered
    assert "unknown is not clear" in lowered
    assert "nothing about direction" in lowered
    assert "improves returns" in lowered
    # Forward-collect only, said on the page rather than only in the code.
    assert "forward-collected only" in lowered


def test_the_page_does_not_style_an_elevated_row_differently_from_a_clear_one():
    """The badge lives in a cell; the <tr> className must not depend on event risk.

    A row-level highlight is how a filter becomes a recommendation, and that holds
    whichever direction the highlight points in.
    """
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
        encoding="utf-8")
    panel = html[html.index("function CheckAllPanel"):html.index("function Panel(")]
    row_open = panel[panel.index("<tr key={r.symbol}"):]
    row_tag = row_open[:row_open.index(">")]
    assert "event_risk" not in row_tag, "the <tr> class depends on event risk"
    assert "border-t border-slate-800 align-top" in row_tag


def test_the_unknown_state_is_not_styled_like_clear():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
        encoding="utf-8")
    block = html[html.index("const EVENT_RISK_STYLE"):]
    block = block[:block.index("}")]
    lines = {line.split(":")[0].strip(): line for line in block.splitlines()
             if ":" in line and "bg-" in line}
    assert lines["CLEAR"].split(":", 1)[1] != lines["UNKNOWN"].split(":", 1)[1], (
        "UNKNOWN renders identically to CLEAR")
