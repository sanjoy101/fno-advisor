"""Detect price discontinuities that a corporate action would explain.

Over six years, splits and bonuses are certain. An unadjusted series through a 1:2
split shows a clean -50% step that no market move produced. Left in place it becomes
a fake -50% bar: ATR explodes, the momentum rule fires, MAE looks catastrophic, and
every statistic that crosses the date is wrong.

**This module reports. It does not adjust.** Adjusting needs the official ratio and
ex-date from a corporate-actions feed, and adjusting with a ratio *inferred from the
price jump itself* is circular -- it would launder a data error into a clean-looking
series. The inferred ratio here is a hint for the human checking the filing, nothing
more.

Two kinds of discontinuity are found:

* ``OVERNIGHT_GAP`` -- previous session's close to the next session's open. This is
  where splits, bonuses and large dividends land.
* ``INTRADAY_BAR`` -- close to close between adjacent one-minute bars inside a
  session. A real move that large in one minute is a circuit event or a bad tick,
  never a corporate action.

Data-availability boundary:
    Reads stored bars only. Purely retrospective and used for data QA, never by the
    exit engine -- knowing about a future split is the most direct look-ahead there is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

from src.positions.models import Bar

log = logging.getLogger("fno.corpactions")

__all__ = [
    "Discontinuity",
    "DEFAULT_THRESHOLD_PCT",
    "COMMON_ACTIONS",
    "infer_action",
    "scan_symbol",
    "scan_panel",
    "group_by_cause",
]

#: Report anything moving more than this in a single step.
DEFAULT_THRESHOLD_PCT = 20.0

#: price_after / price_before for common actions, with the label to suggest.
COMMON_ACTIONS: tuple[tuple[float, str], ...] = (
    (0.5000, "1:2 split, or 1:1 bonus"),
    (0.3333, "1:3 split, or 2:1 bonus"),
    (0.2500, "1:4 split, or 3:1 bonus"),
    (0.2000, "1:5 split"),
    (0.1000, "1:10 split"),
    (0.6667, "1:2 bonus (1 free per 2 held)"),
    (0.7500, "1:3 bonus (1 free per 3 held)"),
    (0.8000, "1:4 bonus"),
    (2.0000, "1:2 reverse split"),
    (5.0000, "1:5 reverse split"),
)


def infer_action(ratio: float, tolerance: float = 0.02) -> str | None:
    """Nearest common corporate action for an observed price ratio, or None.

    Tolerance is RELATIVE to the target ratio, not absolute. An absolute window wide
    enough for a 1:10 split is far too wide near 0.67, where it would label an
    ordinary -35% crash a 1:2 bonus. 2% relative keeps a real ex-date drift inside the
    band while leaving genuine crashes unexplained, which is where they belong.

    A HINT for whoever checks the filing. Never a basis for adjusting the series --
    inferring the ratio from the jump and then dividing by it always produces a
    smooth line, whether or not a corporate action actually happened.
    """
    for target, label in COMMON_ACTIONS:
        if abs(ratio - target) <= tolerance * target:
            return label
    return None


@dataclass(frozen=True)
class Discontinuity:
    """One step in a price series too large to be an ordinary move."""

    symbol: str
    kind: str  # OVERNIGHT_GAP | INTRADAY_BAR
    when: datetime
    prev_date: date
    next_date: date
    price_before: float
    price_after: float
    pct_change: float
    ratio: float
    likely_action: str | None

    @property
    def explained(self) -> bool:
        """True when the size matches a common split/bonus ratio."""
        return self.likely_action is not None

    def as_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "when": self.when.isoformat(),
            "prev_date": self.prev_date.isoformat(),
            "next_date": self.next_date.isoformat(),
            "price_before": round(self.price_before, 2),
            "price_after": round(self.price_after, 2),
            "pct_change": round(self.pct_change, 2),
            "ratio": round(self.ratio, 4),
            "likely_action": self.likely_action or "UNEXPLAINED",
        }

    def describe(self) -> str:
        what = self.likely_action or "UNEXPLAINED -- investigate"
        return (
            f"{self.symbol} {self.kind} {self.prev_date} -> {self.next_date}: "
            f"{self.price_before:.2f} -> {self.price_after:.2f} "
            f"({self.pct_change:+.1f}%, ratio {self.ratio:.4f}) | {what}"
        )


def scan_symbol(
    store,
    symbol: str,
    sessions: Sequence[date],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    check_intraday: bool = True,
) -> list[Discontinuity]:
    """Find discontinuities in one symbol's stored series.

    Sessions with no stored bars are skipped, and the comparison then spans the gap:
    a missing week is not itself a discontinuity, but a split inside that week still
    shows up on the first session that follows.
    """
    found: list[Discontinuity] = []
    prev_close: float | None = None
    prev_date: date | None = None

    for day in sorted(sessions):
        if not store.has_bars(symbol, day):
            continue
        bars: list[Bar] = store.load_bars(symbol, day)
        if not bars:
            continue

        if prev_close is not None and prev_close > 0:
            open_price = bars[0].open
            ratio = open_price / prev_close
            pct = 100.0 * (ratio - 1.0)
            if abs(pct) >= threshold_pct:
                found.append(Discontinuity(
                    symbol=symbol, kind="OVERNIGHT_GAP", when=bars[0].ts,
                    prev_date=prev_date, next_date=day,
                    price_before=prev_close, price_after=open_price,
                    pct_change=pct, ratio=ratio, likely_action=infer_action(ratio),
                ))

        if check_intraday:
            for before, after in zip(bars, bars[1:]):
                if before.close <= 0:
                    continue
                ratio = after.close / before.close
                pct = 100.0 * (ratio - 1.0)
                if abs(pct) >= threshold_pct:
                    found.append(Discontinuity(
                        symbol=symbol, kind="INTRADAY_BAR", when=after.ts,
                        prev_date=day, next_date=day,
                        price_before=before.close, price_after=after.close,
                        pct_change=pct, ratio=ratio, likely_action=infer_action(ratio),
                    ))

        prev_close = bars[-1].close
        prev_date = day

    return found


def scan_panel(
    store,
    symbols: Sequence[str],
    sessions: Sequence[date],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    check_intraday: bool = True,
) -> list[Discontinuity]:
    """Scan every symbol, newest-largest first."""
    out: list[Discontinuity] = []
    for symbol in symbols:
        hits = scan_symbol(store, symbol, sessions,
                           threshold_pct=threshold_pct, check_intraday=check_intraday)
        if hits:
            log.warning("%s: %d discontinuity(ies) over %d sessions",
                        symbol, len(hits), len(sessions))
        out.extend(hits)
    return sorted(out, key=lambda d: (d.next_date, d.symbol))


def group_by_cause(hits: Sequence[Discontinuity]) -> list[dict]:
    """Counts by inferred cause, so a report can lead with the unexplained ones."""
    buckets: dict[str, list[Discontinuity]] = {}
    for hit in hits:
        buckets.setdefault(hit.likely_action or "UNEXPLAINED", []).append(hit)
    rows = [
        {
            "likely_action": cause,
            "n": len(group),
            "symbols": ", ".join(sorted({h.symbol for h in group})),
            "dates": ", ".join(sorted({h.next_date.isoformat() for h in group})[:6]),
        }
        for cause, group in buckets.items()
    ]
    # Unexplained first: those are the ones that need a human.
    return sorted(rows, key=lambda r: (r["likely_action"] != "UNEXPLAINED", -r["n"]))
