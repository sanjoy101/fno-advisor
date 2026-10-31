"""Look-ahead detection harness. Built before the features it guards.

CLAUDE.md rule 2 says every feature must be computable from data available at or
before the prediction timestamp. That is easy to write down and easy to violate: a
stray `bars[-1]`, a `rolling().mean()` without a shift, a scaler fitted on the whole
series. None of those raise. They just make the backtest better than the future.

This harness makes a leak FAIL rather than flatter. A feature is causal at time ``T``
if its value depends only on bars completed by ``T``. Three evaluations must agree:

``baseline``
    ``f(all_bars, T)`` -- the normal call, with future bars sitting in the list.
``truncated``
    ``f(bars_completed_by_T, T)`` -- future bars physically absent.
``poisoned``
    ``f(bars_with_future_replaced_by_nonsense, T)`` -- future bars present but
    absurd.

Truncation alone is not enough. A feature that reads ``len(bars)`` or slices from the
end changes behaviour when bars disappear, so truncation can produce a *different*
answer for an innocent reason, or an *identical* one for a guilty feature that
happens to be scale-free. Poisoning is the sharper instrument: the list keeps its
shape, only the unknowable values change. Anything that reads them moves, loudly.

Usage::

    report = assert_causal(lambda bars, at: rsi_at(bars, at), bars, decision_times)
    assert report.ok, report.describe()
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from src.market.calendar import ensure_ist
from src.positions.models import BAR_INTERVAL, Bar, completed_bars

__all__ = [
    "LeakFailure",
    "LeakReport",
    "poison_future",
    "truncate_to",
    "assert_causal",
    "causal_check",
    "sessions_before",
    "assert_session_causal",
    "POISON_FACTOR",
]

#: Multiplier applied to future bars. Large enough that any leak moves a value far
#: beyond rounding, and prime-ish so a leak cannot cancel out by coincidence.
POISON_FACTOR = 977.0


@dataclass(frozen=True)
class LeakFailure:
    """One timestamp at which a feature disagreed with itself."""

    at: datetime
    variant: str  # "truncated" | "poisoned"
    baseline: str
    observed: str

    def describe(self) -> str:
        return (
            f"  {self.at:%Y-%m-%d %H:%M:%S} [{self.variant}]\n"
            f"      with future bars : {self.baseline}\n"
            f"      without/poisoned : {self.observed}"
        )


@dataclass
class LeakReport:
    name: str
    checks: int = 0
    failures: list[LeakFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def describe(self) -> str:
        if self.ok:
            return f"{self.name}: causal at all {self.checks} timestamps"
        lines = [
            f"{self.name}: LOOK-AHEAD DETECTED at {len(self.failures)} of "
            f"{self.checks} timestamps. The value changed when future bars were "
            f"removed or corrupted, so it was reading them.",
        ]
        lines += [f.describe() for f in self.failures[:5]]
        if len(self.failures) > 5:
            lines.append(f"  ... and {len(self.failures) - 5} more")
        return "\n".join(lines)


def truncate_to(bars: Sequence[Bar], at: datetime) -> list[Bar]:
    """Only the bars that have finished forming by ``at``."""
    return completed_bars(bars, at)


def poison_future(
    bars: Sequence[Bar],
    at: datetime,
    *,
    factor: float = POISON_FACTOR,
) -> list[Bar]:
    """Same bars, but everything not yet complete at ``at`` replaced with nonsense.

    Keeps timestamps and list length so a feature cannot notice by counting; only the
    unknowable OHLCV changes. Values stay internally consistent so ``Bar`` validation
    still passes -- the point is to be wrong, not malformed.
    """
    at = ensure_ist(at)
    out: list[Bar] = []
    for bar in bars:
        if bar.ts + BAR_INTERVAL <= at:
            out.append(bar)
            continue
        close = bar.close * factor
        out.append(
            Bar(
                ts=bar.ts,
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=int(bar.volume * factor) + 7919,
            )
        )
    return out


def _canonical(value: Any) -> str:
    """A stable string for comparison, tolerant of float noise but not of real change.

    Rounds floats to 9 significant figures. A genuine leak moves a value by orders of
    magnitude; only reordered floating-point arithmetic lands inside that.
    """
    if value is None:
        return "None"
    if isinstance(value, bool):
        return repr(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return repr(value)
        return f"{value:.9g}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_canonical(v)}" for k, v in sorted(value.items())) + "}"
    if hasattr(value, "_asdict"):
        return _canonical(value._asdict())
    if hasattr(value, "model_dump"):
        return _canonical(value.model_dump())
    if hasattr(value, "__dataclass_fields__"):
        return _canonical({f: getattr(value, f) for f in value.__dataclass_fields__})
    return repr(value)


def causal_check(
    fn: Callable[[Sequence[Bar], datetime], Any],
    bars: Sequence[Bar],
    times: Sequence[datetime],
    *,
    name: str = "feature",
    factor: float = POISON_FACTOR,
) -> LeakReport:
    """Evaluate ``fn`` three ways at each timestamp and report every disagreement.

    ``fn`` must take ``(bars, at)`` and do its own completed-bar filtering. That is
    the contract every feature in this project follows, and it is what makes this
    check meaningful: a function handed pre-filtered bars could never be caught.
    """
    report = LeakReport(name=name)
    for at in times:
        at = ensure_ist(at)
        baseline = _canonical(fn(bars, at))
        report.checks += 1

        truncated = _canonical(fn(truncate_to(bars, at), at))
        if truncated != baseline:
            report.failures.append(LeakFailure(at, "truncated", baseline, truncated))

        poisoned = _canonical(fn(poison_future(bars, at, factor=factor), at))
        if poisoned != baseline:
            report.failures.append(LeakFailure(at, "poisoned", baseline, poisoned))
    return report


def assert_causal(
    fn: Callable[[Sequence[Bar], datetime], Any],
    bars: Sequence[Bar],
    times: Sequence[datetime],
    *,
    name: str = "feature",
) -> LeakReport:
    """``causal_check`` that raises on any leak. The form tests should use."""
    report = causal_check(fn, bars, times, name=name)
    if not report.ok:
        raise AssertionError(report.describe())
    return report


# --------------------------------------------------------------------------------
# Cross-session leaks
# --------------------------------------------------------------------------------
#
# The bar-series check above cannot see these, and it is worth being explicit about
# why. It varies bars WITHIN one series. A feature built from OTHER SESSIONS -- the
# time-of-day volume baseline is the one here -- leaks by being handed the wrong list
# of dates, and every bar in every one of those dates is genuinely complete. Nothing
# about the current session's bars changes, so poisoning them proves nothing.
#
# Worse, a ratio feature can be invariant to poisoning even when it does read ahead:
# scale the numerator and the denominator by the same factor and the leak cancels.
# So the session-level check below is not a nicety, it is the only thing covering
# this class of bug.


def sessions_before(all_sessions: Sequence[Any], current: Any) -> list[Any]:
    """Strictly earlier sessions, ascending."""
    return sorted(d for d in all_sessions if d < current)


def assert_session_causal(
    fn: Callable[[Sequence[Any]], Any],
    all_sessions: Sequence[Any],
    current: Any,
    *,
    name: str = "session feature",
) -> None:
    """``fn(sessions)`` must give the same answer with and without later sessions.

    ``fn`` receives the FULL session list and is expected to narrow it itself, via
    :func:`src.signals.universe.trailing_sessions`. Passing it a pre-filtered list
    would make this check vacuous for exactly the same reason the bar-level harness
    insists features do their own filtering.
    """
    with_future = _canonical(fn(list(all_sessions)))
    without_future = _canonical(fn(sessions_before(all_sessions, current)))
    if with_future != without_future:
        raise AssertionError(
            f"{name}: CROSS-SESSION LOOK-AHEAD at {current}. The value changed when "
            f"sessions on or after {current} were removed, so it was built from "
            f"them.\n"
            f"      with later sessions : {with_future[:300]}\n"
            f"      earlier only        : {without_future[:300]}"
        )
