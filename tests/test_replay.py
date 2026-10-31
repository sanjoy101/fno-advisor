"""Tests for the replay driver and the feed layer.

Replay is a requirement, so it gets the same scrutiny as the state machine. The two
properties that make it worth anything: it is deterministic, and it feeds the engine
exactly what was knowable at each moment -- no more.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from config.settings import ExitEngineSettings
from src.market.calendar import IST
from src.positions.feed import BarAggregator, ReplayFeed, synthetic_session
from src.positions.models import Position, PositionRuntime, PositionState
from src.positions.replay import TERMINAL_STATES, replay_session
from src.positions.store import PositionStore
from tests.conftest import TRADE_DATE, flat_series, ist, make_series


@pytest.fixture
def session_bars():
    return synthetic_session("RELIANCE", TRADE_DATE, open_price=1000.0)


@pytest.fixture
def replay_position():
    """Entered at 09:45, after enough bars have printed for the indicators to warm."""
    return Position(
        position_id="p-replay",
        symbol="RELIANCE",
        quantity=100,
        entry_price=Decimal("1018.00"),
        entry_time=ist(9, 45),
    )


# --------------------------------------------------------------------------------
# ReplayFeed
# --------------------------------------------------------------------------------


def test_replay_feed_reveals_only_completed_bars():
    bars = make_series([100, 101, 102], start=ist(9, 15))
    feed = ReplayFeed("X", bars)
    feed.advance_to(ist(9, 17, 30))
    assert [b.ts.minute for b in feed.bars()] == [15, 16]


def test_replay_feed_shows_nothing_before_it_is_advanced():
    feed = ReplayFeed("X", make_series([100, 101]))
    assert feed.bars() == []
    assert feed.last_tick_at() is None


def test_replay_feed_last_tick_is_the_latest_completed_bar_close():
    bars = make_series([100, 101, 102], start=ist(9, 15))
    feed = ReplayFeed("X", bars)
    feed.advance_to(ist(9, 17))
    assert feed.last_tick_at() == ist(9, 17)


def test_replay_feed_surfaces_a_data_gap_as_growing_staleness():
    """A hole in stored history must not be silently bridged."""
    bars = make_series([100, 101], start=ist(9, 15)) + make_series([102], start=ist(9, 30))
    feed = ReplayFeed("X", bars)
    feed.advance_to(ist(9, 25))
    stale = (ist(9, 25) - feed.last_tick_at()).total_seconds()
    assert stale == pytest.approx(8 * 60)


def test_replay_feed_steps_are_bar_close_times():
    bars = make_series([100, 101, 102], start=ist(9, 15))
    assert list(ReplayFeed("X", bars).steps()) == [ist(9, 16), ist(9, 17), ist(9, 18)]


def test_replay_feed_sorts_out_of_order_bars():
    bars = make_series([100, 101, 102], start=ist(9, 15))
    feed = ReplayFeed("X", list(reversed(bars)))
    feed.advance_to(ist(9, 18))
    assert [b.ts.minute for b in feed.bars()] == [15, 16, 17]


def test_replay_feed_from_store(tmp_path):
    store = PositionStore(tmp_path)
    bars = flat_series(1000, 10)
    store.save_bars("RELIANCE", TRADE_DATE, bars)
    feed = ReplayFeed.from_store(store, "RELIANCE", TRADE_DATE)
    feed.advance_to(ist(15, 30))
    assert feed.bars() == bars


# --------------------------------------------------------------------------------
# BarAggregator
# --------------------------------------------------------------------------------


def test_aggregator_emits_a_bar_only_once_its_minute_elapsed():
    agg = BarAggregator()
    assert agg.add_tick(ist(9, 15, 10), 100.0, 10) is None
    assert agg.add_tick(ist(9, 15, 50), 101.0, 10) is None
    bar = agg.add_tick(ist(9, 16, 5), 102.0, 5)
    assert bar is not None
    assert bar.ts == ist(9, 15)


def test_aggregator_builds_correct_ohlcv():
    agg = BarAggregator()
    for ts, px, vol in [
        (ist(9, 15, 1), 100.0, 10),
        (ist(9, 15, 20), 105.0, 20),
        (ist(9, 15, 40), 98.0, 30),
        (ist(9, 15, 59), 102.0, 40),
    ]:
        agg.add_tick(ts, px, vol)
    bar = agg.add_tick(ist(9, 16, 1), 103.0, 5)
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 98.0, 102.0)
    assert bar.volume == 100


def test_aggregator_never_exposes_the_forming_bar():
    """The bar still accumulating must not be readable -- that is look-ahead."""
    agg = BarAggregator()
    agg.add_tick(ist(9, 15, 10), 100.0)
    agg.add_tick(ist(9, 15, 30), 999.0)  # a spike inside the forming minute
    assert agg.completed == []


def test_aggregator_tracks_last_tick_time():
    agg = BarAggregator()
    agg.add_tick(ist(9, 15, 10), 100.0)
    assert agg.last_tick_at == ist(9, 15, 10)


def test_aggregator_rejects_naive_tick_times():
    from datetime import datetime

    from src.market.calendar import NaiveDatetimeError

    with pytest.raises(NaiveDatetimeError):
        BarAggregator().add_tick(datetime(2026, 8, 20, 9, 15), 100.0)


def test_aggregator_accumulates_completed_bars():
    agg = BarAggregator()
    for i in range(5):
        agg.add_tick(ist(9, 15) + timedelta(minutes=i, seconds=10), 100.0 + i, 100)
    assert len(agg.completed) == 4  # the fifth is still forming


# --------------------------------------------------------------------------------
# replay_session
# --------------------------------------------------------------------------------


def test_replay_produces_advice_for_every_bar_after_entry(
    replay_position, session_bars, engine_cfg
):
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg,
                            stop_at_exit=False)
    assert result.advices
    assert all(a.asof >= replay_position.entry_time for a in result.advices)


def test_replay_never_advises_before_entry(replay_position, session_bars, engine_cfg):
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg,
                            stop_at_exit=False)
    assert result.advices[0].asof >= replay_position.entry_time


def test_replay_is_deterministic(replay_position, session_bars, engine_cfg):
    """Same bars, same config, byte-identical advice. This is the whole point."""
    a = replay_session(replay_position, session_bars, engine_cfg=engine_cfg)
    b = replay_session(replay_position, session_bars, engine_cfg=engine_cfg)
    assert [(x.state, x.reason, x.asof) for x in a.advices] == [
        (y.state, y.reason, y.asof) for y in b.advices
    ]
    assert a.runtime.current_stop == b.runtime.current_stop


def test_replay_config_change_changes_the_stop_path(replay_position, session_bars):
    """A replay is only evidence about the config it ran under."""
    tight = ExitEngineSettings(trail_atr_multiple=Decimal("0.5"))
    loose = ExitEngineSettings(trail_atr_multiple=Decimal("4.0"))
    a = replay_session(replay_position, session_bars, engine_cfg=tight)
    b = replay_session(replay_position, session_bars, engine_cfg=loose)
    assert [x.stop_price for x in a.advices] != [y.stop_price for y in b.advices]


def test_trail_width_changes_the_exit_once_momentum_is_not_pre_empting_it(
    replay_position, session_bars
):
    """With the momentum rule off, the trail width decides when the stop is hit.

    Documents a real property of this rule set: on this session the momentum rule
    (EMA9 < EMA21 and RSI < 50) fires before the trailing stop is reached, so trail
    width alone does not move the exit. That ordering is worth knowing about.
    """
    off = {"rsi_reversal_level": Decimal("0")}
    tight = ExitEngineSettings(trail_atr_multiple=Decimal("0.5"), **off)
    loose = ExitEngineSettings(trail_atr_multiple=Decimal("4.0"), **off)
    a = replay_session(replay_position, session_bars, engine_cfg=tight)
    b = replay_session(replay_position, session_bars, engine_cfg=loose)
    assert a.exited_at < b.exited_at, "a tighter trail must stop out earlier"


def test_momentum_rule_pre_empts_the_trailing_stop_on_this_session(
    replay_position, session_bars, engine_cfg
):
    """Pins the interaction above so a rule reordering does not pass unnoticed."""
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg)
    assert result.exit_state is PositionState.EXIT_NOW
    assert "Momentum reversal" in result.advices[-1].reason


def test_replay_stops_at_the_first_terminal_exit(replay_position, session_bars, engine_cfg):
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg)
    assert result.exit_state in TERMINAL_STATES
    assert result.advices[-1].state is result.exit_state
    assert all(a.state not in TERMINAL_STATES for a in result.advices[:-1])


def test_replay_can_run_past_an_exit_when_asked(replay_position, session_bars, engine_cfg):
    stopped = replay_session(replay_position, session_bars, engine_cfg=engine_cfg)
    full = replay_session(replay_position, session_bars, engine_cfg=engine_cfg,
                          stop_at_exit=False)
    assert len(full.advices) > len(stopped.advices)


def test_replay_records_state_transitions_not_every_bar(
    replay_position, session_bars, engine_cfg
):
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg,
                            stop_at_exit=False)
    assert len(result.transitions) < len(result.advices)
    states = [t.state for t in result.transitions]
    assert all(a != b for a, b in zip(states, states[1:])), "consecutive duplicates"


def test_replay_stop_never_moves_down_across_a_whole_session(
    replay_position, session_bars, engine_cfg
):
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg,
                            stop_at_exit=False)
    stops = [a.stop_price for a in result.advices if a.stop_price is not None]
    assert stops == sorted(stops)


def test_replay_exercises_the_trail_and_an_exit(replay_position, session_bars, engine_cfg):
    """The synthetic session rises, stalls, then fades -- it should do all three."""
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg,
                            stop_at_exit=False)
    seen = {a.state for a in result.advices}
    assert PositionState.TRAIL in seen
    assert seen & TERMINAL_STATES


def test_replay_forces_exit_by_square_off_if_nothing_else_did(engine_cfg):
    """A position that never triggers a rule still gets closed at 15:15."""
    pos = Position(
        position_id="p-quiet", symbol="ITC", quantity=100,
        entry_price=Decimal("1000.00"), entry_time=ist(9, 20),
    )
    bars = flat_series(1000, 400, spread=1.0, start=ist(9, 15))  # runs past 15:15
    cfg = ExitEngineSettings(time_exit_minutes=100_000)  # disable TIME_EXIT
    result = replay_session(pos, bars, engine_cfg=cfg)
    assert result.exit_state is PositionState.FORCE_EXIT
    assert result.exited_at.time() >= ist(15, 15).time()


def test_replay_every_advice_carries_gross_and_net(
    replay_position, session_bars, engine_cfg
):
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg,
                            stop_at_exit=False)
    for advice in result.advices:
        assert advice.pnl is not None
        assert advice.pnl.net_pnl < advice.pnl.gross_pnl


def test_replay_summary_is_readable(replay_position, session_bars, engine_cfg):
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg)
    text = result.summary()
    assert replay_position.symbol in text
    assert "gross" in text and "net" in text


def test_replay_state_counts(replay_position, session_bars, engine_cfg):
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg,
                            stop_at_exit=False)
    counts = result.state_counts()
    assert sum(counts.values()) == len(result.advices)


def test_replay_result_can_be_journaled(tmp_path, replay_position, session_bars, engine_cfg):
    store = PositionStore(tmp_path)
    result = replay_session(replay_position, session_bars, engine_cfg=engine_cfg)
    for advice in result.advices:
        store.log_advice(advice)
    rows = store.read_advice(TRADE_DATE)
    assert len(rows) == len(result.advices)
    (states,) = store.query("SELECT count(DISTINCT state) FROM advice")
    assert states[0] >= 1


def test_replay_resumes_from_persisted_runtime(tmp_path, replay_position, session_bars,
                                               engine_cfg):
    """Restart mid-session: the ratchet is picked up, not reset."""
    store = PositionStore(tmp_path)
    rt = PositionRuntime.initial(replay_position)
    rt.breakeven_armed = True
    rt.raise_stop(Decimal("1030.0000"))
    store.save_runtime(rt, TRADE_DATE)

    resumed = store.load_or_init_runtime(replay_position)
    result = replay_session(replay_position, session_bars, runtime=resumed,
                            engine_cfg=engine_cfg, stop_at_exit=False)
    stops = [a.stop_price for a in result.advices if a.stop_price is not None]
    assert min(stops) >= Decimal("1030.0000")


# --------------------------------------------------------------------------------
# synthetic_session
# --------------------------------------------------------------------------------


def test_synthetic_session_starts_at_the_open_and_is_deterministic():
    a = synthetic_session("X", TRADE_DATE)
    b = synthetic_session("X", TRADE_DATE)
    assert a == b
    assert a[0].ts == ist(9, 15)


def test_synthetic_session_bars_are_one_minute_apart():
    bars = synthetic_session("X", TRADE_DATE)
    gaps = {(b.ts - a.ts).total_seconds() for a, b in zip(bars, bars[1:])}
    assert gaps == {60.0}


def test_synthetic_session_is_valid_market_data():
    for bar in synthetic_session("X", TRADE_DATE):
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= max(bar.open, bar.close)
        assert bar.volume > 0
