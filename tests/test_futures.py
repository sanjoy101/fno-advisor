"""Futures contract identification, the roll rule, and the OI boundary.

The assertion that matters most here is that OI change is **None** across a roll and
never zero. A zero would flow into any OI feature as "no change", which is silently
wrong at exactly the moment the number is most dramatic; None forces a decision.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import date

import pytest

from src.data.bhavcopy import BhavRow, parse
from src.data.futures import (
    ROLL_DAYS_BEFORE_EXPIRY,
    active_expiry,
    build_continuous,
    expiry_calendar,
    fyers_futures_symbol,
    roll_date_for,
    trading_days_before,
)

# Real 2026 expiries, last-Tuesday era, verified against the NSE_FO symbol master.
JUL = date(2026, 7, 28)
AUG = date(2026, 8, 25)
SEP = date(2026, 9, 29)


def row(day, expiry, close, oi, symbol="RELIANCE", volume=1000):
    return BhavRow(trade_date=day, symbol=symbol, instrument="STF", expiry=expiry,
                   open=close, high=close, low=close, close=close, settle=close,
                   volume=volume, open_interest=oi, change_in_oi=0)


# --------------------------------------------------------------------------------
# Contract identification
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("underlying,expiry,expected", [
    ("RELIANCE", AUG, "NSE:RELIANCE26AUGFUT"),
    ("TATASTEEL", date(2020, 1, 30), "NSE:TATASTEEL20JANFUT"),
    ("INFY", date(2026, 12, 29), "NSE:INFY26DECFUT"),
])
def test_fyers_symbol_format(underlying, expiry, expected):
    """Format verified against the public NSE_FO symbol master."""
    assert fyers_futures_symbol(underlying, expiry) == expected


def test_symbol_uses_the_expiry_month_not_the_trade_month():
    """A late-August session trades the SEPTEMBER contract after the roll."""
    assert fyers_futures_symbol("RELIANCE", SEP) == "NSE:RELIANCE26SEPFUT"


# --------------------------------------------------------------------------------
# The roll rule
# --------------------------------------------------------------------------------


def test_trading_days_before_skips_weekends():
    # 2026-08-25 is a Tuesday; one trading day before is Monday the 24th.
    assert trading_days_before(AUG, 1) == date(2026, 8, 24)
    # Five trading days back crosses a weekend, so it is NOT expiry - 5 calendar days.
    assert trading_days_before(AUG, 5) == date(2026, 8, 18)


def test_trading_days_before_zero_is_identity():
    assert trading_days_before(AUG, 0) == AUG


def test_the_contract_is_used_up_to_the_session_before_expiry():
    """The stated rule, asserted rather than described."""
    assert roll_date_for(AUG, roll_days_before=1) == AUG
    assert active_expiry(date(2026, 8, 24), [AUG, SEP]) == AUG
    assert active_expiry(date(2026, 8, 25), [AUG, SEP]) == SEP


def test_a_wider_roll_moves_off_the_contract_earlier():
    assert active_expiry(date(2026, 8, 20), [AUG, SEP], roll_days_before=1) == AUG
    assert active_expiry(date(2026, 8, 20), [AUG, SEP], roll_days_before=5) == SEP


def test_expiry_day_itself_is_never_traded_on_the_expiring_contract():
    """CLAUDE.md section 4: an expiry day is not an ordinary day."""
    for expiry in (JUL, AUG, SEP):
        assert active_expiry(expiry, [JUL, AUG, SEP]) != expiry


def test_active_expiry_returns_none_past_the_last_listed_contract():
    """The end of available data is not an error."""
    assert active_expiry(date(2026, 10, 1), [JUL, AUG]) is None


# --------------------------------------------------------------------------------
# Continuous series
# --------------------------------------------------------------------------------


def _two_contract_series():
    """Aug and Sep contracts quoted on the same days, rolling on 2026-08-25."""
    days = [date(2026, 8, 21), date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]
    aug_close = {days[0]: 100.0, days[1]: 101.0, days[2]: 101.5}
    sep_close = {days[0]: 103.0, days[1]: 104.0, days[2]: 104.5, days[3]: 105.0}
    aug_oi = {days[0]: 1_000, days[1]: 900, days[2]: 100}
    sep_oi = {days[0]: 5_000, days[1]: 6_000, days[2]: 9_000, days[3]: 9_500}
    out = {}
    for d in days:
        rows = []
        if d in aug_close:
            rows.append(row(d, AUG, aug_close[d], aug_oi[d]))
        rows.append(row(d, SEP, sep_close[d], sep_oi[d]))
        out[d] = rows
    return out, days


def test_continuous_series_switches_contract_on_the_roll_date():
    rows_by_date, days = _two_contract_series()
    bars, rolls = build_continuous(rows_by_date, "RELIANCE")
    assert [b.expiry for b in bars] == [AUG, AUG, SEP, SEP]
    assert [b.is_roll for b in bars] == [False, False, True, False]
    assert len(rolls) == 1
    assert rolls[0].on_date == date(2026, 8, 25)


def test_the_roll_gap_is_measured_between_both_contracts_on_the_same_session():
    """Comparing across a day boundary would fold a day of market move into the gap."""
    rows_by_date, _ = _two_contract_series()
    _, rolls = build_continuous(rows_by_date, "RELIANCE")
    r = rolls[0]
    assert r.from_close == 101.0 and r.to_close == 104.0  # both on 2026-08-24
    assert r.gap == pytest.approx(3.0)
    assert r.gap_pct == pytest.approx(3.0 / 101.0 * 100)


def test_prices_are_raw_and_never_back_adjusted():
    """The discontinuity must survive into the output. That is the whole design."""
    rows_by_date, days = _two_contract_series()
    bars, _ = build_continuous(rows_by_date, "RELIANCE")
    closes = {b.trade_date: b.close for b in bars}
    assert closes[date(2026, 8, 24)] == 101.0   # last Aug print, untouched
    assert closes[date(2026, 8, 25)] == 104.5   # first Sep print, untouched
    # A back-adjusted series would have removed this jump. It must still be here.
    assert closes[date(2026, 8, 25)] - closes[date(2026, 8, 24)] > 3


def test_oi_change_is_none_across_a_roll_and_never_zero():
    """The single most important assertion in this file.

    OI does not carry across a roll: the outgoing contract's open interest collapses
    into expiry while the incoming contract's builds independently. A difference taken
    across that boundary is a large meaningless number, and zero would read as "no
    change" -- silently wrong exactly where an OI feature would be most excited.
    """
    rows_by_date, _ = _two_contract_series()
    bars, _ = build_continuous(rows_by_date, "RELIANCE")
    roll_bar = next(b for b in bars if b.is_roll)
    assert roll_bar.oi_change is None
    # Not merely falsy: zero would read as "no change" and flow into a feature.
    assert roll_bar.oi_change != 0 or roll_bar.oi_change is None
    assert not isinstance(roll_bar.oi_change, int)
    assert roll_bar.oi_change_valid is False


def test_oi_change_is_computed_within_a_contract():
    rows_by_date, _ = _two_contract_series()
    bars, _ = build_continuous(rows_by_date, "RELIANCE")
    within = next(b for b in bars if b.trade_date == date(2026, 8, 24))
    assert within.oi_change == 900 - 1000
    assert within.oi_change_valid is True


def test_the_first_bar_has_no_oi_change_either():
    """Nothing precedes it, so there is no change to state."""
    rows_by_date, _ = _two_contract_series()
    bars, _ = build_continuous(rows_by_date, "RELIANCE")
    assert bars[0].oi_change is None and bars[0].oi_change_valid is False


def test_days_to_expiry_is_recorded():
    rows_by_date, _ = _two_contract_series()
    bars, _ = build_continuous(rows_by_date, "RELIANCE")
    first = bars[0]
    assert first.days_to_expiry == (AUG - first.trade_date).days


def test_a_missing_contract_day_is_skipped_not_interpolated():
    """A fabricated bar in a price series is worse than a hole in it."""
    rows_by_date, days = _two_contract_series()
    del rows_by_date[days[1]]          # drop a whole session
    bars, _ = build_continuous(rows_by_date, "RELIANCE")
    assert days[1] not in {b.trade_date for b in bars}


def test_an_underlying_with_no_futures_yields_an_empty_series():
    rows_by_date, _ = _two_contract_series()
    bars, rolls = build_continuous(rows_by_date, "TMCV")
    assert bars == [] and rolls == []


def test_expiry_calendar_is_derived_from_the_rows():
    rows_by_date, _ = _two_contract_series()
    cal = expiry_calendar(rows_by_date)
    assert cal["RELIANCE"] == [AUG, SEP]


def test_the_roll_default_is_stated_and_stable():
    """A silent change to this would re-date every roll in every stored series."""
    assert ROLL_DAYS_BEFORE_EXPIRY == 1


# --------------------------------------------------------------------------------
# Bhavcopy parsing: both formats, one shape
# --------------------------------------------------------------------------------


def _zip_csv(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bhav.csv", text)
    return buf.getvalue()


LEGACY = (
    "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,"
    "SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,\n"
    "FUTSTK,RELIANCE,30-Jan-2020,0,XX,1520,1530,1510,1520.4,1520.4,"
    "9599,620595.55,54689000,410000,01-Jan-2020,\n"
)

UDIFF = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,"
    "ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
    "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
    "2026-08-20,2026-08-20,FO,NSE,STF,1,,RELIANCE,,2026-08-25,2026-08-25,0,,"
    "RELIANCE26AUGFUT,1310,1315,1305,1312.5,1312.5,1310,1311,1312.5,"
    "73917000,-17013000,5000,100,10,,500,,,,,\n"
)


def test_legacy_bhavcopy_parses_with_open_interest():
    rows = parse(_zip_csv(LEGACY))
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "RELIANCE" and r.trade_date == date(2020, 1, 1)
    assert r.expiry == date(2020, 1, 30)
    assert r.open_interest == 54_689_000 and r.change_in_oi == 410_000
    assert r.is_stock_future


def test_udiff_bhavcopy_parses_with_open_interest():
    rows = parse(_zip_csv(UDIFF))
    r = rows[0]
    assert r.symbol == "RELIANCE" and r.trade_date == date(2026, 8, 20)
    assert r.expiry == date(2026, 8, 25)
    assert r.open_interest == 73_917_000 and r.change_in_oi == -17_013_000
    assert r.is_stock_future


def test_the_format_is_chosen_by_header_not_by_date():
    """The file states which format it is; the cutover date is not worth hardcoding."""
    assert parse(_zip_csv(LEGACY))[0].close == 1520.4
    assert parse(_zip_csv(UDIFF))[0].close == 1312.5


def test_an_unrecognised_header_raises_rather_than_returning_nothing():
    with pytest.raises(ValueError, match="unrecognised bhavcopy header"):
        parse(_zip_csv("A,B,C\n1,2,3\n"))
