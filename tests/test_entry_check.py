"""Tests for the INVALID_ENTRY diagnostic and the check-entry path.

The point of this feature is wording, so the tests check wording as well as state:
the reason must describe what was wrong AT ENTRY, in the past tense, and must not
read as an exit instruction.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from config.settings import ExitEngineSettings
from src.positions.exit_engine import (
    PRECEDENCE,
    EntryCheck,
    check_entry,
    check_hypothetical_entry,
    evaluate,
    evaluate_entry_conditions,
)
from src.positions.models import (
    BAR_INTERVAL,
    ExitRule,
    IndicatorSnapshot,
    Position,
    PositionRuntime,
    PositionState,
)
from tests.conftest import TRADE_DATE, flat_series, ist, make_bar, make_series

WARMUP = 30


def snapshot(**kw) -> IndicatorSnapshot:
    """A crafted snapshot, so each entry condition can be tested on its own."""
    base = dict(
        asof=ist(9, 46), last_price=1000.0, bars_used=31, vwap=1000.0, atr14=2.0,
        ema9=1000.0, ema21=1000.0, rsi14=55.0, volume=1000, avg_volume20=1000.0,
        volume_ratio=1.0,
    )
    return IndicatorSnapshot(**{**base, **kw})


def walk(position, bars, engine_cfg=None, runtime=None):
    """Evaluate bar by bar from entry, as a live run or replay would."""
    runtime = runtime or PositionRuntime.initial(position)
    out = []
    for bar in bars:
        step = bar.close_time
        if step <= position.entry_time:
            continue
        out.append(evaluate(position, runtime, bars, step, step,
                            engine_cfg=engine_cfg or ExitEngineSettings()))
    return out, runtime


@pytest.fixture
def entered_into_a_downtrend():
    """Flat warm-up, entry at 09:45, then price rolls over immediately."""
    closes = [1000.0] * WARMUP + [1000 - i for i in range(1, 16)]
    bars = make_series(closes, start=ist(9, 15))
    position = Position(
        position_id="p-bad", symbol="RELIANCE", quantity=100,
        entry_price=Decimal("1000.00"), entry_time=ist(9, 45),
    )
    return position, bars


# ================================================================================
# The three trigger conditions, each on its own
# ================================================================================


def test_momentum_already_reversed_triggers(engine_cfg):
    problems, _ = evaluate_entry_conditions(
        Decimal("1000"), None, snapshot(ema9=990.0, ema21=1000.0, rsi14=43.0), engine_cfg
    )
    assert len(problems) == 1
    assert "EMA9 990.00 was already below EMA21 1000.00" in problems[0]
    assert "RSI14 was already 43.0" in problems[0]


def test_price_already_below_vwap_triggers(engine_cfg):
    problems, _ = evaluate_entry_conditions(
        Decimal("1000"), None, snapshot(last_price=995.0, vwap=1002.0), engine_cfg
    )
    assert len(problems) == 1
    assert "already below session VWAP 1002.00" in problems[0]


def test_price_already_through_the_stop_triggers(engine_cfg):
    problems, _ = evaluate_entry_conditions(
        Decimal("1000"), Decimal("998.00"), snapshot(last_price=997.0, vwap=990.0),
        engine_cfg,
    )
    assert len(problems) == 1
    assert "already at or through the stop 998.00" in problems[0]


def test_entry_price_at_or_below_the_stop_triggers(engine_cfg):
    """Position validation forbids this, but a hypothetical check can be asked it."""
    problems, _ = evaluate_entry_conditions(
        Decimal("990"), Decimal("995.00"), snapshot(last_price=1000.0, vwap=990.0),
        engine_cfg,
    )
    assert any("already at or below the stop" in p for p in problems)


def test_the_vwap_test_has_no_volume_gate(engine_cfg):
    """The EXIT rule needs above-average volume; the ENTRY test deliberately does not.

    Confirmation matters when abandoning a position you hold. For one you have not
    justified yet, being on the wrong side of VWAP at all is the finding.
    """
    problems, _ = evaluate_entry_conditions(
        Decimal("1000"), None, snapshot(last_price=995.0, vwap=1002.0, volume_ratio=0.2),
        engine_cfg,
    )
    assert any("VWAP" in p for p in problems)


def test_all_three_can_fire_together(engine_cfg):
    problems, _ = evaluate_entry_conditions(
        Decimal("1000"), Decimal("998.00"),
        snapshot(last_price=996.0, vwap=1005.0, ema9=990.0, ema21=1000.0, rsi14=30.0),
        engine_cfg,
    )
    assert len(problems) == 3


# ================================================================================
# A clean entry does not fire it
# ================================================================================


def test_a_clean_entry_is_valid(engine_cfg):
    problems, unevaluated = evaluate_entry_conditions(
        Decimal("1000"), Decimal("990"),
        snapshot(last_price=1005.0, vwap=1000.0, ema9=1006.0, ema21=1002.0, rsi14=61.0),
        engine_cfg,
    )
    assert problems == ()
    assert unevaluated == ()


def test_price_exactly_at_vwap_is_not_a_problem(engine_cfg):
    """Strictly below. Equal is not "wrong side of"."""
    problems, _ = evaluate_entry_conditions(
        Decimal("1000"), None, snapshot(last_price=1000.0, vwap=1000.0), engine_cfg
    )
    assert problems == ()


def test_weak_momentum_alone_is_not_a_problem(engine_cfg):
    """The rule is EMA cross AND weak RSI. Either alone is not a violation."""
    assert evaluate_entry_conditions(
        Decimal("1000"), None, snapshot(ema9=990.0, ema21=1000.0, rsi14=65.0), engine_cfg
    )[0] == ()
    assert evaluate_entry_conditions(
        Decimal("1000"), None, snapshot(ema9=1010.0, ema21=1000.0, rsi14=30.0), engine_cfg
    )[0] == ()


def test_a_clean_entry_produces_no_invalid_entry_state(position, engine_cfg):
    bars = flat_series(1000, WARMUP + 10, spread=1.0, start=ist(9, 15))
    advices, runtime = walk(position, bars, engine_cfg)
    assert advices
    assert all(a.state is not PositionState.INVALID_ENTRY for a in advices)
    assert runtime.entry_checked is True
    assert runtime.entry_verdict == ""


# ================================================================================
# Missing indicators are unevaluated, never passing
# ================================================================================


@pytest.mark.parametrize("field", ["ema9", "ema21", "rsi14"])
def test_missing_momentum_input_is_unevaluated(engine_cfg, field):
    problems, unevaluated = evaluate_entry_conditions(
        Decimal("1000"), None, snapshot(**{field: None}), engine_cfg
    )
    assert problems == ()
    assert any("momentum" in u for u in unevaluated)


def test_missing_vwap_is_unevaluated(engine_cfg):
    problems, unevaluated = evaluate_entry_conditions(
        Decimal("1000"), None, snapshot(vwap=None), engine_cfg
    )
    assert any("VWAP" in u for u in unevaluated)


def test_unevaluated_checks_are_surfaced_in_the_summary(engine_cfg):
    check = EntryCheck(
        valid=True, problems=(), unevaluated=("momentum (EMA21 not computable yet)",),
        asof=ist(9, 20), entry_price=Decimal("1000"), price_at_check=1000.0,
        indicators=None,
    )
    assert "Not checkable" in check.summary()


# ================================================================================
# Fires only on the first bar, and never suppresses later states
# ================================================================================


def test_invalid_entry_fires_on_the_first_bar_after_entry(
    entered_into_a_downtrend, engine_cfg
):
    position, bars = entered_into_a_downtrend
    advices, _ = walk(position, bars, engine_cfg)
    assert advices[0].state is PositionState.INVALID_ENTRY
    assert advices[0].rule is ExitRule.ENTRY_ALREADY_INVALID
    assert advices[0].asof == ist(9, 46)


def test_invalid_entry_fires_exactly_once(entered_into_a_downtrend, engine_cfg):
    position, bars = entered_into_a_downtrend
    advices, _ = walk(position, bars, engine_cfg)
    fired = [a for a in advices if a.state is PositionState.INVALID_ENTRY]
    assert len(fired) == 1


def test_the_engine_continues_normally_from_the_next_bar(
    entered_into_a_downtrend, engine_cfg
):
    """It is a diagnostic, not a mute button. Exit advice must resume immediately."""
    position, bars = entered_into_a_downtrend
    advices, _ = walk(position, bars, engine_cfg)
    assert len(advices) > 1
    assert advices[1].state is PositionState.EXIT_NOW
    assert advices[1].rule in {ExitRule.MOMENTUM_REVERSAL, ExitRule.VWAP_LOSS,
                               ExitRule.STOP_HIT}


def test_later_states_are_not_suppressed_for_a_recovering_position(engine_cfg):
    """Bad entry, then the trade works. TRAIL must still be reachable."""
    closes = [1000.0] * WARMUP + [999.0] + [1000 + i * 3 for i in range(1, 14)]
    bars = make_series(closes, start=ist(9, 15))
    position = Position(
        position_id="p-recover", symbol="RELIANCE", quantity=100,
        entry_price=Decimal("1000.00"), entry_time=ist(9, 45),
    )
    advices, _ = walk(position, bars, engine_cfg)
    states = [a.state for a in advices]
    assert states[0] is PositionState.INVALID_ENTRY
    assert {PositionState.TRAIL, PositionState.PARTIAL_EXIT} & set(states[1:])


def test_the_one_shot_flag_is_set_even_when_the_entry_was_clean(position, engine_cfg):
    bars = flat_series(1000, WARMUP + 5, spread=1.0, start=ist(9, 15))
    _, runtime = walk(position, bars, engine_cfg)
    assert runtime.entry_checked is True


def test_the_verdict_survives_on_the_runtime(entered_into_a_downtrend, engine_cfg):
    """The bar it fired on scrolls away; the finding should not."""
    position, bars = entered_into_a_downtrend
    _, runtime = walk(position, bars, engine_cfg)
    assert runtime.entry_verdict
    assert "unsound" in runtime.entry_verdict


def test_a_pre_set_flag_prevents_a_repeat(entered_into_a_downtrend, engine_cfg):
    position, bars = entered_into_a_downtrend
    runtime = PositionRuntime.initial(position)
    runtime.entry_checked = True
    advices, _ = walk(position, bars, engine_cfg, runtime=runtime)
    assert all(a.state is not PositionState.INVALID_ENTRY for a in advices)


# ================================================================================
# The wording is the feature
# ================================================================================


def test_the_reason_is_about_the_entry_not_about_now(
    entered_into_a_downtrend, engine_cfg
):
    position, bars = entered_into_a_downtrend
    advices, _ = walk(position, bars, engine_cfg)
    reason = advices[0].reason
    assert "should not have been opened" in reason
    assert "already" in reason
    assert "Momentum reversal:" not in reason, "that is the exit rule's wording"


def test_the_reason_says_it_is_not_an_exit_instruction(
    entered_into_a_downtrend, engine_cfg
):
    position, bars = entered_into_a_downtrend
    advices, _ = walk(position, bars, engine_cfg)
    assert "not an exit instruction" in advices[0].reason


def test_invalid_entry_suggests_no_exit_quantity(entered_into_a_downtrend, engine_cfg):
    position, bars = entered_into_a_downtrend
    advices, _ = walk(position, bars, engine_cfg)
    assert advices[0].suggested_exit_quantity == 0
    assert advices[0].state.suggests_exit is False


def test_invalid_entry_still_reports_gross_and_net(
    entered_into_a_downtrend, engine_cfg
):
    position, bars = entered_into_a_downtrend
    advices, _ = walk(position, bars, engine_cfg)
    assert advices[0].pnl is not None
    assert advices[0].pnl.net_pnl < advices[0].pnl.gross_pnl


def test_the_reason_carries_the_actual_numbers(entered_into_a_downtrend, engine_cfg):
    position, bars = entered_into_a_downtrend
    advices, _ = walk(position, bars, engine_cfg)
    import re

    assert re.search(r"\d+\.\d\d", advices[0].reason), "must quote real levels"


# ================================================================================
# Precedence
# ================================================================================


def test_precedence_still_covers_every_state():
    assert set(PRECEDENCE) == set(PositionState)
    assert PRECEDENCE.index(PositionState.INVALID_ENTRY) < \
        PRECEDENCE.index(PositionState.EXIT_NOW)


def test_force_exit_still_beats_invalid_entry(entered_into_a_downtrend, engine_cfg):
    """Square-off is unconditional; a diagnostic must not displace it."""
    position, bars = entered_into_a_downtrend
    runtime = PositionRuntime.initial(position)
    advice = evaluate(position, runtime, bars, ist(15, 20), ist(15, 20),
                      engine_cfg=engine_cfg)
    assert advice.state is PositionState.FORCE_EXIT


def test_a_stale_feed_beats_invalid_entry(entered_into_a_downtrend, engine_cfg):
    """Suppressed advice means suppressed -- including the diagnostic."""
    position, bars = entered_into_a_downtrend
    runtime = PositionRuntime.initial(position)
    now = ist(9, 46)
    advice = evaluate(position, runtime, bars, now, now - timedelta(seconds=90),
                      engine_cfg=engine_cfg)
    assert advice.state is PositionState.STALE_DATA
    assert runtime.entry_checked is False, "an unevaluated check must stay pending"


def test_invalid_entry_beats_exit_now_on_that_one_bar(
    entered_into_a_downtrend, engine_cfg
):
    position, bars = entered_into_a_downtrend
    advices, _ = walk(position, bars, engine_cfg)
    assert advices[0].state is PositionState.INVALID_ENTRY
    assert advices[1].state is PositionState.EXIT_NOW


# ================================================================================
# check_entry: judged on entry-time data, not on what happened next
# ================================================================================


def test_check_entry_uses_only_data_up_to_the_entry_bar(engine_cfg):
    """Judging with later data answers 'did it work out', not 'was it defensible'."""
    closes = [1000.0] * WARMUP + [999.0] + [1200.0] * 10  # spikes AFTER entry
    bars = make_series(closes, start=ist(9, 15))
    position = Position(
        position_id="p", symbol="X", quantity=10,
        entry_price=Decimal("1000.00"), entry_time=ist(9, 45),
    )
    check = check_entry(position, bars, ist(11, 0), engine_cfg=engine_cfg)
    assert check.asof == ist(9, 46)
    assert check.price_at_check == pytest.approx(999.0)
    assert not check.valid


def test_check_entry_returns_none_before_the_first_post_entry_bar(engine_cfg):
    bars = flat_series(1000, WARMUP, spread=1.0, start=ist(9, 15))
    position = Position(
        position_id="p", symbol="X", quantity=10,
        entry_price=Decimal("1000.00"), entry_time=ist(9, 45),
    )
    assert check_entry(position, bars, ist(9, 45), engine_cfg=engine_cfg) is None


def test_check_entry_is_deterministic(entered_into_a_downtrend, engine_cfg):
    position, bars = entered_into_a_downtrend
    a = check_entry(position, bars, ist(11, 0), engine_cfg=engine_cfg)
    b = check_entry(position, bars, ist(11, 0), engine_cfg=engine_cfg)
    assert a == b


def test_a_late_start_records_the_verdict_without_announcing_it(
    entered_into_a_downtrend, engine_cfg
):
    """A restart that missed the entry bar should not report old news as current."""
    position, bars = entered_into_a_downtrend
    runtime = PositionRuntime.initial(position)
    advice = evaluate(position, runtime, bars, ist(9, 55), ist(9, 55),
                      engine_cfg=engine_cfg)
    assert advice.state is not PositionState.INVALID_ENTRY
    assert runtime.entry_checked is True
    assert runtime.entry_verdict, "the finding is still recorded for the journal"


# ================================================================================
# check_hypothetical_entry: the "should I buy" path
# ================================================================================


def test_hypothetical_check_uses_the_last_completed_bar(engine_cfg):
    bars = make_series([1000.0] * WARMUP + [995.0], start=ist(9, 15))
    check = check_hypothetical_entry(bars, ist(9, 46), engine_cfg=engine_cfg)
    assert check.asof == ist(9, 46)
    assert check.price_at_check == pytest.approx(995.0)


def test_hypothetical_check_defaults_the_price_to_the_last_close(engine_cfg):
    bars = flat_series(1000, WARMUP, spread=1.0, start=ist(9, 15))
    check = check_hypothetical_entry(bars, bars[-1].close_time, engine_cfg=engine_cfg)
    assert check.entry_price == Decimal("1000.0")


def test_hypothetical_check_excludes_the_forming_bar(engine_cfg):
    bars = make_series([1000.0] * WARMUP + [500.0], start=ist(9, 15))
    mid = bars[-1].ts + timedelta(seconds=30)
    check = check_hypothetical_entry(bars, mid, engine_cfg=engine_cfg)
    assert check.price_at_check == pytest.approx(1000.0), "forming bar must not leak"


def test_hypothetical_check_flags_a_bad_moment(engine_cfg):
    bars = make_series([1000 - i for i in range(WARMUP)], start=ist(9, 15))
    check = check_hypothetical_entry(bars, bars[-1].close_time, engine_cfg=engine_cfg)
    assert not check.valid
    assert check.problems


def test_hypothetical_check_accepts_a_proposed_stop(engine_cfg):
    bars = flat_series(1000, WARMUP, spread=1.0, start=ist(9, 15))
    check = check_hypothetical_entry(
        bars, bars[-1].close_time, Decimal("1000"), Decimal("1005"),
        engine_cfg=engine_cfg,
    )
    assert any("stop" in p for p in check.problems)


def test_hypothetical_check_returns_none_with_no_bars(engine_cfg):
    assert check_hypothetical_entry([], ist(9, 46), engine_cfg=engine_cfg) is None


def test_hypothetical_render_names_the_symbol(engine_cfg):
    bars = flat_series(1000, WARMUP, spread=1.0, start=ist(9, 15))
    check = check_hypothetical_entry(bars, bars[-1].close_time, engine_cfg=engine_cfg)
    text = check.render("RELIANCE")
    assert "RELIANCE" in text and "ENTRY OK" in text


# ================================================================================
# Persistence
# ================================================================================


def test_entry_state_round_trips_through_the_store(tmp_path, entered_into_a_downtrend,
                                                   engine_cfg):
    from src.positions.store import PositionStore

    position, bars = entered_into_a_downtrend
    _, runtime = walk(position, bars, engine_cfg)
    store = PositionStore(tmp_path)
    store.save_runtime(runtime, position.trade_date)

    loaded = PositionStore(tmp_path).load_runtime(position.position_id,
                                                  position.trade_date)
    assert loaded.entry_checked is True
    assert loaded.entry_verdict == runtime.entry_verdict


def test_a_restart_does_not_re_announce_the_entry_check(tmp_path,
                                                        entered_into_a_downtrend,
                                                        engine_cfg):
    from src.positions.store import PositionStore

    position, bars = entered_into_a_downtrend
    advices, runtime = walk(position, bars, engine_cfg)
    assert advices[0].state is PositionState.INVALID_ENTRY

    store = PositionStore(tmp_path)
    store.save_runtime(runtime, position.trade_date)
    resumed = store.load_or_init_runtime(position)
    again, _ = walk(position, bars, engine_cfg, runtime=resumed)
    assert all(a.state is not PositionState.INVALID_ENTRY for a in again)
