"""Tests for the DEV/HOLDOUT split and the holdout guard.

The guard is the point. A split you can peek at freely is not a split, and the
realistic failure is not malice -- it is passing the full session list to a sweep out
of habit, or re-running the script with one parameter nudged.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from config.settings import ExitEngineSettings
from decimal import Decimal
from src.analysis.split import (
    DevHoldoutSplit,
    HoldoutGuard,
    HoldoutLeakError,
    HoldoutSpentError,
    config_fingerprint,
)

SESSIONS = [date(2026, 2, 16) + timedelta(days=i) for i in range(125)]


@pytest.fixture
def split() -> DevHoldoutSplit:
    return DevHoldoutSplit.chronological(SESSIONS, 0.8)


@pytest.fixture
def guard(split, tmp_path) -> HoldoutGuard:
    return HoldoutGuard(split, tmp_path / "holdout_claim.json")


# --------------------------------------------------------------------------------
# The split
# --------------------------------------------------------------------------------


def test_split_is_80_20(split):
    assert len(split.dev) == 100
    assert len(split.holdout) == 25


def test_holdout_is_strictly_later_than_every_dev_session(split):
    """The only arrangement that answers 'would this have worked next?'."""
    assert max(split.dev) < min(split.holdout)


def test_split_is_chronological_not_shuffled(split):
    assert list(split.dev) == sorted(split.dev)
    assert list(split.holdout) == sorted(split.holdout)
    assert list(split.dev) + list(split.holdout) == sorted(SESSIONS)


def test_split_covers_every_session_exactly_once(split):
    combined = list(split.dev) + list(split.holdout)
    assert len(combined) == len(SESSIONS)
    assert set(combined) == set(SESSIONS)


def test_split_deduplicates_and_sorts_input():
    messy = [date(2026, 3, 2), date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)]
    s = DevHoldoutSplit.chronological(messy, 0.5)
    assert s.dev == (date(2026, 3, 1),)
    assert s.holdout == (date(2026, 3, 2), date(2026, 3, 3))


def test_boundary_is_the_first_holdout_session(split):
    assert split.boundary == split.holdout[0]
    assert split.is_holdout(split.boundary) is True
    assert split.is_holdout(split.dev[-1]) is False


@pytest.mark.parametrize("fraction", [0.0, 1.0, -0.1, 1.5])
def test_invalid_fraction_rejected(fraction):
    with pytest.raises(ValueError):
        DevHoldoutSplit.chronological(SESSIONS, fraction)


@pytest.mark.parametrize(
    "sessions,fraction",
    [
        ([date(2026, 3, 1)], 0.8),                      # DEV rounds to zero
        ([date(2026, 3, 1), date(2026, 3, 2)], 0.1),    # DEV rounds to zero
    ],
)
def test_split_refuses_to_leave_dev_empty(sessions, fraction):
    with pytest.raises(ValueError, match="one side is empty"):
        DevHoldoutSplit.chronological(sessions, fraction)


def test_holdout_can_never_round_to_empty():
    """int(n*f) < n for every f < 1, so the holdout always keeps at least one day."""
    for n in (2, 10, 125, 1000):
        sessions = [date(2026, 3, 1) + timedelta(days=i) for i in range(n)]
        assert len(DevHoldoutSplit.chronological(sessions, 0.999).holdout) >= 1


def test_describe_names_both_ranges(split):
    text = split.describe()
    assert "DEV" in text and "HOLDOUT" in text and "100 sessions" in text


# --------------------------------------------------------------------------------
# Leak guard
# --------------------------------------------------------------------------------


def test_dev_sessions_are_freely_available(guard, split):
    assert guard.dev == list(split.dev)


def test_passing_holdout_to_a_dev_only_function_raises(guard, split):
    with pytest.raises(HoldoutLeakError, match="HOLDOUT session"):
        guard.assert_dev_only(list(split.dev) + list(split.holdout), "the trail sweep")


def test_leak_error_names_the_caller_and_the_boundary(guard, split):
    with pytest.raises(HoldoutLeakError) as exc:
        guard.assert_dev_only(list(split.holdout), "exit_rule_distribution")
    assert "exit_rule_distribution" in str(exc.value)
    assert str(split.dev[-1]) in str(exc.value)


def test_dev_only_sessions_pass_the_guard(guard, split):
    guard.assert_dev_only(list(split.dev))


def test_empty_session_list_passes(guard):
    guard.assert_dev_only([])


# --------------------------------------------------------------------------------
# One-claim discipline
# --------------------------------------------------------------------------------


def test_holdout_starts_unused(guard):
    assert "UNUSED" in guard.status()
    assert guard.existing_claim() is None


def test_first_claim_returns_the_holdout_sessions(guard, split):
    assert guard.claim(ExitEngineSettings(), "baseline") == list(split.holdout)
    assert "SPENT" in guard.status()


def test_claim_is_recorded_to_disk(guard, tmp_path):
    guard.claim(ExitEngineSettings(), "baseline")
    assert (tmp_path / "holdout_claim.json").exists()
    claim = guard.existing_claim()
    assert claim["note"] == "baseline"
    assert claim["holdout_sessions"] == 25


def test_reclaiming_the_same_config_is_allowed(guard):
    """Re-running an identical evaluation yields no new information."""
    cfg = ExitEngineSettings()
    first = guard.claim(cfg, "baseline")
    second = guard.claim(cfg, "baseline again")
    assert first == second


def test_claiming_a_different_config_is_refused(guard):
    guard.claim(ExitEngineSettings(), "first look")
    with pytest.raises(HoldoutSpentError):
        guard.claim(ExitEngineSettings(trail_atr_multiple=Decimal("3.0")), "second look")


def test_refusal_explains_why_it_matters(guard):
    guard.claim(ExitEngineSettings(), "first")
    with pytest.raises(HoldoutSpentError) as exc:
        guard.claim(ExitEngineSettings(momentum_confirmation_bars=3), "second")
    message = str(exc.value)
    assert "second look" in message
    assert "no longer an out-of-sample result" in message
    assert "Do not do it to get a better number" in message


def test_the_claim_survives_a_new_process(split, tmp_path):
    """The obvious way to defeat an in-memory guard is to run the script again."""
    path = tmp_path / "claim.json"
    HoldoutGuard(split, path).claim(ExitEngineSettings(), "run 1")
    fresh = HoldoutGuard(split, path)
    with pytest.raises(HoldoutSpentError):
        fresh.claim(ExitEngineSettings(trail_atr_multiple=Decimal("0.5")), "run 2")


def test_reset_clears_the_claim(guard):
    guard.claim(ExitEngineSettings(), "first")
    guard.reset()
    assert guard.existing_claim() is None
    guard.claim(ExitEngineSettings(trail_atr_multiple=Decimal("2.0")), "after reset")


def test_corrupt_claim_file_is_treated_as_unused(guard, tmp_path):
    (tmp_path / "holdout_claim.json").write_text("{not json")
    assert guard.existing_claim() is None


# --------------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------------


def test_same_config_same_fingerprint():
    assert config_fingerprint(ExitEngineSettings()) == config_fingerprint(ExitEngineSettings())


def test_different_config_different_fingerprint():
    a = ExitEngineSettings()
    b = ExitEngineSettings(trail_atr_multiple=Decimal("2.0"))
    assert config_fingerprint(a) != config_fingerprint(b)


def test_fingerprint_is_sensitive_to_every_swept_parameter():
    base = ExitEngineSettings()
    for field, value in (
        ("trail_atr_multiple", Decimal("2.5")),
        ("momentum_confirmation_bars", 3),
        ("rsi_reversal_level", Decimal("45")),
        ("time_exit_minutes", 60),
    ):
        assert config_fingerprint(base.model_copy(update={field: value})) \
            != config_fingerprint(base), field


def test_fingerprint_handles_a_plain_dict():
    assert config_fingerprint({"a": 1}) == config_fingerprint({"a": 1})
    assert config_fingerprint({"a": 1}) != config_fingerprint({"a": 2})


# --------------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------------


from src.analysis.split import (  # noqa: E402 - grouped with its own tests
    GLOBAL_EXCLUDED_SESSIONS,
    PANEL_2026_SPENT,
    PANEL_FULL_HISTORY,
    SPENT_REGIONS,
    Panel,
    regime_of,
)


def test_full_history_panel_window():
    assert PANEL_FULL_HISTORY.start == date(2020, 1, 1)
    assert PANEL_FULL_HISTORY.end == date(2026, 2, 15)
    assert PANEL_FULL_HISTORY.dev_fraction == 0.8


def test_full_history_panel_session_count():
    """Matches the calendar reconciliation: 1515 regular sessions in the window."""
    assert len(PANEL_FULL_HISTORY.sessions()) == 1515


def test_panel_split_is_chronological_and_80_20():
    s = PANEL_FULL_HISTORY.split()
    assert len(s.dev) == 1212
    assert len(s.holdout) == 303
    assert max(s.dev) < min(s.holdout)


def test_panel_excludes_the_duplicated_session():
    assert date(2026, 8, 20) in GLOBAL_EXCLUDED_SESSIONS
    assert date(2026, 8, 20) not in PANEL_FULL_HISTORY.sessions()


def test_panel_excludes_the_spent_region():
    """Data already looked at can never be a holdout again."""
    sessions = PANEL_FULL_HISTORY.sessions()
    for lo, hi, _ in SPENT_REGIONS:
        assert not [d for d in sessions if lo <= d <= hi]


def test_spent_region_is_documented_with_a_reason():
    for lo, hi, reason in SPENT_REGIONS:
        assert reason and lo < hi


def test_the_two_panels_use_different_claim_files():
    """The 2026 holdout being spent must not decide anything about the new panel."""
    assert PANEL_FULL_HISTORY.claim_path != PANEL_2026_SPENT.claim_path


def test_the_new_panel_holdout_is_untouched(tmp_path):
    panel = Panel(
        name="t", start=date(2020, 1, 1), end=date(2026, 2, 15), dev_fraction=0.8,
        claim_path=tmp_path / "fresh.json",
    )
    assert "UNUSED" in panel.guard().status()


def test_dev_spans_several_regimes():
    """The whole point of six years: DEV must not be one regime."""
    rows = PANEL_FULL_HISTORY.regime_coverage()
    with_dev = [r for r in rows if r["dev_sessions"] > 0]
    assert len(with_dev) >= 3


def test_holdout_regime_coverage_is_reported():
    rows = PANEL_FULL_HISTORY.regime_coverage()
    assert sum(r["holdout_sessions"] for r in rows) == 303


@pytest.mark.parametrize("day,expected", [
    (date(2020, 3, 23), "2020 crash & recovery"),
    (date(2022, 6, 1), "2021-22 trending/bear"),
    (date(2024, 5, 1), "2023-24 recovery"),
    (date(2026, 1, 5), "2025-26"),
])
def test_regime_labels(day, expected):
    assert regime_of(day) == expected


def test_panel_guard_still_blocks_holdout_leaks(tmp_path):
    panel = Panel(
        name="t", start=date(2020, 1, 1), end=date(2026, 2, 15), dev_fraction=0.8,
        claim_path=tmp_path / "c.json",
    )
    guard = panel.guard()
    with pytest.raises(HoldoutLeakError):
        guard.assert_dev_only(list(guard.split.holdout), "a sweep")
    guard.assert_dev_only(guard.dev)


def test_panel_sessions_accepts_injected_sessions():
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(10)]
    panel = Panel(name="t", start=date(2026, 1, 1), end=date(2026, 1, 31),
                  dev_fraction=0.8, claim_path=Path("x.json"), spent=())
    assert panel.sessions(days) == days
