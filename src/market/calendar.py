"""NSE trading calendar and session-time helpers.

Single source of truth for "is the market open", "when does the session start/end",
and "must I be flat by now". Nothing in this project should re-derive a session time
inline -- import from here.

All datetimes handled by this module are timezone-aware in Asia/Kolkata (IST). Naive
datetimes are rejected, never silently localised.

Data-availability boundary (module-wide):
    Every function here is a pure function of the calendar constants below and its
    own arguments. Nothing reads live market data, a broker API, or the wall clock,
    with the single exception of ``now_ist()``. That means no function here can
    introduce look-ahead bias: ``is_trading_day(d)`` gives the same answer whether it
    is asked in 2020 or in 2030.

.. warning::
    The 2026 holiday list is UNVERIFIED -- see :data:`NSE_HOLIDAYS_2026`.
"""

from __future__ import annotations

import warnings
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "IST",
    "PRE_OPEN_START",
    "PRE_OPEN_END",
    "SESSION_OPEN",
    "SESSION_CLOSE",
    "INTRADAY_SQUARE_OFF",
    "NSE_HOLIDAYS_2020",
    "NSE_HOLIDAYS_2021",
    "NSE_HOLIDAYS_2022",
    "NSE_HOLIDAYS_2023",
    "NSE_HOLIDAYS_2024",
    "NSE_HOLIDAYS_2025",
    "NSE_HOLIDAYS_2026",
    "NSE_SPECIAL_TRADING_DAYS",
    "NSE_SHORT_SESSIONS",
    "MUHURAT_SESSIONS_2026",
    "HOLIDAY_DATA_VERIFIED",
    "TAPE_VERIFIED_THROUGH",
    "UnknownHolidayYearError",
    "NaiveDatetimeError",
    "now_ist",
    "ensure_ist",
    "is_trading_day",
    "holiday_name",
    "session_open",
    "session_close",
    "square_off_time",
    "is_market_open",
    "is_pre_open",
    "is_square_off_time",
    "is_muhurat_session",
    "next_trading_day",
    "previous_trading_day",
    "trading_days_between",
]

# --------------------------------------------------------------------------------
# Timezone
# --------------------------------------------------------------------------------

#: The one true timezone for this system. IST has no DST, but always go through the
#: zone rather than hardcoding +05:30.
#:
#: Deliberately ``zoneinfo``, not ``pytz``. Attaching a pytz zone via ``tzinfo=``
#: silently yields the 1947 LMT offset (+05:53) instead of +05:30.
try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError as exc:  # pragma: no cover - environment setup error
    # Windows ships no IANA tz database, so stdlib zoneinfo has nothing to read
    # unless the `tzdata` package is installed. Fail with an actionable message
    # rather than the bare "No time zone found with key Asia/Kolkata".
    raise ZoneInfoNotFoundError(
        "Could not load the Asia/Kolkata timezone. On Windows this means the "
        "`tzdata` package is missing -- Windows has no system IANA database. "
        "Run: pip install tzdata  (it is pinned in requirements.txt)."
    ) from exc


# --------------------------------------------------------------------------------
# Session times (NSE equity / equity-derivatives regular market)
# --------------------------------------------------------------------------------

#: Pre-open call auction starts. Order collection only -- read-only interest to us.
PRE_OPEN_START = time(9, 0)
#: Pre-open window ends (order collection 09:00-09:08, matching to 09:12).
PRE_OPEN_END = time(9, 8)
#: Regular session opens.
SESSION_OPEN = time(9, 15)
#: Regular session closes.
SESSION_CLOSE = time(15, 30)
#: Intraday (MIS/BO/CO) auto square-off. Be flat by here.
INTRADAY_SQUARE_OFF = time(15, 15)


# --------------------------------------------------------------------------------
# Holidays
# --------------------------------------------------------------------------------

# ============================ VERIFICATION STATUS ==============================
# VERIFIED 2026-08-21. The list below replaced a model-recalled one that had three
# wrong dates and one date missing entirely. How it was verified:
#
#   1. EMPIRICALLY, 2026-01-01 .. 2026-08-20: every weekday in that range was
#      queried against the Fyers 1-minute history endpoint for RELIANCE. A weekday
#      returning zero candles is a day the exchange did not trade. This is the
#      strongest evidence available -- it is the exchange's own tape, not a
#      published list that may contain typos or later revisions.
#   2. BY SOURCE, whole year: cross-checked against two independent published
#      lists, which agree with each other and with (1) on every past date.
#         https://www.smallcase.com/learn/nse-holidays-2026/
#         https://niftylens.in/market-calendar/2026/
#
# Errors this replaced, all of which had already corrupted a backfill:
#   * Holi was 03-04; it is 03-03. (03-04 traded -- 19 symbol-days silently lost.)
#   * Bakri Id was 05-27; it is 05-28. (05-27 traded -- 19 symbol-days lost.)
#   * "Id-Ul-Fitr 03-20" was invented; NSE did not close. (19 symbol-days lost.)
#   * 01-15 (civic election holiday) was missing entirely.
#
# STILL SOURCE-ONLY: 2026-09-14 onward has not happened yet, so it cannot be
# checked against the tape. Those five dates rest on the two published lists alone.
# Re-run scripts/verify_holidays.py after each passes to promote it.
#
# What this list can still get wrong, and what to do about it:
#   * Unscheduled closures (national mourning, exchange incident) never appear in
#     any published list. They show up as an EMPTY day in a backfill -- investigate
#     rather than assuming a data fault.
#   * Moon-sighting holidays can be moved by circular days beforehand.
#   * A session can be shortened or split without being a holiday; this table only
#     models whole-day closures.
# ================================================================================

#: NSE trading holidays for 2026 (regular session closed).
#: Weekend-falling holidays are intentionally omitted -- weekends are handled
#: structurally, and NSE does not list them.
NSE_HOLIDAYS_2026: dict[date, str] = {
    # --- confirmed against the exchange tape (see VERIFICATION STATUS above) ---
    date(2026, 1, 15): "Municipal Corporation General Elections",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    # --- future dates: published sources only, not yet confirmable ---
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali-Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}

#: The last date whose holiday status was confirmed against the exchange's own tape.
#: Dates after this rest on published sources alone -- weaker, but the best available
#: for a day that has not happened.
TAPE_VERIFIED_THROUGH: dict[int, date] = {
    2020: date(2020, 12, 31),
    2021: date(2021, 12, 31),
    2022: date(2022, 12, 31),
    2023: date(2023, 12, 31),
    2024: date(2024, 12, 31),
    2025: date(2025, 12, 31),
    2026: date(2026, 8, 20),
}


# ================================================================================
# 2020-2025, derived from the exchange tape on 2026-08-21
#
# Method: daily-resolution candles for RELIANCE, HDFCBANK and INFY over
# 2020-01-01..2026-02-15, unioned. A weekday with no candle for ANY of them is a day
# the exchange did not run a regular session. Probing all three guards against a
# single-symbol suspension masquerading as a holiday -- in the event the three never
# disagreed on a single day.
#
# EVERY weekday was asked about, not only the ones something had already flagged.
# That matters: a wrongly-marked holiday leaves no trace anywhere downstream, so its
# absence has to be checked explicitly or it is never found.
#
# On published lists: they were consulted and are NOT trustworthy. calendarlabs'
# 2020 page disagreed with the tape on 11 dates; direct 1-minute probes confirmed the
# tape was right on all 11 (e.g. it lists 2020-03-06 as Holi when the market traded a
# full 375-bar session, and omits 2020-11-30 Guru Nanak Jayanti when the market was
# shut). Published lists are used here only as corroboration, never as the source.
# ================================================================================

NSE_HOLIDAYS_2020: dict[date, str] = {
    date(2020, 2, 21): "Mahashivratri",
    date(2020, 3, 10): "Holi",
    date(2020, 4, 2): "Shri Ram Navami",
    date(2020, 4, 6): "Shri Mahavir Jayanti",
    date(2020, 4, 10): "Good Friday",
    date(2020, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2020, 5, 1): "Maharashtra Day",
    date(2020, 5, 25): "Id-Ul-Fitr (Ramzan Id)",
    date(2020, 10, 2): "Mahatma Gandhi Jayanti",
    date(2020, 11, 16): "Diwali-Balipratipada",
    date(2020, 11, 30): "Guru Nanak Jayanti",
    date(2020, 12, 25): "Christmas",
}

NSE_HOLIDAYS_2021: dict[date, str] = {
    date(2021, 1, 26): "Republic Day",
    date(2021, 3, 11): "Mahashivratri",
    date(2021, 3, 29): "Holi",
    date(2021, 4, 2): "Good Friday",
    date(2021, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2021, 4, 21): "Shri Ram Navami",
    date(2021, 5, 13): "Id-Ul-Fitr (Ramzan Id)",
    date(2021, 7, 21): "Bakri Id",
    date(2021, 8, 19): "Muharram",
    date(2021, 9, 10): "Ganesh Chaturthi",
    date(2021, 10, 15): "Dussehra",
    date(2021, 11, 4): "Diwali-Laxmi Pujan",  # muhurat session only
    date(2021, 11, 5): "Diwali-Balipratipada",
    date(2021, 11, 19): "Guru Nanak Jayanti",
}

NSE_HOLIDAYS_2022: dict[date, str] = {
    date(2022, 1, 26): "Republic Day",
    date(2022, 3, 1): "Mahashivratri",
    date(2022, 3, 18): "Holi",
    date(2022, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti / Mahavir Jayanti",
    date(2022, 4, 15): "Good Friday",
    date(2022, 5, 3): "Id-Ul-Fitr (Ramzan Id)",
    date(2022, 8, 9): "Muharram",
    date(2022, 8, 15): "Independence Day",
    date(2022, 8, 31): "Ganesh Chaturthi",
    date(2022, 10, 5): "Dussehra",
    date(2022, 10, 24): "Diwali-Laxmi Pujan",  # muhurat session only
    date(2022, 10, 26): "Diwali-Balipratipada",
    date(2022, 11, 8): "Guru Nanak Jayanti",
}

NSE_HOLIDAYS_2023: dict[date, str] = {
    date(2023, 1, 26): "Republic Day",
    date(2023, 3, 7): "Holi",
    date(2023, 3, 30): "Shri Ram Navami",
    date(2023, 4, 4): "Shri Mahavir Jayanti",
    date(2023, 4, 7): "Good Friday",
    date(2023, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2023, 5, 1): "Maharashtra Day",
    date(2023, 6, 29): "Bakri Id",
    date(2023, 8, 15): "Independence Day",
    date(2023, 9, 19): "Ganesh Chaturthi",
    date(2023, 10, 2): "Mahatma Gandhi Jayanti",
    date(2023, 10, 24): "Dussehra",
    date(2023, 11, 14): "Diwali-Balipratipada",
    date(2023, 11, 27): "Guru Nanak Jayanti",
    date(2023, 12, 25): "Christmas",
}

NSE_HOLIDAYS_2024: dict[date, str] = {
    date(2024, 1, 22): "Special holiday (Ram Mandir Pran Pratishtha)",
    date(2024, 1, 26): "Republic Day",
    date(2024, 3, 8): "Mahashivratri",
    date(2024, 3, 25): "Holi",
    date(2024, 3, 29): "Good Friday",
    date(2024, 4, 11): "Id-Ul-Fitr (Ramzan Id)",
    date(2024, 4, 17): "Shri Ram Navami",
    date(2024, 5, 1): "Maharashtra Day",
    date(2024, 5, 20): "General Elections (Mumbai)",
    date(2024, 6, 17): "Bakri Id",
    date(2024, 7, 17): "Muharram",
    date(2024, 8, 15): "Independence Day",
    date(2024, 10, 2): "Mahatma Gandhi Jayanti",
    date(2024, 11, 1): "Diwali-Laxmi Pujan",  # muhurat session only
    date(2024, 11, 15): "Guru Nanak Jayanti",
    date(2024, 11, 20): "Maharashtra Assembly Elections",
    date(2024, 12, 25): "Christmas",
}

NSE_HOLIDAYS_2025: dict[date, str] = {
    date(2025, 2, 26): "Mahashivratri",
    date(2025, 3, 14): "Holi",
    date(2025, 3, 31): "Id-Ul-Fitr (Ramzan Id)",
    date(2025, 4, 10): "Shri Mahavir Jayanti",
    date(2025, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2025, 4, 18): "Good Friday",
    date(2025, 5, 1): "Maharashtra Day",
    date(2025, 8, 15): "Independence Day",
    date(2025, 8, 27): "Ganesh Chaturthi",
    date(2025, 10, 2): "Mahatma Gandhi Jayanti / Dussehra",
    date(2025, 10, 21): "Diwali-Laxmi Pujan",  # muhurat session only
    date(2025, 10, 22): "Diwali-Balipratipada",
    date(2025, 11, 5): "Guru Nanak Jayanti",
    date(2025, 12, 25): "Christmas",
}

#: Weekend days on which NSE ran a FULL 09:15-15:30 session. Verified at 375
#: one-minute bars each.
#:
#: These matter more than they look. The weekend rule would skip them silently, and a
#: wrongly-skipped day leaves no trace at all -- no EMPTY entry, no error, just data
#: that quietly is not there. Union Budget Saturdays are the recurring case.
NSE_SPECIAL_TRADING_DAYS: dict[date, str] = {
    date(2020, 2, 1): "Union Budget (Saturday, full session)",
    date(2024, 1, 20): "Special live trading session (full)",
    date(2025, 2, 1): "Union Budget (Saturday, full session)",
    date(2026, 2, 1): "Union Budget (Sunday, full session)",
}

#: Short ceremonial or test sessions. NOT trading days -- their liquidity profile is
#: not comparable and the 15:15 square-off does not apply. Verified bar counts and
#: windows are in the comments; note NSE moved muhurat to an afternoon slot in 2025.
NSE_SHORT_SESSIONS: dict[date, str] = {
    date(2020, 11, 14): "Diwali Muhurat (60 bars, 18:15-19:14)",
    date(2021, 11, 4): "Diwali Muhurat (60 bars, 18:15-19:14)",
    date(2022, 10, 24): "Diwali Muhurat (60 bars, 18:15-19:14)",
    date(2023, 11, 12): "Diwali Muhurat (60 bars, 18:15-19:14)",
    date(2024, 3, 2): "Special live session / DR test (105 bars, 09:15-12:29)",
    date(2024, 5, 18): "Special live session / DR test (105 bars, 09:15-12:29)",
    date(2024, 11, 1): "Diwali Muhurat (60 bars, 18:00-18:59)",
    date(2025, 10, 21): "Diwali Muhurat (61 bars, 13:45-14:45)",
}

#: Muhurat trading: a special ceremonial session held on a day that is otherwise a
#: holiday (Diwali Laxmi Pujan, 2026-11-08, a Sunday).
#:
#: This is deliberately NOT a trading day. ``is_trading_day()`` returns False for it.
#: The liquidity and volume profile of a muhurat session is not comparable to a
#: regular session, so it must never be included in backtests or daily statistics.
#:
#: TODO(VERIFY): exact date and session window are announced by separate circular,
#: usually only a few weeks in advance.
MUHURAT_SESSIONS_2026: dict[date, str] = {
    date(2026, 11, 8): "Diwali Laxmi Pujan (Muhurat Trading)",
}

#: Holiday tables by year. Adding a year here is what makes it queryable at all.
_HOLIDAYS_BY_YEAR: dict[int, dict[date, str]] = {
    2020: NSE_HOLIDAYS_2020,
    2021: NSE_HOLIDAYS_2021,
    2022: NSE_HOLIDAYS_2022,
    2023: NSE_HOLIDAYS_2023,
    2024: NSE_HOLIDAYS_2024,
    2025: NSE_HOLIDAYS_2025,
    2026: NSE_HOLIDAYS_2026,
}

_MUHURAT_BY_YEAR: dict[int, dict[date, str]] = {
    2026: MUHURAT_SESSIONS_2026,
}

for _day, _label in NSE_SHORT_SESSIONS.items():
    _MUHURAT_BY_YEAR.setdefault(_day.year, {})[_day] = _label

#: Per-year verification status. True only once the year's dates have been checked
#: against a real source -- see VERIFICATION STATUS above. While False, every
#: calendar query warns, because a wrong holiday corrupts a backtest silently.
HOLIDAY_DATA_VERIFIED: dict[int, bool] = {
    # All derived from the exchange tape on 2026-08-21. See the block comment above.
    2020: True,
    2021: True,
    2022: True,
    2023: True,
    2024: True,
    2025: True,
    2026: True,  # tape Jan-Aug 2026 + 2 published lists for Sep-Dec
}

# Warn once per year per process, not once per call -- otherwise a backtest loop
# emits hundreds of thousands of warnings and the signal is lost in the noise.
_warned_years: set[int] = set()


class UnknownHolidayYearError(LookupError):
    """Raised when no holiday table exists for the requested year.

    The calendar fails closed on purpose. Assuming an unknown day is a trading day
    would let a run proceed on a day the exchange was shut.
    """


class NaiveDatetimeError(TypeError):
    """Raised when a naive (timezone-unaware) datetime is passed in.

    Naive datetimes are a bug in this system, not something to auto-localise.
    """


def _check_year_available(year: int) -> None:
    """Ensure a holiday table exists for ``year``, warning if it is unverified."""
    if year not in _HOLIDAYS_BY_YEAR:
        raise UnknownHolidayYearError(
            f"No NSE holiday data for {year}. Add the official circular's dates to "
            f"_HOLIDAYS_BY_YEAR in src/market/calendar.py. Refusing to guess."
        )
    if not HOLIDAY_DATA_VERIFIED.get(year, False) and year not in _warned_years:
        _warned_years.add(year)
        warnings.warn(
            f"NSE holiday list for {year} is UNVERIFIED (model-recalled, not from the "
            f"official NSE circular). Backtests spanning a wrong holiday will be "
            f"silently incorrect. Verify against "
            f"https://www.nseindia.com/resources/exchange-communication-holidays "
            f"then set HOLIDAY_DATA_VERIFIED[{year}] = True.",
            UserWarning,
            stacklevel=3,
        )


# --------------------------------------------------------------------------------
# Timezone helpers
# --------------------------------------------------------------------------------


def now_ist() -> datetime:
    """Current wall-clock time as a tz-aware IST datetime.

    The ONLY place in this project that reads the system clock. Everything else
    takes an explicit ``asof``/``now`` argument so it can be tested deterministically
    and replayed in a backtest without look-ahead.
    """
    return datetime.now(IST)


def ensure_ist(dt: datetime) -> datetime:
    """Return ``dt`` converted to IST, rejecting naive datetimes.

    A naive datetime is raised on rather than assumed to be IST: guessing is how a
    UTC candle timestamp ends up 5h30m out of place with no error.

    Raises:
        NaiveDatetimeError: if ``dt`` has no tzinfo or no UTC offset.
    """
    if not isinstance(dt, datetime):
        raise TypeError(f"expected datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise NaiveDatetimeError(
            f"naive datetime {dt!r} -- all datetimes in this system must be "
            f"timezone-aware. Attach IST explicitly at the boundary where this "
            f"value entered the system."
        )
    return dt.astimezone(IST)


def _as_date(value: date | datetime) -> date:
    """Coerce to a calendar date, converting tz-aware datetimes to their IST date."""
    if isinstance(value, datetime):
        return ensure_ist(value).date()
    if isinstance(value, date):
        return value
    raise TypeError(f"expected date or datetime, got {type(value).__name__}")


# --------------------------------------------------------------------------------
# Trading-day queries
# --------------------------------------------------------------------------------


def is_trading_day(value: date | datetime) -> bool:
    """True if the NSE regular 09:15-15:30 session runs on this date.

    False for weekends, listed holidays, and short/ceremonial sessions. A muhurat or
    DR-test session is not a regular trading day -- see :data:`NSE_SHORT_SESSIONS`.

    True for the handful of WEEKEND days on which NSE ran a full session --
    Union Budget Saturdays and the occasional special live session, listed in
    :data:`NSE_SPECIAL_TRADING_DAYS`. Those are real sessions with real volume, and
    treating them as weekends loses the data with no error and no empty marker.

    Data-availability boundary:
        Pure calendar lookup. No market data, no wall clock. Safe to call for any
        past or future date with a loaded holiday table.

    Raises:
        UnknownHolidayYearError: if no holiday table is loaded for that year.
    """
    d = _as_date(value)
    _check_year_available(d.year)
    if d in NSE_SHORT_SESSIONS:
        return False
    if d in NSE_SPECIAL_TRADING_DAYS:
        return True
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d not in _HOLIDAYS_BY_YEAR[d.year]


def holiday_name(value: date | datetime) -> str | None:
    """Name of the listed NSE holiday on this date, or None.

    Returns None for weekends -- a weekend is not a *holiday*, it is simply outside
    the trading week. Use :func:`is_trading_day` to test tradeability.
    """
    d = _as_date(value)
    _check_year_available(d.year)
    return _HOLIDAYS_BY_YEAR[d.year].get(d)


def is_muhurat_session(value: date | datetime) -> bool:
    """True if this date hosts a Diwali muhurat trading session.

    Muhurat days are NOT trading days (:func:`is_trading_day` returns False). Exclude
    them from backtests and daily statistics -- their volume profile is not
    comparable to a regular session.
    """
    d = _as_date(value)
    _check_year_available(d.year)
    return d in _MUHURAT_BY_YEAR.get(d.year, {})


# --------------------------------------------------------------------------------
# Session boundaries
# --------------------------------------------------------------------------------


def _session_dt(value: date | datetime, at: time, *, label: str) -> datetime:
    """Build a tz-aware IST datetime at ``at`` on a validated trading day."""
    d = _as_date(value)
    if not is_trading_day(d):
        reason = holiday_name(d) or ("weekend" if d.weekday() >= 5 else "not a trading day")
        raise ValueError(f"cannot compute {label} for {d}: {reason}")
    return datetime.combine(d, at, tzinfo=IST)


def session_open(value: date | datetime) -> datetime:
    """Tz-aware IST datetime of the 09:15 regular-session open on this trading day.

    Raises:
        ValueError: if the date is not a trading day. Callers must not silently roll
            forward to the next session -- a run kicked off on a holiday should stop,
            not quietly operate on a different day.
    """
    return _session_dt(value, SESSION_OPEN, label="session_open")


def session_close(value: date | datetime) -> datetime:
    """Tz-aware IST datetime of the 15:30 regular-session close on this trading day.

    Raises:
        ValueError: if the date is not a trading day.
    """
    return _session_dt(value, SESSION_CLOSE, label="session_close")


def square_off_time(value: date | datetime) -> datetime:
    """Tz-aware IST datetime of the 15:15 intraday square-off on this trading day.

    This is the deadline to be flat on an intraday product, 15 minutes before the
    session close.

    Raises:
        ValueError: if the date is not a trading day.
    """
    return _session_dt(value, INTRADAY_SQUARE_OFF, label="square_off_time")


def is_market_open(dt: datetime) -> bool:
    """True if ``dt`` falls inside the 09:15-15:30 regular session on a trading day.

    Boundaries are inclusive at both ends.

    Data-availability boundary:
        Pure function of ``dt`` and the calendar. Does not consult a broker or a
        market-status feed, so it cannot detect an unscheduled halt or a
        circuit-limit freeze -- those are separate, live concerns handled elsewhere.

    Raises:
        NaiveDatetimeError: if ``dt`` is naive.
    """
    dt = ensure_ist(dt)
    if not is_trading_day(dt):
        return False
    return SESSION_OPEN <= dt.time() <= SESSION_CLOSE


def is_pre_open(dt: datetime) -> bool:
    """True if ``dt`` falls in the 09:00-09:08 pre-open order-collection window.

    Informational only -- this system never submits pre-open interest.

    Raises:
        NaiveDatetimeError: if ``dt`` is naive.
    """
    dt = ensure_ist(dt)
    if not is_trading_day(dt):
        return False
    return PRE_OPEN_START <= dt.time() <= PRE_OPEN_END


def is_square_off_time(dt: datetime) -> bool:
    """True once the 15:15 intraday square-off window has begun on a trading day.

    True for the interval [15:15, 15:30] inclusive. False before 15:15, false after
    the 15:30 close (at which point the session is over, not squaring off), and false
    on any non-trading day.

    Data-availability boundary:
        Pure function of ``dt`` and the calendar; no live data.

    Raises:
        NaiveDatetimeError: if ``dt`` is naive.
    """
    dt = ensure_ist(dt)
    if not is_trading_day(dt):
        return False
    t = dt.time()
    return INTRADAY_SQUARE_OFF <= t <= SESSION_CLOSE


# --------------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------------

_MAX_SEARCH_DAYS = 30  # longest plausible NSE closure run; a guard, not a real limit


def next_trading_day(value: date | datetime) -> date:
    """The first trading day strictly after this date.

    Raises:
        UnknownHolidayYearError: if the search crosses into a year with no data.
    """
    d = _as_date(value)
    for _ in range(_MAX_SEARCH_DAYS):
        d += timedelta(days=1)
        if is_trading_day(d):
            return d
    raise RuntimeError(f"no trading day found within {_MAX_SEARCH_DAYS} days after {value}")


def previous_trading_day(value: date | datetime) -> date:
    """The last trading day strictly before this date.

    Commonly the correct "as-of" date for anything published after the close --
    bhavcopy, the F&O ban list, participant-wise OI. See CLAUDE.md section 1.2.

    Raises:
        UnknownHolidayYearError: if the search crosses into a year with no data.
    """
    d = _as_date(value)
    for _ in range(_MAX_SEARCH_DAYS):
        d -= timedelta(days=1)
        if is_trading_day(d):
            return d
    raise RuntimeError(f"no trading day found within {_MAX_SEARCH_DAYS} days before {value}")


def trading_days_between(start: date | datetime, end: date | datetime) -> list[date]:
    """All trading days in the inclusive range [start, end], ascending.

    Data-availability boundary:
        Pure calendar enumeration. Note this returns *scheduled* trading days; it
        cannot know whether data was actually captured for each one.

    Raises:
        ValueError: if ``end`` precedes ``start``.
    """
    s, e = _as_date(start), _as_date(end)
    if e < s:
        raise ValueError(f"end {e} precedes start {s}")
    out: list[date] = []
    d = s
    while d <= e:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out
