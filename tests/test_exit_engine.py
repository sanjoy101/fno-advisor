"""Tests for the exit advisory state machine.

The four rules called out as must-cover each get a dedicated section:
  * the stop never moves down
  * FORCE_EXIT overrides everything
  * a stale feed suppresses advice
  * TIME_EXIT boundary conditions

Thresholds are read back off the computed snapshot rather than hardcoded. ATR moves
as bars arrive, so a hardcoded "T1 is 1003" test would pass or fail for reasons that
have nothing to do with the state machine. Indicator values themselves are pinned in
tests/test_indicators.py.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from config.settings import ExitEngineSettings
from src.positions.exit_engine import PRECEDENCE, _exit_now_reason, evaluate, target_one
from src.positions.models import (
    ExitRule,
    IndicatorSnapshot,
    Position,
    PositionRuntime,
    PositionState,
    completed_bars,
)
from tests.conftest import flat_series, ist, make_bar, make_series

WARMUP = 30  # enough for ATR14, RSI14, EMA21 and the 20-bar volume baseline

_AUTO = object()


def advise(
    position: Position,
    bars,
    *,
    runtime: PositionRuntime | None = None,
    now=None,
    last_tick_at=_AUTO,
    engine_cfg: ExitEngineSettings | None = None,
    cost_cfg=None,
):
    """Evaluate with sane defaults: decide just after the last bar closed.

    ``last_tick_at`` defaults to the close of the most recent COMPLETED bar, which is
    what a real feed's last tick would be. Deriving it from ``bars[-1]`` instead would
    quietly hand the engine a future timestamp and mask genuine staleness.
    """
    runtime = runtime or PositionRuntime.initial(position)
    now = now or bars[-1].close_time
    if last_tick_at is _AUTO:
        done = completed_bars(bars, now)
        last_tick_at = done[-1].close_time if done else None
    advice = evaluate(
        position,
        runtime,
        bars,
        now,
        last_tick_at,
        engine_cfg=engine_cfg or ExitEngineSettings(),
        cost_cfg=cost_cfg,
    )
    return advice, runtime


@pytest.fixture
def calm_bars():
    """30 flat bars at 1000 with a 2.0 range, so ATR converges to 2.0."""
    return flat_series(1000, WARMUP, spread=1.0)


def append(bars, close, *, spread=1.0, volume=1000):
    """Append one more 1-minute bar after the last one."""
    return bars + [
        make_bar(
            bars[-1].ts + timedelta(minutes=1),
            close,
            high=close + spread,
            low=close - spread,
            volume=volume,
        )
    ]


# ================================================================================
# 1. THE STOP NEVER MOVES DOWN
# ================================================================================


def test_raise_stop_refuses_to_lower(position):
    rt = PositionRuntime.initial(position)
    assert rt.raise_stop(Decimal("1010.00")) is True
    assert rt.raise_stop(Decimal("1005.00")) is False
    assert rt.current_stop == Decimal("1010.00")


def test_raise_stop_ignores_an_equal_candidate(position):
    rt = PositionRuntime.initial(position)
    rt.raise_stop(Decimal("1010.00"))
    assert rt.raise_stop(Decimal("1010.00")) is False


def test_raise_stop_ignores_none(position):
    rt = PositionRuntime.initial(position)
    rt.raise_stop(Decimal("1010.00"))
    assert rt.raise_stop(None) is False
    assert rt.current_stop == Decimal("1010.00")


def test_stop_is_monotonic_across_a_whole_price_path(position, calm_bars, engine_cfg):
    """Walk a round trip up and back down; the stop must never retreat."""
    path = [1005, 1010, 1020, 1015, 1012, 1030, 1025, 1008, 1000, 995]
    bars = calm_bars
    for close in path:
        bars = append(bars, close)

    rt = PositionRuntime.initial(position)
    stops = []
    for i in range(WARMUP, len(bars) + 1):
        window = bars[:i]
        evaluate(
            position, rt, window, window[-1].close_time, window[-1].close_time,
            engine_cfg=engine_cfg,
        )
        stops.append(rt.current_stop)

    seen = [s for s in stops if s is not None]
    assert seen, "the trail should have engaged somewhere on this path"
    assert seen == sorted(seen), f"stop moved down: {seen}"


def test_stop_does_not_retreat_when_price_falls_after_a_high(position, calm_bars, engine_cfg):
    bars = append(calm_bars, 1030)
    _, rt = advise(position, bars, engine_cfg=engine_cfg)
    high_water_stop = rt.current_stop
    assert high_water_stop is not None

    bars = append(bars, 1001)
    evaluate(position, rt, bars, bars[-1].close_time, bars[-1].close_time, engine_cfg=engine_cfg)
    assert rt.current_stop == high_water_stop


def test_breakeven_is_armed_after_one_atr_of_profit(position, calm_bars, engine_cfg):
    bars = append(calm_bars, 1030)
    advice, rt = advise(position, bars, engine_cfg=engine_cfg)
    assert rt.breakeven_armed is True
    assert rt.current_stop >= position.entry_price


def test_breakeven_is_not_armed_before_one_atr_of_profit(position, calm_bars, engine_cfg):
    """+0.50 on an ATR of 2.0 is well short of the 1x trigger."""
    bars = append(calm_bars, 1000.5)
    advice, rt = advise(position, bars, engine_cfg=engine_cfg)
    assert rt.breakeven_armed is False
    assert advice.state is PositionState.HOLD


def test_trailing_stop_sits_1_5_atr_below_the_running_high(position, calm_bars, engine_cfg):
    bars = append(calm_bars, 1030)
    advice, rt = advise(position, bars, engine_cfg=engine_cfg)
    atr = advice.indicators.atr14
    expected = rt.highest_price - 1.5 * atr
    assert float(rt.current_stop) == pytest.approx(expected, abs=0.01)


def test_running_high_tracks_the_max_close_not_the_last_close(position, calm_bars, engine_cfg):
    bars = append(append(calm_bars, 1030), 1010)
    _, rt = advise(position, bars, engine_cfg=engine_cfg)
    assert rt.highest_price == pytest.approx(1030.0)


def test_an_explicit_stop_is_the_starting_point_and_is_never_lowered(calm_bars, engine_cfg):
    pos = Position(
        position_id="p-stop",
        symbol="INFY",
        quantity=100,
        entry_price=Decimal("1000.00"),
        entry_time=ist(9, 20),
        stop_loss=Decimal("990.00"),
    )
    rt = PositionRuntime.initial(pos)
    assert rt.current_stop == Decimal("990.00")
    bars = append(calm_bars, 1030)
    evaluate(pos, rt, bars, bars[-1].close_time, bars[-1].close_time, engine_cfg=engine_cfg)
    assert rt.current_stop > Decimal("990.00")


# ================================================================================
# 2. FORCE_EXIT OVERRIDES EVERYTHING
# ================================================================================


def square_off_now():
    return ist(15, 15, 0)


def test_force_exit_at_square_off(position, calm_bars, engine_cfg):
    advice, _ = advise(position, calm_bars, now=square_off_now(), engine_cfg=engine_cfg)
    assert advice.state is PositionState.FORCE_EXIT
    assert "15:15" in advice.reason


def test_force_exit_after_square_off_too(position, calm_bars, engine_cfg):
    advice, _ = advise(position, calm_bars, now=ist(15, 29), engine_cfg=engine_cfg)
    assert advice.state is PositionState.FORCE_EXIT


def test_no_force_exit_one_second_before_square_off(position, calm_bars, engine_cfg):
    advice, _ = advise(position, calm_bars, now=ist(15, 14, 59), engine_cfg=engine_cfg)
    assert advice.state is not PositionState.FORCE_EXIT


@pytest.mark.parametrize(
    "scenario",
    ["calm_hold", "deep_profit", "stop_breached", "vwap_loss"],
    ids=["over-HOLD", "over-PARTIAL/TRAIL", "over-EXIT_NOW", "over-EXIT_NOW-vwap"],
)
def test_force_exit_beats_every_other_state(position, calm_bars, engine_cfg, scenario):
    """Whatever the price is doing, 15:15 wins."""
    bars = {
        "calm_hold": calm_bars,
        "deep_profit": append(calm_bars, 1080),
        "stop_breached": append(append(calm_bars, 1080), 900),
        "vwap_loss": append(calm_bars, 950, volume=50_000),
    }[scenario]
    advice, _ = advise(position, bars, now=square_off_now(), engine_cfg=engine_cfg)
    assert advice.state is PositionState.FORCE_EXIT
    assert advice.suggested_exit_quantity == position.quantity


def test_force_exit_beats_a_stale_feed_because_it_needs_no_price(
    position, calm_bars, engine_cfg
):
    """The square-off deadline is a clock fact. A dead feed cannot make it wrong."""
    now = square_off_now()
    advice, _ = advise(
        position, calm_bars, now=now, last_tick_at=now - timedelta(seconds=300),
        engine_cfg=engine_cfg,
    )
    assert advice.state is PositionState.FORCE_EXIT
    assert advice.feed_stale is True
    assert "WARNING" in advice.reason
    assert advice.pnl.price_is_stale is True, "the P&L must be labelled, not presented as current"


def test_force_exit_when_market_is_closed_with_a_position_still_open(
    position, calm_bars, engine_cfg
):
    saturday = ist(12, 0, d=date(2026, 8, 22))
    advice, _ = advise(position, calm_bars, now=saturday, engine_cfg=engine_cfg)
    assert advice.state is PositionState.FORCE_EXIT
    assert "closed" in advice.reason.lower()


def test_force_exit_suggests_closing_the_whole_remaining_quantity(
    position, calm_bars, engine_cfg
):
    rt = PositionRuntime.initial(position)
    rt.remaining_quantity = 40
    advice, _ = advise(
        position, calm_bars, runtime=rt, now=square_off_now(), engine_cfg=engine_cfg
    )
    assert advice.suggested_exit_quantity == 40


# ================================================================================
# 3. A STALE FEED SUPPRESSES ADVICE
# ================================================================================


def test_stale_feed_yields_stale_data_state(position, calm_bars, engine_cfg):
    now = calm_bars[-1].close_time
    advice, _ = advise(
        position, calm_bars, now=now, last_tick_at=now - timedelta(seconds=45),
        engine_cfg=engine_cfg,
    )
    assert advice.state is PositionState.STALE_DATA
    assert advice.actionable is False


def test_stale_feed_suppresses_an_otherwise_certain_exit(position, calm_bars, engine_cfg):
    """Stop clearly breached, but the price is too old to act on."""
    bars = append(append(calm_bars, 1080), 900)
    now = bars[-1].close_time
    advice, _ = advise(
        position, bars, now=now, last_tick_at=now - timedelta(seconds=60),
        engine_cfg=engine_cfg,
    )
    assert advice.state is PositionState.STALE_DATA
    assert advice.suggested_exit_quantity == 0


def test_stale_feed_does_not_ratchet_the_stop(position, calm_bars, engine_cfg):
    """A trail must not lock onto a level the market may have long since left."""
    bars = append(calm_bars, 1080)
    now = bars[-1].close_time
    _, rt = advise(
        position, bars, now=now, last_tick_at=now - timedelta(seconds=60),
        engine_cfg=engine_cfg,
    )
    assert rt.current_stop is None
    assert rt.breakeven_armed is False
    assert rt.highest_price == pytest.approx(float(position.entry_price))


def test_stale_boundary_is_inclusive_of_the_threshold(position, calm_bars, engine_cfg):
    now = calm_bars[-1].close_time
    fresh, _ = advise(
        position, calm_bars, now=now,
        last_tick_at=now - timedelta(seconds=float(engine_cfg.stale_feed_seconds)),
        engine_cfg=engine_cfg,
    )
    stale, _ = advise(
        position, calm_bars, now=now,
        last_tick_at=now - timedelta(seconds=float(engine_cfg.stale_feed_seconds) + 1),
        engine_cfg=engine_cfg,
    )
    assert fresh.state is not PositionState.STALE_DATA
    assert stale.state is PositionState.STALE_DATA


def test_no_tick_ever_received_counts_as_stale(position, calm_bars, engine_cfg):
    advice, _ = advise(position, calm_bars, last_tick_at=..., engine_cfg=engine_cfg) \
        if False else advise(position, calm_bars, engine_cfg=engine_cfg)
    # explicit None path:
    rt = PositionRuntime.initial(position)
    advice = evaluate(
        position, rt, calm_bars, calm_bars[-1].close_time, None, engine_cfg=engine_cfg
    )
    assert advice.state is PositionState.STALE_DATA
    assert advice.stale_seconds is None


def test_stale_pnl_is_labelled_not_hidden(position, calm_bars, engine_cfg):
    now = calm_bars[-1].close_time
    advice, _ = advise(
        position, calm_bars, now=now, last_tick_at=now - timedelta(seconds=60),
        engine_cfg=engine_cfg,
    )
    assert advice.pnl is not None, "the user still needs the last-known number"
    assert advice.pnl.price_is_stale is True
    assert "STALE" in advice.pnl.summary()


def test_stale_reason_says_what_to_do(position, calm_bars, engine_cfg):
    now = calm_bars[-1].close_time
    advice, _ = advise(
        position, calm_bars, now=now, last_tick_at=now - timedelta(seconds=90),
        engine_cfg=engine_cfg,
    )
    assert "SUPPRESSED" in advice.reason
    assert "broker" in advice.reason.lower()


def test_no_completed_bars_is_not_treated_as_a_signal(position, engine_cfg):
    bars = flat_series(1000, 3, start=ist(9, 15))
    advice, _ = advise(position, bars, now=ist(9, 15, 30), engine_cfg=engine_cfg)
    assert advice.state is PositionState.STALE_DATA
    assert advice.last_price is None


# ================================================================================
# 4. TIME_EXIT BOUNDARY CONDITIONS
# ================================================================================


def series_until(now, *, price=1000.0, final_close=None, spread=10.0, volume=1000, start=None):
    """A 1-minute series whose LAST bar closes exactly at ``now``.

    TIME_EXIT tests decide 90+ minutes after entry, so the bars have to actually run
    up to that moment. A short warm-up series plus a distant ``now`` is a stale feed,
    not a decayed position -- the engine is right to say so, and the test would be
    measuring the wrong thing.

    ``spread=10`` gives ATR 20, so the 0.5x ATR drift band is +/-10 and the P&L band
    can be tested without the drift band interfering.
    """
    start = start or ist(9, 15)
    n = int((now - start).total_seconds() // 60)
    closes = [price] * n
    if final_close is not None and n:
        closes[-1] = final_close
    return make_series(closes, start=start, volumes=[volume] * n, spread=spread)


def test_time_exit_fires_at_exactly_90_minutes(position, engine_cfg):
    now = position.entry_time + timedelta(minutes=90)
    advice, _ = advise(position, series_until(now), now=now, engine_cfg=engine_cfg)
    assert advice.state is PositionState.TIME_EXIT


def test_time_exit_does_not_fire_one_minute_early(position, engine_cfg):
    now = position.entry_time + timedelta(minutes=89)
    advice, _ = advise(position, series_until(now), now=now, engine_cfg=engine_cfg)
    assert advice.state is not PositionState.TIME_EXIT


def test_time_exit_pnl_band_upper_edge_is_inclusive(position, engine_cfg):
    """+0.30% on a 1000 entry is exactly 1003, and 0.30 is inside the band."""
    now = position.entry_time + timedelta(minutes=95)
    bars = series_until(now, final_close=1003)
    advice, _ = advise(position, bars, now=now, engine_cfg=engine_cfg)
    assert advice.pnl.gross_pct == Decimal("0.30")
    assert advice.state is PositionState.TIME_EXIT


def test_time_exit_does_not_fire_just_outside_the_pnl_band(position, engine_cfg):
    now = position.entry_time + timedelta(minutes=95)
    bars = series_until(now, final_close=1004)
    advice, _ = advise(position, bars, now=now, engine_cfg=engine_cfg)
    assert advice.pnl.gross_pct > Decimal("0.30")
    assert advice.state is not PositionState.TIME_EXIT


def test_time_exit_band_is_symmetric_for_losses(position):
    """-0.30% is inside the band too.

    The momentum rule is disabled here (rsi_reversal_level=0) so it cannot pre-empt
    TIME_EXIT: a single down-close on an otherwise flat series drives RSI to 0, which
    is EXIT_NOW by the higher-precedence momentum rule. This test is about the band.
    """
    cfg = ExitEngineSettings(rsi_reversal_level=Decimal("0"))
    now = ist(9, 20) + timedelta(minutes=95)
    pos = Position(
        position_id="p-loss", symbol="RELIANCE", quantity=100,
        entry_price=Decimal("1000.00"), entry_time=ist(9, 20),
    )
    bars = series_until(now, final_close=997)
    advice, _ = advise(pos, bars, now=now, engine_cfg=cfg)
    assert advice.pnl.gross_pct == Decimal("-0.30")
    assert advice.state is PositionState.TIME_EXIT


def test_time_exit_requires_price_near_entry_not_just_flat_pnl(position, engine_cfg):
    """Tiny ATR: P&L is inside the band but the price has moved far in ATR terms."""
    now = position.entry_time + timedelta(minutes=95)
    bars = series_until(now, final_close=1000.5, spread=0.1)
    advice, _ = advise(position, bars, now=now, engine_cfg=engine_cfg)
    atr = advice.indicators.atr14
    assert abs(advice.pnl.gross_pct) <= engine_cfg.time_exit_pnl_pct_band, "premise: in band"
    assert abs(1000.5 - 1000) > 0.5 * atr, "premise: outside the ATR drift band"
    assert advice.state is not PositionState.TIME_EXIT


def test_time_exit_uses_gross_pnl_for_the_band(position, engine_cfg):
    """Costs would push net outside the band and fire this rule early. It uses gross."""
    from config.settings import CostSettings

    costly = CostSettings(slippage_bps_per_leg=Decimal("30"))  # 0.6% round trip
    now = position.entry_time + timedelta(minutes=95)
    advice, _ = advise(
        position, series_until(now), now=now, engine_cfg=engine_cfg, cost_cfg=costly
    )
    assert advice.pnl.gross_pct == Decimal("0.00")
    assert advice.pnl.net_pct < -engine_cfg.time_exit_pnl_pct_band, "premise: net outside band"
    assert advice.state is PositionState.TIME_EXIT


def test_time_exit_threshold_is_configurable(position):
    cfg = ExitEngineSettings(time_exit_minutes=30)
    now = position.entry_time + timedelta(minutes=35)
    advice, _ = advise(position, series_until(now), now=now, engine_cfg=cfg)
    assert advice.state is PositionState.TIME_EXIT


def test_time_exit_cannot_fire_without_atr(position, engine_cfg):
    """Under-warmed history: the ATR band is unknown, so the rule is skipped."""
    now = position.entry_time + timedelta(minutes=120)
    bars = series_until(now, start=now - timedelta(minutes=10), spread=1.0)
    advice, _ = advise(position, bars, now=now, engine_cfg=engine_cfg)
    assert advice.indicators.atr14 is None
    assert advice.state is not PositionState.TIME_EXIT


def test_time_exit_reason_explains_all_three_conditions(position, engine_cfg):
    now = position.entry_time + timedelta(minutes=95)
    advice, _ = advise(position, series_until(now), now=now, engine_cfg=engine_cfg)
    for token in ("min", "%", "ATR", "decayed"):
        assert token in advice.reason


# ================================================================================
# EXIT_NOW conditions
# ================================================================================


def test_exit_now_on_trailing_stop_breach(position, calm_bars, engine_cfg):
    bars = append(calm_bars, 1080)
    advice, rt = advise(position, bars, engine_cfg=engine_cfg)
    stop = rt.current_stop

    bars = append(bars, float(stop) - 5)
    advice = evaluate(
        position, rt, bars, bars[-1].close_time, bars[-1].close_time, engine_cfg=engine_cfg
    )
    assert advice.state is PositionState.EXIT_NOW
    assert "Stop hit" in advice.reason
    assert advice.suggested_exit_quantity == rt.remaining_quantity


def test_exit_now_on_explicit_stop_even_without_indicator_warmup(engine_cfg):
    """An explicit stop needs only a price, so it works from the first bar."""
    pos = Position(
        position_id="p-early",
        symbol="TCS",
        quantity=50,
        entry_price=Decimal("1000.00"),
        entry_time=ist(9, 15),
        stop_loss=Decimal("995.00"),
    )
    bars = make_series([1000, 999, 994], start=ist(9, 15))
    advice, _ = advise(pos, bars, engine_cfg=engine_cfg)
    assert advice.state is PositionState.EXIT_NOW
    assert advice.indicators.atr14 is None
    assert "Warming up" in advice.reason


def test_exit_now_on_vwap_loss_with_above_average_volume(position, calm_bars, engine_cfg):
    bars = append(calm_bars, 995, volume=5000)
    advice, _ = advise(position, bars, engine_cfg=engine_cfg)
    assert advice.state is PositionState.EXIT_NOW
    assert "VWAP lost" in advice.reason


def snapshot(**kw) -> IndicatorSnapshot:
    """A crafted snapshot, for testing one rule's predicate in isolation."""
    base = dict(
        asof=ist(10, 0), last_price=1000.0, bars_used=30, vwap=1000.0, atr14=2.0,
        ema9=1000.0, ema21=1000.0, rsi14=55.0, volume=1000, avg_volume20=1000.0,
        volume_ratio=1.0,
    )
    return IndicatorSnapshot(**{**base, **kw})


def test_vwap_loss_needs_above_average_volume(position, engine_cfg):
    """Below VWAP on ORDINARY volume is drift, not distribution -- no exit."""
    rt = PositionRuntime.initial(position)
    snap = snapshot(last_price=995.0, vwap=1000.0, volume_ratio=1.0)
    assert _exit_now_reason(position, rt, snap, engine_cfg) is None


def test_vwap_loss_fires_once_volume_is_above_average(position, engine_cfg):
    rt = PositionRuntime.initial(position)
    snap = snapshot(last_price=995.0, vwap=1000.0, volume_ratio=1.5)
    hit = _exit_now_reason(position, rt, snap, engine_cfg)
    assert hit is not None
    rule, reason = hit
    assert rule is ExitRule.VWAP_LOSS
    assert "VWAP lost" in reason


def test_vwap_rule_is_skipped_when_volume_ratio_is_unknown(position, engine_cfg):
    """A missing indicator must read as 'unknown', never as a passing condition."""
    rt = PositionRuntime.initial(position)
    snap = snapshot(last_price=995.0, vwap=1000.0, volume_ratio=None)
    assert _exit_now_reason(position, rt, snap, engine_cfg) is None


def test_momentum_rule_is_skipped_when_rsi_is_unknown(position, engine_cfg):
    rt = PositionRuntime.initial(position)
    snap = snapshot(ema9=990.0, ema21=1000.0, rsi14=None)
    assert _exit_now_reason(position, rt, snap, engine_cfg) is None


def test_momentum_needs_both_ema_cross_and_weak_rsi(position, engine_cfg):
    rt = PositionRuntime.initial(position)
    assert _exit_now_reason(position, rt, snapshot(ema9=990.0, ema21=1000.0, rsi14=60.0),
                            engine_cfg) is None
    assert _exit_now_reason(position, rt, snapshot(ema9=1010.0, ema21=1000.0, rsi14=40.0),
                            engine_cfg) is None
    assert _exit_now_reason(position, rt, snapshot(ema9=990.0, ema21=1000.0, rsi14=40.0),
                            engine_cfg) is not None


def test_hold_flags_a_below_vwap_drift_as_a_caution(position, calm_bars):
    """Momentum rule off, so the HOLD branch is reachable and its caution visible."""
    cfg = ExitEngineSettings(rsi_reversal_level=Decimal("0"))
    bars = append(calm_bars, 999.5, volume=1000)
    advice, _ = advise(position, bars, engine_cfg=cfg)
    assert advice.state is PositionState.HOLD
    assert "below VWAP" in advice.reason
    assert "watch it" in advice.reason


def test_exit_now_on_momentum_reversal(position, engine_cfg):
    bars = make_series([1000 - i for i in range(WARMUP)], start=ist(9, 15))
    advice, _ = advise(position, bars, engine_cfg=engine_cfg)
    assert advice.state is PositionState.EXIT_NOW
    assert "Momentum reversal" in advice.reason


def test_stop_breach_is_reported_before_vwap_loss(position, calm_bars, engine_cfg):
    """Both conditions true; the stop is the more specific and is reported."""
    bars = append(calm_bars, 1080)
    _, rt = advise(position, bars, engine_cfg=engine_cfg)
    bars = append(bars, 950, volume=50_000)
    advice = evaluate(
        position, rt, bars, bars[-1].close_time, bars[-1].close_time, engine_cfg=engine_cfg
    )
    assert advice.state is PositionState.EXIT_NOW
    assert "Stop hit" in advice.reason


# ================================================================================
# PARTIAL_EXIT
# ================================================================================


def test_partial_exit_at_target_one(position, calm_bars, engine_cfg):
    bars = append(calm_bars, 1080)
    advice, rt = advise(position, bars, engine_cfg=engine_cfg)
    assert advice.state is PositionState.PARTIAL_EXIT
    assert advice.suggested_exit_quantity == 50
    assert rt.partial_advised is True


def test_partial_exit_fires_only_once(position, calm_bars, engine_cfg):
    bars = append(calm_bars, 1080)
    first, rt = advise(position, bars, engine_cfg=engine_cfg)
    assert first.state is PositionState.PARTIAL_EXIT

    bars = append(bars, 1081)
    second = evaluate(
        position, rt, bars, bars[-1].close_time, bars[-1].close_time, engine_cfg=engine_cfg
    )
    assert second.state is PositionState.TRAIL


def test_explicit_target_takes_precedence_over_the_atr_default(calm_bars, engine_cfg):
    pos = Position(
        position_id="p-t1",
        symbol="HDFCBANK",
        quantity=100,
        entry_price=Decimal("1000.00"),
        entry_time=ist(9, 20),
        target=Decimal("1005.00"),
    )
    bars = append(calm_bars, 1006)
    advice, _ = advise(pos, bars, engine_cfg=engine_cfg)
    assert advice.target1 == Decimal("1005.00")
    assert advice.state is PositionState.PARTIAL_EXIT
    assert "your recorded target" in advice.reason


def test_target_one_defaults_to_one_and_a_half_atr(position, engine_cfg):
    assert target_one(position, 8.0, engine_cfg) == Decimal("1012.00")


def test_target_one_is_none_without_a_target_or_atr(position, engine_cfg):
    assert target_one(position, None, engine_cfg) is None


def test_partial_exit_skipped_when_the_position_cannot_be_split(calm_bars, engine_cfg):
    pos = Position(
        position_id="p-one",
        symbol="WIPRO",
        quantity=1,
        entry_price=Decimal("1000.00"),
        entry_time=ist(9, 20),
    )
    bars = append(calm_bars, 1080)
    advice, _ = advise(pos, bars, engine_cfg=engine_cfg)
    assert advice.state is PositionState.TRAIL
    assert advice.suggested_exit_quantity == 0


def test_partial_fraction_is_configurable(position, calm_bars):
    cfg = ExitEngineSettings(partial_exit_fraction=Decimal("0.25"))
    bars = append(calm_bars, 1080)
    advice, _ = advise(position, bars, engine_cfg=cfg)
    assert advice.suggested_exit_quantity == 25


# ================================================================================
# TRAIL and HOLD
# ================================================================================


def test_trail_between_breakeven_and_target_one(position, calm_bars, engine_cfg):
    """Profit past 1x ATR but short of 1.5x ATR is the TRAIL window."""
    bars = append(calm_bars, 1002.5)
    advice, rt = advise(position, bars, engine_cfg=engine_cfg)
    atr = advice.indicators.atr14
    assert 1002.5 >= 1000 + atr, "test premise: breakeven armed"
    assert 1002.5 < float(advice.target1), "test premise: below T1"
    assert advice.state is PositionState.TRAIL
    assert rt.breakeven_armed is True


def test_trail_after_a_partial_has_been_advised(position, calm_bars, engine_cfg):
    rt = PositionRuntime.initial(position)
    rt.partial_advised = True
    rt.remaining_quantity = 50
    bars = append(calm_bars, 1080)
    advice, _ = advise(position, bars, runtime=rt, engine_cfg=engine_cfg)
    assert advice.state is PositionState.TRAIL


def test_hold_when_nothing_has_happened(position, calm_bars, engine_cfg):
    advice, _ = advise(position, calm_bars, engine_cfg=engine_cfg)
    assert advice.state is PositionState.HOLD
    assert advice.suggested_exit_quantity == 0


def test_hold_reason_quantifies_the_distance_to_the_breakeven_trigger(
    position, calm_bars, engine_cfg
):
    advice, _ = advise(position, calm_bars, engine_cfg=engine_cfg)
    assert "breakeven trigger" in advice.reason


# ================================================================================
# Cross-cutting invariants
# ================================================================================


def test_exactly_one_state_and_it_is_a_known_one(position, calm_bars, engine_cfg):
    advice, _ = advise(position, calm_bars, engine_cfg=engine_cfg)
    assert advice.state in PRECEDENCE


def test_precedence_covers_every_state():
    assert set(PRECEDENCE) == set(PositionState)
    assert len(PRECEDENCE) == len(PositionState)


@pytest.mark.parametrize(
    "closes,volume",
    [([1000], 1000), ([1080], 1000), ([950], 50_000), ([1002.5], 1000)],
    ids=["hold", "partial", "vwap-loss", "trail"],
)
def test_every_advice_reports_gross_and_net(position, calm_bars, engine_cfg, closes, volume):
    bars = calm_bars
    for c in closes:
        bars = append(bars, c, volume=volume)
    advice, _ = advise(position, bars, engine_cfg=engine_cfg)
    assert advice.pnl is not None
    assert advice.pnl.gross_pnl is not None
    assert advice.pnl.net_pnl < advice.pnl.gross_pnl
    assert "gross" in advice.pnl.summary() and "net" in advice.pnl.summary()


def test_pnl_is_computed_on_the_remaining_quantity(position, calm_bars, engine_cfg):
    rt = PositionRuntime.initial(position)
    rt.remaining_quantity = 40
    rt.partial_advised = True
    advice, _ = advise(position, calm_bars, runtime=rt, engine_cfg=engine_cfg)
    assert advice.pnl.quantity == 40


def test_every_state_carries_a_nonempty_reason(position, calm_bars, engine_cfg):
    scenarios = [
        (calm_bars, calm_bars[-1].close_time),
        (append(calm_bars, 1080), None),
        (calm_bars, square_off_now()),
    ]
    for bars, now in scenarios:
        advice, _ = advise(position, bars, now=now, engine_cfg=engine_cfg)
        assert advice.reason.strip()
        assert len(advice.reason) > 20, "a reason must explain, not label"


def test_runtime_records_the_state_it_emitted(position, calm_bars, engine_cfg):
    advice, rt = advise(position, calm_bars, engine_cfg=engine_cfg)
    assert rt.last_state is advice.state
    assert rt.last_evaluated_at == advice.asof


def test_forming_bar_is_never_used(position, calm_bars, engine_cfg):
    """A spike inside the still-forming bar must not influence the decision."""
    spike = append(calm_bars, 1500, spread=1.0)
    mid_bar = spike[-1].ts + timedelta(seconds=30)
    advice, _ = advise(
        position, spike, now=mid_bar, last_tick_at=mid_bar, engine_cfg=engine_cfg
    )
    assert advice.last_price == pytest.approx(1000.0)
    assert advice.state is PositionState.HOLD


def test_evaluation_is_deterministic(position, calm_bars, engine_cfg):
    a, _ = advise(position, calm_bars, engine_cfg=engine_cfg)
    b, _ = advise(position, calm_bars, engine_cfg=engine_cfg)
    assert (a.state, a.reason, a.last_price) == (b.state, b.reason, b.last_price)


def test_engine_never_reads_the_clock(position, calm_bars, engine_cfg, monkeypatch):
    """Injected time only. A clock read would break replay reproducibility."""
    import src.market.calendar as cal

    def boom():
        raise AssertionError("exit engine must not call now_ist()")

    monkeypatch.setattr(cal, "now_ist", boom)
    advise(position, calm_bars, engine_cfg=engine_cfg)


def test_render_includes_state_reason_and_pnl(position, calm_bars, engine_cfg):
    advice, _ = advise(position, calm_bars, engine_cfg=engine_cfg)
    text = advice.render()
    assert advice.state.value in text
    assert "reason" in text
    assert "gross" in text and "net" in text


def test_short_positions_are_rejected_rather_than_evaluated_with_long_rules():
    with pytest.raises(Exception):
        Position(
            position_id="p-short",
            symbol="SBIN",
            quantity=10,
            entry_price=Decimal("500"),
            entry_time=ist(9, 20),
            side="SHORT",  # type: ignore[arg-type]
        )
