"""Tests for the live Fyers feed.

Everything network-facing is injected, so the reconnect, gap-backfill and staleness
paths are exercised with no broker, no token and no market session. Those are exactly
the paths that cannot be tested against a live socket on demand, which is why they are
the ones worth pinning.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from src.market.calendar import IST
from src.positions.feed import BarAggregator
from src.positions.live_feed import (
    DEFAULT_STALE_SECONDS,
    FeedStatus,
    FyersLiveFeed,
    SeedOutcome,
    expected_completed_minutes,
    is_control_frame,
)
from src.positions.models import BAR_INTERVAL, Bar
from tests.conftest import TRADE_DATE, ist, make_series

SYMS = ["RELIANCE", "INFY"]

#: A Saturday, so the market is shut and no completed minute can exist.
CLOSED_DAY = date(2026, 8, 22)


def candle(ts: datetime, price: float, volume: int = 100):
    return [int(ts.astimezone(timezone.utc).timestamp()), price, price + 1,
            price - 1, price, volume]


def session_candles(count: int, start=None, price=1000.0):
    start = start or ist(9, 15)
    return [candle(start + timedelta(minutes=i), price + i * 0.1) for i in range(count)]


class FakeHistory:
    """Stands in for the REST history endpoint."""

    def __init__(self, by_symbol=None, fail=False):
        self.by_symbol = by_symbol or {}
        self.calls: list[tuple[str, date, date]] = []
        self.fail = fail

    def fetch(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self.fail:
            raise RuntimeError("history unavailable")
        return self.by_symbol.get(symbol.upper(), [])


class FakeSocket:
    """Stands in for FyersDataSocket. Records what it was asked to do."""

    instances: list["FakeSocket"] = []

    def __init__(self, kwargs, *, fail_connect=False):
        self.kwargs = kwargs
        self.fail_connect = fail_connect
        self.connected = False
        self.subscriptions: list[tuple] = []
        self.closed = 0
        FakeSocket.instances.append(self)

    def connect(self):
        if self.fail_connect:
            raise RuntimeError("connect refused")
        self.connected = True
        self.kwargs["on_connect"]()

    def subscribe(self, symbols, data_type):
        self.subscriptions.append((tuple(symbols), data_type))

    def close_connection(self):
        self.closed += 1
        self.connected = False


def make_feed(history=None, *, now=None, fail_connect=False, sleeps=None, **kw):
    FakeSocket.instances = []
    clock_at = now or ist(10, 0)
    return FyersLiveFeed(
        SYMS,
        access_token="tok", app_id="APP-100",
        history=history if history is not None else FakeHistory(),
        socket_factory=lambda kwargs: FakeSocket(kwargs, fail_connect=fail_connect),
        clock=lambda: clock_at,
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
        **kw,
    )


# --------------------------------------------------------------------------------
# Interface parity with ReplayFeed
# --------------------------------------------------------------------------------


def test_it_satisfies_the_same_interface_as_replayfeed():
    """The UI must not be able to tell live from replay."""
    from src.positions.feed import ReplayFeed

    live, replay = make_feed(), ReplayFeed("RELIANCE", make_series([100, 101]))
    for name in ("bars", "last_tick_at"):
        assert callable(getattr(live, name)) and callable(getattr(replay, name))


def test_bars_returns_an_empty_list_for_an_unknown_symbol():
    assert make_feed().bars("NOSUCH") == []


def test_bars_never_include_the_forming_bar():
    feed = make_feed()
    feed.on_tick("RELIANCE", ist(9, 15, 10), 100.0, 5)
    feed.on_tick("RELIANCE", ist(9, 15, 50), 999.0, 5)
    assert feed.bars("RELIANCE") == []


# --------------------------------------------------------------------------------
# Warm start
# --------------------------------------------------------------------------------


def test_seed_history_loads_completed_bars():
    history = FakeHistory({"RELIANCE": session_candles(30), "INFY": session_candles(30)})
    feed = make_feed(history, now=ist(10, 0))
    loaded = feed.seed_history(TRADE_DATE)
    assert loaded == {"RELIANCE": 30, "INFY": 30}
    assert len(feed.bars("RELIANCE")) == 30
    assert feed.status.seeded_bars == 60


def test_seed_drops_the_bar_still_forming():
    """A mid-session start must not treat a partial minute as complete."""
    history = FakeHistory({"RELIANCE": session_candles(30)})
    feed = make_feed(history, now=ist(9, 44, 30))  # the 09:44 bar is not done
    feed.seed_history(TRADE_DATE)
    assert all(b.ts + BAR_INTERVAL <= ist(9, 44, 30) for b in feed.bars("RELIANCE"))
    assert len(feed.bars("RELIANCE")) == 29


def test_a_seed_failure_is_recorded_not_swallowed():
    feed = make_feed(FakeHistory(fail=True))
    loaded = feed.seed_history(TRADE_DATE)
    assert loaded == {"RELIANCE": 0, "INFY": 0}
    assert "history unavailable" in feed.status.last_error


def test_seeding_makes_indicators_warm_immediately():
    from src.positions.indicators import atr

    history = FakeHistory({"RELIANCE": session_candles(30)})
    feed = make_feed(history, now=ist(10, 0))
    assert atr(feed.bars("RELIANCE")) is None  # nothing loaded yet
    feed.seed_history(TRADE_DATE)
    assert atr(feed.bars("RELIANCE")) is not None, "a warm start is the point of seeding"


# --------------------------------------------------------------------------------
# Ticks and bar boundaries
# --------------------------------------------------------------------------------


def test_a_bar_closes_when_the_next_minute_ticks():
    feed = make_feed()
    seen = []
    feed.on_bar = lambda s, b: seen.append((s, b))
    feed.on_tick("RELIANCE", ist(9, 15, 10), 100.0, 10)
    feed.on_tick("RELIANCE", ist(9, 16, 5), 101.0, 10)
    assert len(seen) == 1
    assert seen[0][1].ts == ist(9, 15)


def test_a_bucket_closes_once_its_minute_has_elapsed_without_a_new_tick():
    """Otherwise the last bar of a quiet stretch never completes."""
    feed = make_feed()
    feed.on_tick("RELIANCE", ist(9, 15, 10), 100.0, 10)
    assert feed.bars("RELIANCE") == []
    closed = feed.close_due_bars(now=ist(9, 16, 1))
    assert [s for s, _ in closed] == ["RELIANCE"]
    assert len(feed.bars("RELIANCE")) == 1


def test_a_bucket_inside_its_own_minute_is_not_closed_early():
    feed = make_feed()
    feed.on_tick("RELIANCE", ist(9, 15, 10), 100.0, 10)
    assert feed.close_due_bars(now=ist(9, 15, 59)) == []
    assert feed.bars("RELIANCE") == []


def test_ticks_for_an_unsubscribed_symbol_are_ignored():
    feed = make_feed()
    feed.on_tick("NOSUCH", ist(9, 15, 10), 100.0, 10)
    assert feed.last_tick_at("NOSUCH") is None


def test_ohlcv_is_built_correctly_from_ticks():
    feed = make_feed()
    for sec, px, vol in ((5, 100.0, 10), (20, 105.0, 20), (40, 98.0, 30), (59, 102.0, 40)):
        feed.on_tick("RELIANCE", ist(9, 15, sec), px, vol)
    feed.close_due_bars(now=ist(9, 16, 1))
    bar = feed.bars("RELIANCE")[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 98.0, 102.0)
    assert bar.volume == 100


# --------------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------------


def test_status_reports_tick_age():
    feed = make_feed(now=ist(10, 0))
    feed.on_tick("RELIANCE", ist(9, 59, 30), 100.0, 5)
    assert feed.status.tick_age_seconds(ist(10, 0)) == pytest.approx(30.0)


def test_stale_after_the_threshold():
    feed = make_feed()
    feed.on_tick("RELIANCE", ist(9, 59, 0), 100.0, 5)
    assert feed.status.is_stale(ist(9, 59, 20)) is False
    assert feed.status.is_stale(ist(10, 0, 1)) is True


def test_no_tick_at_all_counts_as_stale():
    """Never having received data is not the same as fresh."""
    status = FeedStatus()
    assert status.tick_age_seconds(ist(10, 0)) is None
    assert status.is_stale(ist(10, 0)) is True


def test_the_stale_threshold_is_thirty_seconds():
    assert DEFAULT_STALE_SECONDS == 30.0
    assert make_feed().status.stale_seconds_threshold == 30.0


def test_last_tick_across_the_subscription_is_the_most_recent():
    feed = make_feed()
    feed.on_tick("RELIANCE", ist(9, 50, 0), 100.0, 5)
    feed.on_tick("INFY", ist(9, 55, 0), 200.0, 5)
    assert feed.last_tick_at() == ist(9, 55, 0)
    assert feed.last_tick_at("RELIANCE") == ist(9, 50, 0)


def test_status_dict_carries_everything_the_header_needs():
    feed = make_feed(now=ist(10, 0))
    feed.on_tick("RELIANCE", ist(9, 59, 45), 100.0, 5)
    d = feed.status.as_dict(ist(10, 0))
    for key in ("mode", "connected", "last_tick_at", "tick_age_seconds", "stale",
                "stale_threshold_seconds", "reconnects"):
        assert key in d


# --------------------------------------------------------------------------------
# Connect and subscribe
# --------------------------------------------------------------------------------


def test_start_connects_and_subscribes_every_symbol():
    feed = make_feed()
    feed.start()
    sock = FakeSocket.instances[-1]
    assert sock.connected
    assert feed.status.connected
    subscribed, data_type = sock.subscriptions[0]
    assert set(subscribed) == {"NSE:RELIANCE-EQ", "NSE:INFY-EQ"}
    assert data_type == "SymbolUpdate"


def test_a_close_marks_the_feed_disconnected():
    feed = make_feed()
    feed.start()
    feed._on_close("bye")
    assert feed.status.connected is False


def test_an_error_is_recorded_on_the_status():
    feed = make_feed()
    feed._on_error("boom")
    assert feed.status.connected is False
    assert "boom" in feed.status.last_error


# --------------------------------------------------------------------------------
# Reconnect and gap backfill
# --------------------------------------------------------------------------------


def test_reconnect_backs_off_between_attempts():
    sleeps: list[float] = []
    feed = make_feed(fail_connect=True, sleeps=sleeps, max_reconnect_attempts=4)
    assert feed.reconnect(TRADE_DATE) is False
    assert len(sleeps) == 4
    assert sleeps == sorted(sleeps), "backoff must not shrink"
    assert sleeps[1] > sleeps[0]


def test_backoff_is_capped():
    sleeps: list[float] = []
    feed = make_feed(fail_connect=True, sleeps=sleeps, max_reconnect_attempts=12,
                     backoff_cap=10.0)
    feed.reconnect(TRADE_DATE)
    assert max(sleeps) <= 10.0


def test_a_successful_reconnect_backfills_the_gap():
    """A hole left unfilled looks like a quiet market, not an outage."""
    history = FakeHistory({"RELIANCE": session_candles(30), "INFY": session_candles(30)})
    feed = make_feed(history, now=ist(10, 0))
    feed.start()
    # Live ticks produced only the first few minutes before the socket dropped.
    for i in range(3):
        feed.on_tick("RELIANCE", ist(9, 15 + i, 10), 100.0 + i, 5)
    feed.close_due_bars(now=ist(9, 18, 1))
    before = len(feed.bars("RELIANCE"))

    assert feed.reconnect(TRADE_DATE) is True
    after = len(feed.bars("RELIANCE"))
    assert after > before
    assert feed.status.backfilled_bars > 0
    assert feed.status.reconnects == 1


def test_backfill_does_not_duplicate_bars_already_held():
    history = FakeHistory({"RELIANCE": session_candles(10), "INFY": []})
    feed = make_feed(history, now=ist(10, 0))
    feed.seed_history(TRADE_DATE)
    first = len(feed.bars("RELIANCE"))
    feed.backfill_gap(TRADE_DATE)
    assert len(feed.bars("RELIANCE")) == first
    stamps = [b.ts for b in feed.bars("RELIANCE")]
    assert len(stamps) == len(set(stamps))


def test_live_bars_win_over_the_rest_copy_of_the_same_minute():
    """A bar observed from ticks beats a vendor reconstruction of it."""
    history = FakeHistory({"RELIANCE": [candle(ist(9, 15), 5000.0)], "INFY": []})
    feed = make_feed(history, now=ist(10, 0))
    feed.on_tick("RELIANCE", ist(9, 15, 10), 100.0, 5)
    feed.close_due_bars(now=ist(9, 16, 1))
    feed.backfill_gap(TRADE_DATE)
    assert feed.bars("RELIANCE")[0].close == pytest.approx(100.0)


def test_backfill_failure_is_recorded_and_does_not_raise():
    feed = make_feed(FakeHistory(fail=True))
    assert feed.backfill_gap(TRADE_DATE) == 0
    assert feed.status.last_error


def test_reconnect_closes_the_old_socket_before_opening_a_new_one():
    feed = make_feed(FakeHistory({"RELIANCE": [], "INFY": []}))
    feed.start()
    old = FakeSocket.instances[-1]
    feed.reconnect(TRADE_DATE)
    assert old.closed >= 1
    assert len(FakeSocket.instances) >= 2


def test_reconnect_counts_are_reported():
    feed = make_feed(FakeHistory({"RELIANCE": [], "INFY": []}))
    feed.start()
    feed.reconnect(TRADE_DATE)
    feed.reconnect(TRADE_DATE)
    assert feed.status.reconnects == 2


# --------------------------------------------------------------------------------
# Tick parsing
# --------------------------------------------------------------------------------


def test_parse_tick_extracts_the_bare_symbol():
    feed = make_feed()
    sym, ts, px, vol = feed.parse_tick({
        "symbol": "NSE:RELIANCE-EQ", "ltp": 1234.5,
        "exch_feed_time": int(ist(10, 0).timestamp()), "last_traded_qty": 7,
    })
    assert sym == "RELIANCE"
    assert px == pytest.approx(1234.5)
    assert vol == 7
    assert ts.tzinfo is not None


def test_parse_tick_falls_back_to_the_clock_without_a_feed_time():
    feed = make_feed(now=ist(11, 30))
    _, ts, _, _ = feed.parse_tick({"symbol": "NSE:INFY-EQ", "ltp": 100.0})
    assert ts == ist(11, 30)


def test_a_tick_with_no_price_is_rejected():
    feed = make_feed()
    with pytest.raises(KeyError):
        feed.parse_tick({"symbol": "NSE:INFY-EQ"})


def test_an_unparseable_message_is_logged_not_raised():
    feed = make_feed()
    feed._on_message("not a dict")
    feed._on_message({"symbol": "NSE:INFY-EQ"})
    assert feed.bars("INFY") == []


# --------------------------------------------------------------------------------
# Protocol control frames
#
# Observed on the first live run: three of these arrive per connection and every one
# of them was logged as an unparseable tick. That is not merely noise -- it is the
# noise a genuine parse failure hides in.
# --------------------------------------------------------------------------------

CONTROL_FRAMES = [
    {"type": "cn", "code": 200, "message": "Authentication done", "s": "ok"},
    {"type": "ful", "code": 200, "message": "Full Mode On", "s": "ok"},
    {"type": "sub", "code": 200, "message": "Subscribed", "s": "ok"},
]


def test_handshake_frames_are_recognised_by_type():
    assert all(is_control_frame(f) for f in CONTROL_FRAMES)
    assert not is_control_frame({"symbol": "NSE:INFY-EQ", "ltp": 1.0, "type": "sf"})
    assert not is_control_frame("not a dict")


def test_control_frames_are_dropped_quietly_and_counted(caplog):
    feed = make_feed()
    with caplog.at_level(logging.DEBUG, logger="fno.livefeed"):
        for frame in CONTROL_FRAMES:
            feed._on_message(frame)

    assert feed.status.control_frames == 3
    assert feed.status.last_error is None
    assert feed.bars("RELIANCE") == [] and feed.bars("INFY") == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert any(r.levelno == logging.DEBUG for r in caplog.records), \
        "dropped quietly still means recorded -- debug, not silence"


def test_a_real_parse_failure_stays_visible_among_the_handshake(caplog):
    """The whole point: one warning, and it is the one that matters."""
    feed = make_feed()
    with caplog.at_level(logging.DEBUG, logger="fno.livefeed"):
        for frame in CONTROL_FRAMES:
            feed._on_message(frame)
        feed._on_message({"symbol": "NSE:INFY-EQ"})  # a quote with no ltp

    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1
    assert "unparseable tick" in loud[0].getMessage()


def test_a_control_frame_reporting_a_problem_is_not_hidden(caplog):
    """A rejected subscription is silence on that symbol, not noise. Say so."""
    feed = make_feed()
    with caplog.at_level(logging.DEBUG, logger="fno.livefeed"):
        feed._on_message(
            {"type": "sub", "code": -300, "message": "Invalid symbol", "s": "error"})

    assert "Invalid symbol" in feed.status.last_error
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_an_unknown_frame_type_is_still_loud(caplog):
    """Ignoring by type must not become ignoring anything that fails to parse."""
    feed = make_feed()
    with caplog.at_level(logging.DEBUG, logger="fno.livefeed"):
        feed._on_message({"type": "somethingnew", "code": 200, "s": "ok"})

    assert feed.status.control_frames == 0
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# --------------------------------------------------------------------------------
# Seeding: the three ways to end up with zero bars
#
# Only one of them is normal. Collapsing them is how a Monday-morning seed failure
# reads as "market closed" and the dashboard starts live with cold indicators.
# --------------------------------------------------------------------------------


def _messages(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_seed_case_1_an_api_error_is_loud_and_named(caplog):
    feed = make_feed(FakeHistory(fail=True), now=ist(10, 0))
    with caplog.at_level(logging.DEBUG, logger="fno.livefeed"):
        loaded = feed.seed_history(TRADE_DATE)

    assert loaded == {"RELIANCE": 0, "INFY": 0}
    assert feed.last_seed.outcome is SeedOutcome.API_ERROR
    assert feed.status.seed_outcome == "API_ERROR"
    assert "history unavailable" in feed.status.last_error
    assert any(r.levelno >= logging.ERROR for r in caplog.records)

    text = _messages(caplog)
    assert "symbol=RELIANCE" in text, "the request parameters must be diagnosable later"
    assert f"range={TRADE_DATE}..{TRADE_DATE}" in text


def test_seed_case_2_a_closed_market_is_normal_and_says_so_plainly(caplog):
    feed = make_feed(FakeHistory(), now=ist(10, 0, 0, CLOSED_DAY))
    with caplog.at_level(logging.DEBUG, logger="fno.livefeed"):
        loaded = feed.seed_history(CLOSED_DAY)

    assert loaded == {"RELIANCE": 0, "INFY": 0}
    assert feed.last_seed.outcome is SeedOutcome.MARKET_CLOSED
    assert feed.last_seed.expected_minutes == 0
    assert "weekend" in feed.last_seed.market_note
    assert feed.status.last_error is None, \
        "a shut market must not raise the header alarm, or the alarm means nothing"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "MARKET_CLOSED" in _messages(caplog)


def test_seed_case_2b_before_the_first_minute_completes_is_also_normal():
    """09:15:30 on a trading day: no bar CAN exist yet."""
    feed = make_feed(FakeHistory(), now=ist(9, 15, 30))
    feed.seed_history(TRADE_DATE)
    assert feed.last_seed.outcome is SeedOutcome.MARKET_CLOSED
    assert "has not completed a minute yet" in feed.last_seed.market_note
    assert feed.status.last_error is None


def test_seed_case_3_an_open_session_returning_nothing_is_a_real_problem(caplog):
    feed = make_feed(FakeHistory(), now=ist(10, 0))
    with caplog.at_level(logging.DEBUG, logger="fno.livefeed"):
        loaded = feed.seed_history(TRADE_DATE)

    assert loaded == {"RELIANCE": 0, "INFY": 0}
    assert feed.last_seed.outcome is SeedOutcome.NO_DATA_IN_SESSION
    assert feed.last_seed.expected_minutes == 45  # 09:15 -> 10:00
    assert "NO_DATA_IN_SESSION" in feed.status.last_error
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    assert "candles=0" in _messages(caplog)


def test_the_three_zero_bar_cases_are_told_apart():
    """The regression this whole change exists to prevent."""
    cases = [
        (FakeHistory(fail=True), ist(10, 0), TRADE_DATE),
        (FakeHistory(), ist(10, 0, 0, CLOSED_DAY), CLOSED_DAY),
        (FakeHistory(), ist(10, 0), TRADE_DATE),
    ]
    seen = []
    for history, now, day in cases:
        feed = make_feed(history, now=now)
        assert feed.seed_history(day) == {"RELIANCE": 0, "INFY": 0}
        seen.append(feed.last_seed.outcome)

    assert seen == [SeedOutcome.API_ERROR, SeedOutcome.MARKET_CLOSED,
                    SeedOutcome.NO_DATA_IN_SESSION]


def test_one_completed_minute_is_the_boundary_between_closed_and_missing():
    """Off by one bar and a 09:15 start reports a fault that has not happened."""
    def outcome_at(now):
        feed = make_feed(FakeHistory(), now=now)
        feed.seed_history(TRADE_DATE)
        return feed.last_seed.outcome

    assert outcome_at(ist(9, 15, 59)) is SeedOutcome.MARKET_CLOSED
    assert outcome_at(ist(9, 16, 0)) is SeedOutcome.NO_DATA_IN_SESSION


def test_a_successful_seed_is_reported_as_seeded():
    history = FakeHistory({"RELIANCE": session_candles(30), "INFY": session_candles(30)})
    feed = make_feed(history, now=ist(10, 0))
    feed.seed_history(TRADE_DATE)
    assert feed.last_seed.outcome is SeedOutcome.SEEDED
    assert feed.last_seed.total_bars == 60
    assert feed.status.last_error is None


def test_one_broken_symbol_makes_the_whole_run_not_ok():
    class HalfBroken(FakeHistory):
        def fetch(self, symbol, start, end):
            if symbol.upper() == "INFY":
                raise RuntimeError("token expired")
            return session_candles(30)

    feed = make_feed(HalfBroken(), now=ist(10, 0))
    feed.seed_history(TRADE_DATE)
    assert feed.last_seed.outcome is SeedOutcome.API_ERROR
    assert feed.last_seed.per_symbol["RELIANCE"].outcome is SeedOutcome.SEEDED
    assert "token expired" in feed.status.last_error


def test_expected_completed_minutes_boundaries():
    assert expected_completed_minutes(TRADE_DATE, ist(9, 15)) == 0
    assert expected_completed_minutes(TRADE_DATE, ist(9, 16)) == 1
    assert expected_completed_minutes(TRADE_DATE, ist(15, 30)) == 375
    assert expected_completed_minutes(TRADE_DATE, ist(23, 0)) == 375, \
        "bounded by the close, not by the wall clock"
    assert expected_completed_minutes(CLOSED_DAY, ist(12, 0, 0, CLOSED_DAY)) == 0


# --------------------------------------------------------------------------------
# Aggregator primitives
# --------------------------------------------------------------------------------


def test_seed_replaces_and_merge_adds():
    agg = BarAggregator()
    bars = make_series([100, 101, 102], start=ist(9, 15))
    assert agg.seed(bars) == 3
    assert agg.merge(bars) == 0
    extra = make_series([103], start=ist(9, 18))
    assert agg.merge(extra) == 1
    assert len(agg.completed) == 4


def test_merge_keeps_the_series_sorted():
    agg = BarAggregator()
    agg.seed(make_series([102], start=ist(9, 17)))
    agg.merge(make_series([100], start=ist(9, 15)))
    assert [b.ts for b in agg.completed] == sorted(b.ts for b in agg.completed)


def test_no_order_api_is_reachable_from_the_live_feed():
    import inspect

    import src.positions.live_feed as mod

    source = inspect.getsource(mod)
    for token in ("place_order", "modify_order", "cancel_order", "exit_positions"):
        assert token not in source
