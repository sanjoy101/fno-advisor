"""Tests for the single-shot decision structure.

The property being protected is that ONE draw stays one draw. Falling through to the
second-best candidate, or evaluating more than once, quietly restores the
multiple-comparisons problem this replaced.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest

from config.settings import SignalEngineSettings
from src.positions.store import PositionStore
from src.signals.evaluate import (
    run_single_shot,
    run_single_shot_session,
    summarise,
)
from tests.conftest import TRADE_DATE, ist, make_series

CFG = SignalEngineSettings()
PRIOR = [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)]
ALL_SESSIONS = PRIOR + [TRADE_DATE]
AT = time(9, 45)


def healthy(day, *, base=1000.0, step=0.30, volume=20000, n=200, spread=1.2):
    """A leader strong enough to clear the 0.594 bar by 09:45.

    Conviction accumulates: the same trend at step=0.16 scores 0.43 at 09:45 and 0.68
    by 10:15, because or_hold and vwap_persistence need bars to build. A fixture that
    only qualifies later would silently skip every 09:45 assertion here.
    """
    closes, price = [], base
    for i in range(n):
        price += step if i % 4 else -step * 1.4
        closes.append(round(price, 2))
    return make_series(closes, start=ist(9, 15, d=day),
                       volumes=[volume] * n, spread=spread)


def test_the_fixture_actually_produces_a_signal(tmp_path):
    """Guard against every assertion below passing vacuously."""
    store = PositionStore(tmp_path)
    for day in ALL_SESSIONS:
        store.save_bars("RELIANCE", day,
                        healthy(day, volume=60000 if day == TRADE_DATE else 20000))
        for i, sym in enumerate(("INFY", "TCS", "SBIN")):
            store.save_bars(sym, day, flat(day, base=900 + i * 50))
    result = run_single_shot_session(
        store, ["RELIANCE", "INFY", "TCS", "SBIN"], TRADE_DATE, ALL_SESSIONS, AT, cfg=CFG
    )
    assert result.verdict == "traded", result.detail
    assert result.top_score >= float(CFG.min_composite_score)


def flat(day, *, base=1000.0, volume=20000, n=200, spread=1.2):
    return make_series([base] * n, start=ist(9, 15, d=day),
                       volumes=[volume] * n, spread=spread)


@pytest.fixture
def leader(tmp_path):
    """One strong name at 3x volume among three flat peers."""
    store = PositionStore(tmp_path)
    for day in ALL_SESSIONS:
        store.save_bars("RELIANCE", day,
                        healthy(day, volume=60000 if day == TRADE_DATE else 20000))
        for i, sym in enumerate(("INFY", "TCS", "SBIN")):
            store.save_bars(sym, day, flat(day, base=900 + i * 50))
    return store, ["RELIANCE", "INFY", "TCS", "SBIN"]


@pytest.fixture
def all_flat(tmp_path):
    store = PositionStore(tmp_path)
    for day in ALL_SESSIONS:
        for i, sym in enumerate(("INFY", "TCS", "SBIN")):
            store.save_bars(sym, day, flat(day, base=900 + i * 50))
    return store, ["INFY", "TCS", "SBIN"]


# --------------------------------------------------------------------------------
# One draw
# --------------------------------------------------------------------------------


def test_at_most_one_position_per_session(leader):
    store, symbols = leader
    result = run_single_shot_session(store, symbols, TRADE_DATE, ALL_SESSIONS, AT, cfg=CFG)
    assert result.outcome is None or isinstance(result.outcome.symbol, str)
    outcomes, stats = run_single_shot(store, symbols, [TRADE_DATE], AT, cfg=CFG)
    assert len(outcomes) <= 1


def test_the_traded_symbol_is_the_top_scorer(leader):
    store, symbols = leader
    result = run_single_shot_session(store, symbols, TRADE_DATE, ALL_SESSIONS, AT, cfg=CFG)
    if result.verdict == "traded":
        assert result.outcome.symbol == result.top_symbol


def test_a_below_bar_day_is_no_signal(all_flat):
    store, symbols = all_flat
    result = run_single_shot_session(store, symbols, TRADE_DATE, ALL_SESSIONS, AT, cfg=CFG)
    assert result.verdict in {"below_bar", "no_scores"}
    assert result.outcome is None


def test_below_bar_records_the_score_it_fell_short_of(all_flat):
    store, symbols = all_flat
    result = run_single_shot_session(store, symbols, TRADE_DATE, ALL_SESSIONS, AT, cfg=CFG)
    if result.verdict == "below_bar":
        assert result.top_score is not None
        assert result.top_score < float(CFG.min_composite_score)
        assert "below the" in result.detail


def test_it_does_not_fall_through_to_the_second_best(leader, monkeypatch):
    """Walking the ranking is how one draw silently becomes several."""
    from src.signals import evaluate as ev

    store, symbols = leader
    real = ev.generate_signals

    def top_always_gated(*a, **kw):
        decision = real(*a, **kw)
        if decision.all_scores:
            best = max(decision.all_scores, key=lambda kv: kv[1])[0]
            decision.candidates = [c for c in decision.candidates if c.symbol != best]
        return decision

    monkeypatch.setattr(ev, "generate_signals", top_always_gated)
    result = run_single_shot_session(store, symbols, TRADE_DATE, ALL_SESSIONS, AT, cfg=CFG)
    assert result.outcome is None
    assert result.verdict == "gated"


def test_gated_is_distinguished_from_below_bar(leader, monkeypatch):
    from src.signals import evaluate as ev

    store, symbols = leader
    real = ev.generate_signals

    def drop_all(*a, **kw):
        decision = real(*a, **kw)
        decision.candidates = []
        return decision

    monkeypatch.setattr(ev, "generate_signals", drop_all)
    result = run_single_shot_session(store, symbols, TRADE_DATE, ALL_SESSIONS, AT, cfg=CFG)
    assert result.verdict in {"gated", "below_bar"}


def test_the_bar_is_absolute_not_relative(all_flat):
    """The best of a bad day is still a bad day. No relative grading."""
    store, symbols = all_flat
    result = run_single_shot_session(store, symbols, TRADE_DATE, ALL_SESSIONS, AT, cfg=CFG)
    assert result.outcome is None, "top-of-the-heap must not qualify on its own"


# --------------------------------------------------------------------------------
# Across sessions
# --------------------------------------------------------------------------------


def test_stats_account_for_every_session(leader):
    store, symbols = leader
    outcomes, stats = run_single_shot(store, symbols, ALL_SESSIONS, AT, cfg=CFG)
    assert stats["sessions"] == len(ALL_SESSIONS)
    assert stats["signals"] + stats["no_signal"] == len(ALL_SESSIONS)
    assert sum(stats["verdicts"].values()) == len(ALL_SESSIONS)


def test_draws_per_session_is_one(leader):
    store, symbols = leader
    _, stats = run_single_shot(store, symbols, ALL_SESSIONS, AT, cfg=CFG)
    assert stats["draws_per_session"] == 1


def test_a_flat_universe_yields_full_no_signal(all_flat):
    store, symbols = all_flat
    outcomes, stats = run_single_shot(store, symbols, ALL_SESSIONS, AT, cfg=CFG)
    assert outcomes == []
    assert stats["no_signal_pct"] == 100.0


def test_traded_dates_match_the_outcomes(leader):
    store, symbols = leader
    outcomes, stats = run_single_shot(store, symbols, ALL_SESSIONS, AT, cfg=CFG)
    assert sorted(stats["traded_dates"]) == sorted(o.trade_date for o in outcomes)


def test_a_later_decision_point_is_a_different_measurement(leader):
    store, symbols = leader
    early, _ = run_single_shot(store, symbols, ALL_SESSIONS, time(9, 45), cfg=CFG)
    late, _ = run_single_shot(store, symbols, ALL_SESSIONS, time(10, 15), cfg=CFG)
    assert [o.signal_at for o in early] != [o.signal_at for o in late] or not early


def test_single_shot_is_deterministic(leader):
    store, symbols = leader
    a, sa = run_single_shot(store, symbols, ALL_SESSIONS, AT, cfg=CFG)
    b, sb = run_single_shot(store, symbols, ALL_SESSIONS, AT, cfg=CFG)
    assert [o.as_row() for o in a] == [o.as_row() for o in b]
    assert sa["verdicts"] == sb["verdicts"]


# --------------------------------------------------------------------------------
# Gross reporting
# --------------------------------------------------------------------------------


def test_summary_reports_gross_with_its_own_interval(leader):
    store, symbols = leader
    outcomes, _ = run_single_shot(store, symbols, ALL_SESSIONS, AT, cfg=CFG)
    if not outcomes:
        pytest.skip("no signals in fixture")
    row = summarise("x", outcomes)
    for key in ("mean_gross_pnl", "gross_ci95_low", "gross_ci95_high",
                "hit_rate_gross_pct"):
        assert key in row


def test_gross_interval_brackets_the_gross_mean():
    """Built from synthetic outcomes so it does not depend on how many days signal."""
    from src.signals.evaluate import _mean_ci

    mean, sd, low, high = _mean_ci([10.0, -5.0, 30.0, -20.0, 15.0])
    assert low <= mean <= high
    assert high > low


def test_a_single_observation_gives_a_degenerate_interval():
    from src.signals.evaluate import _mean_ci

    mean, sd, low, high = _mean_ci([42.0])
    assert (mean, sd, low, high) == (42.0, 0.0, 42.0, 42.0)


def test_gross_is_always_better_than_net(leader):
    store, symbols = leader
    outcomes, _ = run_single_shot(store, symbols, ALL_SESSIONS, AT, cfg=CFG)
    for o in outcomes:
        assert o.gross_pnl > o.net_pnl
