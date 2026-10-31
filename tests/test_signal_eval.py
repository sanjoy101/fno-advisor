"""Tests for the signal evaluation harness and the baseline."""

from __future__ import annotations

import math
from datetime import date, time
from decimal import Decimal

import pytest

from config.settings import SignalEngineSettings
from src.positions.store import PositionStore
from src.signals.evaluate import (
    BASELINE_ENTRY_TIME,
    baseline_highest_volume,
    run_signal_session,
    run_signal_sessions,
    summarise,
    verdict,
    write_signal_csv,
)
from tests.conftest import TRADE_DATE, ist, make_series

CFG = SignalEngineSettings()
PRIOR = [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)]
ALL_SESSIONS = PRIOR + [TRADE_DATE]


def healthy_uptrend(day, *, base=1000.0, step=0.16, volume=20000, n=200, spread=1.2):
    closes, price = [], base
    for i in range(n):
        price += step if i % 4 else -step * 1.4
        closes.append(round(price, 2))
    return make_series(closes, start=ist(9, 15, d=day),
                       volumes=[volume] * n, spread=spread)


def flat(day, *, base=1000.0, volume=20000, n=200, spread=1.2):
    return make_series([base] * n, start=ist(9, 15, d=day),
                       volumes=[volume] * n, spread=spread)


@pytest.fixture
def panel(tmp_path):
    """A leader trading at 3x volume, inside three flat peers."""
    store = PositionStore(tmp_path)
    for day in ALL_SESSIONS:
        store.save_bars("RELIANCE", day,
                        healthy_uptrend(day, volume=60000 if day == TRADE_DATE else 20000))
        for i, sym in enumerate(("INFY", "TCS", "SBIN")):
            store.save_bars(sym, day, flat(day, base=900 + i * 50))
    return store, ["RELIANCE", "INFY", "TCS", "SBIN"]


# --------------------------------------------------------------------------------
# Running a session
# --------------------------------------------------------------------------------


def test_a_session_with_a_leader_produces_a_signal(panel):
    store, symbols = panel
    outcomes, evals = run_signal_session(store, symbols, TRADE_DATE, ALL_SESSIONS, cfg=CFG)
    assert outcomes
    assert evals > 0
    assert outcomes[0].symbol == "RELIANCE"


def test_a_flat_session_produces_no_signal(tmp_path):
    store = PositionStore(tmp_path)
    for day in ALL_SESSIONS:
        for sym in ("INFY", "TCS"):
            store.save_bars(sym, day, flat(day))
    outcomes, _ = run_signal_session(store, ["INFY", "TCS"], TRADE_DATE, ALL_SESSIONS,
                                     cfg=CFG)
    assert outcomes == []


def test_the_daily_budget_is_respected(panel):
    """max_candidates is a DAILY cap, not a per-evaluation one."""
    store, symbols = panel
    outcomes, _ = run_signal_session(store, symbols, TRADE_DATE, ALL_SESSIONS, cfg=CFG)
    assert len(outcomes) <= CFG.max_candidates


def test_a_symbol_is_not_signalled_twice_in_a_day(panel):
    store, symbols = panel
    outcomes, _ = run_signal_session(store, symbols, TRADE_DATE, ALL_SESSIONS, cfg=CFG)
    assert len({o.symbol for o in outcomes}) == len(outcomes)


def test_the_signal_stop_is_passed_through_to_the_exit_engine(panel):
    store, symbols = panel
    outcomes, _ = run_signal_session(store, symbols, TRADE_DATE, ALL_SESSIONS, cfg=CFG)
    assert outcomes[0].stop < outcomes[0].entry_price


def test_outcomes_report_gross_and_net(panel):
    store, symbols = panel
    outcomes, _ = run_signal_session(store, symbols, TRADE_DATE, ALL_SESSIONS, cfg=CFG)
    for o in outcomes:
        assert o.net_pnl < o.gross_pnl
        assert o.costs > 0


def test_r_multiple_uses_the_signals_own_declared_risk(panel):
    """Measuring against a widened stop would flatter the number."""
    store, symbols = panel
    outcomes, _ = run_signal_session(store, symbols, TRADE_DATE, ALL_SESSIONS, cfg=CFG)
    o = outcomes[0]
    risk = (o.entry_price - o.stop) * o.quantity
    assert o.r_multiple == pytest.approx(o.net_pnl / risk, rel=0.02)


def test_the_reason_travels_with_the_outcome(panel):
    store, symbols = panel
    outcomes, _ = run_signal_session(store, symbols, TRADE_DATE, ALL_SESSIONS, cfg=CFG)
    assert "FOR:" in outcomes[0].reason


# --------------------------------------------------------------------------------
# Running many sessions
# --------------------------------------------------------------------------------


def test_multi_session_run_counts_no_signal_days(panel):
    store, symbols = panel
    outcomes, stats = run_signal_sessions(store, symbols, ALL_SESSIONS, cfg=CFG)
    assert stats["sessions"] == len(ALL_SESSIONS)
    assert stats["sessions_with_a_signal"] + stats["no_signal_sessions"] == len(ALL_SESSIONS)
    assert stats["signals"] == len(outcomes)


def test_no_signal_days_are_counted_not_dropped(tmp_path):
    store = PositionStore(tmp_path)
    for day in ALL_SESSIONS:
        store.save_bars("INFY", day, flat(day))
    _, stats = run_signal_sessions(store, ["INFY"], ALL_SESSIONS, cfg=CFG)
    assert stats["no_signal_sessions"] == len(ALL_SESSIONS)
    assert stats["signals"] == 0


# --------------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------------


def test_baseline_picks_the_highest_volume_symbol(panel):
    store, symbols = panel
    out = baseline_highest_volume(store, symbols, [TRADE_DATE])
    assert len(out) == 1
    assert out[0].symbol == "RELIANCE", "RELIANCE trades 3x the others today"


def test_baseline_takes_one_position_per_session(panel):
    store, symbols = panel
    out = baseline_highest_volume(store, symbols, ALL_SESSIONS)
    assert len(out) == len(ALL_SESSIONS)
    assert len({o.trade_date for o in out}) == len(ALL_SESSIONS)


def test_baseline_ranks_on_volume_up_to_the_entry_only(tmp_path):
    """Ranking on full-day volume would be look-ahead, and an unfollowable rule."""
    store = PositionStore(tmp_path)
    n = 200
    # EARLY leads before 09:45; LATE only overtakes it in the afternoon.
    early = [8000] * 40 + [1000] * (n - 40)
    late = [1000] * 40 + [90000] * (n - 40)
    for day in ALL_SESSIONS:
        store.save_bars("EARLY", day, make_series([1000.0] * n, start=ist(9, 15, d=day),
                                                  volumes=early, spread=1.0))
        store.save_bars("LATE", day, make_series([1000.0] * n, start=ist(9, 15, d=day),
                                                 volumes=late, spread=1.0))
    out = baseline_highest_volume(store, ["EARLY", "LATE"], [TRADE_DATE])
    assert out[0].symbol == "EARLY"


def test_baseline_enters_at_0945():
    assert BASELINE_ENTRY_TIME == time(9, 45)


def test_baseline_skips_sessions_with_no_data(tmp_path):
    store = PositionStore(tmp_path)
    store.save_bars("INFY", TRADE_DATE, flat(TRADE_DATE))
    out = baseline_highest_volume(store, ["INFY"], [date(2026, 8, 19), TRADE_DATE])
    assert len(out) == 1


# --------------------------------------------------------------------------------
# Summary and verdict
# --------------------------------------------------------------------------------


def test_summary_carries_n(panel):
    store, symbols = panel
    outcomes, _ = run_signal_sessions(store, symbols, ALL_SESSIONS, cfg=CFG)
    row = summarise("signals", outcomes, sessions=len(ALL_SESSIONS))
    assert row["n"] == len(outcomes)
    assert "ci95_low" in row and "ci95_high" in row


def test_summary_reports_gross_net_and_costs(panel):
    store, symbols = panel
    outcomes, _ = run_signal_sessions(store, symbols, ALL_SESSIONS, cfg=CFG)
    row = summarise("signals", outcomes)
    assert row["total_net_pnl"] < row["total_gross_pnl"]
    assert row["total_costs"] > 0


def test_summary_of_nothing_says_so():
    assert summarise("empty", [])["n"] == 0


def test_verdict_calls_a_zero_spanning_interval_inconclusive():
    row = {"n": 40, "mean_net_pnl": 5.0, "ci95_low": -50.0, "ci95_high": 60.0}
    assert "INCONCLUSIVE" in verdict(row)
    assert "cannot distinguish" in verdict(row)


def test_verdict_names_a_loss_plainly():
    row = {"n": 400, "mean_net_pnl": -180.0, "ci95_low": -220.0, "ci95_high": -140.0}
    assert "LOSS-MAKING" in verdict(row)


def test_verdict_on_no_trades():
    assert "Nothing to conclude" in verdict({"n": 0})


def test_signal_csv_round_trips(tmp_path, panel):
    store, symbols = panel
    outcomes, _ = run_signal_sessions(store, symbols, ALL_SESSIONS, cfg=CFG)
    path = write_signal_csv(outcomes, tmp_path / "signals.csv")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "symbol" in text.splitlines()[0]
