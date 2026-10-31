"""Module A -- Morning Setup. Rule-based, arithmetic, and it predicts nothing.

What it answers
---------------

Is today a session, and where in it are we. What capital is available. What target and
loss limit are configured. What one round trip costs against that capital. Whether the
configured target survives that cost. Whether anything is blocking the day.

What it does NOT answer
-----------------------

**Which stock to buy.** There is no symbol anywhere in this module, no ranking, no
score, and no candidate list -- ``test_the_response_carries_no_symbol_ranking_or_candidate_list``
walks the whole payload and fails on any field shaped like one. It reads no indicator,
no market data, no model, and no broker token. It is pure arithmetic over the calendar
and the cost model.

POSSIBLE is not a forecast
--------------------------

The feasibility question here is deliberately narrow, because the wide version needs a
prediction and this module is not allowed one.

``INFEASIBLE`` means the arithmetic rules the day out: **one round trip costs more than
the entire daily loss budget**, so you cannot take a single trade and exit at break-even
gross without breaching your own risk limit. That is a fact about two configured numbers
and a cost model, with no view on the market in it.

``POSSIBLE`` means only that the arithmetic does NOT rule it out. It is not a claim that
the target will be reached, or that any trade will work. The number that matters is
reported beside it -- ``required_gross_pct``, the gross move that must be captured to net
the target after costs -- and the human judges it.

Fail closed
-----------

Missing capital, invalid capital, a missing target, a broken loss limit, a market that
is shut: every one yields ``NO_TRADE``. ``NO_TRADE`` is a first-class result and a
frequently correct one (CLAUDE.md 1.5), not an error path. Nothing here lowers a target
or widens a risk limit to make a day look tradeable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum

from config.settings import CostSettings, get_cost_settings, get_settings
from src.costs import PAISA, RoundTripCosts, round_trip_costs
from src.market.calendar import (
    INTRADAY_SQUARE_OFF,
    SESSION_CLOSE,
    SESSION_OPEN,
    ensure_ist,
    holiday_name,
    is_muhurat_session,
    is_trading_day,
    now_ist,
)

__all__ = [
    "SessionState",
    "Feasibility",
    "Advice",
    "MorningSetup",
    "morning_setup",
    "estimate_round_trip_cost",
    "REFERENCE_PRICE",
    "MAX_SANE_PCT",
]

#: Unit of account for the cost estimate. The intraday cost model is a function of
#: TURNOVER alone -- every component is a percentage of it, and brokerage is
#: min(flat fee, pct of turnover) -- so the price/quantity split cannot change the
#: answer. Using one notional "share" priced at the capital makes the turnover exactly
#: the capital, with no integer truncation and no residual.
#: ``test_the_cost_estimate_is_invariant_to_the_reference_price`` pins the premise.
REFERENCE_PRICE = None  # see estimate_round_trip_cost; None means "price = notional"

#: A percentage above this is a typo, not a configuration. A 500% daily target or loss
#: limit is not a preference to respect.
MAX_SANE_PCT = Decimal("100")

#: Percentages are reported to this precision. Enough to see a cost hurdle of 0.183%,
#: not so much that a raw Decimal division shows 28 significant digits.
PCT_QUANTUM = Decimal("0.0001")
#: Round-trip counts to two places. It is a ratio, not money.
RATIO_QUANTUM = Decimal("0.01")


def _money(value: Decimal | None) -> Decimal | None:
    """Round to paise, half-up -- the same quantum and rule the cost model uses."""
    return None if value is None else value.quantize(PAISA, rounding=ROUND_HALF_UP)


def _q(value: Decimal | None, quantum: Decimal) -> Decimal | None:
    return None if value is None else value.quantize(quantum, rounding=ROUND_HALF_UP)


class SessionState(str, Enum):
    """Where the clock is relative to the NSE session.

    ``PRE_OPEN`` here means "the regular session has not opened yet", which is WIDER
    than the exchange's own 09:00-09:08 order-collection window (``is_pre_open``). It is
    deliberately the wider reading: a morning setup is run at 08:30, and that is the
    state it runs in.
    """

    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    AFTER_SQUARE_OFF = "AFTER_SQUARE_OFF"
    CLOSED = "CLOSED"


class Feasibility(str, Enum):
    """Whether the ARITHMETIC rejects the plan. Never whether it will work.

    ``POSSIBLE`` means costs and limits do not mathematically rule the target out. It is
    not a probability, not a forecast, and not a statement that any trade will succeed.
    """

    #: The arithmetic does not reject the plan. NOT a claim that the target is probable.
    POSSIBLE = "POSSIBLE"
    #: The arithmetic rejects it: one round trip costs more than the whole loss budget.
    INFEASIBLE = "INFEASIBLE"
    #: There is no session to be feasible in -- holiday, weekend, or the session is
    #: over. Reporting POSSIBLE here would read as "the plan is fine" on a day the
    #: market is shut. The cost arithmetic is still reported in ``cost_pct_of_capital``
    #: and ``required_gross_pct``, so nothing is lost by refusing to call it feasible.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: Inputs insufficient to compute -- missing or invalid capital, target or limit.
    UNKNOWN = "UNKNOWN"


class Advice(str, Enum):
    READY = "READY"
    LIMITED = "LIMITED"
    NO_TRADE = "NO_TRADE"


def estimate_round_trip_cost(
    notional: Decimal,
    cfg: CostSettings | None = None,
    *,
    reference_price: Decimal | None = None,
) -> RoundTripCosts:
    """One intraday round trip's cost at this notional, from the shared cost model.

    No formula is duplicated here: it delegates to :func:`src.costs.round_trip_costs`,
    which charges BOTH legs and includes modelled slippage.

    ``reference_price`` exists only so a test can prove the answer does not depend on
    it. Leave it alone in production code.
    """
    cfg = cfg or get_cost_settings()
    notional = Decimal(notional)
    if reference_price is None:
        price, quantity = notional, 1
    else:
        reference_price = Decimal(reference_price)
        quantity = max(1, int(notional / reference_price))
        price = notional / quantity
    return round_trip_costs(price, price, quantity, cfg)


def _session_state(now: datetime, trade_date: date) -> SessionState:
    """Where ``now`` sits relative to ``trade_date``'s session.

    Data-availability boundary:
        A pure function of the clock and the NSE calendar. It reads no feed, no quote
        and no position, so it is identical offline, in replay and live.
    """
    if not is_trading_day(trade_date):
        return SessionState.CLOSED
    if now.date() != trade_date:
        # Describing a different session than the one the clock is in. Its state is
        # decided by the calendar, not by where today's clock happens to be.
        return SessionState.PRE_OPEN if now.date() < trade_date else SessionState.CLOSED
    t = now.timetz().replace(tzinfo=None)
    if t < SESSION_OPEN:
        return SessionState.PRE_OPEN
    if t < INTRADAY_SQUARE_OFF:
        return SessionState.OPEN
    if t <= SESSION_CLOSE:
        return SessionState.AFTER_SQUARE_OFF
    return SessionState.CLOSED


def _as_positive_decimal(value, label: str, blockers: list[str]) -> Decimal | None:
    """A strictly positive Decimal, or None with a blocker recorded. Never a guess."""
    if value is None:
        blockers.append(
            f"{label} is not configured. It is never assumed -- supply it explicitly.")
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        blockers.append(f"{label} is not a number ({value!r}).")
        return None
    if number <= 0:
        blockers.append(f"{label} must be greater than zero, got {number}.")
        return None
    return number


@dataclass(frozen=True)
class MorningSetup:
    """The morning answer. Carries no symbol, no ranking and no prediction."""

    session_state: SessionState
    trade_date: date
    capital: Decimal | None
    configured_daily_target: Decimal | None
    configured_daily_target_pct: Decimal | None
    configured_max_loss: Decimal | None
    configured_max_loss_pct: Decimal | None
    estimated_round_trip_cost: Decimal | None
    estimated_slippage: Decimal | None
    feasibility_status: Feasibility
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    advice: Advice
    generated_at: datetime
    cost_configuration_verified: bool
    #: Gross move that must be captured to NET the target after a round trip's costs.
    required_gross_pct: Decimal | None = None
    #: Round-trip cost as a percentage of capital. The hurdle, stated plainly.
    cost_pct_of_capital: Decimal | None = None
    #: How many round trips the loss budget absorbs in costs alone.
    round_trips_within_loss_budget: Decimal | None = None
    #: True when describing a session other than the one the clock is in. Such a result
    #: PLANS; it never permits activity now.
    planning_only: bool = False

    def as_dict(self) -> dict:
        def money(v):
            return None if v is None else str(v)

        return {
            "session_state": self.session_state.value,
            "trade_date": self.trade_date.isoformat(),
            "capital": money(self.capital),
            "configured_daily_target": money(self.configured_daily_target),
            "configured_daily_target_pct": money(self.configured_daily_target_pct),
            "configured_max_loss": money(self.configured_max_loss),
            "configured_max_loss_pct": money(self.configured_max_loss_pct),
            "estimated_round_trip_cost": money(self.estimated_round_trip_cost),
            "estimated_slippage": money(self.estimated_slippage),
            "feasibility_status": self.feasibility_status.value,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "advice": self.advice.value,
            "generated_at": self.generated_at.isoformat(),
            "cost_configuration_verified": self.cost_configuration_verified,
            "required_gross_pct": money(self.required_gross_pct),
            "cost_pct_of_capital": money(self.cost_pct_of_capital),
            "round_trips_within_loss_budget": money(self.round_trips_within_loss_budget),
            "planning_only": self.planning_only,
        }

    def summary(self) -> str:
        """One human line. Leads with the advice, never with an encouraging number."""
        head = f"{self.advice.value} for {self.trade_date} ({self.session_state.value})"
        if self.blockers:
            return f"{head} -- {self.blockers[0]}"
        if self.required_gross_pct is not None:
            return (f"{head} -- target needs {self.required_gross_pct:.3f}% gross "
                    f"after {self.cost_pct_of_capital:.3f}% costs")
        return head


def morning_setup(
    *,
    capital=None,
    daily_target_pct=None,
    max_daily_loss_pct=None,
    now: datetime | None = None,
    for_date: date | None = None,
    cost_cfg: CostSettings | None = None,
    settings=None,
) -> MorningSetup:
    """Build the morning answer. Pure, offline, and credential-free.

    Args:
        capital: money available today. **Never defaulted** -- absent means NO_TRADE.
        daily_target_pct / max_daily_loss_pct: percentages of capital. None falls back
            to the configured defaults; a missing or non-positive value is a blocker.
        now: injected clock, tz-aware IST. Naive input raises.
        for_date: describe a session other than the clock's. The result is then a PLAN
            and can never be READY.
        cost_cfg / settings: injected so the whole module is testable with no
            environment, no network and no broker credentials.

    Data-availability boundary:
        Reads only the clock, the NSE calendar and the cost configuration. No market
        data of any kind is consulted, so there is nothing here that could be
        look-ahead and nothing that needs a feed.

    Raises:
        NaiveDatetimeError: if ``now`` is naive.
    """
    generated_at = ensure_ist(now if now is not None else now_ist())
    cost_cfg = cost_cfg or get_cost_settings()
    settings = settings or get_settings()

    trade_date = for_date or generated_at.date()
    planning_only = trade_date != generated_at.date()
    state = _session_state(generated_at, trade_date)

    blockers: list[str] = []
    warnings: list[str] = []
    #: Set by a CALENDAR or SESSION blocker, as distinct from a configuration one. The
    #: difference decides whether "feasible" is a meaningful word: a target can be
    #: arithmetically sound and still have no session to be pursued in.
    session_blocked = False

    # ---- calendar -----------------------------------------------------------------
    if not is_trading_day(trade_date):
        session_blocked = True
        named = holiday_name(trade_date)
        if named:
            blockers.append(f"{trade_date} is an NSE holiday ({named}).")
        elif is_muhurat_session(trade_date):
            blockers.append(
                f"{trade_date} is a muhurat session, not a regular trading day. Its "
                f"liquidity profile is not comparable and it is excluded everywhere.")
        elif trade_date.weekday() >= 5:
            blockers.append(f"{trade_date} is a weekend -- not an NSE trading day.")
        else:
            blockers.append(f"{trade_date} is not an NSE trading day.")
    elif state is SessionState.AFTER_SQUARE_OFF:
        session_blocked = True
        blockers.append(
            f"The {INTRADAY_SQUARE_OFF:%H:%M} intraday square-off has passed. No new "
            f"intraday position should be opened today.")
    elif state is SessionState.CLOSED:
        session_blocked = True
        blockers.append(f"The {trade_date} session is over.")
    elif state is SessionState.PRE_OPEN:
        warnings.append(
            f"The session has not opened yet (opens {SESSION_OPEN:%H:%M} IST). This is "
            f"a plan for today, not permission to act now.")

    if planning_only:
        warnings.append(
            f"This describes {trade_date}, which is not the session the clock is in. "
            f"It is a plan and permits nothing now.")

    # ---- configuration ------------------------------------------------------------
    capital_amount = _as_positive_decimal(capital, "Capital", blockers)

    target_pct = daily_target_pct
    if target_pct is None:
        target_pct = getattr(settings, "default_daily_target_pct", None)
    target_pct = _as_positive_decimal(target_pct, "Daily target percent", blockers)

    loss_pct = max_daily_loss_pct
    if loss_pct is None:
        loss_pct = getattr(settings, "default_max_daily_loss_pct", None)
    loss_pct = _as_positive_decimal(loss_pct, "Maximum daily loss percent", blockers)

    for label, pct in (("Daily target percent", target_pct),
                       ("Maximum daily loss percent", loss_pct)):
        if pct is not None and pct > MAX_SANE_PCT:
            blockers.append(
                f"{label} is {pct}%, above the {MAX_SANE_PCT}% sanity bound. That is a "
                f"typo, not a preference.")

    if not cost_cfg.rates_verified:
        warnings.append(
            "Cost rates are UNVERIFIED. They have not been reconciled against real "
            "Fyers contract notes, so the feasibility result below is not reliable and "
            "must not be trusted for a decision. Run scripts/reconcile_costs.py.")

    # ---- arithmetic ---------------------------------------------------------------
    target_amount = loss_amount = None
    round_trip = slippage = required_gross = cost_pct = trips = None
    feasibility = Feasibility.UNKNOWN

    if capital_amount is not None:
        capital_amount = _money(capital_amount)
        costs = estimate_round_trip_cost(capital_amount, cost_cfg)
        round_trip, slippage = costs.total, costs.slippage
        cost_pct = _q(round_trip / capital_amount * Decimal("100"), PCT_QUANTUM)

        if target_pct is not None:
            # Quantised to paise BEFORE anything is derived from it, so every figure
            # on the result is consistent with the one the user is shown.
            target_amount = _money(capital_amount * target_pct / Decimal("100"))
            required_gross = _q((target_amount + round_trip) / capital_amount
                                * Decimal("100"), PCT_QUANTUM)
            if round_trip >= target_amount:
                warnings.append(
                    f"One round trip costs {round_trip} against a daily target of "
                    f"{target_amount}. Costs alone exceed the target, so the day needs "
                    f"more than twice the target in GROSS terms.")

        if loss_pct is not None:
            loss_amount = _money(capital_amount * loss_pct / Decimal("100"))
            trips = (_q(loss_amount / round_trip, RATIO_QUANTUM)
                     if round_trip > 0 else None)
            if round_trip > loss_amount:
                blockers.append(
                    f"One round trip costs {round_trip}, more than the entire daily "
                    f"loss budget of {loss_amount}. A single trade exited at "
                    f"break-even gross would breach the limit, so no trade fits.")
                feasibility = Feasibility.INFEASIBLE
            elif trips is not None and trips < 3:
                warnings.append(
                    f"The daily loss budget absorbs only {trips:.1f} round trips in "
                    f"costs alone, before any adverse move.")

        if (feasibility is not Feasibility.INFEASIBLE
                and target_amount is not None and loss_amount is not None):
            feasibility = Feasibility.POSSIBLE

    # A sound target with no session to pursue it in is not "feasible". INFEASIBLE and
    # UNKNOWN both survive, because a broken loss budget or missing capital is a fault
    # worth reporting whatever day it is.
    if session_blocked and feasibility is Feasibility.POSSIBLE:
        feasibility = Feasibility.NOT_APPLICABLE

    # ---- verdict ------------------------------------------------------------------
    if blockers:
        advice = Advice.NO_TRADE
    elif feasibility is not Feasibility.POSSIBLE:
        # A safety net rather than a live path: every route to a non-POSSIBLE
        # feasibility also records a blocker, so this branch should be unreachable.
        # It stays because the invariant it protects -- advice is READY or LIMITED
        # ONLY when the arithmetic is POSSIBLE -- is the one that must never break.
        # test_advice_is_never_ready_or_limited_unless_feasibility_is_possible pins it.
        advice = Advice.NO_TRADE
        blockers.append(
            "Feasibility could not be established from the supplied configuration.")
    elif warnings:
        advice = Advice.LIMITED
    else:
        advice = Advice.READY

    return MorningSetup(
        session_state=state,
        trade_date=trade_date,
        capital=capital_amount,
        configured_daily_target=target_amount,
        configured_daily_target_pct=target_pct,
        configured_max_loss=loss_amount,
        configured_max_loss_pct=loss_pct,
        estimated_round_trip_cost=round_trip,
        estimated_slippage=slippage,
        feasibility_status=feasibility,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        advice=advice,
        generated_at=generated_at,
        cost_configuration_verified=bool(cost_cfg.rates_verified),
        required_gross_pct=required_gross,
        cost_pct_of_capital=cost_pct,
        round_trips_within_loss_budget=trips,
        planning_only=planning_only,
    )
