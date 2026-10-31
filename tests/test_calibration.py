"""Tests for the excursion measurement that sizes the stop.

This measurement is deliberately forward-looking, which makes it exactly the kind of
code that must be pinned: if it silently measured the wrong window, the derived stop
would be wrong and nothing downstream would notice.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from src.positions.indicators import atr, atr_series
from src.signals.calibration import (
    TIME_BUCKETS,
    bucket_of,
    describe,
    measure_session,
    percentile,
)
from tests.conftest import TRADE_DATE, ist, make_bar, make_series

T1_MULT = 1.5


def series(closes, *, highs=None, lows=None, start=None, spread=1.0):
    bars = []
    start = start or ist(9, 15)
    for i, c in enumerate(closes):
        bars.append(make_bar(
            start.replace(minute=start.minute) + __import__("datetime").timedelta(minutes=i),
            c,
            high=(highs[i] if highs else c + spread),
            low=(lows[i] if lows else c - spread),
        ))
    return bars


# --------------------------------------------------------------------------------
# atr_series
# --------------------------------------------------------------------------------


def test_atr_series_matches_atr_over_every_prefix():
    """Causal by construction: element i must equal atr() over bars[:i+1]."""
    bars = make_series([100 + (i * 37) % 13 for i in range(40)], spread=0.9)
    got = atr_series(bars)
    assert len(got) == len(bars)
    for i, value in enumerate(got):
        assert value == atr(bars[: i + 1]), i


def test_atr_series_is_none_until_warm():
    bars = make_series([100] * 30, spread=1.0)
    got = atr_series(bars, 14)
    assert all(v is None for v in got[:14])
    assert got[14] is not None


# --------------------------------------------------------------------------------
# Buckets
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("at,expected", [
    (ist(9, 30), "09:30-10:29"),
    (ist(10, 29), "09:30-10:29"),
    (ist(10, 30), "10:30-11:59"),
    (ist(12, 0), "12:00-13:29"),
    (ist(14, 30), "13:30-14:30"),
    (ist(15, 0), "outside"),
])
def test_bucket_boundaries(at, expected):
    assert bucket_of(at) == expected


def test_buckets_do_not_overlap():
    spans = [(lo, hi) for _, lo, hi in TIME_BUCKETS]
    for (_, hi), (lo, _) in zip(spans, spans[1:]):
        assert hi < lo


# --------------------------------------------------------------------------------
# measure_session
# --------------------------------------------------------------------------------


def rising_then_target(n=60, base=1000.0):
    """Dips first, then rallies well past any plausible T1."""
    closes = []
    for i in range(n):
        closes.append(base - 3.0 if 5 <= i <= 12 else base + max(0, (i - 20)) * 1.5)
    return make_series(closes, start=ist(9, 15), spread=1.0)


def test_a_winner_records_the_dip_it_survived():
    bars = rising_then_target()
    out = measure_session("X", TRADE_DATE, bars, [time(9, 35)], t1_atr_multiple=T1_MULT)
    assert out
    e = out[0]
    assert e.reached_t1 is True
    assert e.mae_atr > 0, "the position dipped before it worked; that must be recorded"
    assert e.bars_to_t1 is not None and e.bars_to_t1 > 0


def test_adverse_excursion_is_measured_only_up_to_the_t1_touch():
    """A collapse AFTER T1 is not noise the position had to survive."""
    closes = [1000.0] * 20 + [1050.0] + [500.0] * 20
    bars = make_series(closes, start=ist(9, 15), spread=1.0)
    out = measure_session("X", TRADE_DATE, bars, [time(9, 33)], t1_atr_multiple=T1_MULT)
    e = out[0]
    assert e.reached_t1 is True
    assert e.mae_atr < 1.0, "the post-T1 crash leaked into the pre-T1 excursion"


def test_a_loser_is_recorded_but_not_counted_as_survivable():
    closes = [1000.0] * 20 + [990 - i for i in range(30)]
    bars = make_series(closes, start=ist(9, 15), spread=1.0)
    out = measure_session("X", TRADE_DATE, bars, [time(9, 33)], t1_atr_multiple=T1_MULT)
    assert out[0].reached_t1 is False
    assert out[0].bars_to_t1 is None
    assert out[0].mae_atr > 0


def test_excursions_are_expressed_in_atr_units():
    """A multiplier only means something if it is scale free."""
    quiet = make_series([1000.0] * 20 + [1000 + i for i in range(40)],
                        start=ist(9, 15), spread=0.5)
    wild = make_series([1000.0] * 20 + [1000 + i * 4 for i in range(40)],
                       start=ist(9, 15), spread=2.0)
    a = measure_session("Q", TRADE_DATE, quiet, [time(9, 36)], t1_atr_multiple=T1_MULT)
    b = measure_session("W", TRADE_DATE, wild, [time(9, 36)], t1_atr_multiple=T1_MULT)
    assert a and b
    assert a[0].atr != b[0].atr, "test premise: the two have different volatility"


def test_nothing_is_measured_past_the_square_off():
    """The holding window ends at 15:15; a 15:20 spike is not reachable."""
    closes = [1000.0] * 370 + [5000.0] * 20
    bars = make_series(closes, start=ist(9, 15), spread=1.0)
    out = measure_session("X", TRADE_DATE, bars, [time(14, 30)], t1_atr_multiple=T1_MULT)
    if out:
        assert out[0].mfe_atr < 100, "a post-square-off spike leaked into the window"


def test_entry_uses_the_last_completed_bar():
    bars = make_series([1000 + i for i in range(60)], start=ist(9, 15), spread=1.0)
    out = measure_session("X", TRADE_DATE, bars, [time(9, 40)], t1_atr_multiple=T1_MULT)
    assert out[0].entry == pytest.approx(1024.0), "09:39 bar closes at 09:40"


def test_no_measurement_before_atr_is_warm():
    bars = make_series([1000.0] * 60, start=ist(9, 15), spread=1.0)
    out = measure_session("X", TRADE_DATE, bars, [time(9, 20)], t1_atr_multiple=T1_MULT)
    assert out == [], "ATR14 needs 15 bars; nothing can be measured at 09:20"


def test_entry_times_after_the_window_are_skipped():
    bars = make_series([1000.0] * 60, start=ist(9, 15), spread=1.0)
    assert measure_session("X", TRADE_DATE, bars, [time(14, 0)],
                           t1_atr_multiple=T1_MULT) == []


def test_empty_bars_are_handled():
    assert measure_session("X", TRADE_DATE, [], [time(9, 30)], t1_atr_multiple=T1_MULT) == []


def test_bucket_is_attached_to_each_excursion():
    bars = rising_then_target(n=120)
    out = measure_session("X", TRADE_DATE, bars, [time(9, 35), time(10, 45)],
                          t1_atr_multiple=T1_MULT)
    assert {e.bucket for e in out} <= {"09:30-10:29", "10:30-11:59"}


# --------------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------------


def test_percentile_endpoints():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 5.0
    assert percentile(values, 50) == 3.0


def test_percentile_interpolates():
    assert percentile([0.0, 10.0], 75) == pytest.approx(7.5)


def test_percentile_of_nothing_is_none():
    assert percentile([], 50) is None


def test_describe_carries_n():
    row = describe([1.0, 2.0, 3.0], "x")
    assert row["n"] == 3
    assert row["p50"] == 2.0


def test_describe_of_nothing_reports_zero_n():
    assert describe([], "x")["n"] == 0


def test_the_derived_stop_would_sit_beyond_most_winners():
    """The defining property: a p75 stop is not touched by ~75% of winners.

    Exact at scale, approximate on a handful of points -- an interpolated percentile
    lands between order statistics, so at n=10 the realised rate is 70%. The real
    calibration runs on tens of thousands, where it converges.
    """
    survivable = [i / 1000.0 for i in range(1000)]
    stop = percentile(survivable, 75)
    untouched = sum(1 for v in survivable if v <= stop) / len(survivable)
    assert untouched == pytest.approx(0.75, abs=0.01)


def test_a_small_sample_only_approximates_the_target_rate():
    """Documents the caveat above rather than leaving it to be rediscovered."""
    survivable = [0.2, 0.4, 0.5, 0.9, 1.1, 1.3, 2.0, 2.4, 3.1, 4.0]
    stop = percentile(survivable, 75)
    untouched = sum(1 for v in survivable if v <= stop) / len(survivable)
    assert 0.65 <= untouched <= 0.85
