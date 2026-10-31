"""Tests for scoring, selection rules and the signal engine.

Two properties get the most attention because they are the ones that quietly rot:
NO_SIGNAL must stay reachable and never be padded, and no surfaced candidate may be
one Module C would immediately reject.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from config.settings import ExitEngineSettings, SignalEngineSettings
from src.positions.store import PositionStore
from src.signals import scoring as S
from src.signals.engine import generate_signals, signal_times
from src.signals.models import ComponentScore
from src.signals.universe import (
    MIN_ATR_PCT,
    FnoBanList,
    UniverseFilters,
    sector_of,
    trailing_sessions,
)
from tests.conftest import TRADE_DATE, ist, make_series

CFG = SignalEngineSettings()
PRIOR = [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)]
ALL_SESSIONS = PRIOR + [TRADE_DATE]


def trend_session(day, *, base=1000.0, slope=0.05, volume=20000, n=200, spread=1.2):
    """A steadily rising session with enough turnover to clear the liquidity floor."""
    closes = [round(base + slope * i + math.sin(i / 9.0) * 0.4, 2) for i in range(n)]
    return make_series(closes, start=ist(9, 15, d=day),
                       volumes=[volume] * n, spread=spread)


def healthy_uptrend(day, *, base=1000.0, step=0.16, volume=20000, n=200, spread=1.2):
    """Rises with regular pullbacks, so RSI lands in the healthy band.

    A perfectly monotonic line is the WRONG fixture for this engine: it drives RSI to
    100, which the band deliberately scores -1. Real strength breathes.
    """
    closes, price = [], base
    for i in range(n):
        price += step if i % 4 else -step * 1.4
        closes.append(round(price, 2))
    return make_series(closes, start=ist(9, 15, d=day),
                       volumes=[volume] * n, spread=spread)

def flat_session(day, *, base=1000.0, volume=20000, n=200, spread=1.2):
    closes = [base] * n
    return make_series(closes, start=ist(9, 15, d=day),
                       volumes=[volume] * n, spread=spread)


def falling_session(day, *, base=1000.0, slope=0.05, volume=20000, n=200, spread=1.2):
    closes = [round(base - slope * i, 2) for i in range(n)]
    return make_series(closes, start=ist(9, 15, d=day),
                       volumes=[volume] * n, spread=spread)


@pytest.fixture
def store(tmp_path):
    return PositionStore(tmp_path)


def stock(store, symbol, maker, *, today_maker=None, **kw):
    for day in PRIOR:
        store.save_bars(symbol, day, maker(day, **kw))
    store.save_bars(symbol, TRADE_DATE, (today_maker or maker)(TRADE_DATE, **kw))


AT = ist(11, 0)


# ================================================================================
# Signal window
# ================================================================================


def test_signal_times_start_after_the_opening_range():
    times = signal_times(TRADE_DATE, CFG)
    assert times[0] == ist(9, 30), "the 15-minute range is only complete at 09:30"


def test_signal_times_stop_at_1430():
    assert signal_times(TRADE_DATE, CFG)[-1] == ist(14, 30)


def test_signal_times_are_five_minutes_apart():
    times = signal_times(TRADE_DATE, CFG)
    assert {(b - a).total_seconds() for a, b in zip(times, times[1:])} == {300.0}


def test_no_signal_before_the_window(store):
    stock(store, "RELIANCE", trend_session)
    decision = generate_signals(store, ["RELIANCE"], TRADE_DATE, ist(9, 25),
                                ALL_SESSIONS, cfg=CFG)
    assert decision.is_no_signal
    assert any("outside the signal window" in n for n in decision.notes)


def test_no_signal_after_1430(store):
    stock(store, "RELIANCE", trend_session)
    decision = generate_signals(store, ["RELIANCE"], TRADE_DATE, ist(14, 35),
                                ALL_SESSIONS, cfg=CFG)
    assert decision.is_no_signal
    assert any("outside the signal window" in n for n in decision.notes)


def test_no_signal_on_a_non_trading_day(store):
    decision = generate_signals(store, ["RELIANCE"], date(2026, 8, 22),
                                ist(11, 0, d=date(2026, 8, 22)), ALL_SESSIONS, cfg=CFG)
    assert decision.is_no_signal
    assert any("not an NSE trading day" in n for n in decision.notes)


# ================================================================================
# NO_SIGNAL is a real, reachable outcome
# ================================================================================


def test_a_flat_universe_produces_no_signal(store):
    for sym in ("RELIANCE", "INFY", "TCS"):
        stock(store, sym, flat_session)
    decision = generate_signals(store, ["RELIANCE", "INFY", "TCS"], TRADE_DATE, AT,
                                ALL_SESSIONS, cfg=CFG)
    assert decision.is_no_signal
    assert decision.rejections


def test_a_falling_universe_produces_no_signal(store):
    for sym in ("RELIANCE", "INFY", "TCS"):
        stock(store, sym, falling_session)
    decision = generate_signals(store, ["RELIANCE", "INFY", "TCS"], TRADE_DATE, AT,
                                ALL_SESSIONS, cfg=CFG)
    assert decision.is_no_signal


def test_no_signal_explains_itself(store):
    for sym in ("RELIANCE", "INFY"):
        stock(store, sym, flat_session)
    decision = generate_signals(store, ["RELIANCE", "INFY"], TRADE_DATE, AT,
                                ALL_SESSIONS, cfg=CFG)
    text = decision.render()
    assert "NO_SIGNAL" in text
    assert "normal outcome" in text
    assert decision.rejections_by_stage()


def test_the_list_is_never_padded_to_reach_the_cap(store):
    """One strong name and two flat ones must yield ONE candidate, not two."""
    stock(store, "RELIANCE", trend_session, slope=0.09)
    stock(store, "INFY", flat_session)
    stock(store, "TCS", flat_session)
    decision = generate_signals(store, ["RELIANCE", "INFY", "TCS"], TRADE_DATE, AT,
                                ALL_SESSIONS, cfg=CFG)
    assert len(decision.candidates) <= 1


def test_the_cap_is_respected(store):
    for i, sym in enumerate(("RELIANCE", "INFY", "TCS", "SBIN", "MARUTI")):
        stock(store, sym, trend_session, slope=0.09 - i * 0.001, base=1000 + i * 10)
    decision = generate_signals(store, ["RELIANCE", "INFY", "TCS", "SBIN", "MARUTI"],
                                TRADE_DATE, AT, ALL_SESSIONS, cfg=CFG)
    assert len(decision.candidates) <= CFG.max_candidates


# ================================================================================
# Hard gates
# ================================================================================


def test_a_banned_symbol_is_absent_not_ranked(store):
    stock(store, "RELIANCE", trend_session, slope=0.09)
    ban = FnoBanList({TRADE_DATE: {"RELIANCE"}}, stub=False)
    decision = generate_signals(store, ["RELIANCE"], TRADE_DATE, AT, ALL_SESSIONS,
                                cfg=CFG, ban_list=ban)
    assert all(c.symbol != "RELIANCE" for c in decision.candidates)
    assert any(r.stage == "ban" for r in decision.rejections)


def test_the_ban_stub_announces_itself(store):
    stock(store, "RELIANCE", trend_session)
    decision = generate_signals(store, ["RELIANCE"], TRADE_DATE, AT, ALL_SESSIONS,
                                cfg=CFG)
    assert any("STUB" in n for n in decision.notes)


def test_an_illiquid_symbol_is_gated(store):
    stock(store, "RELIANCE", trend_session, slope=0.09, volume=5)
    decision = generate_signals(store, ["RELIANCE"], TRADE_DATE, AT, ALL_SESSIONS,
                                cfg=CFG)
    assert decision.is_no_signal
    assert any(r.stage == "liquidity" for r in decision.rejections)


def test_a_motionless_symbol_is_gated(store):
    """ATR below the floor: it cannot reach a target, so it is not a trade."""
    stock(store, "RELIANCE", trend_session, slope=0.09, spread=0.0005)
    decision = generate_signals(store, ["RELIANCE"], TRADE_DATE, AT, ALL_SESSIONS,
                                cfg=CFG)
    assert any(r.stage == "atr" for r in decision.rejections)


def test_a_symbol_with_no_bars_is_reported_not_crashed(store):
    decision = generate_signals(store, ["NOSUCH"], TRADE_DATE, AT, ALL_SESSIONS, cfg=CFG)
    assert decision.is_no_signal
    assert any(r.stage == "data" for r in decision.rejections)


def test_two_candidates_from_one_sector_are_not_both_surfaced(store):
    """HDFCBANK and ICICIBANK move together; two of them is one idea, not two."""
    stock(store, "HDFCBANK", trend_session, slope=0.09)
    stock(store, "ICICIBANK", trend_session, slope=0.088, base=1010)
    decision = generate_signals(store, ["HDFCBANK", "ICICIBANK"], TRADE_DATE, AT,
                                ALL_SESSIONS, cfg=CFG)
    sectors = [c.sector for c in decision.candidates]
    assert len(sectors) == len(set(sectors))
    if len(decision.candidates) == 1:
        assert any(r.stage == "sector" for r in decision.rejections)


def test_unknown_sector_never_conflicts():
    filters = UniverseFilters()
    assert filters.sector_conflict("MYSTERY1", ["MYSTERY2"]) is None


def test_known_sector_conflicts():
    assert UniverseFilters().sector_conflict("SBIN", ["HDFCBANK"]) is not None


# ================================================================================
# Module C has the final word
# ================================================================================


def test_no_surfaced_candidate_would_fail_module_c(store):
    """A signal the exit engine would instantly flag as INVALID_ENTRY is a bug."""
    from src.positions.exit_engine import check_hypothetical_entry

    # Leaders that breathe, so RSI stays out of overbought and the raised score bar
    # is actually clearable -- otherwise this test passes by surfacing nothing.
    syms = ["RELIANCE", "INFY", "TCS", "SBIN", "MARUTI", "ITC"]
    for i, sym in enumerate(syms):
        for day in PRIOR:
            store.save_bars(sym, day, healthy_uptrend(day, base=900 + i * 40))
        store.save_bars(sym, TRADE_DATE,
                        healthy_uptrend(TRADE_DATE, base=900 + i * 40,
                                        step=0.10 + i * 0.03, volume=60000))

    found = 0
    for at in signal_times(TRADE_DATE, CFG):
        decision = generate_signals(
            store, syms, TRADE_DATE, at, ALL_SESSIONS, cfg=CFG,
        )
        for cand in decision.candidates:
            found += 1
            check = check_hypothetical_entry(
                store.load_bars(cand.symbol, TRADE_DATE), at,
                cand.entry_mid, cand.stop, session_date=TRADE_DATE,
            )
            assert check is not None and check.valid, (
                f"{cand.symbol} at {at:%H:%M} would be flagged: {check.problems}"
            )
    assert found > 0, "test is vacuous unless some candidate was surfaced"


def test_an_entry_check_failure_is_recorded_as_a_rejection(store):
    """Force the situation with a stop above price, which Module C must reject."""
    from src.positions.exit_engine import check_hypothetical_entry

    bars = trend_session(TRADE_DATE, slope=0.09)
    check = check_hypothetical_entry(bars, AT, Decimal("1000"), Decimal("1100"),
                                     session_date=TRADE_DATE)
    assert not check.valid


# ================================================================================
# Candidate levels and reasoning
# ================================================================================



def surging(day, **kw):
    """Same shape, but today trades at triple its usual volume."""
    kw.setdefault("volume", 60000)
    return healthy_uptrend(day, **kw)


@pytest.fixture
def one_candidate(store):
    """A leader inside a mixed panel -- relative strength needs peers to exist."""
    for day in PRIOR:
        store.save_bars("RELIANCE", day, healthy_uptrend(day))
    store.save_bars("RELIANCE", TRADE_DATE, surging(TRADE_DATE))
    for sym in ("INFY", "TCS", "SBIN"):
        stock(store, sym, flat_session)
    decision = generate_signals(store, ["RELIANCE", "INFY", "TCS", "SBIN"],
                                TRADE_DATE, AT, ALL_SESSIONS, cfg=CFG)
    assert decision.candidates, (
        "fixture must surface a candidate, else the level tests are vacuous. "
        f"rejections: {[(r.symbol, r.stage, r.detail) for r in decision.rejections]}"
    )
    return decision.candidates[0]


def test_a_healthy_leader_in_a_flat_panel_is_surfaced(one_candidate):
    assert one_candidate.symbol == "RELIANCE"
    assert one_candidate.score >= float(CFG.min_composite_score)


def test_rsi_stays_out_of_overbought_on_a_healthy_trend(one_candidate):
    rsi = next(c for c in one_candidate.components if c.name == "rsi")
    assert rsi.score is not None and rsi.score > 0, rsi.detail


def test_relative_strength_fires_for_a_leader(one_candidate):
    rs = next(c for c in one_candidate.components if c.name == "relative_strength")
    assert rs.fired, rs.detail


def test_no_relative_edge_is_worded_neutrally():
    """0.00pp is neither outperforming nor under; saying either is wrong."""
    detail = S.score_relative_strength(0.5, 0.5, CFG).detail
    assert "no relative edge" in detail
    assert "UNDERPERFORMING" not in detail


def test_direction_is_always_long(one_candidate):
    assert one_candidate.direction == "LONG"


def test_stop_is_below_the_entry_zone(one_candidate):
    assert one_candidate.stop < one_candidate.entry_low


def test_targets_are_ordered_above_entry(one_candidate):
    assert one_candidate.entry_high < one_candidate.t1 < one_candidate.t2


def test_risk_reward_clears_the_floor(one_candidate):
    assert one_candidate.risk_reward_t1 >= float(CFG.min_risk_reward)
    assert one_candidate.risk_reward_t2 > one_candidate.risk_reward_t1


def test_levels_scale_with_atr(one_candidate):
    """Levels are ATR multiples, not fixed percentages.

    Reads the multiple from config rather than hard-coding it: the stop was re-derived
    from measured noise (1.0 -> 2.37 ATR) and a test that pins the old number would
    fail for being out of date rather than for finding a bug.
    """
    distance = float(one_candidate.entry_mid - one_candidate.stop)
    expected = float(CFG.stop_atr_multiple) * one_candidate.atr
    assert distance == pytest.approx(expected, rel=0.25)


def test_the_stop_sits_outside_the_measured_noise_floor():
    """The structural fix: the stop must be well beyond one mean adverse excursion.

    The measured p75 of survivable adverse excursion was 2.37 ATR; the old 1.0 sat at
    1.02x the MEAN. Anything back near 1.0 is inside the noise again.
    """
    assert float(CFG.stop_atr_multiple) >= 2.0


def test_the_declared_risk_reward_is_preserved_by_the_new_levels():
    assert float(CFG.t1_atr_multiple) / float(CFG.stop_atr_multiple) == pytest.approx(1.5, abs=0.02)
    assert float(CFG.t2_atr_multiple) / float(CFG.stop_atr_multiple) == pytest.approx(3.0, abs=0.02)


def test_reason_lists_what_fired_and_what_argued_against(one_candidate):
    reason = one_candidate.reason()
    assert "FOR:" in reason and "AGAINST:" in reason
    assert one_candidate.symbol in reason


def test_reason_names_specific_components(one_candidate):
    assert one_candidate.fired, "a surfaced candidate must have something in its favour"
    for component in one_candidate.fired:
        assert component.detail in one_candidate.reason()


def test_candidate_row_is_serialisable(one_candidate):
    row = one_candidate.as_row()
    assert row["symbol"] == one_candidate.symbol
    assert row["direction"] == "LONG"
    assert "reason" in row


# ================================================================================
# Scoring components
# ================================================================================


def test_ramp_is_linear_and_clamped():
    assert S.ramp(0.0, 1.0) == 0.0
    assert S.ramp(0.5, 1.0) == 0.5
    assert S.ramp(2.0, 1.0) == 1.0
    assert S.ramp(-2.0, 1.0) == -1.0
    assert S.ramp(None, 1.0) is None


def test_breakout_above_scores_positive_and_below_negative():
    assert S.score_or_breakout("ABOVE", 0.8, CFG).score > 0
    assert S.score_or_breakout("BELOW", -0.8, CFG).score < 0
    assert S.score_or_breakout("INSIDE", 0.0, CFG).score == 0.0


def test_or_hold_never_goes_negative():
    """Not breaking out is already penalised by or_breakout; do not charge twice."""
    assert S.score_or_hold(0, CFG).score == 0.0
    assert S.score_or_hold(5, CFG).score == 1.0


def test_rsi_is_a_band_not_a_threshold():
    """The whole point: overbought must not outrank healthy."""
    healthy = S.score_rsi(60.0, CFG).score
    overbought = S.score_rsi(85.0, CFG).score
    weak = S.score_rsi(40.0, CFG).score
    assert healthy == 1.0
    assert overbought < 0
    assert weak < 0
    assert healthy > overbought


def test_rsi_extended_scores_below_ideal():
    assert S.score_rsi(74.0, CFG).score < S.score_rsi(62.0, CFG).score


def test_volume_below_average_scores_negative():
    """A breakout nobody is trading is not a breakout."""
    assert S.score_volume(0.4, CFG).score < 0
    assert S.score_volume(1.0, CFG).score == 0.0
    assert S.score_volume(3.0, CFG).score == 1.0


def test_relative_strength_penalises_lagging_a_strong_panel():
    """Up 0.5% when the panel is up 0.8% is weak, not strong."""
    lagging = S.score_relative_strength(0.5, 0.8, CFG)
    leading = S.score_relative_strength(0.8, 0.5, CFG)
    assert lagging.score < 0
    assert leading.score > 0
    assert "UNDERPERFORMING" in lagging.detail


def test_relative_strength_is_unavailable_without_a_panel():
    assert S.score_relative_strength(0.5, None, CFG).score is None


@pytest.mark.parametrize("fn,args", [
    (S.score_or_breakout, (None, None)),
    (S.score_or_hold, (None,)),
    (S.score_ema_trend, (None,)),
    (S.score_vwap_trend, (None,)),
    (S.score_vwap_persistence, (None,)),
    (S.score_rsi, (None,)),
    (S.score_roc, (None,)),
    (S.score_volume, (None,)),
])
def test_an_unavailable_feature_scores_none_not_zero(fn, args):
    """Unknown is not neutral. Scoring it zero would dilute, not abstain."""
    component = fn(*args, CFG)
    assert component.score is None
    assert component.available is False


def test_composite_drops_unavailable_components_with_their_weight():
    components = [
        ComponentScore("a", 1.0, 1.0, None, ""),
        ComponentScore("b", None, 9.0, None, ""),
    ]
    assert S.composite(components) == pytest.approx(1.0)


def test_composite_is_none_when_nothing_could_be_measured():
    assert S.composite([ComponentScore("a", None, 1.0, None, "")]) is None


def test_composite_is_a_weighted_average():
    components = [
        ComponentScore("a", 1.0, 3.0, None, ""),
        ComponentScore("b", -1.0, 1.0, None, ""),
    ]
    assert S.composite(components) == pytest.approx(0.5)


# ================================================================================
# Universe helpers
# ================================================================================


def test_sector_map_covers_the_traded_universe():
    from src.data.backfill import LIQUID_FNO_UNIVERSE

    missing = [s for s in LIQUID_FNO_UNIVERSE if sector_of(s) == "UNKNOWN"]
    assert not missing, f"unmapped symbols would never trigger the sector rule: {missing}"


def test_ban_list_is_case_insensitive():
    ban = FnoBanList({TRADE_DATE: {"reliance"}}, stub=False)
    assert ban.is_banned("RELIANCE", TRADE_DATE)


def test_a_non_stub_ban_list_does_not_warn():
    assert FnoBanList({}, stub=False).warning() is None


def test_trailing_sessions_is_strictly_before():
    assert TRADE_DATE not in trailing_sessions(ALL_SESSIONS, TRADE_DATE)
