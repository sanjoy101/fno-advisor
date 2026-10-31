"""Every signal feature, run through the look-ahead harness.

This is the test the whole module is written to satisfy. Each feature is evaluated at
many timestamps with future bars present, absent, and poisoned; any disagreement is a
leak and fails here.

The bar series is deliberately jagged and asymmetric. A flat or monotonic series can
hide a leak, because reading tomorrow's bar returns roughly today's value.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from src.analysis.lookahead import assert_causal, assert_session_causal, causal_check
from src.signals import features as F
from src.signals.universe import trailing_sessions
from src.positions.store import PositionStore
from tests.conftest import TRADE_DATE, ist, make_series


def jagged(n: int = 120, start=None, *, volume_scale: float = 1.0):
    """A non-monotonic, non-repeating series -- a leak here cannot hide."""
    closes = []
    price = 1000.0
    for i in range(n):
        price += ((i * 37) % 13 - 6) * 0.35 + math.sin(i / 5.0) * 0.8
        closes.append(round(price, 2))
    volumes = [int((4000 + ((i * 911) % 7000)) * volume_scale) for i in range(n)]
    return make_series(closes, start=start or ist(9, 15), volumes=volumes, spread=0.9)


BARS = jagged()
#: Decision times across the whole session, including before features are warm.
TIMES = [ist(9, 15) + timedelta(minutes=m) for m in range(16, 120, 5)]


SINGLE_SERIES_FEATURES = [
    ("last_price_at", F.last_price_at),
    ("session_bars", lambda b, at: [x.close for x in F.session_bars(b, at)]),
    ("opening_range", F.opening_range),
    ("breakout_state", F.breakout_state),
    ("breakout_atr_units", F.breakout_atr_units),
    ("breakout_held_bars", F.breakout_held_bars),
    ("ema_alignment_pct", F.ema_alignment_pct),
    ("price_vs_vwap_pct", F.price_vs_vwap_pct),
    ("bars_above_vwap", F.bars_above_vwap),
    ("rsi_at", F.rsi_at),
    ("rate_of_change_pct", F.rate_of_change_pct),
    ("atr_pct", F.atr_pct),
    ("session_return_pct", F.session_return_pct),
]


@pytest.mark.parametrize("name,fn", SINGLE_SERIES_FEATURES, ids=[n for n, _ in SINGLE_SERIES_FEATURES])
def test_feature_is_causal(name, fn):
    assert_causal(fn, BARS, TIMES, name=name)


def test_every_public_feature_is_covered():
    """A new feature must not be able to skip this file unnoticed."""
    covered = {n for n, _ in SINGLE_SERIES_FEATURES}
    cross_sectional = {
        "time_of_day_volume_profile",  # multi-session, covered by the session check
        "volume_profile_for",          # covered by assert_session_causal
        "relative_volume",             # needs a profile, covered below
        "running_vwap_series",         # helper, exercised via bars_above_vwap
        "OPENING_RANGE_MINUTES", "OpeningRange",
    }
    missing = set(F.__all__) - covered - cross_sectional
    assert not missing, f"features with no leak test: {sorted(missing)}"


def test_the_harness_is_actually_exercising_these_features():
    """Guard against a vacuous pass: the features must return real values."""
    values = [F.atr_pct(BARS, t) for t in TIMES]
    assert any(v is not None for v in values)
    assert len({round(v, 6) for v in values if v is not None}) > 3


# --------------------------------------------------------------------------------
# Cross-sectional / multi-session features
# --------------------------------------------------------------------------------


@pytest.fixture
def stocked(tmp_path):
    """Three identical prior sessions, plus a today that trades at exactly 2x volume.

    Prior sessions match so the baseline is unambiguous; today differs so that
    including it in its own baseline visibly changes the answer -- without that, a
    cross-session leak is numerically invisible and the check cannot be proved to
    fire.
    """
    store = PositionStore(tmp_path)
    days = [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19), TRADE_DATE]
    for day in days[:-1]:
        store.save_bars("RELIANCE", day, jagged(120, start=ist(9, 15, d=day)))
    store.save_bars("RELIANCE", TRADE_DATE,
                    jagged(120, start=ist(9, 15, d=TRADE_DATE), volume_scale=2.0))
    return store, days


def test_relative_volume_is_causal(stocked):
    store, days = stocked
    profile = F.time_of_day_volume_profile(store, "RELIANCE", days[:-1])
    bars = store.load_bars("RELIANCE", TRADE_DATE)
    assert_causal(
        lambda b, at: F.relative_volume(b, at, profile),
        bars, TIMES, name="relative_volume",
    )


def test_the_volume_profile_excludes_the_session_being_scored(stocked):
    """The single most likely leak in this module: today's volume in today's baseline."""
    store, days = stocked
    prior = trailing_sessions(days, TRADE_DATE, lookback=20)
    assert TRADE_DATE not in prior
    assert prior == days[:-1]


def test_trailing_sessions_never_returns_the_current_or_a_later_session():
    all_days = [date(2026, 8, d) for d in (10, 11, 12, 13, 14, 17, 18, 19, 20)]
    prior = trailing_sessions(all_days, date(2026, 8, 14), lookback=20)
    assert max(prior) < date(2026, 8, 14)


def test_trailing_sessions_respects_the_lookback():
    all_days = [date(2026, 8, d) for d in range(1, 21)]
    assert len(trailing_sessions(all_days, date(2026, 8, 20), lookback=5)) == 5


def test_the_volume_profile_is_session_causal(stocked):
    """The check that actually covers this feature.

    The bar-series harness cannot: the leak here is being handed the wrong list of
    DATES, and every bar in those dates is genuinely complete. Nothing about today's
    bars changes, so poisoning them proves nothing.
    """
    store, days = stocked
    assert_session_causal(
        lambda sessions: F.volume_profile_for(store, "RELIANCE", sessions, TRADE_DATE),
        days, TRADE_DATE, name="volume_profile_for",
    )


def test_a_profile_that_includes_today_is_caught_by_the_session_check(stocked):
    """Prove the session check fires, or it is just decoration."""
    store, days = stocked

    def leaky(sessions):
        # <= instead of <: today's own volume lands in today's baseline.
        prior = sorted(d for d in sessions if d <= TRADE_DATE)[-20:]
        return F.time_of_day_volume_profile(store, "RELIANCE", prior)

    with pytest.raises(AssertionError, match="CROSS-SESSION LOOK-AHEAD"):
        assert_session_causal(leaky, days, TRADE_DATE, name="leaky_profile")


def test_a_ratio_leak_can_survive_poisoning(stocked):
    """Documents a real limit of multiplicative poisoning, so nobody over-trusts it.

    Scale a numerator and its denominator by the same factor and a leak cancels
    exactly. A baseline rebuilt from the bars handed in is invariant to poisoning --
    which is precisely why the session-level check above has to exist.
    """
    store, _ = stocked
    bars = store.load_bars("RELIANCE", TRADE_DATE)

    def self_referential(b, at):
        totals = {}
        for bar in b:
            totals.setdefault(bar.ts.time(), []).append(bar.volume)
        return F.relative_volume(b, at, {k: sum(v) / len(v) for k, v in totals.items()})

    assert causal_check(self_referential, bars, TIMES, name="ratio").ok
    # ... and it is useless, which is the tell: it can only ever return 1.0.
    values = {self_referential(bars, t) for t in TIMES}
    assert values <= {1.0, None}


def test_relative_volume_is_time_of_day_normalised(stocked):
    """A morning and an afternoon minute must be comparable. That is the whole point.

    Today trades 2x its own baseline at EVERY minute, so a correctly normalised
    feature reports 2.0 at 09:31 and 2.0 at 10:50. A raw-volume feature would report
    a much larger number in the morning purely because mornings are busier.
    """
    store, days = stocked
    profile = F.volume_profile_for(store, "RELIANCE", days, TRADE_DATE)
    bars = store.load_bars("RELIANCE", TRADE_DATE)
    early = F.relative_volume(bars, ist(9, 31), profile)
    late = F.relative_volume(bars, ist(10, 50), profile)
    assert early == pytest.approx(2.0, abs=0.01)
    assert late == pytest.approx(2.0, abs=0.01)


def test_raw_volume_would_not_be_comparable_across_the_day(stocked):
    """Shows what the normalisation is protecting against."""
    store, _ = stocked
    bars = store.load_bars("RELIANCE", TRADE_DATE)
    early_raw = F.session_bars(bars, ist(9, 31))[-1].volume
    late_raw = F.session_bars(bars, ist(10, 50))[-1].volume
    assert early_raw != late_raw, "raw volumes differ; the ratio is what makes them comparable"


def test_relative_volume_is_none_for_an_unknown_minute(stocked):
    store, _ = stocked
    bars = store.load_bars("RELIANCE", TRADE_DATE)
    assert F.relative_volume(bars, ist(9, 31), {}) is None


def test_relative_volume_is_none_on_a_zero_baseline(stocked):
    store, _ = stocked
    bars = store.load_bars("RELIANCE", TRADE_DATE)
    zero = {b.ts.time(): 0.0 for b in bars}
    assert F.relative_volume(bars, ist(9, 31), zero) is None
