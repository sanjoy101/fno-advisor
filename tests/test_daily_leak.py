"""Every daily-horizon feature, run through the cross-session leak check.

The bar-level harness cannot cover these. A daily feature leaks by being handed the
wrong list of DATES -- every bar in those dates is genuinely complete, so poisoning
the current session's bars proves nothing. That is the exact route the time-of-day
volume baseline leaked through, and ``assert_session_causal`` is the only thing that
sees it.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import pytest

from src.analysis.lookahead import assert_session_causal, sessions_before
from src.market.calendar import IST
from src.positions.models import Bar
from src.signals import daily as D
from tests.conftest import ist, make_series

SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(60)]


def synthetic_daily(sessions, *, base=1000.0):
    """A jagged daily series. Flat or monotonic data can hide a leak."""
    out = {}
    price = base
    for i, day in enumerate(sessions):
        price += ((i * 41) % 17 - 8) * 1.3 + math.sin(i / 4.0) * 2.0
        close = round(price, 2)
        out[day] = Bar(
            ts=datetime.combine(day, D.DECISION_TIME, tzinfo=IST),
            open=close - 1.5,
            high=close + 3.0 + (i % 5),
            low=close - 3.0 - (i % 3),
            close=close,
            volume=100_000 + ((i * 977) % 90_000),
        )
    return out


DAILY = synthetic_daily(SESSIONS)
UPTO = SESSIONS[45]

FEATURES = [
    ("daily_close", D.daily_close),
    ("range_breakout_atr", D.range_breakout_atr),
    ("range_breakout_held", D.range_breakout_held),
    ("daily_ema_alignment_pct", D.daily_ema_alignment_pct),
    ("close_vs_sma_pct", D.close_vs_sma_pct),
    ("days_above_sma", D.days_above_sma),
    ("daily_rsi", D.daily_rsi),
    ("daily_roc_pct", D.daily_roc_pct),
    ("daily_relative_volume", D.daily_relative_volume),
    ("daily_atr_pct", D.daily_atr_pct),
    ("daily_return_pct", D.daily_return_pct),
]


@pytest.mark.parametrize("name,fn", FEATURES, ids=[n for n, _ in FEATURES])
def test_daily_feature_is_session_causal(name, fn):
    """Removing every session after UPTO must not change the answer at UPTO."""
    assert_session_causal(
        lambda sess: fn(DAILY, sess, UPTO),
        SESSIONS,
        SESSIONS[46],
        name=name,
    )


@pytest.mark.parametrize("name,fn", FEATURES, ids=[n for n, _ in FEATURES])
def test_daily_feature_is_causal_at_many_dates(name, fn):
    """One date could pass by luck. Check across the series."""
    for upto in SESSIONS[25:55:5]:
        nxt = SESSIONS[SESSIONS.index(upto) + 1]
        assert_session_causal(
            lambda sess, u=upto: fn(DAILY, sess, u), SESSIONS, nxt, name=f"{name}@{upto}"
        )


def test_every_public_daily_feature_is_covered():
    covered = {n for n, _ in FEATURES}
    infrastructure = {
        "DECISION_TIME", "aggregate_daily", "build_daily_map", "daily_series",
    }
    missing = set(D.__all__) - covered - infrastructure
    assert not missing, f"daily features with no leak test: {sorted(missing)}"


def test_the_features_actually_return_values():
    """Guard against a vacuous pass: every feature must produce something at UPTO."""
    for name, fn in FEATURES:
        assert fn(DAILY, SESSIONS, UPTO) is not None, name


def test_a_leaky_daily_feature_is_caught():
    """Prove the check fires, or it is decoration."""

    def leaky(daily, sessions, upto):
        # Reads the whole list it was handed instead of filtering to <= upto.
        series = [daily[d] for d in sorted(sessions) if d in daily]
        return series[-1].close

    with pytest.raises(AssertionError, match="CROSS-SESSION LOOK-AHEAD"):
        assert_session_causal(
            lambda sess: leaky(DAILY, sess, UPTO), SESSIONS, SESSIONS[46], name="leaky"
        )


def test_an_off_by_one_daily_leak_is_caught():
    """Using tomorrow's bar is the subtlest version and must still fail."""

    def peek(daily, sessions, upto):
        ordered = sorted(d for d in sessions if d in daily)
        after = [d for d in ordered if d > upto]
        return daily[after[0]].close if after else daily[ordered[-1]].close

    with pytest.raises(AssertionError):
        assert_session_causal(
            lambda sess: peek(DAILY, sess, UPTO), SESSIONS, SESSIONS[46], name="peek"
        )


# --------------------------------------------------------------------------------
# daily_series -- the gate itself
# --------------------------------------------------------------------------------


def test_daily_series_stops_at_upto():
    series = D.daily_series(DAILY, SESSIONS, UPTO)
    assert series
    assert max(b.ts.astimezone(IST).date() for b in series) == UPTO


def test_daily_series_is_ascending():
    series = D.daily_series(DAILY, SESSIONS, UPTO)
    assert [b.ts for b in series] == sorted(b.ts for b in series)


def test_daily_series_skips_sessions_with_no_bar():
    partial = {k: v for k, v in DAILY.items() if k != SESSIONS[10]}
    series = D.daily_series(partial, SESSIONS, UPTO)
    assert SESSIONS[10] not in [b.ts.astimezone(IST).date() for b in series]


# --------------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------------


def test_aggregation_stops_at_1515():
    """Entry is at 15:15, so the last thirteen minutes must not be visible."""
    bars = make_series([100.0] * 375, start=ist(9, 15), spread=1.0)
    spiked = bars[:360] + [
        Bar(ts=b.ts, open=500.0, high=501.0, low=499.0, close=500.0, volume=b.volume)
        for b in bars[360:]
    ]
    daily = D.aggregate_daily(spiked, bars[0].ts.date())
    assert daily.high < 200.0, "post-15:15 bars leaked into the daily aggregate"
    assert daily.close == pytest.approx(100.0)


def test_aggregation_is_stamped_at_the_decision_moment():
    bars = make_series([100.0] * 375, start=ist(9, 15), spread=1.0)
    daily = D.aggregate_daily(bars, bars[0].ts.date())
    assert daily.ts.time() == D.DECISION_TIME


def test_aggregation_sums_volume_and_takes_extremes():
    bars = make_series([100.0, 105.0, 95.0, 102.0], start=ist(9, 15),
                       volumes=[10, 20, 30, 40], spread=1.0)
    daily = D.aggregate_daily(bars, bars[0].ts.date())
    assert daily.volume == 100
    assert daily.high == pytest.approx(106.0)
    assert daily.low == pytest.approx(94.0)
    assert daily.open == pytest.approx(100.0)
    assert daily.close == pytest.approx(102.0)


def test_aggregation_of_an_empty_session_is_none():
    assert D.aggregate_daily([], date(2026, 8, 20)) is None


def test_aggregation_ignores_other_dates():
    bars = make_series([100.0] * 10, start=ist(9, 15, d=date(2026, 8, 19)), spread=1.0)
    assert D.aggregate_daily(bars, date(2026, 8, 20)) is None


# --------------------------------------------------------------------------------
# Daily ATR picks up the overnight gap -- the point of using it on this horizon
# --------------------------------------------------------------------------------


def test_daily_atr_includes_overnight_gaps():
    """A gapping series must show a larger daily ATR than a continuous one."""
    calm, gappy = {}, {}
    price = 1000.0
    for i, day in enumerate(SESSIONS[:40]):
        ts = datetime.combine(day, D.DECISION_TIME, tzinfo=IST)
        calm[day] = Bar(ts=ts, open=price, high=price + 2, low=price - 2,
                        close=price, volume=1000)
        jump = price + (30 if i % 2 else -30)
        gappy[day] = Bar(ts=ts, open=jump, high=jump + 2, low=jump - 2,
                         close=jump, volume=1000)
    upto = SESSIONS[35]
    assert D.daily_atr_pct(gappy, SESSIONS[:40], upto) > \
        D.daily_atr_pct(calm, SESSIONS[:40], upto)


def test_breakout_excludes_today_from_its_own_range():
    """If today's high defined the level, a breakout could never register."""
    series = SESSIONS[:30]
    rising = {}
    for i, day in enumerate(series):
        close = 1000.0 + i * 5
        rising[day] = Bar(
            ts=datetime.combine(day, D.DECISION_TIME, tzinfo=IST),
            open=close - 1, high=close + 1, low=close - 2, close=close, volume=1000,
        )
    value = D.range_breakout_atr(rising, series, series[-1])
    assert value is not None and value > 0, "a steady new high must score as a breakout"
