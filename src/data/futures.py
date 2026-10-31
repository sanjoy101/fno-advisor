"""Near-month futures: contract identification, the rollover rule, continuous series.

THE ROLLOVER RULE, STATED
-------------------------

**The continuous series uses the near-month contract up to and including the session
``ROLL_DAYS_BEFORE_EXPIRY`` trading days before its expiry date. From the next session
it uses the following month's contract.** The default is 1, so the last day a contract
appears is the trading day before its expiry, and expiry day itself is traded on the
NEXT contract.

Why roll before expiry day rather than on it: on expiry day the near contract
converges to settlement, its volume migrates to the next month, and pin risk distorts
the last hour. CLAUDE.md section 4 already forbids treating an expiry day as an
ordinary day in any statistic; including it in a continuous series would do exactly
that. The parameter is explicit so a different choice can be measured rather than
assumed, and every series records which value produced it.

WHAT IS NOT DONE, DELIBERATELY
------------------------------

**No back-adjustment.** Rolling creates a genuine price discontinuity -- the two
contracts are different instruments trading at different prices. Back-adjusting
rewrites history using knowledge of a future roll, which is look-ahead by the
definition in CLAUDE.md 1.2, and it does it silently. This module stores the RAW
series plus an explicit roll marker and the measured gap at every roll. Anyone who
wants an adjusted series must construct it deliberately, from the gaps reported here,
and say so in whatever result it produces.

**OPEN INTEREST DOES NOT CARRY ACROSS A ROLL.** The new contract's OI is a different
pool of positions; the old contract's OI collapses into expiry while the new one's
builds. A day-on-day OI change computed across a roll boundary is not a small error,
it is a meaningless number of large magnitude -- and it will look like a dramatic
signal. Every row therefore carries ``oi_change_valid``, which is False on the first
day of a new contract, and ``oi_change`` is None there rather than zero. Zero would be
silently wrong; None forces a caller to decide.

OI GRANULARITY
--------------

Daily only. Fyers' history endpoint returns six-field OHLCV and carries no OI at any
resolution, so per-minute OI does not exist in this data. It comes from the exchange
bhavcopy, which is end-of-day by construction. See :mod:`src.data.bhavcopy`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from src.market.calendar import is_trading_day

__all__ = [
    "ROLL_DAYS_BEFORE_EXPIRY",
    "Contract",
    "ContinuousBar",
    "Roll",
    "fyers_futures_symbol",
    "expiry_calendar",
    "trading_days_before",
    "roll_date_for",
    "active_expiry",
    "build_continuous",
]

#: Trading days before expiry at which the series moves to the next contract.
#: 1 => the contract's last day in the series is the day before its expiry.
ROLL_DAYS_BEFORE_EXPIRY = 1

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


@dataclass(frozen=True)
class Contract:
    """One monthly futures contract on one underlying."""

    underlying: str
    expiry: date

    @property
    def fyers_symbol(self) -> str:
        return fyers_futures_symbol(self.underlying, self.expiry)


@dataclass(frozen=True)
class Roll:
    """A contract change in a continuous series, with the discontinuity measured."""

    on_date: date
    from_expiry: date
    to_expiry: date
    #: Close of the outgoing contract on the last session it was used.
    from_close: float
    #: Close of the incoming contract on that SAME session, so the gap compares two
    #: prices struck at the same moment rather than across a day boundary.
    to_close: float

    @property
    def gap(self) -> float:
        return self.to_close - self.from_close

    @property
    def gap_pct(self) -> float:
        return (self.gap / self.from_close * 100.0) if self.from_close else float("nan")


@dataclass(frozen=True)
class ContinuousBar:
    """One day of the continuous series. RAW prices; never back-adjusted."""

    trade_date: date
    underlying: str
    expiry: date
    open: float
    high: float
    low: float
    close: float
    settle: float
    volume: int
    open_interest: int
    #: True on the first session of a new contract.
    is_roll: bool
    #: Day-on-day OI change, or None across a roll where it has no meaning.
    oi_change: int | None
    #: False exactly when ``oi_change`` is None. Explicit so a caller cannot read a
    #: missing value as zero.
    oi_change_valid: bool
    #: Days from this session to the contract's expiry, a standard futures feature
    #: input and the thing that makes roll effects visible.
    days_to_expiry: int


def fyers_futures_symbol(underlying: str, expiry: date) -> str:
    """Fyers symbol for a monthly stock future, e.g. ``NSE:RELIANCE26AUGFUT``.

    Format verified against the public NSE_FO symbol master: underlying, two-digit
    year, three-letter month, ``FUT``. The month is the EXPIRY's month.
    """
    return f"NSE:{underlying.upper()}{expiry:%y}{_MONTHS[expiry.month - 1]}FUT"


def expiry_calendar(rows_by_date: dict[date, list]) -> dict[str, list[date]]:
    """Every expiry the exchange listed, per underlying, from bhavcopy rows.

    This is the exchange's own statement of each contract's expiry on each day it
    traded, not an inference from a published rule. NSE has changed the monthly expiry
    weekday more than once (last Thursday historically; last Tuesday as of 2026), so a
    rule-based calendar is not trustworthy across this range -- see CLAUDE.md 4.0 for
    the same lesson on holidays.
    """
    out: dict[str, set[date]] = {}
    for rows in rows_by_date.values():
        for row in rows:
            if getattr(row, "is_stock_future", False):
                out.setdefault(row.symbol, set()).add(row.expiry)
    return {sym: sorted(exp) for sym, exp in out.items()}


def trading_days_before(day: date, n: int) -> date:
    """The date ``n`` trading days before ``day``. n=0 returns ``day``."""
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    cursor = day
    remaining = n
    guard = 0
    while remaining > 0:
        cursor -= timedelta(days=1)
        guard += 1
        if guard > 400:
            raise RuntimeError(f"no trading day found stepping back from {day}")
        if is_trading_day(cursor):
            remaining -= 1
    return cursor


def roll_date_for(expiry: date, roll_days_before: int = ROLL_DAYS_BEFORE_EXPIRY) -> date:
    """First session on which the series has moved OFF the contract expiring ``expiry``.

    With the default of 1, this is the expiry date itself: the contract's last day in
    the series is the trading day before expiry.
    """
    last_used = trading_days_before(expiry, roll_days_before)
    cursor = last_used + timedelta(days=1)
    guard = 0
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
        guard += 1
        if guard > 400:
            raise RuntimeError(f"no trading day found after {last_used}")
    return cursor


def active_expiry(on_date: date, expiries: list[date],
                  roll_days_before: int = ROLL_DAYS_BEFORE_EXPIRY) -> date | None:
    """Which contract the continuous series uses on ``on_date``.

    The nearest expiry whose roll date has not yet arrived. Returns None when no
    listed contract qualifies, which happens at the very end of the available data
    rather than being an error.
    """
    for expiry in sorted(expiries):
        if expiry < on_date:
            continue
        if on_date < roll_date_for(expiry, roll_days_before):
            return expiry
    return None


def build_continuous(
    rows_by_date: dict[date, list],
    underlying: str,
    *,
    roll_days_before: int = ROLL_DAYS_BEFORE_EXPIRY,
) -> tuple[list[ContinuousBar], list[Roll]]:
    """Assemble the raw continuous near-month series and its roll log.

    Data-availability boundary:
        Uses only end-of-day bhavcopy rows for sessions at or before each output
        date. The contract chosen for a date depends solely on expiry dates published
        before that date, so no future information selects the contract. Prices are
        the exchange's settled end-of-day figures for that session.

    Returns:
        (bars, rolls). Prices are RAW -- the series contains real discontinuities at
        every entry in ``rolls``, and that is deliberate. See the module docstring.
    """
    calendar = expiry_calendar(rows_by_date).get(underlying.upper(), [])
    if not calendar:
        return [], []

    by_date_expiry: dict[tuple[date, date], object] = {}
    for day, rows in rows_by_date.items():
        for row in rows:
            if getattr(row, "is_stock_future", False) and row.symbol == underlying.upper():
                by_date_expiry[(day, row.expiry)] = row

    bars: list[ContinuousBar] = []
    rolls: list[Roll] = []
    previous_expiry: date | None = None
    previous_oi: int | None = None

    for day in sorted(rows_by_date):
        expiry = active_expiry(day, calendar, roll_days_before)
        if expiry is None:
            continue
        row = by_date_expiry.get((day, expiry))
        if row is None:
            # Contract not traded that day. Skipped rather than interpolated: a
            # fabricated bar in a price series is worse than a hole in it.
            continue

        is_roll = previous_expiry is not None and expiry != previous_expiry
        if is_roll:
            prior = by_date_expiry.get((bars[-1].trade_date, previous_expiry))
            incoming = by_date_expiry.get((bars[-1].trade_date, expiry))
            if prior is not None and incoming is not None:
                rolls.append(Roll(
                    on_date=day, from_expiry=previous_expiry, to_expiry=expiry,
                    from_close=prior.close, to_close=incoming.close,
                ))

        oi_valid = not is_roll and previous_oi is not None
        bars.append(ContinuousBar(
            trade_date=day, underlying=underlying.upper(), expiry=expiry,
            open=row.open, high=row.high, low=row.low, close=row.close,
            settle=row.settle, volume=row.volume, open_interest=row.open_interest,
            is_roll=is_roll,
            oi_change=(row.open_interest - previous_oi) if oi_valid else None,
            oi_change_valid=oi_valid,
            days_to_expiry=(expiry - day).days,
        ))
        previous_expiry = expiry
        previous_oi = row.open_interest

    return bars, rolls
