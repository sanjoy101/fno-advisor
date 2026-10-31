"""Tests for the Module C backend and the replay runtime.

Two properties get the most attention: replay and live must be indistinguishable to
everything downstream, and the API must offer no way to reach a signal or an order.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.runtime import Mode, SessionRuntime, build_replay_runtime
from src.market.calendar import IST
from src.positions.feed import MultiSymbolReplayFeed
from src.positions.models import PositionState
from src.positions.store import PositionStore
from tests.conftest import TRADE_DATE, flat_series, ist, make_series

SYMS = ["RELIANCE", "INFY"]


def session(price=1000.0, n=200, spread=1.2, day=TRADE_DATE):
    return make_series([price] * n, start=ist(9, 15, d=day),
                       volumes=[20000] * n, spread=spread)


def falling(day=TRADE_DATE, n=200):
    return make_series([1000 - i * 0.4 for i in range(n)], start=ist(9, 15, d=day),
                       volumes=[20000] * n, spread=1.2)


@pytest.fixture
def store(tmp_path):
    s = PositionStore(tmp_path)
    s.save_bars("RELIANCE", TRADE_DATE, session())
    s.save_bars("INFY", TRADE_DATE, falling())
    return s


@pytest.fixture
def runtime(store):
    return build_replay_runtime(store, SYMS, TRADE_DATE, speed=60.0, tick_seconds=0.01)


@pytest.fixture
def client(runtime):
    # run_loop=False: the clock is advanced explicitly, so tests stay deterministic
    # instead of racing the background timer.
    with TestClient(create_app(runtime, run_loop=False)) as c:
        yield c


def advance(runtime, to_time):
    runtime._virtual_now = ist(*to_time)
    runtime.feed.advance_to(runtime._virtual_now)


# --------------------------------------------------------------------------------
# Replay / live parity
# --------------------------------------------------------------------------------


def test_the_replay_feed_matches_the_live_feed_interface():
    """The UI must not be able to tell them apart."""
    from src.positions.live_feed import FyersLiveFeed

    replay = MultiSymbolReplayFeed({"RELIANCE": session()})
    live = FyersLiveFeed(["RELIANCE"], history=None)
    for name in ("bars", "last_tick_at", "status", "stop"):
        assert hasattr(replay, name) and hasattr(live, name), name


def test_replay_mode_is_labelled_in_health(runtime):
    assert runtime.health()["mode"] == Mode.REPLAY
    assert runtime.feed.status.mode == "REPLAY"


def test_replay_uses_a_virtual_clock_not_the_wall_clock(runtime):
    advance(runtime, (11, 0))
    assert runtime.now() == ist(11, 0)


def test_replay_only_reveals_completed_bars(runtime):
    advance(runtime, (9, 30))
    bars = runtime.feed.bars("RELIANCE")
    assert bars
    assert max(b.ts for b in bars) < ist(9, 30)


def test_replay_last_tick_is_the_last_completed_bar_close(runtime):
    advance(runtime, (9, 30))
    assert runtime.feed.last_tick_at("RELIANCE") == ist(9, 30)


def test_a_gap_in_stored_bars_surfaces_as_staleness(store):
    early = make_series([1000.0] * 20, start=ist(9, 15), volumes=[20000] * 20, spread=1.0)
    late = make_series([1000.0] * 20, start=ist(10, 30), volumes=[20000] * 20, spread=1.0)
    feed = MultiSymbolReplayFeed({"RELIANCE": early + late})
    feed.advance_to(ist(10, 0))
    stale = (ist(10, 0) - feed.last_tick_at("RELIANCE")).total_seconds()
    assert stale > 30, "a hole in stored data must not be bridged silently"


# --------------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------------


def test_add_position_records_it(client):
    r = client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00"})
    assert r.status_code == 201
    body = r.json()
    assert body["symbol"] == "RELIANCE"
    assert "did not place an order" in body["note"]


def test_an_exact_duplicate_position_is_rejected(client):
    """Two identical cards read as one position rendered twice. They are not."""
    body = {"symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
            "entry_time": ist(9, 30).isoformat()}
    assert client.post("/positions", json=body).status_code == 201
    second = client.post("/positions", json=body)
    assert second.status_code == 409
    assert "already recorded" in second.json()["detail"]
    assert len(client.get("/positions").json()["positions"]) == 1


def test_a_duplicate_can_be_recorded_when_explicitly_allowed(client):
    """Averaging in at the same price is unlikely, not impossible. Opt in."""
    body = {"symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
            "entry_time": ist(9, 30).isoformat()}
    assert client.post("/positions", json=body).status_code == 201
    assert client.post("/positions", json={**body, "allow_duplicate": True}
                       ).status_code == 201
    assert len(client.get("/positions").json()["positions"]) == 2


def test_a_position_differing_in_any_field_is_not_a_duplicate(client):
    base = {"symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
            "entry_time": ist(9, 30).isoformat()}
    assert client.post("/positions", json=base).status_code == 201
    for change in ({"quantity": 50}, {"entry_price": "1000.05"},
                   {"entry_time": ist(9, 31).isoformat()}, {"symbol": "INFY"}):
        assert client.post("/positions", json={**base, **change}).status_code == 201


def test_add_position_rejects_a_stop_above_entry(client):
    r = client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 10, "entry_price": "1000",
        "stop_loss": "1100"})
    assert r.status_code == 400


def test_add_position_rejects_zero_quantity(client):
    r = client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 0, "entry_price": "1000"})
    assert r.status_code == 422


def test_listing_positions_returns_state_and_health(client, runtime):
    advance(runtime, (10, 0))
    client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
        "entry_time": ist(9, 45).isoformat()})
    data = client.get("/positions").json()
    assert len(data["positions"]) == 1
    card = data["positions"][0]
    assert card["state"]
    assert card["reason"]
    assert data["health"]["mode"] == "REPLAY"


def test_every_card_carries_gross_and_net_together(client, runtime):
    advance(runtime, (10, 0))
    client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
        "entry_time": ist(9, 45).isoformat()})
    card = client.get("/positions").json()["positions"][0]
    assert card["gross_pnl"] is not None
    assert card["net_pnl"] is not None
    assert Decimal(card["net_pnl"]) < Decimal(card["gross_pnl"])


def test_every_price_carries_its_age(client, runtime):
    advance(runtime, (10, 0))
    client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
        "entry_time": ist(9, 45).isoformat()})
    card = client.get("/positions").json()["positions"][0]
    assert card["last_price"] is not None
    assert card["price_age_seconds"] is not None


def test_closing_a_position_reports_realised_pnl(client, runtime):
    advance(runtime, (10, 0))
    pid = client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
        "entry_time": ist(9, 45).isoformat()}).json()["position_id"]
    r = client.request("DELETE", f"/positions/{pid}", json={"exit_price": "1010.00"})
    assert r.status_code == 200
    body = r.json()
    assert Decimal(body["net_pnl"]) < Decimal(body["gross_pnl"])
    assert client.get("/positions").json()["positions"] == []


def test_closing_an_unknown_position_is_404(client):
    r = client.request("DELETE", "/positions/nope", json={"exit_price": "100"})
    assert r.status_code == 404


def test_a_stale_position_reports_stale_state(client, runtime):
    """The gap is in the stored session, so the engine must suppress advice."""
    advance(runtime, (10, 0))
    client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
        "entry_time": ist(9, 45).isoformat()})
    runtime._virtual_now = ist(14, 0)  # clock jumps, no new bars revealed
    card = client.get("/positions").json()["positions"][0]
    assert card["state"] == "STALE_DATA"
    assert card["feed_stale"] is True
    assert card["pnl_is_stale"] is True


def test_force_exit_appears_after_the_square_off(client, runtime):
    advance(runtime, (10, 0))
    client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
        "entry_time": ist(9, 45).isoformat()})
    advance(runtime, (15, 20))
    card = client.get("/positions").json()["positions"][0]
    assert card["state"] == "FORCE_EXIT"
    assert card["suggested_exit_quantity"] == 100


# --------------------------------------------------------------------------------
# Check entry
# --------------------------------------------------------------------------------


def test_check_entry_records_nothing(client, runtime, store):
    advance(runtime, (11, 0))
    r = client.post("/check-entry", json={"symbol": "INFY"})
    assert r.status_code == 200
    assert r.json()["recorded"] is False
    assert store.list_positions(TRADE_DATE, status=None) == []


def test_check_entry_flags_a_bad_moment(client, runtime):
    advance(runtime, (11, 0))
    body = client.post("/check-entry", json={"symbol": "INFY"}).json()
    assert body["valid"] is False, "INFY is falling all session"
    assert body["problems"]


def test_check_entry_with_no_bars_says_so(client, runtime):
    runtime._virtual_now = ist(9, 15)
    body = client.post("/check-entry", json={"symbol": "RELIANCE"}).json()
    assert body["valid"] is None
    assert "nothing to judge" in body["summary"].lower()


# --------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------


# --------------------------------------------------------------------------------
# "No verdict" has two causes and only one of them resolves by waiting.
#
# The panel said "No completed bar for RISHABH yet" for a symbol that was never
# subscribed. There is no feed behind it, so that bar is never coming -- the message
# sent the user back to refresh a panel that could not change.
# --------------------------------------------------------------------------------


def test_an_unsubscribed_symbol_says_waiting_will_not_help(client, runtime):
    advance(runtime, (10, 0))
    body = client.post("/check-entry", json={"symbol": "RISHABH"}).json()

    assert body["valid"] is None
    assert body["reason"] == "NOT_SUBSCRIBED"
    assert "not in the tracked universe" in body["summary"]
    assert "no bar for it will ever arrive" in body["summary"], \
        "it must say the bar is not coming, not merely that there isn't one"
    assert "--symbols" in body["summary"], "and how to fix it"
    for symbol in SYMS:
        assert symbol in body["summary"], "the tracked list must be named, not implied"
    assert body["tracked_symbols"] == sorted(SYMS)
    assert body["recorded"] is False


def test_a_subscribed_symbol_with_no_bar_yet_says_only_that(client, runtime):
    """09:15:30 on a tracked symbol: the bar really is coming."""
    runtime._virtual_now = ist(9, 15, 30)
    body = client.post("/check-entry", json={"symbol": "RELIANCE"}).json()

    assert body["valid"] is None
    assert body["reason"] == "NO_BAR_YET"
    assert body["summary"] == (
        "First bar for RELIANCE has not closed yet — nothing to judge the entry against."
    )
    assert "tracked universe" not in body["summary"]


def test_the_two_no_verdict_cases_do_not_share_a_message(client, runtime):
    """The regression: one message for both is what made waiting look useful."""
    runtime._virtual_now = ist(9, 15, 30)
    not_subscribed = client.post("/check-entry", json={"symbol": "RISHABH"}).json()
    no_bar = client.post("/check-entry", json={"symbol": "RELIANCE"}).json()

    assert not_subscribed["valid"] is no_bar["valid"] is None, "both give no verdict"
    assert not_subscribed["reason"] != no_bar["reason"]
    assert not_subscribed["summary"] != no_bar["summary"]


def test_an_unsubscribed_symbol_is_not_a_server_error(client, runtime):
    """It is a legitimate question with a useful answer, not a 4xx."""
    advance(runtime, (10, 0))
    assert client.post("/check-entry", json={"symbol": "RISHABH"}).status_code == 200


def test_a_reached_verdict_carries_no_reason(client, runtime):
    advance(runtime, (10, 0))
    body = client.post("/check-entry", json={"symbol": "RELIANCE"}).json()
    assert body["valid"] in (True, False)
    assert body["reason"] is None


def test_every_bulk_row_has_a_verdict_or_a_named_reason(client, runtime):
    """check_entry_all only ever asks about subscribed symbols, so NOT_SUBSCRIBED
    must never appear there -- if it does, the two have drifted apart."""
    advance(runtime, (10, 0))
    for row in client.get("/check-entry-all").json()["results"]:
        assert row.get("reason") != "NOT_SUBSCRIBED"


# --------------------------------------------------------------------------------
# Bulk entry check: a FILTER, and it must never drift into being a ranking.
# Module B ranked this universe and produced no edge at any horizon (CLAUDE.md 2.3).
# --------------------------------------------------------------------------------


def test_check_entry_all_covers_every_subscribed_symbol(client, runtime):
    advance(runtime, (10, 0))
    body = client.get("/check-entry-all").json()
    assert [r["symbol"] for r in body["results"]] == sorted(SYMS)


def test_check_entry_all_is_alphabetical_and_nothing_else(store):
    """The ONLY permitted ordering. Not by verdict, not by anything computed.

    Symbols are registered in deliberately non-alphabetical order, and INFY is built
    to fail the check while RELIANCE passes -- so an implementation that grouped
    SOUND first, or preserved subscription order, would produce a different list.
    """
    syms = ["TCS", "INFY", "RELIANCE", "ABB"]
    for s in syms:
        store.save_bars(s, TRADE_DATE, falling() if s == "INFY" else session())
    rt = build_replay_runtime(store, syms, TRADE_DATE, speed=60.0, tick_seconds=0.01)
    rt._virtual_now = ist(11, 0)
    rt.feed.advance_to(rt._virtual_now)

    with TestClient(create_app(rt, run_loop=False)) as c:
        results = c.get("/check-entry-all").json()["results"]

    order = [r["symbol"] for r in results]
    assert order == ["ABB", "INFY", "RELIANCE", "TCS"], order
    assert order == sorted(order)
    # And the verdicts are genuinely mixed, so alphabetical is not accidentally
    # agreeing with a verdict-grouped order.
    assert len({r["valid"] for r in results}) > 1, "test needs mixed verdicts to bite"


def test_check_entry_all_carries_no_score_or_rank_field(client, runtime):
    """The response must not contain the fields a ranking would need.

    This is the guard the constraint rests on: adding a score is how a filter quietly
    becomes the signal panel that failed evaluation.
    """
    advance(runtime, (10, 0))
    body = client.get("/check-entry-all").json()

    banned = {
        "score", "composite_score", "rank", "ranking", "rating", "confidence",
        "probability", "prob", "expected_return", "edge", "signal", "conviction",
        "best", "top", "recommended", "preferred", "priority", "weight", "percentile",
        "z_score", "zscore", "strength", "grade", "stars", "target_price",
    }

    def walk(node, path="root"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in banned, f"ranking field {key!r} at {path}"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(body)
    assert body["is_ranking"] is False
    assert body["ordering"] == "alphabetical"


def test_check_entry_all_states_that_sound_is_not_a_prediction(client, runtime):
    advance(runtime, (10, 0))
    body = client.get("/check-entry-all").json()
    disclaimer = body["disclaimer"].lower()
    assert "not" in disclaimer and "rise" in disclaimer
    assert "not ranked" in disclaimer


def test_check_entry_all_records_nothing(client, runtime, store):
    advance(runtime, (10, 0))
    before = store.list_positions(TRADE_DATE, status=None)
    body = client.get("/check-entry-all").json()
    assert body["recorded"] is False
    assert store.list_positions(TRADE_DATE, status=None) == before
    assert client.get("/positions").json()["positions"] == []


def test_check_entry_all_rows_match_single_check_entry_exactly(client, runtime):
    """One implementation. If these diverge, there are two entry checks."""
    advance(runtime, (10, 0))
    bulk = {r["symbol"]: r for r in client.get("/check-entry-all").json()["results"]}
    for symbol in SYMS:
        single = client.post("/check-entry", json={"symbol": symbol}).json()
        assert bulk[symbol] == single, f"{symbol} differs between bulk and single"


def test_check_entry_all_reuses_the_single_check_implementation():
    """Structural, not behavioural: check_entry_all must call check_entry."""
    import inspect

    from src.api.runtime import SessionRuntime

    source = inspect.getsource(SessionRuntime.check_entry_all)
    assert "self.check_entry(" in source, (
        "check_entry_all must delegate to check_entry, not reimplement the predicates")


def test_health_reports_everything_the_header_needs(client, runtime):
    advance(runtime, (10, 0))
    h = client.get("/health").json()
    for key in ("mode", "now", "session_state", "feed", "token", "symbols",
                "square_off_at"):
        assert key in h
    assert "tick_age_seconds" in h["feed"]
    assert "stale" in h["feed"]


def test_health_never_returns_the_token_itself(client, runtime):
    h = client.get("/health").json()
    assert "access_token" not in json.dumps(h)


def test_replay_health_marks_the_token_not_applicable(client):
    assert client.get("/health").json()["token"]["applicable"] is False


@pytest.mark.parametrize("at,expected", [
    ((9, 0), "PRE-OPEN"), ((11, 0), "OPEN"),
    ((15, 20), "SQUARE-OFF WINDOW"), ((15, 45), "CLOSED"),
])
def test_session_state_transitions(client, runtime, at, expected):
    runtime._virtual_now = ist(*at)
    assert client.get("/health").json()["session_state"] == expected


# --------------------------------------------------------------------------------
# Stream
# --------------------------------------------------------------------------------


def test_the_stream_pushes_a_snapshot_on_connect(client, runtime):
    advance(runtime, (10, 0))
    client.post("/positions", json={
        "symbol": "RELIANCE", "quantity": 100, "entry_price": "1000.00",
        "entry_time": ist(9, 45).isoformat()})
    with client.websocket_connect("/stream") as ws:
        payload = ws.receive_json()
        assert "health" in payload and "positions" in payload
        assert len(payload["positions"]) == 1


# --------------------------------------------------------------------------------
# The "checked today" scoreboard.
#
# A record of the past. It is the most dangerous surface on the page: a table of
# symbols with a return column beside each is a buy list unless something structural
# stops it being one, and Module B's ranking failed evaluation at every horizon.
# --------------------------------------------------------------------------------


def test_a_check_is_logged_with_verdict_and_price(client, runtime):
    advance(runtime, (10, 0))
    client.post("/check-entry", json={"symbol": "RELIANCE"})

    rows = client.get("/checks").json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "RELIANCE"
    assert row["verdict"] in ("SOUND", "UNSOUND")
    assert row["price_at_check"] is not None
    assert row["reason"]
    assert row["checked_at"]


def test_a_check_that_reached_no_verdict_is_not_logged(client, runtime):
    """NOT_SUBSCRIBED and NO_BAR_YET evaluated nothing and can never settle."""
    advance(runtime, (10, 0))
    client.post("/check-entry", json={"symbol": "RISHABH"})
    runtime._virtual_now = ist(9, 15, 30)
    client.post("/check-entry", json={"symbol": "RELIANCE"})
    assert client.get("/checks").json()["rows"] == []


def test_no_verdict_is_given_on_a_stale_feed(client, runtime):
    """Exit advice is suppressed on stale data; an entry verdict must be too.

    All three entry predicates are price-based and read the same bars the exit rules
    read. Answering "price is already below VWAP" from a price an hour old states the
    past in the present tense -- the same fault as a header reading LIVE with no data.
    """
    advance(runtime, (10, 0))
    runtime._virtual_now = ist(11, 45)          # clock moves on, bars do not

    body = client.post("/check-entry", json={"symbol": "RELIANCE"}).json()
    assert body["valid"] is None
    assert body["reason"] == "STALE_FEED"
    assert "suppressed" in body["summary"]
    assert body["stale_seconds"] > 60 * 60
    assert client.get("/checks").json()["rows"] == [], (
        "a verdict that was refused must not reach the scoreboard")


def test_no_verdict_is_given_once_the_market_has_closed(client, runtime):
    """The case that actually bit: a check at 18:18 on a session that ended at 15:30.

    It is a clock fact and needs no threshold, and such a check could never settle --
    its horizon is already past with no future bars to reach.
    """
    advance(runtime, (10, 0))
    runtime._virtual_now = ist(18, 18)

    body = client.post("/check-entry", json={"symbol": "RELIANCE"}).json()
    assert body["valid"] is None
    assert body["reason"] == "MARKET_CLOSED"
    assert "no current price" in body["summary"]
    assert client.get("/checks").json()["rows"] == []


def test_the_bulk_check_refuses_on_a_stale_feed_too(client, runtime):
    """One implementation, so the bulk sweep cannot log what the single check refuses."""
    advance(runtime, (10, 0))
    runtime._virtual_now = ist(11, 45)

    rows = client.get("/check-entry-all").json()["results"]
    assert {r["reason"] for r in rows} == {"STALE_FEED"}
    assert all(r["valid"] is None for r in rows)
    assert client.get("/checks").json()["rows"] == []


def test_a_fresh_feed_still_produces_a_verdict(client, runtime):
    """The refusal must not swallow the normal case."""
    advance(runtime, (10, 0))
    body = client.post("/check-entry", json={"symbol": "RELIANCE"}).json()
    assert body["valid"] in (True, False)
    assert len(client.get("/checks").json()["rows"]) == 1


def test_a_bar_being_mid_minute_is_not_a_stale_feed(client, runtime):
    """A completed bar is only produced once a minute BY CONSTRUCTION.

    Measuring bar recency against the 30-second tick threshold would refuse every check
    made in the second half of any minute. The entry rule allows one bar interval on top
    of the threshold, which is derived from the bar size rather than picked.
    """
    advance(runtime, (10, 0))
    for second in (1, 29, 31, 45, 59, 89):
        runtime._virtual_now = ist(10, 0) + timedelta(seconds=second)
        body = client.post("/check-entry", json={"symbol": "RELIANCE"}).json()
        assert body["valid"] in (True, False), (
            f"a check {second}s into the bar was refused: {body.get('reason')}")

    runtime._virtual_now = ist(10, 0) + timedelta(seconds=91)
    refused = client.post("/check-entry", json={"symbol": "RELIANCE"}).json()
    assert refused["reason"] == "STALE_FEED"


def test_a_healthy_replayed_position_does_not_flicker_within_a_minute(store):
    """FIXED 2026-08-25 by unifying last_tick_at's meaning. NOT by moving a threshold.

    It used to read HOLD, HOLD, STALE_DATA, STALE_DATA across one virtual minute: in
    REPLAY ``last_tick_at`` was the last completed BAR's close, so the exit engine's
    30-second TICK threshold declared a perfectly healthy replayed feed stale for the
    last 29 seconds of every minute. The usual speed settings only dodged it because
    their clock steps landed exactly on :00 and :30.

    Both modes now mean "the most recent moment this feed had data" -- see
    tests/test_feed_semantics.py for the invariant itself. ``stale_feed_seconds`` is
    untouched at 30.
    """
    from src.api.runtime import build_replay_runtime

    rt = build_replay_runtime(store, SYMS, TRADE_DATE)
    rt.add_position(symbol="RELIANCE", quantity=100, entry_price="1000.00",
                    entry_time=ist(9, 30))

    states = {}
    for second in (0, 15, 29, 31, 45, 59):
        at = ist(10, 0) + timedelta(seconds=second)
        rt._virtual_now = at
        rt.feed.advance_to(at)
        states[second] = rt.evaluate_all()[0]["state"]

    assert "STALE_DATA" not in states.values(), states
    assert len(set(states.values())) == 1, f"the state still changes mid-minute: {states}"


def test_the_bulk_check_logs_every_symbol_it_evaluated(client, runtime):
    advance(runtime, (10, 0))
    client.get("/check-entry-all")
    rows = client.get("/checks").json()["rows"]
    assert {r["symbol"] for r in rows} == set(SYMS)
    assert {r["source"] for r in rows} == {"BULK"}


def test_rows_are_ordered_by_check_time_newest_first(client, runtime):
    """The ordering constraint, asserted against deliberately mixed outcomes."""
    for hh, mm in ((9, 45), (10, 30), (11, 15)):
        advance(runtime, (hh, mm))
        client.post("/check-entry", json={"symbol": "RELIANCE"})
        client.post("/check-entry", json={"symbol": "INFY"})

    body = client.get("/checks").json()
    stamps = [r["checked_at"] for r in body["rows"]]
    assert stamps == sorted(stamps, reverse=True), stamps
    assert body["ordering"] == "checked_at_desc"
    assert body["is_ranking"] is False


def test_there_is_no_way_to_sort_or_filter_the_log_from_the_api(client, runtime):
    """A sort parameter is all it would take. There must not be one.

    Ordering must be identical however the endpoint is called, including with the
    parameters someone would reach for first.
    """
    advance(runtime, (10, 0))
    client.get("/check-entry-all")
    plain = client.get("/checks").json()["rows"]

    for query in ("?sort=change", "?sort=-change_since_you_checked_pct",
                  "?order_by=return", "?verdict=SOUND", "?filter=SOUND",
                  "?sort=change&verdict=SOUND"):
        got = client.get(f"/checks{query}").json()["rows"]
        assert [r["check_id"] for r in got] == [r["check_id"] for r in plain], (
            f"{query} changed the result -- the log must not be sortable or filterable")


def test_sound_and_unsound_are_never_returned_separately(client, runtime):
    """Showing only the SOUND rows turns a scoreboard into a track record."""
    advance(runtime, (10, 0))
    client.get("/check-entry-all")
    verdicts = {r["verdict"] for r in client.get("/checks").json()["rows"]}
    assert verdicts == {"SOUND", "UNSOUND"}, (
        f"the fixture needs both verdicts for this test to bite: {verdicts}")


def test_the_log_carries_no_ranking_field_anywhere(client, runtime):
    """The same recursive guard the bulk check has, for the same reason."""
    advance(runtime, (10, 0))
    client.get("/check-entry-all")
    body = client.get("/checks").json()

    banned = {
        "score", "composite_score", "rank", "ranking", "rating", "confidence",
        "probability", "expected_return", "edge", "signal", "conviction",
        "best", "top", "top_performers", "recommended", "preferred", "priority",
        "weight", "percentile", "z_score", "strength", "grade", "winners",
    }

    def walk(node, path="root"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in banned, f"ranking field {key!r} at {path}"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(body)


def test_the_change_field_is_named_in_the_past_tense(client, runtime):
    """"change_since_you_checked" describes something that already happened."""
    advance(runtime, (10, 0))
    client.post("/check-entry", json={"symbol": "RELIANCE"})
    row = client.get("/checks").json()["rows"][0]
    assert "change_since_you_checked_pct" in row
    for forward_looking in ("expected", "forecast", "projected", "target_return"):
        assert not any(forward_looking in k for k in row), row.keys()


def test_an_unsettled_check_is_marked_live_and_kept_out_of_the_numbers(client, runtime):
    # 12:00 is inside the fixture's bars (which end 12:34), so the feed is fresh and a
    # verdict is given -- but the 13:00 settle horizon is past the last bar, so it
    # cannot settle and must stay LIVE.
    advance(runtime, (12, 0))
    client.post("/check-entry", json={"symbol": "RELIANCE"})
    body = client.get("/checks").json()

    assert body["rows"][0]["outcome_state"] == "LIVE"
    assert body["summary"]["live_excluded"] == 1
    assert body["summary"]["sound_n"] + body["summary"]["unsound_n"] == 0


def test_a_check_settles_once_its_horizon_has_passed(client, runtime):
    advance(runtime, (10, 0))
    client.post("/check-entry", json={"symbol": "RELIANCE"})
    assert client.get("/checks").json()["rows"][0]["outcome_state"] == "LIVE"

    # Reading the scoreboard settles what is due, so a bare GET is enough -- the panel
    # must be correct without the background loop having run.
    advance(runtime, (11, 30))
    row = client.get("/checks").json()["rows"][0]
    assert row["outcome_state"] == "SETTLED"
    assert row["price_now"] is not None
    assert row["change_since_you_checked_pct"] is not None


def test_a_settled_outcome_is_frozen_and_never_moves_again(client, runtime):
    """The whole value of this ledger is that it was written before the answer."""
    advance(runtime, (10, 0))
    client.post("/check-entry", json={"symbol": "RELIANCE"})
    advance(runtime, (11, 30))
    first = client.get("/checks").json()["rows"][0]

    advance(runtime, (14, 0))
    later = client.get("/checks").json()["rows"][0]
    assert later["price_now"] == first["price_now"]
    assert later["change_since_you_checked_pct"] == first["change_since_you_checked_pct"]


def test_the_settlement_horizon_is_capped_at_the_square_off(runtime, store):
    from src.positions.check_log import SETTLE_MINUTES, settle_time

    late = settle_time(ist(14, 45), TRADE_DATE)
    assert late.time() == time(15, 15), "a 60-min hold from 14:45 was never holdable"
    early = settle_time(ist(10, 0), TRADE_DATE)
    assert early == ist(10, 0) + timedelta(minutes=SETTLE_MINUTES)


def test_the_summary_says_inconclusive_until_the_sample_is_big_enough(client, runtime):
    advance(runtime, (10, 0))
    client.get("/check-entry-all")
    advance(runtime, (11, 30))
    client.get("/positions")

    summary = client.get("/checks").json()["summary"]
    assert summary["label"] == "INCONCLUSIVE"
    assert summary["conclusive"] is False
    assert "INCONCLUSIVE" in summary["headline"]
    assert str(summary["minimum_n_each"]) in summary["headline"]


def test_the_log_persists_across_runtimes(tmp_path):
    """Persistence is the point: the sample has to accumulate across sessions."""
    from src.api.runtime import build_replay_runtime

    store = PositionStore(tmp_path)
    store.save_bars("RELIANCE", TRADE_DATE, session())
    first = build_replay_runtime(store, ["RELIANCE"], TRADE_DATE)
    advance(first, (10, 0))
    first.check_entry("RELIANCE")
    assert len(first.checked_log()["rows"]) == 1

    # A completely separate runtime over the same data directory.
    second = build_replay_runtime(store, ["RELIANCE"], TRADE_DATE)
    advance(second, (10, 5))
    rows = second.checked_log()["rows"]
    assert len(rows) == 1, "the check log did not survive a new runtime"
    assert rows[0]["symbol"] == "RELIANCE"


# --------------------------------------------------------------------------------
# Token-invalid handling, at the LIVE feed/runtime boundary.
#
# The exit engine stays CREDENTIAL-AGNOSTIC and PURE: PositionState is unchanged, the
# precedence chain is unchanged, and there is no TOKEN_INVALID state. Suppression
# happens where the credential is known -- the runtime -- and the engine is simply not
# called for price-based advice while the token is dead.
#
# TOKEN_INVALID is NOT the same thing as STALE_DATA. A stale feed is a socket that has
# gone quiet; an invalid token is an authorisation that has lapsed. They have different
# causes, different fixes, and must stay distinguishable in health and in logs.
# --------------------------------------------------------------------------------


class FakeTokenLoader:
    """Stands in for FyersAuth.load(). No filesystem, no network, no broker.

    ``calls`` counts how often the cache was actually read, which is how the tests
    tell "revalidated" from "re-read from disk".
    """

    def __init__(self, bundle=None, error=None):
        self.bundle = bundle
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.bundle


def token_valid_on(day=TRADE_DATE, *, hour=8, minute=30):
    from src.data.fyers_auth import FyersAuth, TokenBundle

    generated = ist(hour, minute, d=day)
    return TokenBundle(access_token="x" * 40, app_id="APP-100",
                       generated_at=generated,
                       expires_at=FyersAuth.expiry_for(generated))


@pytest.fixture
def live(store, monkeypatch):
    """A LIVE-mode runtime on stored bars, with the clock and token loader injected.

    Mode is only a flag; the feed is injected exactly as it is in REPLAY. That is what
    makes the LIVE-only code path testable with no broker and no market.
    """
    from src.api.runtime import SessionRuntime

    def make(at, loader):
        monkeypatch.setattr("src.api.runtime.now_ist", lambda: at)
        feed = MultiSymbolReplayFeed({s: store.load_bars(s, TRADE_DATE) for s in SYMS})
        feed.advance_to(at)
        return SessionRuntime(
            mode=Mode.LIVE, store=store, symbols=SYMS, trade_date=TRADE_DATE,
            feed=feed, token_loader=loader,
        )

    return make


def only_card(runtime):
    cards = runtime.evaluate_all()
    assert len(cards) == 1, cards
    return cards[0]


def add_open_position(runtime, at=None):
    return runtime.add_position(
        symbol="RELIANCE", quantity=100, entry_price=Decimal("1000.00"),
        entry_time=at or ist(9, 30))


# ---- 1. invalid at construction ------------------------------------------------


def test_an_invalid_token_at_construction_suppresses_price_based_advice(live):
    from src.data.fyers_auth import TokenMissingError

    rt = live(ist(12, 0), FakeTokenLoader(error=TokenMissingError("no token cached")))
    add_open_position(rt)

    health = rt.feed_health()
    assert health.token_valid is False
    assert health.advice_suppressed is True
    assert health.reason == "TOKEN_INVALID"

    card = only_card(rt)
    assert card["advice_suppressed"] is True
    assert card["suppressed_reason"] == "TOKEN_INVALID"
    assert card["suggests_exit"] is False
    assert card["suggested_exit_quantity"] == 0


# ---- 2. becoming invalid mid-session -------------------------------------------


def test_a_token_that_expires_mid_session_starts_suppressing(live, monkeypatch):
    """Revalidated before every evaluation, not only at construction."""
    loader = FakeTokenLoader(bundle=token_valid_on())
    rt = live(ist(12, 0), loader)
    add_open_position(rt)

    assert rt.feed_health().token_valid is True
    assert only_card(rt)["advice_suppressed"] is False

    # Next calendar day: the same cached bundle is no longer valid. Nothing about the
    # feed changed -- only the credential.
    #
    # monkeypatch, NOT a raw `mod.now_ist = ...`: a bare assignment is never undone and
    # leaks the frozen clock into every later test in the session, so the suite passes
    # or fails depending on which files were selected.
    monkeypatch.setattr("src.api.runtime.now_ist",
                        lambda: ist(12, 0, d=TRADE_DATE + timedelta(days=1)))
    assert rt.feed_health().token_valid is False
    assert rt.feed_health().reason == "TOKEN_INVALID"


def test_the_token_is_reread_before_every_evaluation_not_remembered(live):
    """Deliberately NOT cached, and this test is what stops it being cached later.

    A remembered bundle stays valid for its whole IST day, so a token deleted, replaced
    or revoked out of band would go unnoticed until midnight while the dashboard kept
    advising. Re-reading is what makes invalidation AND recovery immediate.
    """
    loader = FakeTokenLoader(bundle=token_valid_on())
    rt = live(ist(12, 0), loader)

    for _ in range(5):
        assert rt.feed_health().token_valid is True
    assert loader.calls == 5, (
        f"the token was read {loader.calls} times for 5 evaluations -- it must be "
        f"revalidated before each one, not remembered")


def test_a_token_removed_out_of_band_suppresses_immediately(live):
    """The case caching would have hidden until midnight."""
    from src.data.fyers_auth import TokenMissingError

    loader = FakeTokenLoader(bundle=token_valid_on())
    rt = live(ist(12, 0), loader)
    add_open_position(rt)
    assert only_card(rt)["advice_suppressed"] is False

    loader.bundle, loader.error = None, TokenMissingError("cache deleted")
    assert only_card(rt)["advice_suppressed"] is True


# ---- 3. recovery ----------------------------------------------------------------


def test_a_refreshed_token_resumes_advice_without_a_restart(live):
    from src.data.fyers_auth import TokenExpiredError

    loader = FakeTokenLoader(error=TokenExpiredError("expired"))
    rt = live(ist(12, 0), loader)
    add_open_position(rt)
    assert only_card(rt)["advice_suppressed"] is True

    # The user runs `python -m src.cli auth` in another terminal.
    loader.error = None
    loader.bundle = token_valid_on()

    health = rt.feed_health()
    assert health.token_valid is True
    assert health.advice_suppressed is False
    card = only_card(rt)
    assert card["advice_suppressed"] is False
    assert card["state_is_current"] is True
    assert card["suppressed_reason"] is None
    assert card["price_is_current"] is True, "a fresh feed and a good token means live"
    # A real evaluation ran again: the engine persisted state it had not before.
    stored = rt.store.load_runtime(rt.open_positions()[0].position_id, TRADE_DATE)
    assert stored is not None and stored.last_state is not None


# ---- 4 & 9. suppression, and no price presented as current ----------------------


def test_no_price_is_presented_as_current_while_the_token_is_invalid(live):
    from src.data.fyers_auth import TokenExpiredError

    rt = live(ist(12, 0), FakeTokenLoader(error=TokenExpiredError("expired")))
    add_open_position(rt)
    card = only_card(rt)

    # Asserted unconditionally: the fixture HAS bars at 12:00, so a conditional here
    # would pass vacuously if the card ever stopped carrying a price at all.
    assert card["price_is_current"] is False
    assert card["last_price"] is not None, "the fixture must have a last-known price"
    assert card["price_age_seconds"] is not None, "a price must carry its age"
    assert card["gross_pnl"] is not None and card["net_pnl"] is not None, (
        "gross and net must both be present, or both absent")
    assert card["pnl_is_stale"] is True


def test_token_invalid_is_not_reported_as_stale_data(live):
    """Different causes, different fixes. They must stay distinguishable."""
    from src.data.fyers_auth import TokenExpiredError

    rt = live(ist(12, 0), FakeTokenLoader(error=TokenExpiredError("expired")))
    add_open_position(rt)
    card = only_card(rt)

    assert card["suppressed_reason"] == "TOKEN_INVALID"
    assert card["state"] != PositionState.STALE_DATA.value
    health = rt.health()
    assert health["feed_health"]["reason"] == "TOKEN_INVALID"
    assert health["feed"]["stale"] is False, (
        "the feed itself is fine here -- only the credential is dead")


# ---- 5. persisted state must survive --------------------------------------------


def test_an_invalid_token_does_not_overwrite_persisted_runtime_state(live):
    from src.data.fyers_auth import TokenExpiredError

    loader = FakeTokenLoader(bundle=token_valid_on())
    rt = live(ist(12, 0), loader)
    position = add_open_position(rt)
    only_card(rt)                                   # a real evaluation, persisted

    before = rt.store.load_runtime(position.position_id, TRADE_DATE)
    assert before is not None and before.last_state is not None

    loader.bundle, loader.error = None, TokenExpiredError("expired")
    card = only_card(rt)

    after = rt.store.load_runtime(position.position_id, TRADE_DATE)
    assert after.last_state == before.last_state, "the stored state was overwritten"
    assert after.last_evaluated_at == before.last_evaluated_at
    assert after.current_stop == before.current_stop, "the ratchet was disturbed"
    # And the card shows the preserved state, explicitly marked as not current.
    assert card["state"] == before.last_state.value
    assert card["state_is_current"] is False


# ---- 6 & 7. FORCE_EXIT is a clock fact and survives an invalid token ------------


def test_force_exit_is_still_emitted_with_an_invalid_token(live):
    """Square-off needs no price and no credential. Going silent here is the worst
    available outcome: the position is real whatever the token is doing."""
    from src.data.fyers_auth import TokenExpiredError

    rt = live(ist(15, 16), FakeTokenLoader(error=TokenExpiredError("expired")))
    add_open_position(rt)
    card = only_card(rt)

    assert card["state"] == PositionState.FORCE_EXIT.value
    assert card["state_is_current"] is True
    assert card["suggests_exit"] is True
    assert card["suggested_exit_quantity"] == 100
    assert card["suppressed_reason"] == "TOKEN_INVALID"
    assert "square" in card["reason"].lower() or "15:15" in card["reason"]


def test_force_exit_pnl_with_an_invalid_token_is_labelled_stale_or_absent(live):
    from src.data.fyers_auth import TokenExpiredError

    rt = live(ist(15, 16), FakeTokenLoader(error=TokenExpiredError("expired")))
    add_open_position(rt)
    card = only_card(rt)

    assert card["price_is_current"] is False
    # The fixture's bars end at 12:34, so at 15:16 a last-known price exists and the
    # P&L computed from it must be labelled stale rather than shown as current.
    assert card["last_price"] is not None
    assert card["price_age_seconds"] > 60 * 60, "the price is hours old and must say so"
    assert card["gross_pnl"] is not None and card["net_pnl"] is not None
    assert card["pnl_is_stale"] is True


def test_force_exit_with_an_invalid_token_still_does_not_persist_runtime(live,
                                                                        monkeypatch):
    from src.data.fyers_auth import TokenExpiredError

    loader = FakeTokenLoader(bundle=token_valid_on())
    rt = live(ist(12, 0), loader)
    position = add_open_position(rt)
    only_card(rt)
    before = rt.store.load_runtime(position.position_id, TRADE_DATE)

    loader.bundle, loader.error = None, TokenExpiredError("expired")
    monkeypatch.setattr("src.api.runtime.now_ist", lambda: ist(15, 16))
    rt.feed.advance_to(ist(15, 16))
    assert only_card(rt)["state"] == PositionState.FORCE_EXIT.value

    after = rt.store.load_runtime(position.position_id, TRADE_DATE)
    assert after.last_state == before.last_state


# ---- 8. exposed through /health -------------------------------------------------


def test_health_exposes_feed_health_and_the_suppression_reason(live):
    from src.data.fyers_auth import TokenExpiredError

    rt = live(ist(12, 0), FakeTokenLoader(error=TokenExpiredError("expired")))
    health = rt.health()

    assert health["token"]["valid"] is False
    fh = health["feed_health"]
    assert fh["token_valid"] is False
    assert fh["advice_suppressed"] is True
    assert fh["reason"] == "TOKEN_INVALID"
    assert fh["detail"], "the reason must carry an actionable detail line"
    assert "access_token" not in json.dumps(health)


def test_a_token_shaped_value_never_reaches_health_the_banner_or_a_log(live):
    """Defence in depth on the one credential-adjacent string that leaves the process.

    `FeedHealth.detail` is built from an auth exception message and rendered into
    /health, onto the dashboard banner and into a log line. No auth error embeds a
    token today; this is what keeps that true if one ever changes.
    """
    leaky = RuntimeError(
        "cache unreadable: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.leaked-secret-value")
    rt = live(ist(12, 0), FakeTokenLoader(error=leaky))

    detail = rt.feed_health().detail
    assert "leaked-secret-value" not in detail
    assert "eyJ" not in detail
    assert "<redacted token>" in detail
    assert "leaked-secret-value" not in json.dumps(rt.health())

    add_open_position(rt)
    assert "leaked-secret-value" not in json.dumps(only_card(rt))


@pytest.mark.parametrize("scenario", ["valid", "expired", "missing"])
def test_health_token_and_feed_health_can_never_disagree(live, scenario):
    """Two fields describing the same fact must agree, whatever the disk holds.

    They did not: `_token_status` built its own FyersAuth against the REAL wall clock
    while feed_health used the injected loader, so /health could report
    `token.valid: true` beside `feed_health.token_valid: false` -- a healthy-looking
    token badge over suppressed advice. It also made this field depend on whatever
    token happened to be cached on the developer's machine.
    """
    from src.data.fyers_auth import TokenExpiredError, TokenMissingError

    loader = {
        "valid": FakeTokenLoader(bundle=token_valid_on()),
        "expired": FakeTokenLoader(error=TokenExpiredError("expired")),
        "missing": FakeTokenLoader(error=TokenMissingError("no token cached")),
    }[scenario]

    health = live(ist(12, 0), loader).health()
    assert health["token"]["valid"] == health["feed_health"]["token_valid"], health
    assert health["token"]["valid"] is (scenario == "valid")


def test_health_token_state_never_consults_the_real_token_file(live, monkeypatch):
    """No hidden filesystem dependency: the injected loader is the only source."""
    def explode(*a, **k):
        raise AssertionError("/health read the real token cache")

    monkeypatch.setattr("src.data.fyers_auth.FyersAuth.load", explode)
    health = live(ist(12, 0), FakeTokenLoader(bundle=token_valid_on())).health()
    assert health["token"]["valid"] is True


def test_health_reports_advice_permitted_when_the_token_is_good(live):
    rt = live(ist(12, 0), FakeTokenLoader(bundle=token_valid_on()))
    fh = rt.health()["feed_health"]
    assert fh["token_valid"] is True
    assert fh["advice_suppressed"] is False
    assert fh["reason"] is None


# ---- 10. REPLAY never touches a token -------------------------------------------


def test_replay_never_reads_a_broker_token(runtime):
    """A replayed session needs no credential and must not go looking for one."""
    def explode():
        raise AssertionError("REPLAY read the token cache")

    runtime._token_loader = explode
    advance(runtime, (10, 0))

    fh = runtime.feed_health()
    assert fh.applicable is False
    assert fh.advice_suppressed is False
    assert runtime.health()["feed_health"]["applicable"] is False
    assert runtime.evaluate_all() == []          # no positions, and no token read


def test_replay_advice_is_unaffected_by_the_token_gate(runtime, store):
    runtime.add_position(symbol="RELIANCE", quantity=100,
                         entry_price=Decimal("1000.00"), entry_time=ist(9, 30))
    advance(runtime, (10, 0))
    card = runtime.evaluate_all()[0]
    assert card["advice_suppressed"] is False
    assert card["state_is_current"] is True


def test_the_exit_engine_never_learns_what_a_token_is():
    """Structural: the engine must stay credential-agnostic and pure.

    Scoped to CODE, not prose. The engine's docstrings legitimately explain WHY
    square_off_reason is safe to call when a token has lapsed, and a naive substring
    scan flags that explanation -- the same trap the bulk-table "not a ranking" scan hit.
    A commented-out credential check does not run.
    """
    import ast
    import inspect

    from src.positions import exit_engine

    tree = ast.parse(inspect.getsource(exit_engine))
    for node in ast.walk(tree):                       # strip every docstring
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    code = ast.unparse(tree).lower()                   # comments are gone with ast

    for word in ("token", "credential", "fyers_auth", "access_token", "secret"):
        assert word not in code, (
            f"the exit engine's CODE references {word!r} -- suppression belongs at the "
            f"runtime boundary, not in the state machine")
    assert not hasattr(PositionState, "TOKEN_INVALID")
    assert "TOKEN_INVALID" not in {s.value for s in PositionState}


# --------------------------------------------------------------------------------
# The honesty constraint
# --------------------------------------------------------------------------------


def test_there_is_no_signal_endpoint(client):
    """Module B has no edge. Surfacing its scores would be a known-false claim."""
    paths = [r.path for r in client.app.routes]
    for banned in ("/signals", "/candidates", "/rank", "/predictions", "/scores"):
        assert banned not in paths


def test_no_route_places_an_order(client):
    paths = [r.path for r in client.app.routes]
    for banned in ("/orders", "/buy", "/sell", "/execute", "/trade"):
        assert banned not in paths


def test_the_api_module_reaches_no_order_api():
    import inspect

    import src.api.app as app_mod
    import src.api.runtime as rt_mod

    for mod in (app_mod, rt_mod):
        source = inspect.getsource(mod)
        for token in ("place_order", "modify_order", "cancel_order", "exit_positions"):
            assert token not in source, f"{mod.__name__} references {token}"


def test_the_ui_carries_no_signal_panel():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
        encoding="utf-8")
    assert "no signal panel" in html.lower()
    for banned in ("composite score", "confidence", "predicted", "buy signal"):
        assert banned not in html.lower(), f"UI mentions {banned}"


def test_the_ui_uses_no_browser_storage():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
        encoding="utf-8")
    assert "localStorage" not in html
    assert "sessionStorage" not in html


def test_the_ui_states_it_never_places_orders():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
        encoding="utf-8")
    assert "never places" in html.lower()


def test_the_ui_source_contains_the_replay_disclaimer():
    """SOURCE-level check only. It says nothing about what renders.

    This assertion passed for the entire period the dashboard displayed a blank black
    page: the string lives inside the <script type="text/babel"> block, which is part
    of the served HTML whether or not one line of it ever executes.

    Whether a human actually SEES the banner is asserted in
    tests/test_web_render.py::test_the_mode_banner_is_visible_and_says_replay, which
    drives a real browser and measures the element's box. Do not let this test stand
    in for that one.
    """
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
        encoding="utf-8")
    assert "REPLAY — NOT LIVE" in html


# --------------------------------------------------------------------------------
# Replay runtime construction
# --------------------------------------------------------------------------------


def test_the_replay_clock_stops_at_the_session_tail_whatever_the_speed(store):
    """The clock must not park itself past the last bar by a tick's worth of time.

    At 600x one tick is 10 virtual minutes, so an unclamped clock sat at 15:45 on a
    session whose last bar was 15:29 -- a quarter hour of market that never happened,
    displayed as the current time.
    """
    for speed in (60.0, 600.0, 3600.0):
        runtime = build_replay_runtime(store, SYMS, TRADE_DATE,
                                       speed=speed, tick_seconds=1.0)
        last = runtime.feed.last_bar_time()
        for _ in range(500):
            runtime._advance_replay()
            if runtime._stopped:
                break
        assert runtime._stopped, f"replay never halted at speed {speed}"
        assert runtime.now() <= last + runtime.REPLAY_TAIL, (
            f"clock ran to {runtime.now()} past last bar {last} at speed {speed}")


def test_force_exit_is_still_reported_after_the_replay_ends(client, runtime):
    """Terminal state, deliberately: an open position past square-off is FORCE_EXIT.

    The replay ending does not close anything -- the user still holds it -- so the
    advice must keep saying so rather than going quiet.
    """
    client.post("/positions", json={"symbol": "RELIANCE", "quantity": 100,
                                    "entry_price": "1000.00",
                                    "entry_time": ist(9, 30).isoformat()})
    advance(runtime, (15, 20))
    cards = client.get("/positions").json()["positions"]
    assert [c["state"] for c in cards] == ["FORCE_EXIT"]
    assert runtime.health()["session_state"] != "OPEN"


def test_building_a_replay_runtime_without_bars_fails_loudly(tmp_path):
    with pytest.raises(ValueError, match="no stored bars"):
        build_replay_runtime(PositionStore(tmp_path), ["RELIANCE"], TRADE_DATE)


def test_replay_advances_and_finishes(store):
    rt = build_replay_runtime(store, SYMS, TRADE_DATE, speed=3600.0, tick_seconds=1.0)
    for _ in range(20):
        rt._advance_replay()
    assert rt._stopped or rt.now() > ist(9, 15)


def test_replay_runtime_only_includes_symbols_with_bars(store):
    rt = build_replay_runtime(store, SYMS + ["NOSUCH"], TRADE_DATE)
    assert "NOSUCH" not in rt.symbols
