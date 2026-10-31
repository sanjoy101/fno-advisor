"""The forward-only announcement collector: immutability, revisions, and no backfill.

The property these tests exist to protect is that ``first_seen_at`` is OUR clock and
cannot be moved by a later edit at the source. Everything else in the event-risk feature
rests on it, and no other harness in this repo can catch a violation -- a revised filing
leaves no trace of what it used to say.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from src.data.announcements import (
    AnnouncementStore,
    NseAnnouncementClient,
    announcement_key,
    calendar_key,
    collect_once,
    observation_rows,
    parse_announcements,
    parse_event_calendar,
)
from src.market.calendar import IST

DAY = date(2026, 8, 20)


def at(h: int, m: int, s: int = 0, d: date = DAY) -> datetime:
    from datetime import time

    return datetime.combine(d, time(h, m, s), tzinfo=IST)


def ann_row(symbol="RELIANCE", desc="Financial Results", seq="1001",
            an_dt="20-Aug-2026 08:30:00", text="the company informed the exchange"):
    return {"symbol": symbol, "desc": desc, "seq_id": seq, "an_dt": an_dt,
            "attchmntText": text, "attchmntFile": "https://example.invalid/a.pdf"}


def cal_row(symbol="INFY", purpose="Financial Results", d="20-Aug-2026",
            bm_desc="To consider results"):
    return {"symbol": symbol, "purpose": purpose, "date": d, "bm_desc": bm_desc,
            "company": "X"}


@pytest.fixture
def store(tmp_path):
    return AnnouncementStore(tmp_path)


# --------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------


def test_parse_accepts_both_bare_list_and_wrapped_payloads():
    rows = [ann_row(), ann_row(symbol="INFY", seq="1002")]
    assert len(parse_announcements(json.dumps(rows).encode())) == 2
    assert len(parse_announcements(json.dumps({"data": rows}).encode())) == 2


def test_parse_narrows_to_our_symbols():
    rows = [ann_row(symbol="RELIANCE"), ann_row(symbol="TEGA", seq="9")]
    got = parse_announcements(json.dumps(rows).encode(), symbols=["reliance"])
    assert [r["symbol"] for r in got] == ["RELIANCE"]


def test_parse_rejects_an_unrecognised_payload():
    with pytest.raises(ValueError, match="unrecognised announcements payload"):
        parse_announcements(b'"a string"')
    with pytest.raises(ValueError, match="unrecognised event-calendar payload"):
        parse_event_calendar(b'42')


def test_keys_are_stable_across_a_content_revision():
    """A revision must land on the SAME key, or it looks like a brand-new filing."""
    original = ann_row(desc="Financial Results", text="original wording")
    revised = ann_row(desc="Financial Results", text="COMPLETELY REWRITTEN")
    assert announcement_key(original) == announcement_key(revised)


def test_key_falls_back_when_the_exchange_omits_seq_id():
    row = ann_row(seq="")
    key = announcement_key(row)
    assert key.startswith("ann:RELIANCE|")
    assert announcement_key(dict(row, symbol="reliance")) == key


def test_calendar_key_distinguishes_purpose_and_date():
    a = calendar_key(cal_row())
    assert a != calendar_key(cal_row(d="21-Aug-2026"))
    assert a != calendar_key(cal_row(purpose="Fund Raising"))


# --------------------------------------------------------------------------------
# first_seen_at is ours and never moves
# --------------------------------------------------------------------------------


def test_first_seen_at_is_the_earliest_sighting_not_the_latest(store):
    rows = observation_rows(announcements=[ann_row()], calendar=[])
    store.record(source="announcement", rows=rows, observed_at=at(9, 20))
    store.record(source="announcement", rows=rows, observed_at=at(11, 45))
    store.record(source="announcement", rows=rows, observed_at=at(15, 5))

    obs = store.observations()
    assert len(obs) == 1
    only = next(iter(obs.values()))
    assert only.first_seen_at == at(9, 20), (
        "a later sighting moved first_seen_at forward; it must be the earliest")


def test_a_later_edit_at_the_source_cannot_move_first_seen_at_earlier(store):
    """The whole point. A revision is new content, not new evidence about the past."""
    store.record(source="announcement",
                 rows=observation_rows(announcements=[ann_row(text="v1")], calendar=[]),
                 observed_at=at(13, 0))
    # The source now claims it was broadcast at 08:00 and rewrites the body.
    revised = ann_row(text="v2 rewritten", an_dt="20-Aug-2026 08:00:00")
    store.record(source="announcement",
                 rows=observation_rows(announcements=[revised], calendar=[]),
                 observed_at=at(13, 30))

    only = next(iter(store.observations().values()))
    assert only.first_seen_at == at(13, 0)
    assert only.revised is True, "a content change must be surfaced, not smoothed over"


def test_a_repeat_sighting_is_not_a_revision(store):
    rows = observation_rows(announcements=[ann_row()], calendar=[])
    first = store.record(source="announcement", rows=rows, observed_at=at(9, 20))
    second = store.record(source="announcement", rows=rows, observed_at=at(9, 35))
    assert first["new"] == 1 and first["revised"] == 0
    assert second["new"] == 0 and second["revised"] == 0 and second["repeated"] == 1
    assert next(iter(store.observations().values())).revised is False


def test_the_ledger_is_append_only(store):
    rows = observation_rows(announcements=[ann_row()], calendar=[])
    store.record(source="announcement", rows=rows, observed_at=at(9, 20))
    after_first = store.ledger_path.read_text(encoding="utf-8")
    store.record(source="announcement", rows=rows, observed_at=at(9, 35))
    after_second = store.ledger_path.read_text(encoding="utf-8")
    assert after_second.startswith(after_first), "earlier ledger lines were rewritten"


def test_an_unreadable_ledger_line_is_skipped_not_fatal(store):
    store.record(source="announcement",
                 rows=observation_rows(announcements=[ann_row()], calendar=[]),
                 observed_at=at(9, 20))
    with store.ledger_path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    assert len(store.observations()) == 1


# --------------------------------------------------------------------------------
# "We looked and saw nothing" vs "we have not looked"
# --------------------------------------------------------------------------------


def test_last_poll_at_is_none_before_anything_is_collected(store):
    assert store.last_poll_at() is None


def test_an_empty_poll_still_records_that_we_looked(store):
    """Otherwise no news is indistinguishable from no collector."""
    store.record_empty_poll("announcement", at(10, 0))
    assert store.last_poll_at() == at(10, 0)
    assert store.observations() == {}, "a heartbeat must not become an announcement"


def test_last_poll_at_tracks_the_newest_sighting(store):
    store.record_empty_poll("announcement", at(10, 0))
    store.record(source="announcement",
                 rows=observation_rows(announcements=[ann_row()], calendar=[]),
                 observed_at=at(10, 15))
    assert store.last_poll_at() == at(10, 15)


# --------------------------------------------------------------------------------
# collect_once: raw first, and an outage is a state
# --------------------------------------------------------------------------------


class FakeClient:
    """Injected in place of the real one, same rule as the live feed's fakes."""

    def __init__(self, announcements=None, calendar=None,
                 ann_error=None, cal_error=None):
        self.announcements = announcements if announcements is not None else []
        self.calendar = calendar if calendar is not None else []
        self.ann_error = ann_error
        self.cal_error = cal_error
        self.requested_days = []

    def fetch_announcements(self, day):
        self.requested_days.append(day)
        if self.ann_error:
            raise self.ann_error
        return json.dumps(self.announcements).encode()

    def fetch_event_calendar(self):
        if self.cal_error:
            raise self.cal_error
        return json.dumps(self.calendar).encode()


def test_collect_once_persists_raw_before_parsing(store):
    client = FakeClient(announcements=[ann_row()], calendar=[cal_row()])
    collect_once(client, store, ["RELIANCE", "INFY"], DAY, now=at(10, 0))

    saved = sorted(p.name for p in
                   (store.raw_dir / "2026" / "2026-08-20").glob("*.json"))
    assert saved == ["ann_100000.json", "cal_100000.json"]
    # The RAW payload, not a filtered or normalised one -- a parser change must replay
    # from disk without asking NSE again.
    raw = json.loads((store.raw_dir / "2026" / "2026-08-20"
                      / "ann_100000.json").read_bytes())
    assert raw == [ann_row()]


def test_collect_once_records_both_feeds(store):
    client = FakeClient(announcements=[ann_row()], calendar=[cal_row()])
    out = collect_once(client, store, ["RELIANCE", "INFY"], DAY, now=at(10, 0))
    assert out["recorded"]["new"] == 2
    sources = {o.source for o in store.observations().values()}
    assert sources == {"announcement", "calendar"}


def test_collect_once_survives_one_feed_failing(store):
    client = FakeClient(announcements=[ann_row()],
                        cal_error=RuntimeError("nse timed out"))
    out = collect_once(client, store, ["RELIANCE"], DAY, now=at(10, 0))
    assert out["recorded"]["new"] == 1
    assert any("calendar" in e for e in out["errors"])


def test_collect_once_reports_a_total_outage_rather_than_clearing_anything(store):
    client = FakeClient(ann_error=RuntimeError("down"), cal_error=RuntimeError("down"))
    out = collect_once(client, store, ["RELIANCE"], DAY, now=at(10, 0))
    assert out["recorded"] is None
    assert len(out["errors"]) == 2
    assert store.last_poll_at() is None, (
        "a failed poll must not look like a successful one")


def test_collect_once_filters_to_our_symbols_but_counts_the_whole_feed(store):
    client = FakeClient(
        announcements=[ann_row(symbol="RELIANCE"), ann_row(symbol="TEGA", seq="2")],
        calendar=[cal_row(symbol="DHRUV")],
    )
    out = collect_once(client, store, ["RELIANCE"], DAY, now=at(10, 0))
    assert out["announcements_all"] == 2
    assert out["announcements_ours"] == 1
    assert {o.symbol for o in store.observations().values()} == {"RELIANCE"}


def test_a_successful_poll_with_nothing_for_us_still_marks_that_we_looked(store):
    client = FakeClient(announcements=[ann_row(symbol="TEGA")], calendar=[])
    collect_once(client, store, ["RELIANCE"], DAY, now=at(10, 0))
    assert store.last_poll_at() == at(10, 0)
    assert store.observations() == {}


# --------------------------------------------------------------------------------
# No historical backfill. Structural, because this one cannot be caught downstream.
# --------------------------------------------------------------------------------


def test_the_client_only_ever_requests_a_single_day(store):
    """Both date bounds are the same day. There is no range parameter to widen."""
    import inspect

    source = inspect.getsource(NseAnnouncementClient.fetch_announcements)
    assert "from_date={stamp}&to_date={stamp}" in source, (
        "fetch_announcements must pin both bounds to the same session; a range is how "
        "a forward-only collector quietly becomes a backfill"
    )


def test_no_function_in_the_module_is_named_like_a_backfill():
    """A named backfill is the shape this feature must never grow.

    Retroactively edited filings cannot reconstruct what was knowable at the time, and
    neither the poison harness nor assert_session_causal can detect that class of leak
    -- so the only safe posture is to never create the data.
    """
    import src.data.announcements as mod

    offenders = [
        name for name in dir(mod)
        if callable(getattr(mod, name, None))
        and any(word in name.lower() for word in ("backfill", "history", "historical"))
    ]
    assert not offenders, f"historical-collection helpers present: {offenders}"


def test_collect_once_requests_only_the_day_it_was_given(store):
    client = FakeClient(announcements=[], calendar=[])
    collect_once(client, store, ["RELIANCE"], DAY, now=at(10, 0))
    collect_once(client, store, ["RELIANCE"], DAY, now=at(10, 30))
    assert client.requested_days == [DAY, DAY]


def test_the_module_documents_why_there_is_no_backfill():
    import src.data.announcements as mod

    doc = (mod.__doc__ or "").lower()
    assert "revis" in doc and "backfill" in doc
    assert "first_seen_at" in doc
