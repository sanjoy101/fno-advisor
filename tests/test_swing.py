"""Tests for the multi-day swing arm.

Concentrated on the three things this horizon introduces that can silently flatter a
result: gap-through-stop fills, delivery costs, and overlapping-window sample size.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from config.settings import CostSettings
from src.costs import delivery_round_trip_costs, round_trip_costs
from src.market.calendar import IST
from src.positions.models import Bar
from src.signals import daily as D
from src.signals.swing import (
    HORIZONS,
    effective_n,
    mde_from,
    measure_swing_excursions,
    panel_return_pct,
    simulate_swing,
)

DAYS = [date(2026, 1, 5) + timedelta(days=i) for i in range(40)]


def bar(day, o, h, l, c, v=1000):
    return Bar(ts=datetime.combine(day, D.DECISION_TIME, tzinfo=IST),
               open=o, high=h, low=l, close=c, volume=v)


def flat_series(n=20, price=1000.0):
    return [bar(DAYS[i], price, price + 2, price - 2, price) for i in range(n)]


# --------------------------------------------------------------------------------
# Gap-through-stop: the headline risk of this horizon
# --------------------------------------------------------------------------------


def test_a_gap_below_the_stop_fills_at_the_open_not_the_stop():
    """A stop cannot execute overnight. Assuming it can invents money."""
    series = flat_series(3)
    series.append(bar(DAYS[3], 940.0, 945.0, 935.0, 942.0))  # opens far below
    trade = simulate_swing(series, 2, horizon=3, stop=980.0, target=1030.0)
    assert trade.exit_reason == "STOP_GAP"
    assert trade.exit_price == pytest.approx(940.0), "must fill at the open"
    assert trade.exit_price < trade.stop


def test_the_gap_shortfall_is_recorded():
    series = flat_series(3)
    series.append(bar(DAYS[3], 940.0, 945.0, 935.0, 942.0))
    trade = simulate_swing(series, 2, horizon=3, stop=980.0, target=1030.0)
    assert trade.gap_through_rupees == pytest.approx(40.0)
    assert trade.gapped_through is True


def test_an_intraday_touch_of_the_stop_fills_at_the_stop():
    series = flat_series(3)
    series.append(bar(DAYS[3], 1000.0, 1005.0, 970.0, 995.0))  # opens fine, dips
    trade = simulate_swing(series, 2, horizon=3, stop=980.0, target=1030.0)
    assert trade.exit_reason == "STOP"
    assert trade.exit_price == pytest.approx(980.0)
    assert trade.gap_through_rupees == 0.0


def test_a_favourable_gap_fills_at_the_open_too():
    series = flat_series(3)
    series.append(bar(DAYS[3], 1060.0, 1070.0, 1055.0, 1065.0))
    trade = simulate_swing(series, 2, horizon=3, stop=980.0, target=1030.0)
    assert trade.exit_reason == "TARGET_GAP"
    assert trade.exit_price == pytest.approx(1060.0)


def test_a_day_touching_both_resolves_as_the_loss():
    """Within one daily bar the order is unknowable, so assume the worse."""
    series = flat_series(3)
    series.append(bar(DAYS[3], 1000.0, 1040.0, 970.0, 1010.0))
    trade = simulate_swing(series, 2, horizon=3, stop=980.0, target=1030.0)
    assert trade.exit_reason == "STOP"


def test_an_untouched_position_exits_at_the_horizon():
    series = flat_series(10)
    trade = simulate_swing(series, 2, horizon=3, stop=900.0, target=1100.0)
    assert trade.exit_reason == "TIME"
    assert trade.days_held == 3
    assert trade.exit_price == pytest.approx(series[5].close)


def test_days_held_matches_the_exit_day():
    series = flat_series(3)
    series.append(bar(DAYS[3], 1000.0, 1005.0, 995.0, 1000.0))
    series.append(bar(DAYS[4], 1000.0, 1005.0, 970.0, 990.0))
    trade = simulate_swing(series, 2, horizon=5, stop=980.0, target=1100.0)
    assert trade.days_held == 2
    assert trade.exit_date == DAYS[4]


def test_horizon_bounds_the_hold():
    series = flat_series(20)
    for h in HORIZONS:
        trade = simulate_swing(series, 2, horizon=h, stop=1.0, target=1e9)
        assert trade.days_held <= h


def test_no_trade_when_there_is_no_forward_data():
    series = flat_series(5)
    assert simulate_swing(series, len(series) - 1, horizon=3, stop=1.0, target=2.0) is None


def test_excursions_are_recorded_over_the_whole_hold():
    series = flat_series(3)
    series.append(bar(DAYS[3], 1000.0, 1050.0, 960.0, 1000.0))
    trade = simulate_swing(series, 2, horizon=3, stop=1.0, target=1e9)
    assert trade.mfe_pct == pytest.approx(5.0, abs=0.1)
    assert trade.mae_pct == pytest.approx(4.0, abs=0.1)


# --------------------------------------------------------------------------------
# Delivery costs -- zero brokerage is not zero cost
# --------------------------------------------------------------------------------


def test_delivery_costs_more_than_intraday_despite_zero_brokerage():
    """STT is 0.1% on BOTH delivery legs against 0.025% sell-only intraday."""
    cfg = CostSettings()
    intraday = round_trip_costs(1000, 1000, 100, cfg)
    delivery = delivery_round_trip_costs(1000, 1000, 100, cfg)
    assert delivery.total > intraday.total
    assert delivery.buy.brokerage == 0


def test_delivery_charges_stt_on_both_legs():
    cfg = CostSettings()
    d = delivery_round_trip_costs(1000, 1000, 100, cfg)
    assert d.buy.stt > 0 and d.sell.stt > 0
    assert d.buy.stt == d.sell.stt


def test_intraday_charges_stt_on_the_sell_only():
    cfg = CostSettings()
    i = round_trip_costs(1000, 1000, 100, cfg)
    assert i.buy.stt == 0 and i.sell.stt > 0


def test_the_dp_charge_is_flat_and_hurts_small_positions_most():
    cfg = CostSettings()
    small = delivery_round_trip_costs(1000, 1000, 10, cfg)
    large = delivery_round_trip_costs(1000, 1000, 1000, cfg)
    small_pct = float(small.total) / (1000 * 10)
    large_pct = float(large.total) / (1000 * 1000)
    assert small_pct > large_pct


def test_delivery_pnl_reports_gross_and_net():
    from src.costs import delivery_pnl

    pnl = delivery_pnl(1000, 1050, 100, CostSettings())
    assert pnl.gross_pnl == Decimal("5000.00")
    assert pnl.net_pnl < pnl.gross_pnl


# --------------------------------------------------------------------------------
# Calibration on the NEW horizon
# --------------------------------------------------------------------------------


def test_a_winner_is_defined_by_profit_not_by_a_target():
    """Defining winners by a target would be circular: the target comes FROM this."""
    series = [bar(DAYS[i], 1000 + i, 1004 + i, 996 + i, 1000 + i) for i in range(40)]
    out = measure_swing_excursions("X", series, 3)
    assert out
    assert all(e.profitable for e in out), "a steadily rising series is all winners"


def test_a_falling_series_produces_no_winners():
    series = [bar(DAYS[i], 1000 - i, 1004 - i, 996 - i, 1000 - i) for i in range(40)]
    out = measure_swing_excursions("X", series, 3)
    assert out
    assert not any(e.profitable for e in out)


def test_excursions_cover_the_whole_horizon():
    """MAE is the worst point over the hold, not up to some target touch."""
    series = flat_series(30)
    series.append(bar(DAYS[30], 1000, 1005, 900, 1002))
    out = measure_swing_excursions("X", series, 3, min_history=25)
    deep = [e for e in out if e.mae_atr > 10]
    assert deep, "a 10% intraday collapse must show up as a large adverse excursion"


def test_excursions_are_in_atr_units():
    series = [bar(DAYS[i], 1000, 1010, 990, 1000) for i in range(40)]
    out = measure_swing_excursions("X", series, 3)
    assert out
    assert all(e.atr > 0 for e in out)


def test_excursions_need_history_before_they_start():
    series = flat_series(20)
    out = measure_swing_excursions("X", series, 3, min_history=25)
    assert out == []


# --------------------------------------------------------------------------------
# Panel baseline
# --------------------------------------------------------------------------------


def test_panel_return_is_equal_weight():
    a = {DAYS[0]: bar(DAYS[0], 100, 100, 100, 100), DAYS[3]: bar(DAYS[3], 110, 110, 110, 110)}
    b = {DAYS[0]: bar(DAYS[0], 100, 100, 100, 100), DAYS[3]: bar(DAYS[3], 90, 90, 90, 90)}
    assert panel_return_pct({"A": a, "B": b}, DAYS[0], DAYS[3]) == pytest.approx(0.0)


def test_panel_return_skips_symbols_missing_either_date():
    a = {DAYS[0]: bar(DAYS[0], 100, 100, 100, 100), DAYS[3]: bar(DAYS[3], 110, 110, 110, 110)}
    b = {DAYS[0]: bar(DAYS[0], 100, 100, 100, 100)}
    assert panel_return_pct({"A": a, "B": b}, DAYS[0], DAYS[3]) == pytest.approx(10.0)


def test_panel_return_of_a_same_day_exit_is_zero():
    a = {DAYS[0]: bar(DAYS[0], 100, 100, 100, 100)}
    assert panel_return_pct({"A": a}, DAYS[0], DAYS[0]) == 0.0


def test_panel_return_is_none_with_no_usable_symbols():
    assert panel_return_pct({}, DAYS[0], DAYS[3]) is None


# --------------------------------------------------------------------------------
# Effective sample size -- overlapping windows are not independent draws
# --------------------------------------------------------------------------------


def test_effective_n_divides_by_the_horizon():
    dates = [DAYS[i] for i in range(20)]
    assert effective_n(dates, 5)["effective_n"] == 4
    assert effective_n(dates, 1)["effective_n"] == 20


def test_effective_n_counts_dates_not_trades():
    """Two trades opened the same day are one draw on that day, not two."""
    dates = [DAYS[0], DAYS[0], DAYS[1], DAYS[1]]
    out = effective_n(dates, 1)
    assert out["trades"] == 4
    assert out["distinct_entry_dates"] == 2
    assert out["effective_n"] == 2


def test_effective_n_reports_the_overstatement():
    dates = [DAYS[i // 2] for i in range(40)]
    out = effective_n(dates, 5)
    assert out["overstatement_factor"] > 1


def test_mde_uses_the_effective_n_not_the_trade_count():
    """Feeding the raw count would understate the detectable effect."""
    sd = 3.0
    assert mde_from(50, sd) > mde_from(600, sd)


def test_mde_is_infinite_for_a_degenerate_sample():
    assert mde_from(1, 1.0) == float("inf")


def test_horizons_are_the_three_arms():
    assert HORIZONS == (1, 3, 5)
