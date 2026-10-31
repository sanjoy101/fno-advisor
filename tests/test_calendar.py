"""Tests for the NSE trading calendar.

These tests pin behaviour that, if wrong, fails silently in production: timezone
offset, session boundaries off by a minute, and holiday/weekend confusion.
"""

from __future__ import annotations

import warnings
from datetime import date, datetime, time, timedelta, timezone

import pytest

from src.market import calendar as cal


@pytest.fixture(autouse=True)
def _silence_unverified_warning():
    """The 2026 holiday table is intentionally unverified and warns on every call.

    Silenced here so it does not drown the suite; `test_unverified_year_warns`
    asserts the warning still fires.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        yield


# Reference dates. 2026-08-20 is a Thursday, an ordinary non-holiday session.
ORDINARY = date(2026, 8, 20)
SATURDAY = date(2026, 8, 22)
SUNDAY = date(2026, 8, 23)
REPUBLIC_DAY = date(2026, 1, 26)  # Monday
MUHURAT = date(2026, 11, 8)  # Sunday, Diwali Laxmi Pujan


# --------------------------------------------------------------------------------
# Timezone
# --------------------------------------------------------------------------------


def test_ist_offset_is_530_not_lmt():
    """Guards against the pytz LMT trap: a wrong zone gives +05:53, not +05:30."""
    offset = datetime(2026, 8, 20, 9, 15, tzinfo=cal.IST).utcoffset()
    assert offset == timedelta(hours=5, minutes=30)


def test_ist_has_no_dst_across_the_year():
    for month in range(1, 13):
        dt = datetime(2026, month, 15, 12, 0, tzinfo=cal.IST)
        assert dt.utcoffset() == timedelta(hours=5, minutes=30)


def test_ensure_ist_rejects_naive_datetime():
    with pytest.raises(cal.NaiveDatetimeError):
        cal.ensure_ist(datetime(2026, 8, 20, 9, 15))


def test_ensure_ist_converts_from_utc():
    utc = datetime(2026, 8, 20, 3, 45, tzinfo=timezone.utc)
    ist = cal.ensure_ist(utc)
    assert (ist.hour, ist.minute) == (9, 15)
    assert ist.date() == ORDINARY


def test_now_ist_is_aware_and_ist():
    now = cal.now_ist()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=5, minutes=30)


# --------------------------------------------------------------------------------
# is_trading_day
# --------------------------------------------------------------------------------


def test_ordinary_weekday_is_trading_day():
    assert cal.is_trading_day(ORDINARY) is True


@pytest.mark.parametrize("d", [SATURDAY, SUNDAY])
def test_weekend_is_not_trading_day(d):
    assert cal.is_trading_day(d) is False


def test_listed_holiday_is_not_trading_day():
    assert cal.is_trading_day(REPUBLIC_DAY) is False
    assert cal.holiday_name(REPUBLIC_DAY) == "Republic Day"


def test_every_listed_2026_holiday_is_a_weekday():
    """NSE does not list weekend-falling holidays; a weekend entry means a bad date."""
    for d, name in cal.NSE_HOLIDAYS_2026.items():
        assert d.weekday() < 5, f"{name} on {d} falls on a weekend -- verify the date"


def test_holiday_name_is_none_for_ordinary_and_weekend_days():
    assert cal.holiday_name(ORDINARY) is None
    assert cal.holiday_name(SATURDAY) is None


def test_is_trading_day_accepts_datetime():
    assert cal.is_trading_day(datetime(2026, 8, 20, 10, 0, tzinfo=cal.IST)) is True


def test_is_trading_day_rejects_naive_datetime():
    with pytest.raises(cal.NaiveDatetimeError):
        cal.is_trading_day(datetime(2026, 8, 20, 10, 0))


# --------------------------------------------------------------------------------
# Muhurat
# --------------------------------------------------------------------------------


def test_muhurat_day_is_not_a_trading_day():
    """A muhurat session is ceremonial; it must never enter backtests as a normal day."""
    assert cal.is_muhurat_session(MUHURAT) is True
    assert cal.is_trading_day(MUHURAT) is False


def test_ordinary_day_is_not_muhurat():
    assert cal.is_muhurat_session(ORDINARY) is False


# --------------------------------------------------------------------------------
# Fail-closed behaviour
# --------------------------------------------------------------------------------


def test_unknown_year_raises_rather_than_assuming_open():
    with pytest.raises(cal.UnknownHolidayYearError):
        cal.is_trading_day(date(2031, 6, 10))


def test_2026_is_verified_and_does_not_warn():
    """Verified 2026-08-21 against the exchange tape plus two published lists."""
    cal._warned_years.discard(2026)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        cal.is_trading_day(ORDINARY)
    assert cal.HOLIDAY_DATA_VERIFIED[2026] is True
    assert not [w for w in caught if "UNVERIFIED" in str(w.message)]


def test_an_unverified_year_still_warns(monkeypatch):
    """The guard must stay live for any year added without verification."""
    monkeypatch.setitem(cal._HOLIDAYS_BY_YEAR, 2027, {date(2027, 1, 26): "Republic Day"})
    monkeypatch.setitem(cal.HOLIDAY_DATA_VERIFIED, 2027, False)
    cal._warned_years.discard(2027)
    with pytest.warns(UserWarning, match="UNVERIFIED"):
        cal.is_trading_day(date(2027, 6, 10))
    cal._warned_years.discard(2027)


def test_unverified_warning_fires_once_per_year_not_per_call(monkeypatch):
    monkeypatch.setitem(cal._HOLIDAYS_BY_YEAR, 2027, {date(2027, 1, 26): "Republic Day"})
    monkeypatch.setitem(cal.HOLIDAY_DATA_VERIFIED, 2027, False)
    cal._warned_years.discard(2027)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        for _ in range(50):
            cal.is_trading_day(date(2027, 6, 10))
    assert len([w for w in caught if "UNVERIFIED" in str(w.message)]) == 1
    cal._warned_years.discard(2027)


# --------------------------------------------------------------------------------
# The specific dates a bad calendar got wrong -- regression guards
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "day,name",
    [
        (date(2026, 1, 15), "Municipal Corporation General Elections"),
        (date(2026, 3, 3), "Holi"),
        (date(2026, 5, 28), "Bakri Id"),
    ],
)
def test_dates_the_old_calendar_missed_are_holidays(day, name):
    """Confirmed by zero candles on the exchange tape."""
    assert cal.is_trading_day(day) is False
    assert cal.holiday_name(day) == name


@pytest.mark.parametrize("day", [date(2026, 3, 4), date(2026, 3, 20), date(2026, 5, 27)])
def test_dates_the_old_calendar_wrongly_skipped_are_trading_days(day):
    """Each traded a full 375-bar session; skipping them lost 19 symbol-days each."""
    assert cal.is_trading_day(day) is True
    assert cal.holiday_name(day) is None


def test_no_id_ul_fitr_holiday_in_2026():
    """It was invented by the earlier list; NSE did not close for it."""
    assert not any("Id-Ul-Fitr" in n for n in cal.NSE_HOLIDAYS_2026.values())


def test_tape_verified_boundary_is_recorded():
    """Dates past this rest on published sources alone, and that must stay visible."""
    assert cal.TAPE_VERIFIED_THROUGH[2026] == date(2026, 8, 20)
    assert any(d > cal.TAPE_VERIFIED_THROUGH[2026] for d in cal.NSE_HOLIDAYS_2026)


def test_2026_holiday_count():
    assert len(cal.NSE_HOLIDAYS_2026) == 16


# --------------------------------------------------------------------------------
# 2020-2025, derived from the exchange tape
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("year,count", [
    (2020, 12), (2021, 14), (2022, 13), (2023, 15), (2024, 17), (2025, 14),
])
def test_holiday_counts_match_the_tape(year, count):
    assert len(cal._HOLIDAYS_BY_YEAR[year]) == count


@pytest.mark.parametrize("year", [2020, 2021, 2022, 2023, 2024, 2025, 2026])
def test_every_loaded_year_is_verified(year):
    assert cal.HOLIDAY_DATA_VERIFIED[year] is True
    assert year in cal.TAPE_VERIFIED_THROUGH


@pytest.mark.parametrize("year", [2020, 2021, 2022, 2023, 2024, 2025])
def test_no_listed_holiday_falls_on_a_weekend(year):
    """NSE does not list weekend holidays; a weekend entry means a bad date."""
    for day, name in cal._HOLIDAYS_BY_YEAR[year].items():
        assert day.weekday() < 5, f"{name} on {day} is a weekend"
        assert day.year == year


def test_loaded_years_do_not_warn():
    for year in (2020, 2021, 2022, 2023, 2024, 2025):
        cal._warned_years.discard(year)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        for year in (2020, 2021, 2022, 2023, 2024, 2025):
            cal.is_trading_day(date(year, 6, 10))
    assert not [w for w in caught if "UNVERIFIED" in str(w.message)]


# --------------------------------------------------------------------------------
# Weekend full sessions -- the silent-loss case
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("day", sorted(cal.NSE_SPECIAL_TRADING_DAYS))
def test_weekend_full_sessions_are_trading_days(day):
    """375-bar sessions on a weekend. The weekend rule would lose them with no trace."""
    assert day.weekday() >= 5, "these are the weekend cases"
    assert cal.is_trading_day(day) is True


def test_budget_saturdays_are_included():
    for day in (date(2020, 2, 1), date(2025, 2, 1), date(2026, 2, 1)):
        assert cal.is_trading_day(day) is True
        assert day in cal.trading_days_between(day, day)


def test_special_sessions_have_normal_session_times():
    """A full weekend session runs 09:15-15:30 like any other."""
    assert cal.session_open(date(2025, 2, 1)).time() == cal.SESSION_OPEN
    assert cal.session_close(date(2025, 2, 1)).time() == cal.SESSION_CLOSE
    assert cal.square_off_time(date(2025, 2, 1)).time() == cal.INTRADAY_SQUARE_OFF


# --------------------------------------------------------------------------------
# Short / ceremonial sessions
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("day", sorted(cal.NSE_SHORT_SESSIONS))
def test_short_sessions_are_not_trading_days(day):
    assert cal.is_trading_day(day) is False


@pytest.mark.parametrize("day", [
    date(2021, 11, 4), date(2022, 10, 24), date(2024, 11, 1), date(2025, 10, 21),
])
def test_weekday_muhurat_days_are_excluded(day):
    """These are WEEKDAYS that traded, so only an explicit rule keeps them out."""
    assert day.weekday() < 5
    assert cal.is_trading_day(day) is False
    assert cal.is_muhurat_session(day) is True


def test_dr_test_saturdays_are_excluded():
    for day in (date(2024, 3, 2), date(2024, 5, 18)):
        assert cal.is_trading_day(day) is False


def test_session_times_refuse_short_session_days():
    with pytest.raises(ValueError):
        cal.session_open(date(2024, 11, 1))


# --------------------------------------------------------------------------------
# Reconciliation against the tape
# --------------------------------------------------------------------------------


def test_calendar_reconciles_exactly_with_the_tape():
    """1515 regular sessions + 8 short sessions = 1523 days the tape shows traded.

    Both directions in one assertion: nothing traded that we call closed, and nothing
    is called open that never traded.
    """
    span = cal.trading_days_between(date(2020, 1, 1), date(2026, 2, 15))
    shorts = [d for d in cal.NSE_SHORT_SESSIONS
              if date(2020, 1, 1) <= d <= date(2026, 2, 15)]
    assert len(span) == 1515
    assert len(shorts) == 8
    assert len(span) + len(shorts) == 1523


def test_no_overlap_between_trading_and_short_sessions():
    assert not set(cal.NSE_SPECIAL_TRADING_DAYS) & set(cal.NSE_SHORT_SESSIONS)


def test_no_short_session_is_also_listed_as_a_holiday_name_only():
    """A weekday muhurat day must appear in BOTH tables: closed, and short-session."""
    for day in (date(2021, 11, 4), date(2022, 10, 24), date(2024, 11, 1),
                date(2025, 10, 21)):
        assert day in cal._HOLIDAYS_BY_YEAR[day.year]
        assert day in cal.NSE_SHORT_SESSIONS


@pytest.mark.parametrize("day", [
    date(2020, 3, 6), date(2020, 9, 17), date(2020, 9, 25),
    date(2020, 10, 22), date(2020, 11, 11), date(2020, 11, 25),
])
def test_days_a_published_list_wrongly_called_holidays_are_trading_days(day):
    """calendarlabs listed these as 2020 holidays; the tape shows 375-bar sessions."""
    assert cal.is_trading_day(day) is True


@pytest.mark.parametrize("day", [date(2020, 3, 10), date(2020, 11, 16), date(2020, 11, 30)])
def test_days_a_published_list_omitted_are_holidays(day):
    """The same list omitted these; the tape shows the market shut."""
    assert cal.is_trading_day(day) is False


# --------------------------------------------------------------------------------
# Session boundaries
# --------------------------------------------------------------------------------


def test_session_open_is_0915_ist_aware():
    dt = cal.session_open(ORDINARY)
    assert dt.time() == time(9, 15)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)


def test_session_close_is_1530_ist():
    assert cal.session_close(ORDINARY).time() == time(15, 30)


def test_square_off_is_1515_and_15min_before_close():
    sq = cal.square_off_time(ORDINARY)
    assert sq.time() == time(15, 15)
    assert cal.session_close(ORDINARY) - sq == timedelta(minutes=15)


@pytest.mark.parametrize("fn", ["session_open", "session_close", "square_off_time"])
@pytest.mark.parametrize("d", [SATURDAY, REPUBLIC_DAY])
def test_session_times_refuse_non_trading_days(fn, d):
    """A run started on a holiday must stop, not roll silently to another day."""
    with pytest.raises(ValueError):
        getattr(cal, fn)(d)


# --------------------------------------------------------------------------------
# is_market_open
# --------------------------------------------------------------------------------


def _at(t: time, d: date = ORDINARY) -> datetime:
    return datetime.combine(d, t, tzinfo=cal.IST)


@pytest.mark.parametrize(
    "t, expected",
    [
        (time(9, 14, 59), False),  # one second before open
        (time(9, 15), True),  # open, inclusive
        (time(12, 0), True),
        (time(15, 15), True),  # square-off is still inside the session
        (time(15, 30), True),  # close, inclusive
        (time(15, 30, 1), False),  # one second after close
        (time(0, 0), False),
    ],
)
def test_is_market_open_boundaries(t, expected):
    assert cal.is_market_open(_at(t)) is expected


@pytest.mark.parametrize("d", [SATURDAY, SUNDAY, REPUBLIC_DAY, MUHURAT])
def test_market_never_open_on_non_trading_days(d):
    assert cal.is_market_open(_at(time(12, 0), d)) is False


def test_is_market_open_rejects_naive():
    with pytest.raises(cal.NaiveDatetimeError):
        cal.is_market_open(datetime(2026, 8, 20, 12, 0))


# --------------------------------------------------------------------------------
# is_square_off_time
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "t, expected",
    [
        (time(9, 15), False),
        (time(15, 14, 59), False),  # off-by-one guard on the lower bound
        (time(15, 15), True),  # square-off begins, inclusive
        (time(15, 20), True),
        (time(15, 30), True),  # close, inclusive
        (time(15, 30, 1), False),  # after close it is over, not squaring off
        (time(23, 0), False),
    ],
)
def test_is_square_off_time_boundaries(t, expected):
    assert cal.is_square_off_time(_at(t)) is expected


@pytest.mark.parametrize("d", [SATURDAY, REPUBLIC_DAY])
def test_square_off_false_on_non_trading_days(d):
    assert cal.is_square_off_time(_at(time(15, 20), d)) is False


def test_is_square_off_time_rejects_naive():
    with pytest.raises(cal.NaiveDatetimeError):
        cal.is_square_off_time(datetime(2026, 8, 20, 15, 20))


# --------------------------------------------------------------------------------
# Pre-open
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "t, expected",
    [
        (time(8, 59, 59), False),
        (time(9, 0), True),
        (time(9, 8), True),
        (time(9, 9), False),
        (time(9, 15), False),
    ],
)
def test_is_pre_open_boundaries(t, expected):
    assert cal.is_pre_open(_at(t)) is expected


# --------------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------------


def test_next_trading_day_skips_the_weekend():
    friday = date(2026, 8, 21)
    assert cal.next_trading_day(friday) == date(2026, 8, 24)  # Monday


def test_previous_trading_day_skips_the_weekend():
    monday = date(2026, 8, 24)
    assert cal.previous_trading_day(monday) == date(2026, 8, 21)  # Friday


def test_next_trading_day_skips_a_holiday():
    """2026-01-26 (Republic Day, Mon) -- Friday 23rd rolls to Tuesday 27th."""
    assert cal.next_trading_day(date(2026, 1, 23)) == date(2026, 1, 27)


def test_previous_trading_day_skips_a_holiday():
    assert cal.previous_trading_day(date(2026, 1, 27)) == date(2026, 1, 23)


def test_navigation_is_strict_not_inclusive():
    assert cal.next_trading_day(ORDINARY) != ORDINARY
    assert cal.previous_trading_day(ORDINARY) != ORDINARY


def test_previous_trading_day_is_the_asof_for_post_close_publications():
    """Bhavcopy/ban-list for day D are usable on D+1 -- see CLAUDE.md 1.2."""
    tuesday = date(2026, 1, 27)
    assert cal.previous_trading_day(tuesday) == date(2026, 1, 23)


# --------------------------------------------------------------------------------
# trading_days_between
# --------------------------------------------------------------------------------


def test_trading_days_between_is_inclusive_and_excludes_weekends():
    days = cal.trading_days_between(date(2026, 8, 17), date(2026, 8, 23))
    assert days == [
        date(2026, 8, 17),
        date(2026, 8, 18),
        date(2026, 8, 19),
        date(2026, 8, 20),
        date(2026, 8, 21),
    ]


def test_trading_days_between_excludes_holidays():
    days = cal.trading_days_between(date(2026, 1, 23), date(2026, 1, 27))
    assert REPUBLIC_DAY not in days
    assert days == [date(2026, 1, 23), date(2026, 1, 27)]


def test_trading_days_between_rejects_reversed_range():
    with pytest.raises(ValueError):
        cal.trading_days_between(date(2026, 8, 21), date(2026, 8, 17))


def test_trading_days_between_single_non_trading_day_is_empty():
    assert cal.trading_days_between(SATURDAY, SUNDAY) == []
