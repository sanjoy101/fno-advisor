"""Tests for the intraday equity cost model.

The expensive failure modes here are silent ones: charging only one leg, putting STT
on the wrong side, or letting a gross number escape without its net. Each has a test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from config.settings import CostSettings
from src import costs


@pytest.fixture
def cfg() -> CostSettings:
    """Explicit config so tests do not depend on a local .env."""
    return CostSettings()


@pytest.fixture
def zero_cfg() -> CostSettings:
    """All rates zeroed, to isolate arithmetic from rate values."""
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


# --------------------------------------------------------------------------------
# to_decimal
# --------------------------------------------------------------------------------


def test_to_decimal_avoids_binary_float_error():
    assert costs.to_decimal(1234.55) == Decimal("1234.55")
    assert costs.to_decimal(0.1) == Decimal("0.1")


def test_to_decimal_passes_decimal_through():
    d = Decimal("99.99")
    assert costs.to_decimal(d) is d


# --------------------------------------------------------------------------------
# Leg composition -- which charge lands on which side
# --------------------------------------------------------------------------------


def test_buy_leg_pays_stamp_duty_but_no_stt(cfg):
    leg = costs.buy_leg_costs(1000, 100, cfg)
    assert leg.stt == Decimal("0.00"), "intraday equity STT is sell-side only"
    assert leg.stamp_duty > 0


def test_sell_leg_pays_stt_but_no_stamp_duty(cfg):
    leg = costs.sell_leg_costs(1000, 100, cfg)
    assert leg.stt > 0
    assert leg.stamp_duty == Decimal("0.00"), "stamp duty is buy-side only"


def test_known_leg_values(cfg):
    """Turnover 100,000: pins each component against a hand computation."""
    leg = costs.sell_leg_costs(1000, 100, cfg)
    assert leg.turnover == Decimal("100000.00")
    # brokerage: min(20, 0.03% of 100000 = 30) -> 20
    assert leg.brokerage == Decimal("20.00")
    # STT 0.025% of 100000
    assert leg.stt == Decimal("25.00")
    # exchange 0.00297% of 100000
    assert leg.exchange_txn == Decimal("2.97")
    # SEBI Rs 10/crore -> 0.0001% of 100000
    assert leg.sebi_fee == Decimal("0.10")
    # GST 18% on (20 + 2.97 + 0.10 + 0.10)
    assert leg.gst == Decimal("4.17")
    # slippage 5bps of 100000
    assert leg.slippage == Decimal("50.00")


def test_brokerage_is_capped_at_the_flat_fee(cfg):
    """Large turnover: the percentage exceeds the cap, so the cap applies."""
    leg = costs.buy_leg_costs(10_000, 100, cfg)  # 1,000,000 turnover
    assert leg.brokerage == cfg.brokerage_per_order


def test_brokerage_uses_percentage_when_below_the_cap(cfg):
    """Small turnover: 0.03% of 10,000 = 3.00, below the Rs 20 cap."""
    leg = costs.buy_leg_costs(100, 100, cfg)
    assert leg.brokerage == Decimal("3.00")


def test_leg_total_is_the_sum_of_its_parts(cfg):
    leg = costs.sell_leg_costs(1234.55, 37, cfg)
    parts = (
        leg.brokerage + leg.stt + leg.exchange_txn + leg.sebi_fee
        + leg.ipft + leg.gst + leg.stamp_duty + leg.slippage
    )
    assert leg.total == parts


# --------------------------------------------------------------------------------
# Both legs are always charged
# --------------------------------------------------------------------------------


def test_round_trip_charges_both_legs(cfg):
    rt = costs.round_trip_costs(1000, 1010, 100, cfg)
    assert rt.buy.total > 0
    assert rt.sell.total > 0
    assert rt.total == rt.buy.total + rt.sell.total


def test_round_trip_cost_exceeds_either_single_leg(cfg):
    """Guards the classic bug: charging entry only."""
    rt = costs.round_trip_costs(1000, 1010, 100, cfg)
    assert rt.total > rt.buy.total
    assert rt.total > rt.sell.total


def test_slippage_is_charged_on_both_legs(cfg):
    rt = costs.round_trip_costs(1000, 1000, 100, cfg)
    assert rt.slippage == rt.buy.slippage + rt.sell.slippage
    assert rt.buy.slippage > 0 and rt.sell.slippage > 0


def test_slippage_is_never_silently_zero(cfg):
    assert cfg.slippage_bps_per_leg > 0, "a zero slippage default would flatter every result"


# --------------------------------------------------------------------------------
# P&L: gross and net always travel together
# --------------------------------------------------------------------------------


def test_net_is_always_worse_than_gross(cfg):
    for exit_price in (900, 1000, 1100):
        pnl = costs.unrealised_pnl(1000, exit_price, 100, cfg)
        assert pnl.net_pnl < pnl.gross_pnl


def test_flat_price_still_costs_money(cfg):
    """Buying and selling at the same price is a loss, never a wash."""
    pnl = costs.unrealised_pnl(1000, 1000, 100, cfg)
    assert pnl.gross_pnl == Decimal("0.00")
    assert pnl.net_pnl < 0
    assert pnl.net_pnl == -pnl.costs.total


def test_gross_arithmetic(zero_cfg):
    pnl = costs.unrealised_pnl(1000, 1010, 50, zero_cfg)
    assert pnl.gross_pnl == Decimal("500.00")
    assert pnl.net_pnl == Decimal("500.00")  # all rates zeroed


def test_loss_is_negative_and_net_is_more_negative(cfg):
    pnl = costs.realised_pnl(1000, 990, 100, cfg)
    assert pnl.gross_pnl == Decimal("-1000.00")
    assert pnl.net_pnl < pnl.gross_pnl


def test_percentages_are_on_entry_notional(zero_cfg):
    pnl = costs.unrealised_pnl(1000, 1010, 100, zero_cfg)
    assert pnl.gross_pct == Decimal("1.00")


def test_summary_never_shows_gross_without_net(cfg):
    text = costs.unrealised_pnl(1000, 1010, 100, cfg).summary()
    assert "gross" in text and "net" in text


def test_as_dict_carries_both_gross_and_net(cfg):
    d = costs.unrealised_pnl(1000, 1010, 100, cfg).as_dict()
    assert "gross_pnl" in d and "net_pnl" in d


def test_breakdown_cannot_be_built_without_costs():
    """PnLBreakdown has no gross-only constructor -- net is structurally required."""
    with pytest.raises(TypeError):
        costs.PnLBreakdown(gross_pnl=Decimal("100"))  # type: ignore[call-arg]


def test_realised_and_unrealised_are_labelled(cfg):
    assert costs.realised_pnl(1000, 1010, 10, cfg).realised is True
    assert costs.unrealised_pnl(1000, 1010, 10, cfg).realised is False


def test_stale_price_is_flagged_through_to_the_summary(cfg):
    pnl = costs.unrealised_pnl(1000, 1010, 10, cfg, price_is_stale=True)
    assert pnl.price_is_stale is True
    assert "STALE" in pnl.summary()


# --------------------------------------------------------------------------------
# Config drives everything
# --------------------------------------------------------------------------------


def test_zeroing_config_zeroes_costs(zero_cfg):
    rt = costs.round_trip_costs(1000, 1010, 100, zero_cfg)
    assert rt.total == Decimal("0.00")


def test_raising_a_rate_raises_the_cost(cfg):
    hi = CostSettings(slippage_bps_per_leg=Decimal("50"))
    assert (
        costs.round_trip_costs(1000, 1010, 100, hi).total
        > costs.round_trip_costs(1000, 1010, 100, cfg).total
    )


def test_cost_rates_default_to_unverified(cfg):
    """The rates are approximations until reconciled against real contract notes."""
    assert cfg.rates_verified is False


# --------------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("qty", [0, -1])
def test_non_positive_quantity_rejected(cfg, qty):
    with pytest.raises(ValueError):
        costs.buy_leg_costs(1000, qty, cfg)


@pytest.mark.parametrize("price", [0, -5])
def test_non_positive_price_rejected(cfg, price):
    with pytest.raises(ValueError):
        costs.sell_leg_costs(price, 10, cfg)


@pytest.mark.parametrize("segment", ["EQ", "equity", "CASH"])
def test_equity_segments_accepted(segment):
    costs.assert_equity_intraday(segment)


@pytest.mark.parametrize("segment", ["FUT", "OPT", "FUTIDX", "OPTSTK"])
def test_fno_segments_rejected_rather_than_undercharged(segment):
    with pytest.raises(NotImplementedError):
        costs.assert_equity_intraday(segment)
