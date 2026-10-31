"""Tests for corporate-action discontinuity detection.

The property that matters most is what this module does NOT do: it never adjusts a
price series. Inferring a ratio from a jump and then dividing by it always produces a
smooth line, whether or not a corporate action happened — that is laundering, not
cleaning.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.data.corporate_actions import (
    COMMON_ACTIONS,
    DEFAULT_THRESHOLD_PCT,
    Discontinuity,
    group_by_cause,
    infer_action,
    scan_panel,
    scan_symbol,
)
from src.positions.store import PositionStore
from tests.conftest import flat_series, ist, make_series

D1, D2, D3 = date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)


@pytest.fixture
def store(tmp_path) -> PositionStore:
    return PositionStore(tmp_path)


def session_at(price: float, day: date, *, count: int = 30):
    """A flat session at ``price`` on ``day``."""
    start = ist(9, 15, d=day)
    return flat_series(price, count, start=start, spread=0.5)


# --------------------------------------------------------------------------------
# infer_action
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("ratio,expected_fragment", [
    (0.50, "1:2 split"), (0.3333, "1:3 split"), (0.20, "1:5 split"),
    (0.10, "1:10 split"), (0.6667, "1:2 bonus"), (2.0, "reverse split"),
])
def test_common_ratios_are_recognised(ratio, expected_fragment):
    assert expected_fragment in infer_action(ratio)


def test_an_ordinary_crash_is_not_labelled_a_corporate_action():
    """-25% is a bad day, not a split. It must stay UNEXPLAINED."""
    assert infer_action(0.75) is not None  # 1:3 bonus is a real 0.75
    assert infer_action(0.72) is None
    assert infer_action(0.60) is None


def test_inference_tolerates_small_deviation():
    """A split rarely lands exactly on the ratio -- the ex-date open moves too.

    The band is 2% RELATIVE, so around a 0.5 ratio it spans roughly 0.490..0.510.
    """
    assert infer_action(0.505) is not None
    assert infer_action(0.495) is not None
    assert infer_action(0.470) is None, "3 sigma past the band is a price move, not a split"


def test_every_common_action_is_self_consistent():
    for ratio, label in COMMON_ACTIONS:
        assert infer_action(ratio) is not None, label


# --------------------------------------------------------------------------------
# Overnight gaps
# --------------------------------------------------------------------------------


def test_a_two_for_one_split_is_detected(store):
    store.save_bars("SPLITCO", D1, session_at(1000.0, D1))
    store.save_bars("SPLITCO", D2, session_at(500.0, D2))
    hits = scan_symbol(store, "SPLITCO", [D1, D2])
    gaps = [h for h in hits if h.kind == "OVERNIGHT_GAP"]
    assert len(gaps) == 1
    assert gaps[0].pct_change == pytest.approx(-50.0, abs=0.2)
    assert "1:2 split" in gaps[0].likely_action
    assert gaps[0].explained is True


def test_a_normal_overnight_move_is_not_reported(store):
    store.save_bars("CALM", D1, session_at(1000.0, D1))
    store.save_bars("CALM", D2, session_at(1030.0, D2))
    assert scan_symbol(store, "CALM", [D1, D2]) == []


def test_a_large_unexplained_gap_is_reported_as_unexplained(store):
    """-35% overnight matches no common ratio. A human has to look."""
    store.save_bars("MYSTERY", D1, session_at(1000.0, D1))
    store.save_bars("MYSTERY", D2, session_at(650.0, D2))
    hits = scan_symbol(store, "MYSTERY", [D1, D2])
    assert len(hits) == 1
    assert hits[0].likely_action is None
    assert hits[0].explained is False
    assert "UNEXPLAINED" in hits[0].describe()


def test_threshold_is_configurable(store):
    store.save_bars("X", D1, session_at(1000.0, D1))
    store.save_bars("X", D2, session_at(900.0, D2))
    assert scan_symbol(store, "X", [D1, D2]) == []
    assert len(scan_symbol(store, "X", [D1, D2], threshold_pct=5.0)) == 1


def test_gap_spans_a_missing_session(store):
    """A hole in the data is not itself a discontinuity, but a split inside it shows."""
    store.save_bars("GAPCO", D1, session_at(1000.0, D1))
    store.save_bars("GAPCO", D3, session_at(500.0, D3))
    hits = scan_symbol(store, "GAPCO", [D1, D2, D3])
    assert len(hits) == 1
    assert hits[0].prev_date == D1 and hits[0].next_date == D3


def test_a_missing_session_alone_is_not_a_discontinuity(store):
    store.save_bars("GAPCO", D1, session_at(1000.0, D1))
    store.save_bars("GAPCO", D3, session_at(1005.0, D3))
    assert scan_symbol(store, "GAPCO", [D1, D2, D3]) == []


# --------------------------------------------------------------------------------
# Intraday bars
# --------------------------------------------------------------------------------


def test_an_intraday_spike_is_detected(store):
    bars = make_series([100, 101, 160, 102], start=ist(9, 15, d=D1))
    store.save_bars("SPIKE", D1, bars)
    hits = scan_symbol(store, "SPIKE", [D1])
    assert [h.kind for h in hits] == ["INTRADAY_BAR", "INTRADAY_BAR"]
    assert hits[0].pct_change > 20


def test_intraday_scanning_can_be_disabled(store):
    store.save_bars("SPIKE", D1, make_series([100, 160], start=ist(9, 15, d=D1)))
    assert scan_symbol(store, "SPIKE", [D1], check_intraday=False) == []


def test_a_calm_session_produces_nothing(store):
    store.save_bars("CALM", D1, session_at(1000.0, D1))
    assert scan_symbol(store, "CALM", [D1]) == []


# --------------------------------------------------------------------------------
# Panel scan and grouping
# --------------------------------------------------------------------------------


def test_scan_panel_covers_every_symbol(store):
    store.save_bars("A", D1, session_at(1000.0, D1))
    store.save_bars("A", D2, session_at(500.0, D2))
    store.save_bars("B", D1, session_at(800.0, D1))
    store.save_bars("B", D2, session_at(160.0, D2))
    hits = scan_panel(store, ["A", "B"], [D1, D2])
    assert {h.symbol for h in hits} == {"A", "B"}


def test_scan_panel_on_a_clean_store_is_empty(store):
    for day in (D1, D2):
        store.save_bars("A", day, session_at(1000.0, day))
    assert scan_panel(store, ["A"], [D1, D2]) == []


def test_grouping_puts_unexplained_first(store):
    hits = [
        Discontinuity("A", "OVERNIGHT_GAP", ist(9, 15), D1, D2, 100, 50, -50, 0.5,
                      "1:2 split, or 1:1 bonus"),
        Discontinuity("B", "OVERNIGHT_GAP", ist(9, 15), D1, D2, 100, 65, -35, 0.65, None),
    ]
    rows = group_by_cause(hits)
    assert rows[0]["likely_action"] == "UNEXPLAINED"


def test_grouping_counts_and_lists_symbols():
    hits = [
        Discontinuity(s, "OVERNIGHT_GAP", ist(9, 15), D1, D2, 100, 50, -50, 0.5,
                      "1:2 split, or 1:1 bonus")
        for s in ("A", "B")
    ]
    rows = group_by_cause(hits)
    assert rows[0]["n"] == 2
    assert rows[0]["symbols"] == "A, B"


# --------------------------------------------------------------------------------
# It must never adjust
# --------------------------------------------------------------------------------


def test_scanning_does_not_modify_stored_bars(store):
    original = session_at(1000.0, D1)
    store.save_bars("SPLITCO", D1, original)
    store.save_bars("SPLITCO", D2, session_at(500.0, D2))
    scan_symbol(store, "SPLITCO", [D1, D2])
    assert store.load_bars("SPLITCO", D1) == original
    assert store.load_bars("SPLITCO", D2)[0].close == pytest.approx(500.0)


def test_module_exposes_no_adjustment_function():
    """Adjusting needs the official ratio and ex-date, not a ratio read off the jump."""
    import src.data.corporate_actions as mod

    for name in dir(mod):
        assert "adjust" not in name.lower(), f"{name} looks like an adjuster"
