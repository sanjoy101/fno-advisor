"""Universe filters: F&O ban, liquidity, minimum movement, sector.

These are hard gates, applied before scoring. A symbol that fails one is not ranked
low, it is absent -- ranking a banned symbol 3rd still leaves it one good day away
from being surfaced.

Sector data is a hand-maintained map. It is small, it changes rarely, and getting it
wrong shows up as two correlated candidates rather than as an exception, so it is
written out explicitly rather than derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

__all__ = [
    "SECTORS",
    "sector_of",
    "FnoBanList",
    "trailing_sessions",
    "MIN_TURNOVER_RUPEES",
    "MIN_ATR_PCT",
    "UniverseFilters",
]

#: Sector for each symbol in the current universe. Deliberately coarse -- the purpose
#: is only to stop two candidates that will rise and fall together being surfaced as
#: if they were independent ideas.
SECTORS: dict[str, str] = {
    "RELIANCE": "ENERGY",
    "ONGC": "ENERGY",
    "HDFCBANK": "BANK",
    "ICICIBANK": "BANK",
    "SBIN": "BANK",
    "AXISBANK": "BANK",
    "KOTAKBANK": "BANK",
    "BAJFINANCE": "NBFC",
    "INFY": "IT",
    "TCS": "IT",
    "WIPRO": "IT",
    "TMPV": "AUTO",
    "TMCV": "AUTO",
    "MARUTI": "AUTO",
    "LT": "INFRA",
    "ADANIENT": "INFRA",
    "BHARTIARTL": "TELECOM",
    "ITC": "FMCG",
    "SUNPHARMA": "PHARMA",
    "HINDALCO": "METALS",
    "TATASTEEL": "METALS",
}


def sector_of(symbol: str) -> str:
    """Sector for a symbol, or ``UNKNOWN``.

    UNKNOWN is deliberately NOT treated as its own sector by the one-per-sector rule:
    see :meth:`UniverseFilters.sector_conflict`. Two unmapped symbols might well be
    the same sector, and assuming otherwise is the mistake the rule exists to prevent.
    """
    return SECTORS.get(symbol.strip().upper(), "UNKNOWN")


class FnoBanList:
    """Symbols in the F&O ban period on a given date.

    **Stub.** The real list is published by NSE each evening for the next day. Until
    that scrape exists this returns whatever it was handed, and
    :attr:`is_stub` stays True so a report can say the check was not real.

    A symbol in ban has no new-position capacity: it must be skipped entirely, not
    ranked low. See CLAUDE.md section 4.
    """

    def __init__(self, by_date: dict[date, set[str]] | None = None, *, stub: bool = True):
        self._by_date = {d: {s.upper() for s in syms} for d, syms in (by_date or {}).items()}
        self.is_stub = stub

    def banned_on(self, day: date) -> set[str]:
        return set(self._by_date.get(day, set()))

    def is_banned(self, symbol: str, day: date) -> bool:
        return symbol.strip().upper() in self._by_date.get(day, set())

    def warning(self) -> str | None:
        if not self.is_stub:
            return None
        return (
            "F&O ban check is a STUB -- no NSE ban list has been scraped, so no symbol "
            "was excluded on ban grounds. A banned symbol could be surfaced."
        )


def trailing_sessions(
    all_sessions: Sequence[date],
    current: date,
    *,
    lookback: int = 20,
) -> list[date]:
    """The ``lookback`` sessions STRICTLY BEFORE ``current``, most recent last.

    The one function standing between the volume profile and a look-ahead bug. The
    strict ``<`` is the whole point: including ``current`` would put today's volume
    into today's baseline, and every bar would score about 1.0x no matter what
    happened.
    """
    prior = sorted(d for d in all_sessions if d < current)
    return prior[-lookback:] if lookback > 0 else prior


#: Minimum traded value so far today, in rupees. Below this an exit at the advised
#: price is not a realistic assumption.
MIN_TURNOVER_RUPEES = 50_000_000.0

#: Minimum ATR as a percent of price. A stock that does not move cannot reach a
#: target, and a target it cannot reach is not a trade.
MIN_ATR_PCT = 0.15


@dataclass(frozen=True)
class UniverseFilters:
    """The hard gates. Each returns a human-readable reason when it rejects."""

    min_turnover: float = MIN_TURNOVER_RUPEES
    min_atr_pct: float = MIN_ATR_PCT
    one_per_sector: bool = True

    def liquidity_reject(self, turnover: float | None) -> str | None:
        if turnover is None:
            return "turnover unknown"
        if turnover < self.min_turnover:
            return (
                f"turnover Rs {turnover:,.0f} so far is below the "
                f"Rs {self.min_turnover:,.0f} floor"
            )
        return None

    def atr_reject(self, atr_pct: float | None) -> str | None:
        if atr_pct is None:
            return "ATR not computable yet"
        if atr_pct < self.min_atr_pct:
            return (
                f"ATR {atr_pct:.2f}% of price is below the {self.min_atr_pct:.2f}% "
                f"floor -- too still to reach a target"
            )
        return None

    def sector_conflict(self, symbol: str, already: Iterable[str]) -> str | None:
        """Reject when a candidate from the same sector is already selected.

        UNKNOWN never conflicts. Two unmapped symbols may or may not be related, and
        blocking on a guess would silently drop good candidates; the gap belongs in
        SECTORS, not in a fabricated conflict.
        """
        if not self.one_per_sector:
            return None
        mine = sector_of(symbol)
        if mine == "UNKNOWN":
            return None
        for other in already:
            if sector_of(other) == mine:
                return f"{sector_of(symbol)} already represented by {other}"
        return None
