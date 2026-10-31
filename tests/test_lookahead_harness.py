"""Tests for the look-ahead harness itself.

A leak detector that never fires is worse than none: it certifies. So the first duty
here is to prove the harness CATCHES deliberately leaky functions, and only then that
it clears honest ones.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.analysis.lookahead import (
    POISON_FACTOR,
    LeakReport,
    assert_causal,
    causal_check,
    poison_future,
    truncate_to,
)
from src.positions.indicators import atr, ema, rsi, session_vwap
from src.positions.models import BAR_INTERVAL, completed_bars
from tests.conftest import flat_series, ist, make_series

BARS = make_series([100 + (i % 7) - 3 for i in range(60)], start=ist(9, 15))
TIMES = [ist(9, 15) + timedelta(minutes=m) for m in (20, 30, 40, 50)]


# --------------------------------------------------------------------------------
# The harness catches leaks
# --------------------------------------------------------------------------------


def test_reading_the_last_bar_regardless_of_time_is_caught():
    """The classic: grab bars[-1] without asking whether it has happened yet."""
    leaky = lambda bars, at: bars[-1].close  # noqa: E731
    report = causal_check(leaky, BARS, TIMES, name="last_close")
    assert not report.ok
    assert len(report.failures) >= len(TIMES)


def test_reading_one_bar_into_the_future_is_caught():
    """Off by one bar is still a leak, and the subtlest one in practice."""

    def peek(bars, at):
        done = completed_bars(bars, at)
        future = [b for b in bars if b.ts + BAR_INTERVAL > at]
        return done[-1].close + (future[0].close if future else 0.0)

    assert not causal_check(peek, BARS, TIMES, name="peek_one").ok


def test_a_full_series_mean_is_caught():
    """Fitting or averaging over everything, the scaler mistake from CLAUDE.md 1.2."""
    leaky = lambda bars, at: sum(b.close for b in bars) / len(bars)  # noqa: E731
    assert not causal_check(leaky, BARS, TIMES, name="whole_series_mean").ok


def test_a_backfilled_value_is_caught():
    """bfill() reaches backwards from the future. Forward-fill only."""

    def backfilled(bars, at):
        done = completed_bars(bars, at)
        future = [b for b in bars if b.ts + BAR_INTERVAL > at]
        return future[0].close if future else done[-1].close

    assert not causal_check(backfilled, BARS, TIMES, name="bfill").ok


def test_a_leak_hidden_from_truncation_is_still_caught_by_poisoning():
    """A feature that keys off list LENGTH survives truncation but not poisoning."""

    def sneaky(bars, at):
        done = completed_bars(bars, at)
        # Reads a future bar only when one exists, so truncation looks clean.
        tail = [b for b in bars if b.ts + BAR_INTERVAL > at]
        return done[-1].close if not tail else tail[-1].close

    report = causal_check(sneaky, BARS, TIMES, name="sneaky")
    assert not report.ok
    assert any(f.variant == "poisoned" for f in report.failures)


def test_assert_causal_raises_with_a_readable_message():
    with pytest.raises(AssertionError) as exc:
        assert_causal(lambda bars, at: bars[-1].close, BARS, TIMES, name="last_close")
    message = str(exc.value)
    assert "LOOK-AHEAD DETECTED" in message
    assert "last_close" in message
    assert "with future bars" in message


# --------------------------------------------------------------------------------
# The harness clears honest functions
# --------------------------------------------------------------------------------


def test_a_causal_function_passes():
    honest = lambda bars, at: completed_bars(bars, at)[-1].close  # noqa: E731
    report = assert_causal(honest, BARS, TIMES, name="last_completed_close")
    assert report.ok
    assert report.checks == len(TIMES)


def test_a_function_returning_none_passes():
    assert assert_causal(lambda bars, at: None, BARS, TIMES, name="none").ok


@pytest.mark.parametrize(
    "name,fn",
    [
        ("vwap", lambda bars, at: session_vwap(completed_bars(bars, at))),
        ("atr14", lambda bars, at: atr(completed_bars(bars, at))),
        ("ema9", lambda bars, at: ema(completed_bars(bars, at), 9)),
        ("rsi14", lambda bars, at: rsi(completed_bars(bars, at))),
    ],
)
def test_the_existing_indicators_are_causal(name, fn):
    """Module C's indicators were written to this rule. Verify, do not assume."""
    assert_causal(fn, BARS, TIMES, name=name)


def test_a_causal_function_over_a_whole_session_passes():
    bars = flat_series(1000, 120, spread=1.0, start=ist(9, 15))
    times = [b.close_time for b in bars[20:]]
    assert_causal(
        lambda b, at: atr(completed_bars(b, at)), bars, times, name="atr_full_session"
    )


# --------------------------------------------------------------------------------
# Poisoning and truncation mechanics
# --------------------------------------------------------------------------------


def test_poison_leaves_completed_bars_untouched():
    at = ist(9, 30)
    poisoned = poison_future(BARS, at)
    for original, replaced in zip(BARS, poisoned):
        if original.ts + BAR_INTERVAL <= at:
            assert original == replaced


def test_poison_changes_every_future_bar():
    at = ist(9, 30)
    poisoned = poison_future(BARS, at)
    changed = [
        (o, p) for o, p in zip(BARS, poisoned) if o.ts + BAR_INTERVAL > at
    ]
    assert changed
    assert all(p.close == pytest.approx(o.close * POISON_FACTOR) for o, p in changed)


def test_poison_preserves_length_and_timestamps():
    poisoned = poison_future(BARS, ist(9, 30))
    assert len(poisoned) == len(BARS)
    assert [b.ts for b in poisoned] == [b.ts for b in BARS]


def test_poisoned_bars_are_still_valid_market_data():
    """Being wrong is the point; being malformed would fail for the wrong reason."""
    for bar in poison_future(BARS, ist(9, 30)):
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= max(bar.open, bar.close)
        assert bar.volume >= 0


def test_truncate_matches_completed_bars():
    at = ist(9, 33)
    assert truncate_to(BARS, at) == completed_bars(BARS, at)


def test_report_describes_a_clean_run():
    report = LeakReport(name="x", checks=4)
    assert report.ok
    assert "causal at all 4" in report.describe()


def test_float_noise_does_not_count_as_a_leak():
    """Reordered arithmetic must not be reported; a real leak moves orders of magnitude."""

    def jittery(bars, at):
        done = completed_bars(bars, at)
        return sum(b.close for b in done) + 1e-15

    assert assert_causal(jittery, BARS, TIMES, name="jittery").ok
