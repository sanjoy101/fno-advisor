"""Tests for the report generator and the DEV/HOLDOUT protocol it runs.

Two properties matter beyond "does it render":
  * synthetic numbers are never presented as findings, and
  * the holdout is genuinely used once, by one config, with the sweeps blind to it.
"""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.analysis.split import DevHoldoutSplit, HoldoutGuard, HoldoutSpentError
from src.data.backfill import LIQUID_FNO_UNIVERSE
from src.positions.feed import synthetic_session
from src.positions.store import PositionStore

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "momentum_analysis.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("momentum_analysis", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tiny_panel(script, tmp_path, monkeypatch):
    """A small synthetic panel wired into the script's module-level constants."""
    store = PositionStore(tmp_path / "data")
    start, end = date(2026, 6, 1), date(2026, 6, 30)
    from src.market.calendar import trading_days_between

    days = trading_days_between(start, end)
    for i, symbol in enumerate(LIQUID_FNO_UNIVERSE[:4]):
        for day in days:
            store.save_bars(symbol, day, synthetic_session(symbol, day, open_price=900 + i * 90))
    monkeypatch.setattr(script, "PANEL_START", start)
    monkeypatch.setattr(script, "PANEL_END", end)
    monkeypatch.setattr(script, "EXCLUDED_SESSIONS", ())
    monkeypatch.setattr(script, "SPENT", ())
    monkeypatch.setattr(script, "CLAIM_PATH", tmp_path / "claim.json")
    monkeypatch.setattr(script, "CSV_DEV", tmp_path / "dev.csv")
    monkeypatch.setattr(script, "CSV_HOLDOUT", tmp_path / "holdout.csv")
    return store, tmp_path, days


# --------------------------------------------------------------------------------
# No-data behaviour
# --------------------------------------------------------------------------------


def test_empty_store_produces_a_no_data_report(script, tmp_path, monkeypatch):
    monkeypatch.setattr(script, "CLAIM_PATH", tmp_path / "claim.json")
    out = tmp_path / "report.md"
    assert script.main(["--data-dir", str(tmp_path / "empty"), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "NO DATA YET" in text
    assert "the bar store is empty" in text


def test_no_data_report_refuses_synthetic_substitution(script, tmp_path, monkeypatch):
    monkeypatch.setattr(script, "CLAIM_PATH", tmp_path / "claim.json")
    out = tmp_path / "report.md"
    script.main(["--data-dir", str(tmp_path / "empty"), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "No numbers have been produced from synthetic data" in text
    assert "Distribution of exit rules" not in text


def test_no_data_report_does_not_spend_the_holdout(script, tmp_path, monkeypatch):
    claim = tmp_path / "claim.json"
    monkeypatch.setattr(script, "CLAIM_PATH", claim)
    script.main(["--data-dir", str(tmp_path / "empty"), "--out", str(tmp_path / "r.md")])
    assert not claim.exists(), "a failed run must not burn the holdout"


# --------------------------------------------------------------------------------
# Panel construction
# --------------------------------------------------------------------------------


def test_panel_excludes_the_duplicated_session(script):
    assert date(2026, 8, 20) in script.EXCLUDED_SESSIONS
    assert date(2026, 8, 20) not in script.panel_sessions()


def test_panel_is_the_six_year_window(script):
    sessions = script.panel_sessions()
    assert len(sessions) == 1515
    assert sessions[0] == date(2020, 1, 1)
    assert sessions[-1] == date(2026, 2, 13)


def test_panel_excludes_the_spent_2026_region(script):
    """That range's DEV was swept and its holdout evaluated. It can never come back."""
    sessions = set(script.panel_sessions())
    for lo, hi, _ in script.SPENT:
        assert not [d for d in sessions if lo <= d <= hi]


def test_panel_uses_a_fresh_claim_file(script):
    assert "full_history" in str(script.CLAIM_PATH)
    assert script.CLAIM_PATH != Path("reports/.holdout_claim.json")


def test_symbol_screen_drops_materially_short_symbols(script):
    cov = {"GOOD": 125, "ALSOGOOD": 122, "SHORT": 40}
    keep, drop, threshold = script.usable_symbols(cov, 125)
    assert keep == ["ALSOGOOD", "GOOD"]
    assert drop == [("SHORT", 40)]
    assert threshold == 118


def test_symbol_screen_keeps_everything_when_coverage_is_even(script):
    keep, drop, _ = script.usable_symbols({s: 125 for s in LIQUID_FNO_UNIVERSE}, 125)
    assert len(keep) == len(LIQUID_FNO_UNIVERSE)
    assert drop == []


# --------------------------------------------------------------------------------
# Selection rule
# --------------------------------------------------------------------------------


def test_selection_picks_the_highest_net_pnl(script):
    rows = [
        {"config": "trail=1.5x", "n": 10, "total_net_pnl": 100.0},
        {"config": "trail=3.0x", "n": 10, "total_net_pnl": 900.0},
        {"config": "confirm=2bar", "n": 10, "total_net_pnl": 500.0},
    ]
    assert script.select_best(rows)["config"] == "trail=3.0x"


def test_selection_breaks_ties_toward_the_simpler_rule(script):
    rows = [
        {"config": "confirm=5bar", "n": 10, "total_net_pnl": 500.0},
        {"config": "confirm=1bar", "n": 10, "total_net_pnl": 500.0},
    ]
    assert script.select_best(rows)["config"] == "confirm=1bar"


def test_selection_returns_none_with_no_usable_rows(script):
    assert script.select_best([{"config": "a", "n": 0}]) is None


@pytest.mark.parametrize("label,field,value", [
    ("trail=2.0x", "trail_atr_multiple", 2.0),
    ("confirm=3bar", "momentum_confirmation_bars", 3),
])
def test_config_for_reconstructs_the_swept_setting(script, label, field, value):
    from config.settings import ExitEngineSettings

    cfg = script.config_for(label, ExitEngineSettings())
    assert float(getattr(cfg, field)) == float(value)


# --------------------------------------------------------------------------------
# The protocol end to end
# --------------------------------------------------------------------------------


def test_full_run_renders_every_section(script, tiny_panel):
    store, tmp_path, _ = tiny_panel
    out = tmp_path / "report.md"
    assert script.main(["--data-dir", str(store.root), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    for heading in (
        "## Protocol (fixed before any result was seen)",
        "## 1. Distribution of exit rules",
        "## 2. Exit time distribution",
        "## 3. Forward return after exit",
        "### Power, using the measured SD",
        "## 4. Trail multiple sweep",
        "## 5. Momentum confirmation sweep",
        "## 6. Holdout — one evaluation, one config",
        "## Caveats",
    ):
        assert heading in text, heading


def test_run_declares_the_selection_rule_before_results(script, tiny_panel):
    store, tmp_path, _ = tiny_panel
    out = tmp_path / "report.md"
    script.main(["--data-dir", str(store.root), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    protocol_at = text.index("## Protocol")
    sweep_at = text.index("## 4. Trail multiple sweep")
    assert protocol_at < sweep_at, "the rule must be stated before the numbers"
    assert script.SELECTION_RULE in text


def test_sweeps_are_labelled_dev_only(script, tiny_panel):
    store, tmp_path, _ = tiny_panel
    out = tmp_path / "report.md"
    script.main(["--data-dir", str(store.root), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "Trail multiple sweep (DEV only)" in text
    assert "Momentum confirmation sweep (DEV only)" in text


def test_run_spends_the_holdout_exactly_once(script, tiny_panel):
    store, tmp_path, _ = tiny_panel
    script.main(["--data-dir", str(store.root), "--out", str(tmp_path / "r.md")])
    claim = tmp_path / "claim.json"
    assert claim.exists()
    import json

    payload = json.loads(claim.read_text())
    assert payload["holdout_sessions"] > 0
    assert "best DEV config" in payload["note"]


def test_rerunning_the_same_analysis_is_idempotent(script, tiny_panel):
    """Same data, same config selected, so it is not a second peek."""
    store, tmp_path, _ = tiny_panel
    assert script.main(["--data-dir", str(store.root), "--out", str(tmp_path / "a.md")]) == 0
    assert script.main(["--data-dir", str(store.root), "--out", str(tmp_path / "b.md")]) == 0


def test_a_second_config_on_the_holdout_is_refused(script, tiny_panel):
    """The guard fires even from a fresh HoldoutGuard, as a new process would build."""
    from config.settings import ExitEngineSettings

    store, tmp_path, days = tiny_panel
    script.main(["--data-dir", str(store.root), "--out", str(tmp_path / "r.md")])

    split = DevHoldoutSplit.chronological(days, 0.8)
    guard = HoldoutGuard(split, tmp_path / "claim.json")
    with pytest.raises(HoldoutSpentError, match="second look"):
        guard.claim(ExitEngineSettings(momentum_confirmation_bars=4), "sneaky retune")


def test_holdout_status_flag_reports_without_spending(script, tmp_path, monkeypatch):
    claim = tmp_path / "claim.json"
    monkeypatch.setattr(script, "CLAIM_PATH", claim)
    assert script.main(["--holdout-status"]) == 0
    assert not claim.exists()


def test_report_states_the_holdout_is_now_spent(script, tiny_panel):
    store, tmp_path, _ = tiny_panel
    out = tmp_path / "report.md"
    script.main(["--data-dir", str(store.root), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "HOLDOUT SPENT" in text
    assert "The holdout is now spent" in text


def test_report_carries_the_in_sample_caveat(script, tiny_panel):
    store, tmp_path, _ = tiny_panel
    out = tmp_path / "report.md"
    script.main(["--data-dir", str(store.root), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "in-sample maxima" in text
    assert "optimistic by construction" in text


def test_md_table_renders_and_handles_empty(script):
    assert "no rows" in script.md_table([])
    table = script.md_table([{"a": 1, "b": 2}])
    assert "| a | b |" in table and "| 1 | 2 |" in table
