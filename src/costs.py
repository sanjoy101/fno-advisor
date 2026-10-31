"""Indian intraday EQUITY transaction cost model.

Covers the NSE cash segment traded intraday (MIS): brokerage, STT, exchange
transaction charge, SEBI turnover fee, IPFT, GST, stamp duty, and modelled
slippage.

Two rules from CLAUDE.md are enforced structurally here rather than by convention:

  * **Both legs are always charged.** :func:`round_trip_costs` computes a buy leg and
    a sell leg. There is no single-leg entry point, because "forgot to charge the
    exit" is the most common way a backtest invents an edge that does not exist.
  * **No function returns a bare gross number.** :func:`unrealised_pnl` and
    :func:`realised_pnl` return a :class:`PnLBreakdown` carrying gross, the itemised
    costs, and net together. Callers cannot accidentally render gross alone.

Scope: intraday equity only. Futures and options rates exist in
:class:`config.settings.CostSettings` but are NOT implemented here -- options STT is
charged on premium rather than notional and needs its own code path. Calling this
model for an F&O instrument would understate cost. See :func:`assert_equity_intraday`.

.. warning::
    Every rate defaults to an approximation and is marked unverified. Reconcile
    against real Fyers contract notes and set ``COST_RATES_VERIFIED=true`` before
    treating any cost-dependent conclusion as settled.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from config.settings import CostSettings, get_cost_settings

__all__ = [
    "PAISA",
    "LegCosts",
    "RoundTripCosts",
    "PnLBreakdown",
    "to_decimal",
    "buy_leg_costs",
    "sell_leg_costs",
    "round_trip_costs",
    "unrealised_pnl",
    "realised_pnl",
    "delivery_buy_leg_costs",
    "delivery_sell_leg_costs",
    "delivery_round_trip_costs",
    "delivery_pnl",
]

#: Currency quantum. All money is rounded to paise, half-up, the way a contract
#: note does it.
PAISA = Decimal("0.01")


def to_decimal(value: Decimal | float | int | str) -> Decimal:
    """Convert to Decimal without inheriting binary float error.

    Goes via ``str`` for floats: ``Decimal(1234.55)`` is
    ``1234.5500000000000682...`` whereas ``Decimal(str(1234.55))`` is exact. Prices
    arrive from the broker as floats and become money here.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    """Round to paise, half-up."""
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LegCosts:
    """Itemised cost of one leg (one executed order) of an intraday equity trade."""

    side: str  # "BUY" or "SELL"
    price: Decimal
    quantity: int
    turnover: Decimal

    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    sebi_fee: Decimal
    ipft: Decimal
    gst: Decimal
    stamp_duty: Decimal
    slippage: Decimal

    @property
    def statutory(self) -> Decimal:
        """Everything that is not brokerage or slippage."""
        return _money(
            self.stt + self.exchange_txn + self.sebi_fee + self.ipft + self.gst + self.stamp_duty
        )

    @property
    def total(self) -> Decimal:
        """Total cost of this leg, including modelled slippage."""
        return _money(self.brokerage + self.statutory + self.slippage)

    def as_dict(self) -> dict[str, str]:
        """Itemisation with values as strings, for display and for the journal."""
        return {
            "side": self.side,
            "turnover": str(self.turnover),
            "brokerage": str(self.brokerage),
            "stt": str(self.stt),
            "exchange_txn": str(self.exchange_txn),
            "sebi_fee": str(self.sebi_fee),
            "ipft": str(self.ipft),
            "gst": str(self.gst),
            "stamp_duty": str(self.stamp_duty),
            "slippage": str(self.slippage),
            "total": str(self.total),
        }


@dataclass(frozen=True)
class RoundTripCosts:
    """Cost of a complete round trip. Both legs, always."""

    buy: LegCosts
    sell: LegCosts

    @property
    def total(self) -> Decimal:
        return _money(self.buy.total + self.sell.total)

    @property
    def slippage(self) -> Decimal:
        return _money(self.buy.slippage + self.sell.slippage)

    def as_dict(self) -> dict[str, object]:
        return {"buy": self.buy.as_dict(), "sell": self.sell.as_dict(), "total": str(self.total)}


@dataclass(frozen=True)
class PnLBreakdown:
    """P&L with gross, costs, and net bound together.

    There is deliberately no way to construct this with only a gross figure, and no
    accessor that returns gross without net alongside it. See CLAUDE.md 1.4.
    """

    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    costs: RoundTripCosts
    net_pnl: Decimal
    gross_pct: Decimal
    net_pct: Decimal
    realised: bool
    #: True when the exit price used is not current (stale feed). The numbers are
    #: still arithmetically correct; they are just not about *now*.
    price_is_stale: bool = False

    def summary(self) -> str:
        """One-line human-readable P&L. Never prints gross without net."""
        stale = " [STALE PRICE]" if self.price_is_stale else ""
        kind = "realised" if self.realised else "unrealised"
        return (
            f"{kind}: gross {self.gross_pnl:+.2f} ({self.gross_pct:+.2f}%) | "
            f"costs {self.costs.total:.2f} | net {self.net_pnl:+.2f} ({self.net_pct:+.2f}%){stale}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "quantity": self.quantity,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "gross_pnl": str(self.gross_pnl),
            "net_pnl": str(self.net_pnl),
            "gross_pct": str(self.gross_pct),
            "net_pct": str(self.net_pct),
            "costs": self.costs.as_dict(),
            "realised": self.realised,
            "price_is_stale": self.price_is_stale,
        }


def _brokerage(turnover: Decimal, cfg: CostSettings) -> Decimal:
    """Per-order brokerage: the percentage of turnover, capped at the flat fee.

    Fyers-style "Rs 20 per order or 0.03%, whichever is lower".
    """
    return _money(min(cfg.brokerage_per_order, turnover * cfg.brokerage_pct_of_turnover))


def _slippage(turnover: Decimal, cfg: CostSettings) -> Decimal:
    """Modelled slippage for one leg, in basis points of turnover.

    Slippage is MODELLED, never assumed zero -- a zero here is how a strategy that
    loses money in the market shows a profit on paper. The flat-bps default is
    deliberately crude and conservative; replace it with a spread-aware model once
    quote data is being stored, and record which model a result was produced under.
    """
    return _money(turnover * cfg.slippage_bps_per_leg / Decimal("10000"))


def buy_leg_costs(
    price: Decimal | float,
    quantity: int,
    cfg: CostSettings | None = None,
) -> LegCosts:
    """Costs of the BUY leg of an intraday equity trade.

    Buy side pays stamp duty; it does NOT pay STT (intraday equity STT is sell-side
    only). Getting that backwards roughly doubles the modelled STT.
    """
    cfg = cfg or get_cost_settings()
    price = to_decimal(price)
    _validate_leg(price, quantity)
    turnover = price * quantity

    brokerage = _brokerage(turnover, cfg)
    exchange_txn = _money(turnover * cfg.exchange_txn_equity)
    sebi_fee = _money(turnover * cfg.sebi_turnover_fee)
    ipft = _money(turnover * cfg.ipft_charge)
    gst = _money((brokerage + exchange_txn + sebi_fee + ipft) * cfg.gst_rate)

    return LegCosts(
        side="BUY",
        price=price,
        quantity=quantity,
        turnover=_money(turnover),
        brokerage=brokerage,
        stt=Decimal("0.00"),  # intraday equity: sell side only
        exchange_txn=exchange_txn,
        sebi_fee=sebi_fee,
        ipft=ipft,
        gst=gst,
        stamp_duty=_money(turnover * cfg.stamp_duty_equity_buy),
        slippage=_slippage(turnover, cfg),
    )


def sell_leg_costs(
    price: Decimal | float,
    quantity: int,
    cfg: CostSettings | None = None,
) -> LegCosts:
    """Costs of the SELL leg of an intraday equity trade.

    Sell side pays STT (0.025% of turnover for intraday equity); it does NOT pay
    stamp duty.
    """
    cfg = cfg or get_cost_settings()
    price = to_decimal(price)
    _validate_leg(price, quantity)
    turnover = price * quantity

    brokerage = _brokerage(turnover, cfg)
    exchange_txn = _money(turnover * cfg.exchange_txn_equity)
    sebi_fee = _money(turnover * cfg.sebi_turnover_fee)
    ipft = _money(turnover * cfg.ipft_charge)
    gst = _money((brokerage + exchange_txn + sebi_fee + ipft) * cfg.gst_rate)

    return LegCosts(
        side="SELL",
        price=price,
        quantity=quantity,
        turnover=_money(turnover),
        brokerage=brokerage,
        stt=_money(turnover * cfg.stt_equity_intraday_sell),
        exchange_txn=exchange_txn,
        sebi_fee=sebi_fee,
        ipft=ipft,
        gst=gst,
        stamp_duty=Decimal("0.00"),  # buy side only
        slippage=_slippage(turnover, cfg),
    )


def _validate_leg(price: Decimal, quantity: int) -> None:
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")


def round_trip_costs(
    buy_price: Decimal | float,
    sell_price: Decimal | float,
    quantity: int,
    cfg: CostSettings | None = None,
) -> RoundTripCosts:
    """Full round-trip cost. Charges BOTH legs, always.

    There is no single-leg public entry point for P&L on purpose.
    """
    cfg = cfg or get_cost_settings()
    return RoundTripCosts(
        buy=buy_leg_costs(buy_price, quantity, cfg),
        sell=sell_leg_costs(sell_price, quantity, cfg),
    )


def _pnl(
    entry_price: Decimal | float,
    exit_price: Decimal | float,
    quantity: int,
    *,
    realised: bool,
    price_is_stale: bool,
    cfg: CostSettings | None,
) -> PnLBreakdown:
    cfg = cfg or get_cost_settings()
    entry = to_decimal(entry_price)
    exit_ = to_decimal(exit_price)

    costs = round_trip_costs(entry, exit_, quantity, cfg)
    gross = _money((exit_ - entry) * quantity)
    net = _money(gross - costs.total)

    basis = entry * quantity
    gross_pct = _money(gross / basis * 100) if basis else Decimal("0.00")
    net_pct = _money(net / basis * 100) if basis else Decimal("0.00")

    return PnLBreakdown(
        quantity=quantity,
        entry_price=entry,
        exit_price=exit_,
        gross_pnl=gross,
        costs=costs,
        net_pnl=net,
        gross_pct=gross_pct,
        net_pct=net_pct,
        realised=realised,
        price_is_stale=price_is_stale,
    )


def unrealised_pnl(
    entry_price: Decimal | float,
    last_price: Decimal | float,
    quantity: int,
    cfg: CostSettings | None = None,
    *,
    price_is_stale: bool = False,
) -> PnLBreakdown:
    """Mark-to-market P&L on an open position, gross AND net.

    The exit leg's cost is charged even though the exit has not happened. That is
    deliberate: the position cannot be closed without paying it, so a net figure
    that ignores it overstates what you actually have.

    Data-availability boundary:
        ``last_price`` must be the close of a COMPLETED bar, or a live tick that has
        already printed. Never pass the high/low/close of the bar currently forming
        -- that is look-ahead. Pass ``price_is_stale=True`` when the price is older
        than the feed's staleness threshold so the figure is labelled rather than
        presented as current.
    """
    return _pnl(
        entry_price,
        last_price,
        quantity,
        realised=False,
        price_is_stale=price_is_stale,
        cfg=cfg,
    )


def realised_pnl(
    entry_price: Decimal | float,
    exit_price: Decimal | float,
    quantity: int,
    cfg: CostSettings | None = None,
) -> PnLBreakdown:
    """P&L on a closed position, gross AND net.

    ``exit_price`` should be the price actually filled in the broker app, not the
    price the engine advised at -- the difference between the two is real slippage,
    and recording it is how the modelled slippage assumption eventually gets checked.
    """
    return _pnl(entry_price, exit_price, quantity, realised=True, price_is_stale=False, cfg=cfg)


# --------------------------------------------------------------------------------
# Delivery (CNC) -- a multi-day hold is not an intraday trade
# --------------------------------------------------------------------------------


def delivery_buy_leg_costs(
    price: Decimal | float,
    quantity: int,
    cfg: CostSettings | None = None,
) -> LegCosts:
    """BUY leg of a DELIVERY trade.

    Unlike intraday, the buy leg pays STT -- 0.1%, the same as the sell. Stamp duty is
    also higher. Brokerage may be zero at a discount broker, which is exactly why the
    STT line has to be read carefully: zero brokerage is not zero cost.
    """
    cfg = cfg or get_cost_settings()
    price = to_decimal(price)
    _validate_leg(price, quantity)
    turnover = price * quantity

    brokerage = _money(cfg.brokerage_per_delivery_order)
    exchange_txn = _money(turnover * cfg.exchange_txn_equity)
    sebi_fee = _money(turnover * cfg.sebi_turnover_fee)
    ipft = _money(turnover * cfg.ipft_charge)
    gst = _money((brokerage + exchange_txn + sebi_fee + ipft) * cfg.gst_rate)

    return LegCosts(
        side="BUY",
        price=price,
        quantity=quantity,
        turnover=_money(turnover),
        brokerage=brokerage,
        stt=_money(turnover * cfg.stt_delivery_each_side),
        exchange_txn=exchange_txn,
        sebi_fee=sebi_fee,
        ipft=ipft,
        gst=gst,
        stamp_duty=_money(turnover * cfg.stamp_duty_delivery_buy),
        slippage=_slippage(turnover, cfg),
    )


def delivery_sell_leg_costs(
    price: Decimal | float,
    quantity: int,
    cfg: CostSettings | None = None,
) -> LegCosts:
    """SELL leg of a DELIVERY trade, including the flat DP charge.

    The DP charge is a rupee amount, not a percentage, so it is regressive: it is
    negligible on a large position and material on a small one. It is folded into
    brokerage here so the leg total stays honest.
    """
    cfg = cfg or get_cost_settings()
    price = to_decimal(price)
    _validate_leg(price, quantity)
    turnover = price * quantity

    brokerage = _money(cfg.brokerage_per_delivery_order + cfg.dp_charge_per_sell)
    exchange_txn = _money(turnover * cfg.exchange_txn_equity)
    sebi_fee = _money(turnover * cfg.sebi_turnover_fee)
    ipft = _money(turnover * cfg.ipft_charge)
    gst = _money((brokerage + exchange_txn + sebi_fee + ipft) * cfg.gst_rate)

    return LegCosts(
        side="SELL",
        price=price,
        quantity=quantity,
        turnover=_money(turnover),
        brokerage=brokerage,
        stt=_money(turnover * cfg.stt_delivery_each_side),
        exchange_txn=exchange_txn,
        sebi_fee=sebi_fee,
        ipft=ipft,
        gst=gst,
        stamp_duty=Decimal("0.00"),
        slippage=_slippage(turnover, cfg),
    )


def delivery_round_trip_costs(
    buy_price: Decimal | float,
    sell_price: Decimal | float,
    quantity: int,
    cfg: CostSettings | None = None,
) -> RoundTripCosts:
    """Full delivery round trip. Both legs, always."""
    cfg = cfg or get_cost_settings()
    return RoundTripCosts(
        buy=delivery_buy_leg_costs(buy_price, quantity, cfg),
        sell=delivery_sell_leg_costs(sell_price, quantity, cfg),
    )


def delivery_pnl(
    entry_price: Decimal | float,
    exit_price: Decimal | float,
    quantity: int,
    cfg: CostSettings | None = None,
    *,
    realised: bool = True,
) -> PnLBreakdown:
    """P&L on a delivery position, gross AND net."""
    cfg = cfg or get_cost_settings()
    entry = to_decimal(entry_price)
    exit_ = to_decimal(exit_price)
    costs = delivery_round_trip_costs(entry, exit_, quantity, cfg)
    gross = _money((exit_ - entry) * quantity)
    net = _money(gross - costs.total)
    basis = entry * quantity
    return PnLBreakdown(
        quantity=quantity,
        entry_price=entry,
        exit_price=exit_,
        gross_pnl=gross,
        costs=costs,
        net_pnl=net,
        gross_pct=_money(gross / basis * 100) if basis else Decimal("0.00"),
        net_pct=_money(net / basis * 100) if basis else Decimal("0.00"),
        realised=realised,
    )


def assert_equity_intraday(segment: str) -> None:
    """Guard: this model covers intraday equity only.

    Raises:
        NotImplementedError: for futures/options, where charges differ and options
            STT is levied on premium rather than notional. Failing loudly beats
            silently understating cost on an F&O leg.
    """
    if segment.upper() not in {"EQ", "EQUITY", "CASH", "INTRADAY_EQUITY"}:
        raise NotImplementedError(
            f"cost model covers intraday EQUITY only, got segment {segment!r}. "
            f"F&O rates exist in CostSettings but need a separate code path "
            f"(options STT is on premium, not notional)."
        )
