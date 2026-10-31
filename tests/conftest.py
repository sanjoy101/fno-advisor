"""Shared test fixtures and bar builders.

Bars are built explicitly rather than fetched, so every test is deterministic and
runs with no broker, no network, and no clock.
"""

from __future__ import annotations

import warnings
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from config.settings import CostSettings, ExitEngineSettings
from src.market.calendar import IST
from src.positions.models import Bar, Position

#: An ordinary 2026 trading session (Thursday, no holiday).
TRADE_DATE = date(2026, 8, 20)


@pytest.fixture(autouse=True)
def _silence_unverified_holiday_warning():
    """The 2026 holiday table warns by design; tests/test_calendar.py asserts it."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        yield


def ist(h: int, m: int, s: int = 0, d: date = TRADE_DATE) -> datetime:
    """Tz-aware IST datetime on the given trading date."""
    return datetime.combine(d, time(h, m, s), tzinfo=IST)


def make_bar(
    ts: datetime,
    close: float,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: int = 1000,
) -> Bar:
    """One bar, with sane OHLC defaults derived from ``close``.

    ``ts`` is the bar OPEN time, matching the Fyers convention.
    """
    o = close if open_ is None else open_
    h = max(o, close) if high is None else high
    l = min(o, close) if low is None else low
    return Bar(ts=ts, open=o, high=h, low=l, close=close, volume=volume)


def make_series(
    closes: list[float],
    *,
    start: datetime | None = None,
    volumes: list[int] | None = None,
    spread: float = 0.0,
) -> list[Bar]:
    """A 1-minute bar series from a list of closes, starting at the session open.

    ``spread`` widens each bar's high/low around its close, which is how ATR is given
    something to measure in tests that need a non-zero ATR.
    """
    start = start or ist(9, 15)
    vols = volumes or [1000] * len(closes)
    if len(vols) != len(closes):
        raise ValueError("volumes must match closes in length")
    bars: list[Bar] = []
    for i, (c, v) in enumerate(zip(closes, vols)):
        bars.append(
            make_bar(
                start + timedelta(minutes=i),
                c,
                high=c + spread,
                low=c - spread,
                volume=v,
            )
        )
    return bars


def flat_series(
    price: float,
    count: int,
    *,
    start: datetime | None = None,
    spread: float = 1.0,
    volume: int = 1000,
) -> list[Bar]:
    """A warm-up series at a constant price -- enough history for ATR(14)/RSI(14).

    With ``spread=1.0`` each bar has a 2.0 range, so ATR converges to 2.0.
    """
    return make_series(
        [price] * count,
        start=start,
        volumes=[volume] * count,
        spread=spread,
    )


@pytest.fixture
def cost_cfg() -> CostSettings:
    """Cost config independent of any local .env."""
    return CostSettings()


@pytest.fixture
def zero_cost_cfg() -> CostSettings:
    """All costs zeroed, so P&L assertions isolate price movement."""
    return CostSettings(
        brokerage_per_order=Decimal("0"),
        brokerage_pct_of_turnover=Decimal("0"),
        stt_equity_intraday_sell=Decimal("0"),
        exchange_txn_equity=Decimal("0"),
        sebi_turnover_fee=Decimal("0"),
        ipft_charge=Decimal("0"),
        gst_rate=Decimal("0"),
        stamp_duty_equity_buy=Decimal("0"),
        slippage_bps_per_leg=Decimal("0"),
    )


@pytest.fixture
def engine_cfg() -> ExitEngineSettings:
    """Engine thresholds independent of any local .env."""
    return ExitEngineSettings()


@pytest.fixture
def position() -> Position:
    """A long recorded at 09:20, 100 shares at 1000.00, no explicit stop or target."""
    return Position(
        position_id="p-test-1",
        symbol="RELIANCE",
        quantity=100,
        entry_price=Decimal("1000.00"),
        entry_time=ist(9, 20),
    )
