"""Tests for the entry-check diagnostic.

The machinery has to be right before its numbers mean anything, and the failure modes
are all silent: a look-ahead leak, a prose-based classification that drifts, a beta
effect reported as selection skill, or an interval computed on 20 correlated readings
as if they were 20 draws.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from config.settings import get_exit_engine_settings
from src.analysis.entry_check import (
    FORWARD_MINUTES,
    SAMPLE_TIMES,
    EntryObservation,
    combination_breakdown,
    demean_by_slot,
    forward_comparison,
    observe_session,
    problem_breakdown,
    verdict_distribution,
    vwap_deviation_profile,
)
from src.market.calendar import IST, square_off_time
from src.positions.exit_engine import (
    EntryProblem,
    entry_findings,
    evaluate_entry_conditions,
)
from src.positions.indicators import compute_indicators
from src.positions.models import Bar
from tests.conftest import TRADE_DATE, ist, make_series

CFG = get_exit_engine_settings()


def rising(n=375, start=1000.0, step=0.5):
    return make_series([start + i * step for i in range(n)],
                       start=ist(9, 15), volumes=[10000] * n, spread=1.0)


def falling(n=375, start=1000.0, step=0.5):
    return make_series([start - i * step for i in range(n)],
                       start=ist(9, 15), volumes=[10000] * n, spread=1.0)


def obs(*, valid=True, codes=(), forward=0.0, session=TRADE_DATE, slot="11:00",
        symbol="X", dev_pct=None, dev_atr=None, minutes=FORWARD_MINUTES):
    return EntryObservation(
        symbol=symbol, session=session, slot=slot,
        asof=datetime.combine(session, time(11, 0), tzinfo=IST),
        valid=valid, codes=tuple(codes), unevaluated=(), price=100.0,
        vwap=100.0, atr14=1.0, vwap_dev_pct=dev_pct, vwap_dev_atr=dev_atr,
        forward_pct=forward, forward_minutes=minutes,
    )


# --------------------------------------------------------------------------------
# Tags, not prose
# --------------------------------------------------------------------------------


def test_codes_and_prose_always_agree():
    """One implementation. A second copy of these predicates would drift silently."""
    for bars in (rising(), falling()):
        for at in (ist(9, 45), ist(11, 0), ist(13, 0)):
            from src.positions.models import completed_bars

            snap = compute_indicators(completed_bars(bars, at), session_date=TRADE_DATE)
            findings, unevaluated = entry_findings(
                Decimal(str(snap.last_price)), None, snap, CFG)
            problems, unevaluated2 = evaluate_entry_conditions(
                Decimal(str(snap.last_price)), None, snap, CFG)
            assert tuple(p for _, p in findings) == problems
            assert unevaluated == unevaluated2


def test_every_code_is_carried_on_the_entry_check():
    from src.positions.exit_engine import check_hypothetical_entry

    check = check_hypothetical_entry(falling(), ist(11, 0), session_date=TRADE_DATE)
    assert not check.valid
    assert len(check.codes) == len(check.problems)
    assert EntryProblem.BELOW_VWAP in check.codes


def test_the_diagnostic_never_classifies_by_reading_the_prose():
    """CLAUDE.md 2.2: reword a message and every aggregate changes silently."""
    import inspect

    import src.analysis.entry_check as mod

    source = inspect.getsource(mod)
    for phrase in ("already below", "was already", "bought into", ".problems"):
        assert phrase not in source, f"classification by prose: {phrase!r}"


# --------------------------------------------------------------------------------
# Look-ahead
# --------------------------------------------------------------------------------


def test_a_slot_sees_no_bar_that_has_not_closed():
    """The bar containing the sample instant is still forming."""
    bars = rising()
    for o in observe_session("X", TRADE_DATE, bars, slots=(time(11, 0),)):
        assert o.asof <= ist(11, 0)
        assert o.asof == ist(11, 0), "the 10:59 bar closes exactly at 11:00"


def test_the_verdict_does_not_move_when_the_future_is_deleted():
    """The poison test in miniature: truncating later bars must change nothing."""
    bars = rising()
    at = time(11, 0)
    full = observe_session("X", TRADE_DATE, bars, slots=(at,))[0]
    truncated_bars = [b for b in bars if b.ts < ist(11, 0)]
    short = observe_session("X", TRADE_DATE, truncated_bars, slots=(at,))[0]

    assert full.valid == short.valid
    assert full.codes == short.codes
    assert full.price == pytest.approx(short.price)
    assert full.vwap == pytest.approx(short.vwap)
    assert short.forward_pct is None, "no future bars means no forward return"
    assert full.forward_pct is not None


def test_the_forward_return_reads_only_bars_after_the_decision():
    bars = rising()
    o = observe_session("X", TRADE_DATE, bars, slots=(time(11, 0),))[0]
    at_price = o.price
    later = [b for b in bars if b.ts + timedelta(minutes=1) <= ist(12, 0)][-1]
    assert o.forward_pct == pytest.approx(100.0 * (later.close - at_price) / at_price)


# --------------------------------------------------------------------------------
# The square-off cap
# --------------------------------------------------------------------------------


def test_the_forward_window_is_capped_at_the_square_off():
    """A 60-minute hold from 14:30 ends at 15:30, which this system never holds."""
    bars = rising()
    late = observe_session("X", TRADE_DATE, bars, slots=(time(14, 30),))[0]
    assert late.forward_minutes == 45
    assert square_off_time(TRADE_DATE).time() == time(15, 15)

    early = observe_session("X", TRADE_DATE, bars, slots=(time(11, 0),))[0]
    assert early.forward_minutes == FORWARD_MINUTES


def test_the_sample_slots_do_not_overlap_their_own_forward_windows():
    """Otherwise one symbol's observations are not independent of each other."""
    gaps = [
        (datetime.combine(TRADE_DATE, b, tzinfo=IST)
         - datetime.combine(TRADE_DATE, a, tzinfo=IST)).total_seconds() / 60
        for a, b in zip(SAMPLE_TIMES, SAMPLE_TIMES[1:])
    ]
    assert all(g >= FORWARD_MINUTES for g in gaps), gaps


# --------------------------------------------------------------------------------
# Distributions
# --------------------------------------------------------------------------------


def test_a_falling_session_is_unsound_and_a_rising_one_is_sound():
    """Sanity: the check must at least respond to the obvious cases."""
    up = observe_session("X", TRADE_DATE, rising())
    down = observe_session("X", TRADE_DATE, falling())
    assert all(o.valid for o in up)
    assert not any(o.valid for o in down)
    assert all(EntryProblem.BELOW_VWAP in o.fired for o in down)


def test_verdict_distribution_counts_both_sides():
    rows = verdict_distribution([obs(valid=True), obs(valid=False), obs(valid=False)])
    assert rows[0]["n"] == 3 and rows[0]["sound"] == 1 and rows[0]["unsound"] == 2
    assert rows[0]["sound_pct"] == pytest.approx(33.33, abs=0.01)


def test_problem_breakdown_separates_present_from_sole_cause():
    both = obs(valid=False, codes=(EntryProblem.BELOW_VWAP,
                                   EntryProblem.MOMENTUM_REVERSED))
    only_vwap = obs(valid=False, codes=(EntryProblem.BELOW_VWAP,))
    rows = {r["problem"]: r for r in problem_breakdown([both, only_vwap])}

    assert rows["BELOW_VWAP"]["present_n"] == 2
    assert rows["BELOW_VWAP"]["sole_cause_n"] == 1
    assert rows["MOMENTUM_REVERSED"]["present_n"] == 1
    assert rows["MOMENTUM_REVERSED"]["sole_cause_n"] == 0


def test_a_predicate_that_never_fires_is_still_reported():
    """A zero here is the finding, not an empty row to drop."""
    rows = {r["problem"]: r for r in
            problem_breakdown([obs(valid=False, codes=(EntryProblem.BELOW_VWAP,))])}
    assert "STOP_ALREADY_HIT" in rows
    assert rows["STOP_ALREADY_HIT"]["present_n"] == 0


def test_the_stop_predicate_cannot_fire_without_a_stop():
    """The structural claim the report makes, pinned."""
    from src.positions.models import completed_bars

    snap = compute_indicators(completed_bars(falling(), ist(11, 0)),
                              session_date=TRADE_DATE)
    findings, _ = entry_findings(Decimal(str(snap.last_price)), None, snap, CFG)
    assert EntryProblem.STOP_ALREADY_HIT not in {c for c, _ in findings}

    # And with a stop below the current price, it still cannot fire.
    below = Decimal(str(snap.last_price - 10))
    findings, _ = entry_findings(Decimal(str(snap.last_price)), below, snap, CFG)
    assert EntryProblem.STOP_ALREADY_HIT not in {c for c, _ in findings}

    # It fires only on a stop the user placed at or above the current price.
    above = Decimal(str(snap.last_price + 10))
    findings, _ = entry_findings(Decimal(str(snap.last_price)), above, snap, CFG)
    assert EntryProblem.STOP_ALREADY_HIT in {c for c, _ in findings}


def test_combination_breakdown_shows_overlap():
    rows = combination_breakdown([
        obs(valid=False, codes=(EntryProblem.BELOW_VWAP,
                                EntryProblem.MOMENTUM_REVERSED)),
        obs(valid=False, codes=(EntryProblem.BELOW_VWAP,)),
    ])
    combos = {r["combination"]: r["n"] for r in rows}
    assert combos["BELOW_VWAP + MOMENTUM_REVERSED"] == 1
    assert combos["BELOW_VWAP"] == 1


def test_vwap_deviation_reports_a_distribution_not_a_count():
    fired = [
        obs(valid=False, codes=(EntryProblem.BELOW_VWAP,),
            dev_pct=-d, dev_atr=-d)
        for d in (0.05, 0.10, 0.20, 0.40, 0.80)
    ]
    profile = vwap_deviation_profile(fired)
    assert profile["n"] == 5
    assert profile["pct_below_vwap"]["p50"] == pytest.approx(0.20)
    assert profile["within_0.50pct"] == pytest.approx(80.0)
    assert profile["within_0.10pct"] == pytest.approx(20.0)


def test_deviation_ignores_verdicts_that_fired_for_another_reason():
    momentum_only = obs(valid=False, codes=(EntryProblem.MOMENTUM_REVERSED,),
                        dev_pct=5.0, dev_atr=5.0)
    assert vwap_deviation_profile([momentum_only])["n"] == 0


# --------------------------------------------------------------------------------
# The comparison, and the beta trap
# --------------------------------------------------------------------------------


def test_demeaning_removes_a_pure_market_move():
    """Every symbol falls 1% and the verdict is uninformative -- diff must be zero."""
    day = TRADE_DATE
    group = ([obs(valid=True, forward=-1.0, session=day, symbol=f"S{i}")
              for i in range(5)]
             + [obs(valid=False, forward=-1.0, session=day, symbol=f"T{i}")
                for i in range(5)])
    excess = demean_by_slot(group)
    assert all(v == pytest.approx(0.0) for v in excess.values())

    diff = forward_comparison(group, label="t")
    assert diff.diff == pytest.approx(0.0)


def test_the_raw_figure_can_show_a_gap_where_the_demeaned_one_does_not():
    """The swing experiment's lesson, in one test: gross was all beta.

    SOUND fires on the up day, UNSOUND on the down day. Within each day every symbol
    moves identically, so the check selected nothing -- but the raw comparison sees a
    2pp gap purely from which days it fired on.
    """
    up = [obs(valid=True, forward=+1.0, session=date(2024, 1, 2), symbol=f"S{i}")
          for i in range(5)]
    down = [obs(valid=False, forward=-1.0, session=date(2024, 1, 3), symbol=f"S{i}")
            for i in range(5)]

    raw = forward_comparison(up + down, label="raw", demeaned=False)
    demeaned = forward_comparison(up + down, label="demeaned")

    assert raw.diff == pytest.approx(2.0), "the raw view sees a large gap"
    assert demeaned.diff == pytest.approx(0.0), "and it is entirely the market's move"


def test_demeaning_keeps_a_real_selection_effect():
    """The other half: if SOUND really does pick the better symbol, it must survive."""
    day = TRADE_DATE
    group = ([obs(valid=True, forward=+1.0, session=day, symbol=f"S{i}")
              for i in range(5)]
             + [obs(valid=False, forward=-1.0, session=day, symbol=f"T{i}")
                for i in range(5)])
    assert forward_comparison(group, label="t").diff == pytest.approx(2.0)


def test_a_single_symbol_cell_is_dropped():
    """A cross-section of one has no cross-section to subtract."""
    lonely = [obs(valid=True, forward=5.0, session=TRADE_DATE)]
    assert demean_by_slot(lonely) == {}


def test_intervals_use_independent_cells_not_observation_count():
    """20 symbols in one minute are one draw with 20 readings, not 20 draws."""
    day = TRADE_DATE
    group = ([obs(valid=True, forward=1.0 + 0.01 * i, session=day, symbol=f"S{i}")
              for i in range(20)]
             + [obs(valid=False, forward=-1.0 - 0.01 * i, session=day, symbol=f"T{i}")
                for i in range(20)])
    diff = forward_comparison(group, label="t")

    assert diff.effective_n == 1, "one session, one slot"
    assert diff.effective_stderr > diff.stderr or diff.effective_stderr == float("inf")


def test_a_gap_smaller_than_the_mde_is_called_an_absence_of_measurement():
    """"We measured +0.02 but could only detect +0.15" is a fact about the sample."""
    day_count = 40
    group = []
    for d in range(day_count):
        session = date(2024, 1, 1) + timedelta(days=d)
        for i in range(10):
            group.append(obs(valid=True, forward=(i - 5) * 1.0 + 0.001,
                             session=session, symbol=f"S{i}"))
            group.append(obs(valid=False, forward=(i - 5) * 1.0,
                             session=session, symbol=f"T{i}"))
    diff = forward_comparison(group, label="t")
    verdict = diff.verdict()
    assert "INDISTINGUISHABLE" in verdict
    assert "absence of measurement" in verdict


def test_no_claim_is_made_on_a_tiny_sample():
    group = [obs(valid=True, forward=1.0, symbol="A"),
             obs(valid=False, forward=-1.0, symbol="B")]
    assert "NO CLAIM POSSIBLE" in forward_comparison(group, label="t").verdict()


# --------------------------------------------------------------------------------
# The script refuses to fabricate
# --------------------------------------------------------------------------------


def test_the_diagnostic_refuses_to_run_on_synthetic_bars():
    import inspect
    from pathlib import Path

    source = Path("scripts/entry_check_diagnostic.py").read_text(encoding="utf-8")
    assert "synthetic_session" not in source.replace(
        "`synthetic_session()` describes", "")
    assert "NO DATA YET" in source


def test_the_diagnostic_asserts_dev_only():
    from pathlib import Path

    source = Path("scripts/entry_check_diagnostic.py").read_text(encoding="utf-8")
    assert "assert_dev_only" in source
    assert "guard.claim" not in source, "a diagnostic must not spend the holdout"
