"""Tests for batch replay, MAE/MFE, and the momentum diagnostics.

These test the MACHINERY, using synthetic bars. They deliberately assert nothing about
what the momentum rule does in the market -- that question needs real data, and
tests/test_momentum_findings.py has none to work with.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from config.settings import ExitEngineSettings
from src.analysis.momentum import (
    Stat,
    exit_rule_distribution,
    exit_state_distribution,
    exit_time_histogram,
    forward_price,
    forward_returns_post_exit,
    minimum_detectable_effect,
    summarise,
    sweep_momentum_confirmation,
    sweep_summary,
    sweep_trail_multiple,
)
from src.positions.feed import synthetic_session
from src.positions.models import ExitRule, PositionState
from src.positions.replay import (
    DEFAULT_ENTRY_TIME,
    batch_replay,
    outcomes_to_dataframe,
    replay_one_session,
    write_outcomes_csv,
)
from src.positions.store import PositionStore
from tests.conftest import flat_series, ist, make_series

DAY = date(2026, 8, 20)
DAY2 = date(2026, 8, 21)


@pytest.fixture
def stocked(tmp_path) -> PositionStore:
    """A store with two symbols x two days of synthetic sessions."""
    store = PositionStore(tmp_path)
    for symbol, base in (("RELIANCE", 1000.0), ("INFY", 1500.0)):
        for day in (DAY, DAY2):
            store.save_bars(symbol, day, synthetic_session(symbol, day, open_price=base))
    return store


# --------------------------------------------------------------------------------
# ExitRule plumbing
# --------------------------------------------------------------------------------


def test_exit_rule_is_tagged_not_grepped(position, engine_cfg):
    """Analysis keys off an enum, so rewording a reason string cannot break it."""
    from src.positions.exit_engine import evaluate
    from src.positions.models import PositionRuntime

    bars = flat_series(1000, 30, spread=1.0)
    rt = PositionRuntime.initial(position)
    advice = evaluate(position, rt, bars, ist(15, 20), ist(15, 20), engine_cfg=engine_cfg)
    assert advice.state is PositionState.FORCE_EXIT
    assert advice.rule is ExitRule.SQUARE_OFF


def test_hold_carries_no_rule(position, engine_cfg):
    from src.positions.exit_engine import evaluate
    from src.positions.models import PositionRuntime

    bars = flat_series(1000, 30, spread=1.0)
    advice = evaluate(
        position, PositionRuntime.initial(position), bars,
        bars[-1].close_time, bars[-1].close_time, engine_cfg=engine_cfg,
    )
    assert advice.state is PositionState.HOLD
    assert advice.rule is ExitRule.NONE


def test_momentum_confirmation_of_one_matches_the_original_rule(position):
    """N=1 must be behaviour-identical to the pre-existing single-bar rule."""
    from src.positions.exit_engine import evaluate
    from src.positions.models import PositionRuntime

    bars = make_series([1000 - i for i in range(30)], start=ist(9, 15))
    cfg = ExitEngineSettings(momentum_confirmation_bars=1)
    advice = evaluate(
        position, PositionRuntime.initial(position), bars,
        bars[-1].close_time, bars[-1].close_time, engine_cfg=cfg,
    )
    assert advice.rule is ExitRule.MOMENTUM_REVERSAL


def test_higher_confirmation_suppresses_a_one_bar_blip(position):
    """A reversal that has not persisted must not fire when N is raised."""
    from src.positions.exit_engine import evaluate
    from src.positions.models import PositionRuntime

    # Rising all session, then a single sharp down bar.
    closes = [1000 + i for i in range(29)] + [995.0]
    bars = make_series(closes, start=ist(9, 15))
    fired = {}
    for n in (1, 5):
        cfg = ExitEngineSettings(momentum_confirmation_bars=n)
        advice = evaluate(
            position, PositionRuntime.initial(position), bars,
            bars[-1].close_time, bars[-1].close_time, engine_cfg=cfg,
        )
        fired[n] = advice.rule is ExitRule.MOMENTUM_REVERSAL
    assert fired[5] is False, "a single-bar blip must not satisfy a 5-bar requirement"


def test_confirmation_reason_states_the_bar_count(position):
    from src.positions.exit_engine import evaluate
    from src.positions.models import PositionRuntime

    bars = make_series([1000 - i for i in range(40)], start=ist(9, 15))
    cfg = ExitEngineSettings(momentum_confirmation_bars=3)
    advice = evaluate(
        position, PositionRuntime.initial(position), bars,
        bars[-1].close_time, bars[-1].close_time, engine_cfg=cfg,
    )
    if advice.rule is ExitRule.MOMENTUM_REVERSAL:
        assert "3 consecutive bars" in advice.reason


# --------------------------------------------------------------------------------
# replay_one_session
# --------------------------------------------------------------------------------


def test_entry_uses_the_last_completed_bar_close(stocked, engine_cfg):
    bars = stocked.load_bars("RELIANCE", DAY)
    outcome = replay_one_session("RELIANCE", DAY, bars, engine_cfg=engine_cfg)
    entry_at = datetime.combine(DAY, DEFAULT_ENTRY_TIME, tzinfo=ist(9, 45).tzinfo)
    expected = [b for b in bars if b.ts + timedelta(minutes=1) <= entry_at][-1].close
    assert outcome.entry_price == pytest.approx(expected)


def test_quantity_comes_from_a_fixed_notional(stocked, engine_cfg):
    """Comparable P&L across symbols with very different share prices."""
    a = replay_one_session("RELIANCE", DAY, stocked.load_bars("RELIANCE", DAY),
                           engine_cfg=engine_cfg)
    b = replay_one_session("INFY", DAY, stocked.load_bars("INFY", DAY),
                           engine_cfg=engine_cfg)
    for o in (a, b):
        assert 90_000 <= o.quantity * o.entry_price <= 100_000


def test_session_with_no_bar_by_entry_time_is_skipped_not_invented(engine_cfg):
    bars = flat_series(1000, 5, start=ist(14, 0))
    assert replay_one_session("X", DAY, bars, engine_cfg=engine_cfg) is None


def test_outcome_reports_gross_and_net(stocked, engine_cfg):
    outcome = replay_one_session("RELIANCE", DAY, stocked.load_bars("RELIANCE", DAY),
                                 engine_cfg=engine_cfg)
    assert outcome.net_pnl < outcome.gross_pnl
    assert outcome.costs > 0


def test_outcome_records_the_exit_rule(stocked, engine_cfg):
    outcome = replay_one_session("RELIANCE", DAY, stocked.load_bars("RELIANCE", DAY),
                                 engine_cfg=engine_cfg)
    assert outcome.exit_rule in {r.value for r in ExitRule}


# --------------------------------------------------------------------------------
# MAE / MFE
# --------------------------------------------------------------------------------


def test_mfe_and_mae_are_non_negative(stocked, engine_cfg):
    for symbol in ("RELIANCE", "INFY"):
        outcome = replay_one_session(symbol, DAY, stocked.load_bars(symbol, DAY),
                                     engine_cfg=engine_cfg)
        assert outcome.mfe >= 0
        assert outcome.mae >= 0


def test_mfe_uses_bar_highs_not_closes(engine_cfg):
    """The excursion is what price reached, which is what a stop would have felt."""
    from src.positions.models import Bar

    bars = []
    for i in range(60):
        ts = ist(9, 15) + timedelta(minutes=i)
        spike = 1050.0 if i == 40 else 1001.0
        bars.append(Bar(ts=ts, open=1000.0, high=spike, low=999.0, close=1000.0, volume=1000))
    outcome = replay_one_session("X", DAY, bars, engine_cfg=engine_cfg)
    assert outcome.mfe == pytest.approx(50.0, abs=1.0), "a high-only spike must count"


def test_excursions_exclude_the_exit_bar(engine_cfg):
    """Movement at or after the exit moment is not part of the holding period."""
    from src.positions.models import Bar

    bars = []
    for i in range(120):
        ts = ist(9, 15) + timedelta(minutes=i)
        # A huge high on the very last bar, long after any exit.
        high = 5000.0 if i == 119 else 1001.0
        bars.append(Bar(ts=ts, open=1000.0, high=high, low=999.0, close=1000.0, volume=1000))
    outcome = replay_one_session("X", DAY, bars, engine_cfg=engine_cfg)
    if outcome.exit_time is not None and outcome.exit_time < bars[-1].ts:
        assert outcome.mfe < 100.0, "post-exit spike leaked into MFE"


def test_mae_mfe_percentages_match_the_absolutes(stocked, engine_cfg):
    outcome = replay_one_session("RELIANCE", DAY, stocked.load_bars("RELIANCE", DAY),
                                 engine_cfg=engine_cfg)
    assert outcome.mfe_pct == pytest.approx(100 * outcome.mfe / outcome.entry_price)
    assert outcome.mae_pct == pytest.approx(100 * outcome.mae / outcome.entry_price)


# --------------------------------------------------------------------------------
# batch_replay
# --------------------------------------------------------------------------------


def test_batch_replay_covers_every_symbol_day(stocked, engine_cfg):
    outcomes = batch_replay(stocked, ["RELIANCE", "INFY"], [DAY, DAY2], engine_cfg=engine_cfg)
    assert len(outcomes) == 4
    assert {o.symbol for o in outcomes} == {"RELIANCE", "INFY"}
    assert {o.trade_date for o in outcomes} == {DAY, DAY2}


def test_missing_symbol_days_are_skipped_not_counted_as_flat(stocked, engine_cfg):
    """A missing day is missing data, not a no-trade day."""
    outcomes = batch_replay(
        stocked, ["RELIANCE", "NOTSTORED"], [DAY, date(2026, 8, 19)], engine_cfg=engine_cfg
    )
    assert {o.symbol for o in outcomes} == {"RELIANCE"}
    assert len(outcomes) == 1


def test_batch_replay_is_deterministic(stocked, engine_cfg):
    a = batch_replay(stocked, ["RELIANCE"], [DAY], engine_cfg=engine_cfg)
    b = batch_replay(stocked, ["RELIANCE"], [DAY], engine_cfg=engine_cfg)
    assert [o.as_row() for o in a] == [o.as_row() for o in b]


def test_batch_replay_on_an_empty_store(tmp_path, engine_cfg):
    assert batch_replay(PositionStore(tmp_path), ["RELIANCE"], [DAY],
                        engine_cfg=engine_cfg) == []


# --------------------------------------------------------------------------------
# DataFrame / CSV
# --------------------------------------------------------------------------------


def test_dataframe_has_a_row_per_outcome(stocked, engine_cfg):
    outcomes = batch_replay(stocked, ["RELIANCE", "INFY"], [DAY, DAY2], engine_cfg=engine_cfg)
    frame = outcomes_to_dataframe(outcomes)
    assert len(frame) == 4
    for column in ("symbol", "exit_rule", "gross_pnl", "net_pnl", "mae", "mfe"):
        assert column in frame.columns


def test_dataframe_keeps_gross_and_net_together(stocked, engine_cfg):
    frame = outcomes_to_dataframe(batch_replay(stocked, ["RELIANCE"], [DAY],
                                               engine_cfg=engine_cfg))
    assert (frame["net_pnl"] < frame["gross_pnl"]).all()


def test_dataframe_timestamps_are_ist(stocked, engine_cfg):
    frame = outcomes_to_dataframe(batch_replay(stocked, ["RELIANCE"], [DAY],
                                               engine_cfg=engine_cfg))
    assert str(frame["entry_time"].dt.tz) == "Asia/Kolkata"


def test_empty_outcomes_give_an_empty_frame_not_a_crash():
    assert len(outcomes_to_dataframe([])) == 0


def test_csv_is_written(tmp_path, stocked, engine_cfg):
    outcomes = batch_replay(stocked, ["RELIANCE"], [DAY], engine_cfg=engine_cfg)
    path = write_outcomes_csv(outcomes, tmp_path / "out" / "outcomes.csv")
    assert path.exists()
    assert "exit_rule" in path.read_text(encoding="utf-8").splitlines()[0]


# --------------------------------------------------------------------------------
# Stat -- the discipline layer
# --------------------------------------------------------------------------------


def test_stat_requires_a_sample_size():
    with pytest.raises(TypeError):
        Stat(label="x", mean=0.1)  # type: ignore[call-arg]


def test_summarise_computes_mean_median_sd():
    stat = summarise("x", [1.0, 2.0, 3.0, 4.0])
    assert stat.n == 4
    assert stat.mean == pytest.approx(2.5)
    assert stat.median == pytest.approx(2.5)
    assert stat.sd == pytest.approx(math.sqrt(5 / 3))


def test_tiny_sample_refuses_to_make_a_claim():
    assert "NO CLAIM POSSIBLE" in summarise("x", [0.5] * 10).verdict()


def test_interval_crossing_zero_is_reported_as_indistinguishable():
    values = [0.1, -0.1] * 100
    assert "NOT DISTINGUISHABLE FROM ZERO" in summarise("x", values).verdict()


def test_moderate_sample_is_directional_only():
    values = [0.5] * 50 + [0.4] * 50
    verdict = summarise("x", values).verdict()
    assert "DIRECTIONAL ONLY" in verdict


def test_large_consistent_sample_is_supported():
    values = ([0.5] * 150 + [0.4] * 150) * 2
    assert "SUPPORTED" in summarise("x", values).verdict()


def test_verdict_always_states_n():
    for values in ([0.1] * 5, [0.1, -0.1] * 100, [0.5] * 400):
        assert "n=" in summarise("x", values).verdict()


def test_mde_shrinks_with_sample_size():
    assert minimum_detectable_effect(100, 1.0) > minimum_detectable_effect(1000, 1.0)


def test_mde_is_infinite_for_a_degenerate_sample():
    assert minimum_detectable_effect(1, 1.0) == float("inf")


def test_stat_row_carries_n_and_mde():
    row = summarise("x", [0.1, 0.2, 0.3]).as_row()
    assert "n" in row and "mde_pct" in row


# --------------------------------------------------------------------------------
# Distributions
# --------------------------------------------------------------------------------


def test_rule_distribution_percentages_sum_to_100(stocked, engine_cfg):
    outcomes = batch_replay(stocked, ["RELIANCE", "INFY"], [DAY, DAY2], engine_cfg=engine_cfg)
    rows = exit_rule_distribution(outcomes)
    assert sum(r["n"] for r in rows) == len(outcomes)
    assert sum(r["pct"] for r in rows) == pytest.approx(100.0, abs=0.05)


def test_state_distribution_also_sums(stocked, engine_cfg):
    outcomes = batch_replay(stocked, ["RELIANCE"], [DAY, DAY2], engine_cfg=engine_cfg)
    assert sum(r["n"] for r in exit_state_distribution(outcomes)) == len(outcomes)


def test_distribution_on_empty_input():
    assert exit_rule_distribution([]) == []


def test_exit_time_histogram_bins(stocked, engine_cfg):
    outcomes = batch_replay(stocked, ["RELIANCE", "INFY"], [DAY, DAY2], engine_cfg=engine_cfg)
    rows = exit_time_histogram(outcomes, bin_minutes=15)
    assert sum(r["n"] for r in rows) == len([o for o in outcomes if o.exit_time])
    for row in rows:
        assert int(row["bin"][3:]) % 15 == 0


def test_exit_time_histogram_can_filter_by_rule(stocked, engine_cfg):
    outcomes = batch_replay(stocked, ["RELIANCE"], [DAY], engine_cfg=engine_cfg)
    rule = outcomes[0].exit_rule
    rows = exit_time_histogram(outcomes, rule=rule)
    assert sum(r["n"] for r in rows) == sum(1 for o in outcomes if o.exit_rule == rule)


# --------------------------------------------------------------------------------
# Forward returns
# --------------------------------------------------------------------------------


def test_forward_price_reads_the_bar_60_minutes_later():
    bars = make_series([100 + i for i in range(180)], start=ist(9, 15))
    price, censored = forward_price(bars, ist(10, 0), minutes=60)
    assert censored is False
    # Target 11:00; the last bar completed by then opened 10:59, which is
    # 104 minutes after the 09:15 start, so close = 100 + 104.
    assert price == pytest.approx(204.0)


def test_forward_window_past_session_end_is_censored_not_truncated():
    """Truncating to the close would shrink measured moves and fake a null result."""
    bars = make_series([100] * 60, start=ist(9, 15))
    price, censored = forward_price(bars, ist(10, 10), minutes=60)
    assert censored is True
    assert price is None


def test_forward_returns_exclude_censored_observations(stocked, engine_cfg):
    outcomes = batch_replay(stocked, ["RELIANCE", "INFY"], [DAY, DAY2], engine_cfg=engine_cfg)
    stat, rows = forward_returns_post_exit(stocked, outcomes, minutes=60)
    assert stat.n == len(rows)
    assert stat.n + stat.censored == len(outcomes)


def test_forward_returns_can_filter_to_momentum_exits(stocked, engine_cfg):
    outcomes = batch_replay(stocked, ["RELIANCE", "INFY"], [DAY, DAY2], engine_cfg=engine_cfg)
    stat, rows = forward_returns_post_exit(stocked, outcomes, rule="MOMENTUM_REVERSAL")
    assert all(r["exit_rule"] == "MOMENTUM_REVERSAL" for r in rows)
    assert "MOMENTUM_REVERSAL" in stat.label


def test_forward_return_sign_convention():
    """Positive means price rose after the exit -- the cutting-winners signature."""
    bars = make_series([100 + i for i in range(180)], start=ist(9, 15))
    price, _ = forward_price(bars, ist(10, 0), minutes=60)
    exit_price = 85.0
    assert 100.0 * (price - exit_price) / exit_price > 0


# --------------------------------------------------------------------------------
# Sweeps
# --------------------------------------------------------------------------------


def test_trail_sweep_runs_every_value(stocked, engine_cfg):
    rows = sweep_trail_multiple(stocked, ["RELIANCE"], [DAY, DAY2],
                                values=(0.5, 1.5, 3.0), base=engine_cfg)
    assert [r["config"] for r in rows] == ["trail=0.5x", "trail=1.5x", "trail=3.0x"]
    assert all(r["n"] > 0 for r in rows)


def test_confirmation_sweep_runs_every_value(stocked, engine_cfg):
    rows = sweep_momentum_confirmation(stocked, ["RELIANCE"], [DAY, DAY2],
                                       values=(1, 3), base=engine_cfg)
    assert [r["config"] for r in rows] == ["confirm=1bar", "confirm=3bar"]


def test_sweep_reports_gross_and_net(stocked, engine_cfg):
    rows = sweep_trail_multiple(stocked, ["RELIANCE"], [DAY], values=(1.5,), base=engine_cfg)
    assert rows[0]["total_net_pnl"] < rows[0]["total_gross_pnl"]


def test_sweep_summary_flags_an_inert_parameter():
    rows = [{"config": "a", "n": 10, "total_net_pnl": 500.0},
            {"config": "b", "n": 10, "total_net_pnl": 500.0}]
    assert "IDENTICAL" in sweep_summary(rows)
    assert "inert" in sweep_summary(rows)


def test_sweep_summary_refuses_to_recommend_a_winner():
    rows = [{"config": "a", "n": 10, "total_net_pnl": 100.0},
            {"config": "b", "n": 10, "total_net_pnl": 900.0}]
    text = sweep_summary(rows)
    assert "in-sample maximum" in text
    assert "not a recommendation" in text


def test_sweep_summary_handles_no_outcomes():
    assert "nothing to compare" in sweep_summary([{"config": "a", "n": 0}])
