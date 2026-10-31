"""Module A -- Morning Setup. Rule-based, advisory-only, and predicts nothing.

What this module is allowed to say
---------------------------------

It answers arithmetic and calendar questions: is today a session, where in it are we,
what does a round trip cost against this capital, and does the configured target survive
that cost. It NEVER answers "which stock will go up", never ranks anything, and never
carries a score.

The distinction that matters most here is between INFEASIBLE and POSSIBLE. ``POSSIBLE``
means only that the arithmetic does not rule the target out. It is not a forecast, and
these tests assert that the word "possible" never becomes a claim about outcomes.

Failing closed
--------------

Missing capital, invalid capital, a missing target, a broken loss limit, a closed
market: every one of them yields ``NO_TRADE``. ``NO_TRADE`` is a first-class answer
here, not an error path -- CLAUDE.md 1.5.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from config.settings import CostSettings
from src.market.calendar import IST, NaiveDatetimeError
from src.morning.setup import (
    Advice,
    Feasibility,
    MorningSetup,
    SessionState,
    estimate_round_trip_cost,
    morning_setup,
)

TRADING_DAY = date(2026, 8, 20)      # Thursday, a real session
SATURDAY = date(2026, 8, 22)
SUNDAY = date(2026, 8, 23)
HOLIDAY = date(2026, 1, 26)          # Republic Day, a Monday

CAPITAL = Decimal("100000")


def at(h: int, m: int = 0, s: int = 0, d: date = TRADING_DAY) -> datetime:
    return datetime.combine(d, time(h, m, s), tzinfo=IST)


def setup(**kw) -> MorningSetup:
    """A setup with everything supplied unless a test overrides it."""
    kw.setdefault("capital", CAPITAL)
    kw.setdefault("now", at(8, 30))
    kw.setdefault("daily_target_pct", Decimal("1.0"))
    kw.setdefault("max_daily_loss_pct", Decimal("1.0"))
    return morning_setup(**kw)


class StubSettings:
    """Duck-typed settings, so a MISSING configured value is testable at all.

    pydantic always supplies its defaults, so the only way to exercise "the target is
    not configured" is to hand in an object that genuinely lacks the value.
    """

    def __init__(self, target=Decimal("1.0"), max_loss=Decimal("1.0")):
        self.default_daily_target_pct = target
        self.default_max_daily_loss_pct = max_loss


# --------------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------------


def test_a_valid_trading_day_is_recognised():
    result = setup(now=at(10, 0))
    assert result.session_state is SessionState.OPEN
    assert result.trade_date == TRADING_DAY
    assert not [b for b in result.blockers if "trading day" in b.lower()]


@pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
def test_a_weekend_is_closed_and_no_trade(day):
    result = setup(now=at(10, 0, d=day))
    assert result.session_state is SessionState.CLOSED
    assert result.advice is Advice.NO_TRADE
    assert any("not an nse trading day" in b.lower() or "weekend" in b.lower()
               for b in result.blockers), result.blockers


def test_an_nse_holiday_is_closed_and_names_the_holiday():
    result = setup(now=at(10, 0, d=HOLIDAY))
    assert result.session_state is SessionState.CLOSED
    assert result.advice is Advice.NO_TRADE
    assert any("republic day" in b.lower() for b in result.blockers), result.blockers


@pytest.mark.parametrize("hh,mm", [(7, 0), (9, 0), (9, 8), (9, 14)])
def test_before_the_open_is_pre_open(hh, mm):
    """PRE_OPEN here means "the session has not opened yet", which is wider than the
    exchange's own 09:00-09:08 order-collection window. That is the state a morning
    setup actually runs in."""
    assert setup(now=at(hh, mm)).session_state is SessionState.PRE_OPEN


@pytest.mark.parametrize("hh,mm", [(9, 15), (12, 0), (15, 14)])
def test_inside_the_session_is_open(hh, mm):
    assert setup(now=at(hh, mm)).session_state is SessionState.OPEN


@pytest.mark.parametrize("hh,mm", [(15, 15), (15, 20), (15, 30)])
def test_the_square_off_window_is_after_square_off(hh, mm):
    result = setup(now=at(hh, mm))
    assert result.session_state is SessionState.AFTER_SQUARE_OFF
    assert result.advice is Advice.NO_TRADE


def test_after_the_close_is_closed():
    result = setup(now=at(16, 0))
    assert result.session_state is SessionState.CLOSED
    assert result.advice is Advice.NO_TRADE


def test_pre_open_is_not_no_trade_just_because_the_market_is_shut():
    """The whole point of the module is to run BEFORE the open and still be useful."""
    result = setup(now=at(8, 30))
    assert result.session_state is SessionState.PRE_OPEN
    assert result.advice is not Advice.NO_TRADE
    assert any("has not opened" in w.lower() for w in result.warnings), result.warnings


# --------------------------------------------------------------------------------
# Fail closed on configuration
# --------------------------------------------------------------------------------


def test_missing_capital_is_no_trade_and_never_assumed():
    result = morning_setup(capital=None, now=at(8, 30))
    assert result.advice is Advice.NO_TRADE
    assert result.capital is None, "capital must never be invented"
    assert result.feasibility_status is Feasibility.UNKNOWN
    assert any("capital" in b.lower() for b in result.blockers), result.blockers


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), "not-a-number", ""])
def test_invalid_capital_is_no_trade(bad):
    result = morning_setup(capital=bad, now=at(8, 30))
    assert result.advice is Advice.NO_TRADE
    assert result.feasibility_status is Feasibility.UNKNOWN
    assert any("capital" in b.lower() for b in result.blockers), result.blockers


def test_a_missing_daily_target_is_no_trade():
    result = morning_setup(capital=CAPITAL, now=at(8, 30),
                           settings=StubSettings(target=None))
    assert result.advice is Advice.NO_TRADE
    assert result.configured_daily_target is None
    assert any("target" in b.lower() for b in result.blockers), result.blockers


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-2"), Decimal("500")])
def test_an_invalid_max_loss_configuration_is_no_trade(bad):
    result = setup(max_daily_loss_pct=bad)
    assert result.advice is Advice.NO_TRADE
    assert any("loss" in b.lower() for b in result.blockers), result.blockers


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("500")])
def test_an_invalid_daily_target_is_no_trade(bad):
    result = setup(daily_target_pct=bad)
    assert result.advice is Advice.NO_TRADE
    assert any("target" in b.lower() for b in result.blockers), result.blockers


def test_a_missing_max_loss_is_no_trade():
    result = morning_setup(capital=CAPITAL, now=at(8, 30),
                           settings=StubSettings(max_loss=None))
    assert result.advice is Advice.NO_TRADE
    assert result.configured_max_loss is None


# --------------------------------------------------------------------------------
# Costs and feasibility
# --------------------------------------------------------------------------------


def test_the_cost_estimate_comes_from_the_shared_cost_model():
    """No duplicated formula. The number must equal src.costs for the same notional."""
    from src.costs import round_trip_costs

    result = setup(now=at(8, 30))
    expected = round_trip_costs(CAPITAL, CAPITAL, 1)
    assert result.estimated_round_trip_cost == expected.total
    assert result.estimated_slippage == expected.slippage


def test_the_cost_estimate_is_invariant_to_the_reference_price():
    """Intraday cost is a function of TURNOVER alone, which is why a synthetic
    reference price is a legitimate unit of account rather than a market assumption."""
    notional = Decimal("100000")
    assert (estimate_round_trip_cost(notional).total
            == estimate_round_trip_cost(notional, reference_price=Decimal("1")).total
            == estimate_round_trip_cost(notional, reference_price=Decimal("2500")).total)


def test_both_legs_are_charged():
    rt = estimate_round_trip_cost(Decimal("100000"))
    assert rt.buy.total > 0 and rt.sell.total > 0
    assert rt.total == rt.buy.total + rt.sell.total


def test_a_target_smaller_than_the_cost_of_one_round_trip_warns_loudly():
    """A fact, not a forecast: you must capture more than 2x the target in gross."""
    result = setup(daily_target_pct=Decimal("0.01"))       # Rs 10 on Rs 1,00,000
    assert result.estimated_round_trip_cost > result.configured_daily_target
    assert any("round trip" in w.lower() and "target" in w.lower()
               for w in result.warnings), result.warnings


def test_a_loss_budget_smaller_than_one_round_trip_is_infeasible():
    """Genuine arithmetic infeasibility: you cannot take even ONE trade and exit at
    break-even gross without breaching the daily loss limit."""
    result = setup(max_daily_loss_pct=Decimal("0.01"))     # Rs 10 loss budget
    assert result.feasibility_status is Feasibility.INFEASIBLE
    assert result.advice is Advice.NO_TRADE
    assert any("loss" in b.lower() and "round trip" in b.lower()
               for b in result.blockers), result.blockers


def test_a_workable_configuration_is_possible_not_predicted():
    result = setup(now=at(8, 30))
    assert result.feasibility_status is Feasibility.POSSIBLE
    assert result.required_gross_pct > result.configured_daily_target_pct, (
        "the required gross move must exceed the target by at least the cost")


def test_required_gross_is_target_plus_cost():
    result = setup()
    expected = ((result.configured_daily_target + result.estimated_round_trip_cost)
                / result.capital * Decimal("100"))
    assert abs(result.required_gross_pct - expected) < Decimal("0.0001")


def test_the_module_never_lowers_the_target_or_raises_the_risk_limit():
    """The configured numbers come back exactly as configured, feasible or not."""
    for target in (Decimal("0.01"), Decimal("1.0"), Decimal("5.0")):
        result = setup(daily_target_pct=target)
        assert result.configured_daily_target_pct == target
        assert result.configured_daily_target == CAPITAL * target / Decimal("100")


def test_money_is_decimal_throughout():
    result = setup()
    for field in ("capital", "configured_daily_target", "configured_max_loss",
                  "estimated_round_trip_cost", "estimated_slippage"):
        assert isinstance(getattr(result, field), Decimal), field


def test_money_is_quantised_to_paise_and_ratios_stay_readable():
    """A raw Decimal division renders 28 significant digits, which is not a number a
    human reads. Money uses the cost model's own paise quantum and rule."""
    result = setup(capital=Decimal("33333"), daily_target_pct=Decimal("0.7"))
    for field in ("capital", "configured_daily_target", "configured_max_loss",
                  "estimated_round_trip_cost", "estimated_slippage"):
        value = getattr(result, field)
        assert -value.as_tuple().exponent == 2, f"{field} is not paise: {value}"

    assert -result.round_trips_within_loss_budget.as_tuple().exponent <= 2
    for pct in (result.required_gross_pct, result.cost_pct_of_capital):
        assert -pct.as_tuple().exponent <= 4, pct


# --------------------------------------------------------------------------------
# Unverified cost rates
# --------------------------------------------------------------------------------


def test_unverified_cost_rates_warn_prominently_and_block_ready():
    result = setup(now=at(10, 0), cost_cfg=CostSettings(rates_verified=False))
    assert result.cost_configuration_verified is False
    assert result.advice is not Advice.READY, (
        "READY would describe an unreliable feasibility result as reliable")
    assert any("unverified" in w.lower() or "contract note" in w.lower()
               for w in result.warnings), result.warnings


def test_verified_cost_rates_permit_ready_during_the_session():
    result = setup(now=at(10, 0), cost_cfg=CostSettings(rates_verified=True))
    assert result.cost_configuration_verified is True
    assert result.advice is Advice.READY, result.warnings


def test_the_feasibility_result_is_never_called_reliable_while_unverified():
    result = setup(cost_cfg=CostSettings(rates_verified=False))
    text = " ".join(result.warnings).lower()
    assert "not" in text and ("reliable" in text or "trusted" in text)


# --------------------------------------------------------------------------------
# Time handling
# --------------------------------------------------------------------------------


def test_generated_at_is_timezone_aware_ist():
    result = setup(now=at(8, 30))
    assert result.generated_at.tzinfo is not None
    assert result.generated_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert result.generated_at == at(8, 30)


def test_a_naive_datetime_is_rejected():
    with pytest.raises(NaiveDatetimeError):
        morning_setup(capital=CAPITAL, now=datetime(2026, 8, 20, 8, 30))


def test_the_injected_clock_makes_it_deterministic():
    a = setup(now=at(9, 30))
    b = setup(now=at(9, 30))
    assert a.as_dict() == b.as_dict()


def test_it_never_reads_the_wall_clock_when_a_clock_is_injected(monkeypatch):
    def explode():
        raise AssertionError("morning setup read the wall clock")

    monkeypatch.setattr("src.morning.setup.now_ist", explode)
    assert setup(now=at(9, 30)).session_state is SessionState.OPEN


# --------------------------------------------------------------------------------
# Planning the next session
# --------------------------------------------------------------------------------


def test_planning_a_future_session_never_permits_trading_now():
    """Rule 6's exception: describing the NEXT session is allowed, permitting is not."""
    result = setup(now=at(16, 0), for_date=date(2026, 8, 21))
    assert result.trade_date == date(2026, 8, 21)
    assert result.planning_only is True
    assert result.advice is not Advice.READY
    assert any("plan" in w.lower() for w in result.warnings), result.warnings


def test_without_for_date_the_current_session_is_described():
    result = setup(now=at(10, 0))
    assert result.trade_date == TRADING_DAY
    assert result.planning_only is False


# --------------------------------------------------------------------------------
# The honesty constraints
# --------------------------------------------------------------------------------


def test_no_trade_is_a_first_class_result_not_an_error():
    result = setup(now=at(10, 0, d=SATURDAY))
    assert result.advice is Advice.NO_TRADE
    assert result.as_dict()["advice"] == "NO_TRADE"
    assert result.generated_at is not None, "a NO_TRADE result is still a full result"
    assert result.session_state is SessionState.CLOSED


def test_the_response_carries_no_symbol_ranking_or_candidate_list():
    payload = setup().as_dict()

    def walk(node, path="root"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in {
                    "symbol", "symbols", "candidate", "candidates", "ranking", "rank",
                    "score", "confidence", "best", "top", "picks", "watchlist",
                    "recommended", "signal", "signals", "prediction",
                }, f"ranking-shaped field {key!r} at {path}"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(payload)


def module_code() -> str:
    """The module's CODE, with every docstring and comment stripped.

    Scoped to code because the prose legitimately discusses what the module does not do
    -- and a naive substring scan flags its own explanation. `"rsi"` matched inside
    "ve*rsi*on" on the first attempt. Comments vanish with `ast.unparse`; docstrings
    have to be removed deliberately.
    """
    import ast
    import inspect

    from src.morning import setup as mod

    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    return ast.unparse(tree)


def assert_absent(code: str, names, why: str):
    """Whole-identifier matching. `rsi` must not match `version`."""
    import re

    for name in names:
        assert not re.search(rf"\b{re.escape(name)}\b", code), f"{why}: {name!r}"


def test_the_module_reaches_no_order_api_and_no_broker_token():
    """Rules 11 and 12: no order endpoint, and no credential needed offline."""
    assert_absent(
        module_code(),
        ("place_order", "modify_order", "cancel_order", "exit_positions",
         "fyers_auth", "FyersAuth", "access_token", "require_token", "basket", "gtt"),
        "morning setup reaches a broker")


def test_the_module_is_independent_of_the_exit_engine():
    """Rule 13. It must not reach the state machine."""
    assert_absent(
        module_code(),
        ("exit_engine", "PositionState", "ExitAdvice", "ExitRule", "evaluate"),
        "morning setup depends on the exit engine")


def test_it_uses_no_indicators_and_no_market_data():
    """Rule 10: no predictions, no technical indicators, no ML."""
    assert_absent(
        module_code(),
        ("indicators", "vwap", "atr", "rsi", "ema", "compute_indicators",
         "sklearn", "LogisticRegression", "xgboost", "torch", "predict", "model",
         "Bar", "bars", "feed", "quote"),
        "morning setup touches market data or a model")


def test_the_module_imports_nothing_beyond_config_costs_and_the_calendar():
    """The dependency surface, asserted rather than described."""
    import ast
    import inspect

    from src.morning import setup as mod

    imported = set()
    for node in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    allowed = {
        "__future__", "dataclasses", "datetime", "decimal", "enum",
        "config.settings", "src.costs", "src.market.calendar",
    }
    assert imported <= allowed, f"unexpected imports: {sorted(imported - allowed)}"


def test_it_runs_offline_with_no_env_and_no_network(monkeypatch):
    """Rule 12: a morning setup must work with no broker credentials at all."""
    for var in ("FYERS_APP_ID", "FYERS_SECRET", "FYERS_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
    result = setup(now=at(8, 30))
    assert result.advice in set(Advice)
    assert result.session_state is SessionState.PRE_OPEN


#: The published output schema. Pinned exactly, so a field cannot be added or renamed
#: without a deliberate decision -- this payload is what any future CLI, API or journal
#: entry will read.
EXPECTED_KEYS = {
    "session_state", "trade_date", "capital",
    "configured_daily_target", "configured_daily_target_pct",
    "configured_max_loss", "configured_max_loss_pct",
    "estimated_round_trip_cost", "estimated_slippage",
    "feasibility_status", "blockers", "warnings", "advice", "generated_at",
    "cost_configuration_verified", "required_gross_pct", "cost_pct_of_capital",
    "round_trips_within_loss_budget", "planning_only",
}

#: Every output the brief requires, by name.
REQUIRED_BY_BRIEF = {
    "session_state", "trade_date", "capital", "configured_daily_target",
    "configured_max_loss", "estimated_round_trip_cost", "estimated_slippage",
    "feasibility_status", "blockers", "warnings", "advice", "generated_at",
    "cost_configuration_verified",
}


def test_as_dict_is_json_serialisable_with_money_as_strings():
    import json

    payload = setup().as_dict()
    text = json.dumps(payload)
    assert '"capital": "100000.00"' in text, text
    assert REQUIRED_BY_BRIEF <= set(payload), REQUIRED_BY_BRIEF - set(payload)


def test_the_output_schema_is_exact_and_stable():
    """Every result carries the same keys, whatever the verdict."""
    for kw in (dict(), dict(capital=None), dict(now=at(10, 0, d=SATURDAY)),
               dict(max_daily_loss_pct=Decimal("0.01")), dict(now=at(16, 0))):
        assert set(setup(**kw).as_dict()) == EXPECTED_KEYS, kw


def test_the_result_is_immutable():
    """A typed, frozen result: nothing downstream can quietly rewrite a verdict."""
    result = setup()
    with pytest.raises(Exception):
        result.advice = Advice.READY


def test_enums_not_bare_strings():
    result = setup()
    assert isinstance(result.session_state, SessionState)
    assert isinstance(result.feasibility_status, Feasibility)
    assert isinstance(result.advice, Advice)


def test_a_shut_market_is_not_reported_as_feasible():
    """A sound target with no session to pursue it in is NOT_APPLICABLE, not POSSIBLE.

    `{"advice": "NO_TRADE", "feasibility_status": "POSSIBLE"}` for a Saturday reads as
    "the plan is fine" on a day the market is shut. The cost arithmetic survives in
    cost_pct_of_capital and required_gross_pct, so refusing the word costs nothing.
    """
    for kw in (dict(now=at(10, 0, d=SATURDAY)),
               dict(now=at(10, 0, d=HOLIDAY)),
               dict(now=at(15, 20)),
               dict(now=at(16, 0))):
        result = setup(**kw)
        assert result.feasibility_status is Feasibility.NOT_APPLICABLE, kw
        assert result.advice is Advice.NO_TRADE, kw
        # The arithmetic is still there to read.
        assert result.cost_pct_of_capital is not None
        assert result.required_gross_pct is not None


def test_a_broken_loss_budget_stays_infeasible_even_on_a_closed_day():
    """A configuration fault is worth reporting whatever the calendar says."""
    result = setup(now=at(10, 0, d=SATURDAY), max_daily_loss_pct=Decimal("0.01"))
    assert result.feasibility_status is Feasibility.INFEASIBLE


def test_missing_capital_stays_unknown_even_on_a_closed_day():
    result = setup(now=at(10, 0, d=SATURDAY), capital=None)
    assert result.feasibility_status is Feasibility.UNKNOWN


def test_advice_is_never_ready_or_limited_unless_feasibility_is_possible():
    """The invariant the whole verdict rests on, over every combination that matters."""
    for day in (TRADING_DAY, SATURDAY, HOLIDAY):
        for hh in (8, 10, 15, 16):
            for capital in (CAPITAL, None, Decimal("-1")):
                for target in (Decimal("1.0"), Decimal("0"), None):
                    for loss in (Decimal("1.0"), Decimal("0.01"), Decimal("0")):
                        r = morning_setup(capital=capital, now=at(hh, 0, d=day),
                                          daily_target_pct=target,
                                          max_daily_loss_pct=loss)
                        if r.advice in (Advice.READY, Advice.LIMITED):
                            assert r.feasibility_status is Feasibility.POSSIBLE, (
                                f"{r.advice} with {r.feasibility_status}")
                            assert not r.blockers, r.blockers


def test_the_gross_hurdle_is_reported_beside_the_net_target():
    """The project's gross/net rule, in the form this module can express it.

    The configured target is what you keep; required_gross_pct is what you must capture
    to keep it. Neither is meaningful without the other, so neither appears alone.
    """
    result = setup()
    assert result.configured_daily_target is not None      # net, after costs
    assert result.required_gross_pct is not None           # gross, before costs
    assert result.estimated_round_trip_cost is not None
    assert result.cost_pct_of_capital is not None
    assert result.required_gross_pct > result.configured_daily_target_pct

    payload = result.as_dict()
    assert (payload["required_gross_pct"] is None) == (
        payload["configured_daily_target"] is None)


def test_the_settings_it_reads_carry_no_credentials():
    """Item 17, precisely.

    The module reads CostSettings and Settings, which pydantic sources from .env. That
    is configuration, not credentials: neither class has a secret, and FyersSettings --
    the only one that does -- is never imported. So no credential is loaded even though
    the same file is consulted for cost rates.
    """
    from config.settings import get_cost_settings, get_settings

    for cfg in (get_settings(), get_cost_settings()):
        for name in dir(cfg):
            assert not any(w in name.lower() for w in ("secret", "token", "app_id",
                                                       "password", "api_key")), name

    assert_absent(module_code(),
                  ("FyersSettings", "get_fyers_settings", "token_path"),
                  "morning setup reaches credential settings")


def test_blockers_always_force_no_trade():
    """The invariant the whole module rests on."""
    cases = [
        dict(now=at(10, 0, d=SATURDAY)),
        dict(capital=None),
        dict(capital=Decimal("-5")),
        dict(max_daily_loss_pct=Decimal("0")),
        dict(now=at(16, 0)),
    ]
    for kw in cases:
        result = setup(**kw)
        assert result.blockers, f"{kw} produced no blocker"
        assert result.advice is Advice.NO_TRADE, kw
