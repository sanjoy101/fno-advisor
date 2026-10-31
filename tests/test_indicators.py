"""Tests for the exit-engine indicators.

Each indicator gets a hand-computable known value (small period, arithmetic you can
verify on paper) plus its insufficient-history behaviour. The look-ahead guard in
completed_bars() is tested here too, because every indicator depends on it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.positions import indicators as ind
from src.positions.models import BAR_INTERVAL, Bar, completed_bars
from tests.conftest import TRADE_DATE, flat_series, ist, make_bar, make_series


# --------------------------------------------------------------------------------
# completed_bars -- the look-ahead guard every indicator relies on
# --------------------------------------------------------------------------------


def test_forming_bar_is_excluded():
    """At 09:17:30 the 09:17 bar is still forming; only its open is known."""
    bars = make_series([100, 101, 102], start=ist(9, 15))  # 09:15, 09:16, 09:17
    done = completed_bars(bars, ist(9, 17, 30))
    assert [b.ts.minute for b in done] == [15, 16]


def test_bar_becomes_available_exactly_at_its_close_time():
    bars = make_series([100, 101], start=ist(9, 15))
    assert len(completed_bars(bars, ist(9, 15, 59))) == 0
    assert len(completed_bars(bars, ist(9, 16, 0))) == 1


def test_completed_bars_rejects_naive_asof():
    from src.market.calendar import NaiveDatetimeError
    from datetime import datetime

    with pytest.raises(NaiveDatetimeError):
        completed_bars(make_series([100]), datetime(2026, 8, 20, 9, 20))


def test_bar_close_time_is_open_plus_interval():
    bar = make_bar(ist(9, 15), 100)
    assert bar.close_time == ist(9, 15) + BAR_INTERVAL == ist(9, 16)


# --------------------------------------------------------------------------------
# VWAP
# --------------------------------------------------------------------------------


def test_vwap_is_volume_weighted_typical_price():
    """tp 10 on 100 shares, tp 20 on 300 -> (1000 + 6000) / 400 = 17.5."""
    bars = [
        make_bar(ist(9, 15), 10, high=11, low=9, volume=100),
        make_bar(ist(9, 16), 20, high=22, low=18, volume=300),
    ]
    assert ind.session_vwap(bars) == pytest.approx(17.5)


def test_vwap_is_session_anchored_not_rolling():
    """Yesterday's bars must not contaminate today's average."""
    yesterday = [
        make_bar(ist(15, 25, d=date(2026, 8, 19)), 500, high=500, low=500, volume=10_000)
    ]
    today = [make_bar(ist(9, 15), 100, high=100, low=100, volume=100)]
    assert ind.session_vwap(yesterday + today) == pytest.approx(100.0)


def test_vwap_anchor_can_be_given_explicitly():
    bars = [make_bar(ist(9, 15), 100, high=100, low=100, volume=100)]
    assert ind.session_vwap(bars, session_date=date(2026, 8, 19)) is None


def test_vwap_is_cumulative_not_windowed():
    """Adding a bar moves VWAP toward it but never discards earlier bars."""
    bars = [make_bar(ist(9, 15) + timedelta(minutes=i), 100, high=100, low=100, volume=100)
            for i in range(30)]
    bars.append(make_bar(ist(9, 45), 200, high=200, low=200, volume=100))
    # A 20-bar rolling VWAP would be pulled far higher than this.
    assert ind.session_vwap(bars) == pytest.approx((100 * 30 * 100 + 200 * 100) / 3100)


def test_vwap_none_on_empty_and_on_zero_volume():
    assert ind.session_vwap([]) is None
    zero = [make_bar(ist(9, 15), 100, high=100, low=100, volume=0)]
    assert ind.session_vwap(zero) is None


# --------------------------------------------------------------------------------
# ATR
# --------------------------------------------------------------------------------


def test_true_range_accounts_for_gaps():
    """A bar that gaps up has TR larger than its own high-low range."""
    bars = [
        make_bar(ist(9, 15), 100, high=101, low=99),
        make_bar(ist(9, 16), 110, high=111, low=109),
    ]
    # high-low = 2, but |high - prev_close| = 11
    assert ind.true_ranges(bars) == [pytest.approx(11.0)]


def test_true_ranges_has_one_fewer_value_than_bars():
    assert len(ind.true_ranges(make_series([1, 2, 3, 4]))) == 3


def test_atr_known_value_wilder_smoothing():
    """period=2. TRs are 2, 2, 4 -> seed (2+2)/2 = 2.0 -> (2.0*1 + 4)/2 = 3.0."""
    bars = [
        make_bar(ist(9, 15), 9, high=10, low=8),
        make_bar(ist(9, 16), 10, high=11, low=9),
        make_bar(ist(9, 17), 11, high=12, low=10),
        make_bar(ist(9, 18), 14, high=15, low=11),
    ]
    assert ind.atr(bars, period=2) == pytest.approx(3.0)


def test_atr_converges_to_constant_range():
    """Every bar spans 2.0, so ATR is 2.0 regardless of smoothing."""
    assert ind.atr(flat_series(100, 30, spread=1.0), period=14) == pytest.approx(2.0)


def test_atr_needs_period_plus_one_bars():
    assert ind.atr(flat_series(100, 14), period=14) is None
    assert ind.atr(flat_series(100, 15), period=14) is not None


def test_atr_rejects_bad_period():
    with pytest.raises(ValueError):
        ind.atr(flat_series(100, 20), period=0)


# --------------------------------------------------------------------------------
# EMA
# --------------------------------------------------------------------------------


def test_ema_known_value():
    """period=3, closes 1,2,3,4. seed = 2.0, k = 0.5 -> 4*0.5 + 2.0*0.5 = 3.0."""
    assert ind.ema(make_series([1, 2, 3, 4]), period=3) == pytest.approx(3.0)


def test_ema_of_constant_series_is_that_constant():
    assert ind.ema(flat_series(250, 30), period=9) == pytest.approx(250.0)


def test_fast_ema_leads_slow_ema_in_an_uptrend():
    bars = make_series([100 + i for i in range(40)])
    assert ind.ema(bars, 9) > ind.ema(bars, 21)


def test_fast_ema_trails_slow_ema_in_a_downtrend():
    bars = make_series([200 - i for i in range(40)])
    assert ind.ema(bars, 9) < ind.ema(bars, 21)


def test_ema_needs_period_bars():
    assert ind.ema(flat_series(100, 8), period=9) is None
    assert ind.ema(flat_series(100, 9), period=9) is not None


# --------------------------------------------------------------------------------
# RSI
# --------------------------------------------------------------------------------


def test_rsi_known_value():
    """period=2, closes 10,11,12,11.

    gains 1,1,0  losses 0,0,1
    seed: avg_gain 1.0, avg_loss 0.0
    step: avg_gain (1.0*1+0)/2 = 0.5, avg_loss (0.0*1+1)/2 = 0.5 -> RS 1 -> RSI 50.
    """
    assert ind.rsi(make_series([10, 11, 12, 11]), period=2) == pytest.approx(50.0)


def test_rsi_is_100_when_every_close_is_up():
    assert ind.rsi(make_series([100 + i for i in range(20)])) == pytest.approx(100.0)


def test_rsi_is_0_when_every_close_is_down():
    assert ind.rsi(make_series([200 - i for i in range(20)])) == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_neutral_not_a_divide_by_zero():
    """No gains and no losses: 50, not a crash and not a misleading 100."""
    assert ind.rsi(flat_series(100, 20)) == pytest.approx(50.0)


def test_rsi_needs_period_plus_one_bars():
    assert ind.rsi(flat_series(100, 14), period=14) is None
    assert ind.rsi(flat_series(100, 15), period=14) is not None


def test_rsi_stays_within_bounds():
    bars = make_series([100, 105, 98, 110, 95, 120, 90, 130, 85, 140,
                        88, 132, 91, 125, 99, 118, 103, 111, 107, 109])
    value = ind.rsi(bars)
    assert 0.0 <= value <= 100.0


# --------------------------------------------------------------------------------
# Volume
# --------------------------------------------------------------------------------


def test_average_volume_excludes_the_current_bar():
    """20 bars at 10, then 1000. The baseline must stay 10, not be dragged upward."""
    bars = make_series([100] * 21, volumes=[10] * 20 + [1000])
    assert ind.average_volume(bars, period=20) == pytest.approx(10.0)


def test_volume_ratio_known_value():
    bars = make_series([100] * 21, volumes=[10] * 20 + [30])
    assert ind.volume_ratio(bars, period=20) == pytest.approx(3.0)


def test_volume_ratio_is_one_for_steady_volume():
    bars = make_series([100] * 21, volumes=[500] * 21)
    assert ind.volume_ratio(bars, period=20) == pytest.approx(1.0)


def test_volume_ratio_needs_period_plus_one_bars():
    assert ind.volume_ratio(make_series([100] * 20), period=20) is None
    assert ind.volume_ratio(make_series([100] * 21), period=20) is not None


def test_volume_ratio_none_rather_than_infinite_on_a_dead_baseline():
    bars = make_series([100] * 21, volumes=[0] * 20 + [500])
    assert ind.volume_ratio(bars, period=20) is None


# --------------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------------


def test_snapshot_asof_is_the_bar_close_not_the_bar_open():
    """The moment the numbers existed, not the moment the bar started."""
    bars = flat_series(100, 30, start=ist(9, 15))
    snap = ind.compute_indicators(bars)
    assert snap.asof == bars[-1].ts + BAR_INTERVAL


def test_snapshot_populates_every_indicator_with_enough_history():
    snap = ind.compute_indicators(flat_series(100, 40))
    for field in ("vwap", "atr14", "ema9", "ema21", "rsi14", "avg_volume20", "volume_ratio"):
        assert getattr(snap, field) is not None, field
    assert snap.has_core_indicators


def test_snapshot_leaves_unavailable_indicators_none_rather_than_guessing():
    snap = ind.compute_indicators(flat_series(100, 5))
    assert snap.vwap is not None  # VWAP needs only one bar
    assert snap.atr14 is None
    assert snap.rsi14 is None
    assert snap.ema9 is None
    assert not snap.has_core_indicators


def test_snapshot_none_for_no_bars():
    assert ind.compute_indicators([]) is None


def test_snapshot_last_price_is_the_last_close():
    bars = make_series([100, 101, 102])
    assert ind.compute_indicators(bars).last_price == pytest.approx(102.0)


# --------------------------------------------------------------------------------
# Bar validation
# --------------------------------------------------------------------------------


def test_bar_rejects_naive_timestamp():
    from datetime import datetime

    with pytest.raises(Exception):
        Bar(ts=datetime(2026, 8, 20, 9, 15), open=1, high=1, low=1, close=1, volume=1)


def test_bar_rejects_inconsistent_ohlc():
    with pytest.raises(Exception):
        Bar(ts=ist(9, 15), open=100, high=99, low=98, close=100, volume=1)


def test_bar_rejects_negative_volume():
    with pytest.raises(Exception):
        Bar(ts=ist(9, 15), open=100, high=100, low=100, close=100, volume=-1)


def test_bar_typical_price():
    bar = make_bar(ist(9, 15), 10, high=12, low=8)
    assert bar.typical_price == pytest.approx(10.0)
