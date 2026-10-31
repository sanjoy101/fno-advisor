"""Tests for the CLI.

Two things matter here beyond "does it run": the advisory framing survives into the
output the user actually reads, and no verb does anything to a broker.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.cli import build_parser, main
from src.positions.store import PositionStore
from tests.conftest import TRADE_DATE


@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path)


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def add(capsys, data_dir, **over):
    argv = [
        "--data-dir", data_dir, "add-position",
        "--symbol", over.get("symbol", "RELIANCE"),
        "--qty", str(over.get("qty", 100)),
        "--price", str(over.get("price", "1000.00")),
        "--entry-time", over.get("entry_time", f"{TRADE_DATE}T09:20:00+05:30"),
        "--position-id", over.get("position_id", "p-cli"),
    ]
    for flag in ("stop", "target"):
        if flag in over:
            argv += [f"--{flag}", str(over[flag])]
    return run(capsys, *argv)


# --------------------------------------------------------------------------------
# add-position
# --------------------------------------------------------------------------------


def test_add_position_persists_it(capsys, data_dir):
    code, out, _ = add(capsys, data_dir)
    assert code == 0
    assert "Recorded p-cli" in out
    stored = PositionStore(data_dir).get_position("p-cli", TRADE_DATE)
    assert stored.symbol == "RELIANCE"
    assert stored.entry_price == Decimal("1000.00")
    assert stored.quantity == 100


def test_add_position_also_seeds_runtime_state(capsys, data_dir):
    add(capsys, data_dir)
    rt = PositionStore(data_dir).load_runtime("p-cli", TRADE_DATE)
    assert rt is not None
    assert rt.remaining_quantity == 100


def test_add_position_generates_an_id_when_not_given(capsys, data_dir):
    code, out, _ = run(
        capsys, "--data-dir", data_dir, "add-position",
        "--symbol", "infy", "--qty", "10", "--price", "1500",
        "--entry-time", f"{TRADE_DATE}T09:20:00+05:30",
    )
    assert code == 0
    assert "INFY-" in out


def test_add_position_records_optional_levels(capsys, data_dir):
    add(capsys, data_dir, stop="990.00", target="1020.00")
    stored = PositionStore(data_dir).get_position("p-cli", TRADE_DATE)
    assert stored.stop_loss == Decimal("990.00")
    assert stored.target == Decimal("1020.00")


def test_add_position_explains_the_defaults_when_levels_are_omitted(capsys, data_dir):
    _, out, _ = add(capsys, data_dir)
    assert "1.5x ATR" in out
    assert "ATR trail" in out


def test_add_position_rejects_a_stop_above_entry(capsys, data_dir):
    code, _, err = add(capsys, data_dir, stop="1010.00")
    assert code == 1
    assert "stop_loss" in err


def test_add_position_warns_on_a_non_trading_day(capsys, data_dir):
    _, out, _ = add(capsys, data_dir, entry_time="2026-08-22T09:20:00+05:30")
    assert "not an NSE trading day" in out


def test_add_position_rejects_a_bad_price(capsys, data_dir):
    with pytest.raises(SystemExit):
        run(capsys, "--data-dir", data_dir, "add-position",
            "--symbol", "X", "--qty", "1", "--price", "not-a-number")


# --------------------------------------------------------------------------------
# list-positions
# --------------------------------------------------------------------------------


def test_list_positions_shows_what_was_added(capsys, data_dir):
    add(capsys, data_dir)
    code, out, _ = run(capsys, "--data-dir", data_dir, "list-positions",
                       "--date", str(TRADE_DATE))
    assert code == 0
    assert "p-cli" in out and "RELIANCE" in out


def test_list_positions_on_an_empty_day(capsys, data_dir):
    code, out, _ = run(capsys, "--data-dir", data_dir, "list-positions", "--date", "2026-08-19")
    assert code == 0
    assert "No positions" in out


# --------------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------------


def test_replay_demo_runs_end_to_end(capsys, data_dir):
    add(capsys, data_dir, price="1018.00", entry_time=f"{TRADE_DATE}T09:45:00+05:30")
    code, out, _ = run(capsys, "--data-dir", data_dir, "replay",
                       "--position-id", "p-cli", "--date", str(TRADE_DATE), "--demo")
    assert code == 0
    assert "State changes" in out
    assert "evaluations" in out


def test_replay_labels_synthetic_data_as_not_market_data(capsys, data_dir):
    add(capsys, data_dir, price="1018.00", entry_time=f"{TRADE_DATE}T09:45:00+05:30")
    _, out, _ = run(capsys, "--data-dir", data_dir, "replay",
                    "--position-id", "p-cli", "--date", str(TRADE_DATE), "--demo")
    assert "SYNTHETIC" in out


def test_replay_reports_gross_and_net(capsys, data_dir):
    add(capsys, data_dir, price="1018.00", entry_time=f"{TRADE_DATE}T09:45:00+05:30")
    _, out, _ = run(capsys, "--data-dir", data_dir, "replay",
                    "--position-id", "p-cli", "--date", str(TRADE_DATE), "--demo")
    assert "gross" in out and "net" in out


def test_replay_can_write_to_the_journal(capsys, data_dir):
    add(capsys, data_dir, price="1018.00", entry_time=f"{TRADE_DATE}T09:45:00+05:30")
    run(capsys, "--data-dir", data_dir, "replay", "--position-id", "p-cli",
        "--date", str(TRADE_DATE), "--demo", "--log")
    assert PositionStore(data_dir).read_advice(TRADE_DATE)


def test_replay_without_bars_fails_loudly(capsys, data_dir):
    add(capsys, data_dir)
    code, _, err = run(capsys, "--data-dir", data_dir, "replay",
                       "--position-id", "p-cli", "--date", str(TRADE_DATE))
    assert code == 1
    assert "No stored bars" in err


def test_replay_of_an_unknown_position_fails_loudly(capsys, data_dir):
    code, _, err = run(capsys, "--data-dir", data_dir, "replay",
                       "--position-id", "ghost", "--date", str(TRADE_DATE), "--demo")
    assert code == 1
    assert "No position" in err


# --------------------------------------------------------------------------------
# watch
# --------------------------------------------------------------------------------


def test_watch_demo_prints_state_changes(capsys, data_dir):
    add(capsys, data_dir, price="1018.00", entry_time=f"{TRADE_DATE}T09:45:00+05:30")
    code, out, _ = run(capsys, "--data-dir", data_dir, "watch",
                       "--date", str(TRADE_DATE), "--replay-demo")
    assert code == 0
    assert "RELIANCE" in out
    assert "reason" in out


def test_watch_tells_you_to_execute_manually(capsys, data_dir):
    add(capsys, data_dir, price="1018.00", entry_time=f"{TRADE_DATE}T09:45:00+05:30")
    _, out, _ = run(capsys, "--data-dir", data_dir, "watch",
                    "--date", str(TRADE_DATE), "--replay-demo")
    assert "broker app" in out


def test_watch_with_no_positions_says_so(capsys, data_dir):
    code, out, _ = run(capsys, "--data-dir", data_dir, "watch", "--date", "2026-08-19")
    assert code == 0
    assert "Nothing to watch" in out


def test_watch_without_bars_fails_loudly(capsys, data_dir):
    add(capsys, data_dir)
    code, _, err = run(capsys, "--data-dir", data_dir, "watch", "--date", str(TRADE_DATE))
    assert code == 1
    assert "No stored bars" in err


# --------------------------------------------------------------------------------
# book-partial / close-position -- recording, never executing
# --------------------------------------------------------------------------------


def test_book_partial_reduces_the_held_quantity(capsys, data_dir):
    add(capsys, data_dir)
    code, out, _ = run(capsys, "--data-dir", data_dir, "book-partial",
                       "--position-id", "p-cli", "--qty", "50", "--date", str(TRADE_DATE))
    assert code == 0
    assert "Still held: 50" in out
    assert PositionStore(data_dir).load_runtime("p-cli", TRADE_DATE).remaining_quantity == 50


def test_book_partial_refuses_to_empty_the_position(capsys, data_dir):
    add(capsys, data_dir)
    code, _, err = run(capsys, "--data-dir", data_dir, "book-partial",
                       "--position-id", "p-cli", "--qty", "100", "--date", str(TRADE_DATE))
    assert code == 1
    assert "close-position" in err


def test_close_position_reports_realised_pnl_gross_and_net(capsys, data_dir):
    add(capsys, data_dir)
    code, out, _ = run(capsys, "--data-dir", data_dir, "close-position",
                       "--position-id", "p-cli", "--price", "1010.00",
                       "--exit-time", f"{TRADE_DATE}T14:05:00+05:30", "--date", str(TRADE_DATE))
    assert code == 0
    assert "gross" in out and "net" in out
    assert "realised" in out
    assert PositionStore(data_dir).position_status("p-cli", TRADE_DATE) == "CLOSED"


def test_close_position_uses_the_remaining_quantity_after_a_partial(capsys, data_dir):
    add(capsys, data_dir)
    run(capsys, "--data-dir", data_dir, "book-partial", "--position-id", "p-cli",
        "--qty", "60", "--date", str(TRADE_DATE))
    _, out, _ = run(capsys, "--data-dir", data_dir, "close-position",
                    "--position-id", "p-cli", "--price", "1010.00",
                    "--exit-time", f"{TRADE_DATE}T14:05:00+05:30", "--date", str(TRADE_DATE))
    assert "Closed p-cli: 40 @" in out


def test_close_of_an_unknown_position_fails_loudly(capsys, data_dir):
    code, _, err = run(capsys, "--data-dir", data_dir, "close-position",
                       "--position-id", "ghost", "--price", "100", "--date", str(TRADE_DATE))
    assert code == 1


# --------------------------------------------------------------------------------
# check-entry -- asks, records nothing
# --------------------------------------------------------------------------------


def test_check_entry_runs_against_a_demo_session(capsys, data_dir):
    code, out, _ = run(capsys, "--data-dir", data_dir, "check-entry",
                       "--symbol", "RELIANCE", "--date", str(TRADE_DATE), "--demo")
    assert code in (0, 2)
    assert "RELIANCE" in out
    assert "ENTRY" in out


def test_check_entry_records_nothing(capsys, data_dir):
    """The whole point: ask before buying, as often as you like."""
    run(capsys, "--data-dir", data_dir, "check-entry", "--symbol", "RELIANCE",
        "--date", str(TRADE_DATE), "--demo")
    store = PositionStore(data_dir)
    assert store.list_positions(TRADE_DATE, status=None) == []
    assert store.read_advice(TRADE_DATE) == []
    assert store.load_runtime("RELIANCE", TRADE_DATE) is None


def test_check_entry_says_nothing_was_recorded(capsys, data_dir):
    _, out, _ = run(capsys, "--data-dir", data_dir, "check-entry", "--symbol",
                    "RELIANCE", "--date", str(TRADE_DATE), "--demo")
    assert "Nothing was recorded" in out


def test_check_entry_labels_synthetic_data(capsys, data_dir):
    _, out, _ = run(capsys, "--data-dir", data_dir, "check-entry", "--symbol",
                    "RELIANCE", "--date", str(TRADE_DATE), "--demo")
    assert "SYNTHETIC" in out


def test_check_entry_exit_code_signals_validity(capsys, data_dir):
    """0 = sound, 2 = unsound, so it is usable as a shell guard before buying."""
    early, _, _ = run(capsys, "--data-dir", data_dir, "check-entry", "--symbol",
                      "RELIANCE", "--date", str(TRADE_DATE), "--demo", "--at", "10:00")
    late, _, _ = run(capsys, "--data-dir", data_dir, "check-entry", "--symbol",
                     "RELIANCE", "--date", str(TRADE_DATE), "--demo", "--at", "11:40")
    assert {early, late} <= {0, 2}
    assert early != late, "a rising and a falling moment should not score the same"


def test_check_entry_reports_a_proposed_stop(capsys, data_dir):
    _, out, _ = run(capsys, "--data-dir", data_dir, "check-entry", "--symbol",
                    "RELIANCE", "--date", str(TRADE_DATE), "--demo",
                    "--at", "10:00", "--price", "1010", "--stop", "1015")
    assert "stop" in out


def test_bare_hhmm_resolves_against_the_date_flag_not_today(capsys, data_dir):
    """A bare HH:MM used to resolve against today, silently querying the wrong day."""
    from src.cli import _parse_time

    assert _parse_time("10:00", on=TRADE_DATE).date() == TRADE_DATE
    assert _parse_time("10:00").date() == date.today()


def test_check_entry_at_different_times_can_disagree(capsys, data_dir):
    _, early, _ = run(capsys, "--data-dir", data_dir, "check-entry", "--symbol",
                      "RELIANCE", "--date", str(TRADE_DATE), "--demo", "--at", "10:00")
    _, late, _ = run(capsys, "--data-dir", data_dir, "check-entry", "--symbol",
                     "RELIANCE", "--date", str(TRADE_DATE), "--demo", "--at", "11:40")
    assert early != late, "the two moments must be evaluated on different bars"


def test_check_entry_without_bars_fails_loudly(capsys, data_dir):
    code, _, err = run(capsys, "--data-dir", data_dir, "check-entry",
                       "--symbol", "NOSUCH", "--date", str(TRADE_DATE))
    assert code == 1
    assert "No stored bars" in err


def test_check_entry_shows_the_indicator_levels(capsys, data_dir):
    _, out, _ = run(capsys, "--data-dir", data_dir, "check-entry", "--symbol",
                    "RELIANCE", "--date", str(TRADE_DATE), "--demo", "--at", "11:00")
    for token in ("VWAP", "EMA9", "EMA21", "RSI14"):
        assert token in out


# --------------------------------------------------------------------------------
# journal / session
# --------------------------------------------------------------------------------


def test_journal_summarises_states(capsys, data_dir):
    add(capsys, data_dir, price="1018.00", entry_time=f"{TRADE_DATE}T09:45:00+05:30")
    run(capsys, "--data-dir", data_dir, "replay", "--position-id", "p-cli",
        "--date", str(TRADE_DATE), "--demo", "--log")
    code, out, _ = run(capsys, "--data-dir", data_dir, "journal")
    assert code == 0
    assert "state" in out


def test_journal_on_an_empty_store(capsys, data_dir):
    code, out, _ = run(capsys, "--data-dir", data_dir, "journal")
    assert code == 0
    assert "empty" in out


def test_session_reports_market_status(capsys, data_dir):
    code, out, _ = run(capsys, "--data-dir", data_dir, "session")
    assert code == 0
    assert "IST" in out


# --------------------------------------------------------------------------------
# Advisory framing
# --------------------------------------------------------------------------------


def test_banner_states_it_never_places_orders(capsys, data_dir):
    add(capsys, data_dir, price="1018.00", entry_time=f"{TRADE_DATE}T09:45:00+05:30")
    _, out, _ = run(capsys, "--data-dir", data_dir, "replay",
                    "--position-id", "p-cli", "--date", str(TRADE_DATE), "--demo")
    assert "never places orders" in out


def test_no_verb_sounds_like_it_places_an_order():
    """The CLI vocabulary must not imply execution."""
    parser = build_parser()
    actions = [a for a in parser._actions if isinstance(a, type(parser._subparsers._actions[0]))]
    verbs = set()
    for action in parser._subparsers._group_actions:
        verbs |= set(action.choices)
    forbidden = {"buy", "sell", "order", "place", "execute", "trade", "square-off"}
    assert not (verbs & forbidden), f"execution-sounding verbs: {verbs & forbidden}"


def test_cli_imports_no_broker_order_api():
    """Nothing in the CLI's import graph reaches an order endpoint."""
    import inspect

    import src.cli as cli

    source = inspect.getsource(cli)
    for token in ("place_order", "modify_order", "cancel_order", "exit_positions"):
        assert token not in source


def test_unknown_command_exits_nonzero(capsys):
    with pytest.raises(SystemExit):
        main(["nonsense"])


# --------------------------------------------------------------------------------
# dashboard: the mode default is a safety property, not a convenience
# --------------------------------------------------------------------------------


def _dashboard_args(*argv):
    return build_parser().parse_args(["dashboard", *argv])


def test_dashboard_defaults_to_replay_not_live():
    """The whole point of the default. A mistyped command must not open LIVE."""
    assert _dashboard_args().mode == "replay"


def test_dashboard_live_must_be_asked_for_explicitly():
    assert _dashboard_args("--mode", "live").mode == "live"


def test_dashboard_rejects_an_unknown_mode():
    with pytest.raises(SystemExit):
        _dashboard_args("--mode", "paper")


def test_dashboard_binds_loopback_by_default():
    """There is no authentication on this page; it must not default to routable."""
    assert _dashboard_args().host == "127.0.0.1"
    assert _dashboard_args().port == 8000


def test_dashboard_takes_date_and_speed_as_flags():
    args = _dashboard_args("--date", "2024-11-26", "--speed", "120")
    assert args.date == "2024-11-26"
    assert args.speed == 120.0


def _free_port() -> str:
    """An unused port, so these tests never depend on 8000 being free.

    They previously used the default port and failed with the port-collision message
    when any dashboard happened to be running -- the right error for the wrong
    question.
    """
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def test_dashboard_replay_without_a_date_fails_loudly(capsys, data_dir):
    """Rather than serving a blank page that looks like a quiet market."""
    code, _, err = run(capsys, "--data-dir", data_dir, "dashboard",
                       "--port", _free_port())
    assert code == 1
    assert "needs a date" in err


def test_dashboard_with_no_stored_bars_says_so(capsys, data_dir):
    code, _, err = run(capsys, "--data-dir", data_dir, "dashboard",
                       "--date", "2019-01-02", "--port", _free_port())
    assert code == 1
    assert "no stored bars" in err


def test_an_argument_error_is_reported_even_when_the_port_is_busy(capsys, data_dir):
    """Argument errors are unconditional; port availability is environmental.

    Checking the port first meant a stale dashboard masked "needs a date" with a
    message about something the user had not asked about.
    """
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    busy = str(listener.getsockname()[1])
    try:
        code, _, err = run(capsys, "--data-dir", data_dir, "dashboard", "--port", busy)
        assert code == 1
        assert "needs a date" in err, f"port error masked the argument error: {err}"
    finally:
        listener.close()


def test_dashboard_mode_is_not_read_from_the_environment(monkeypatch):
    """Mode comes from the flag alone.

    An env var that can flip a dashboard to LIVE is a way to be looking at real money
    while believing you are in replay, so nothing reads one.
    """
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("DASHBOARD_MODE", "live")
    monkeypatch.setenv("FNO_MODE", "live")
    assert _dashboard_args().mode == "replay"


def test_the_dashboard_refuses_a_port_that_is_already_serving(capsys, data_dir):
    """Otherwise you launch --date A, the bind fails, and your browser shows date B.

    That is how three "bugs" in a rendered dashboard turned out to be one stale
    process: the page was a different session entirely, and nothing said so.
    """
    import socket
    import threading

    from src.api.serve import check_port_free

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    threading.Thread(target=lambda: None, daemon=True).start()
    try:
        with pytest.raises(ValueError, match="already in use"):
            check_port_free("127.0.0.1", port)
    finally:
        listener.close()


def test_check_port_free_passes_on_an_unused_port():
    import socket

    from src.api.serve import check_port_free

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    check_port_free("127.0.0.1", port)  # must not raise


# --------------------------------------------------------------------------------
# morning -- Module A, wired to the CLI
#
# The command is a THIN presenter. Every rule lives in src/morning/setup.py; these
# tests check that the CLI passes arguments through, renders the answer faithfully,
# and adds no judgement of its own.
# --------------------------------------------------------------------------------

MORNING_DAY = "2026-08-20"      # Thursday, a real session
MORNING_SAT = "2026-08-22"
MORNING_HOLIDAY = "2026-01-26"  # Republic Day


def morning(capsys, *extra, capital="100000", date_=MORNING_DAY, at="08:30"):
    argv = ["morning"]
    if capital is not None:
        argv += ["--capital", capital]
    if date_ is not None:
        argv += ["--date", date_]
    if at is not None:
        argv += ["--at", at]
    return run(capsys, *argv, *extra)


def test_morning_with_valid_arguments_prints_the_full_answer(capsys):
    code, out, _ = morning(capsys, "--target-pct", "1.0", "--max-loss-pct", "1.0")
    assert code == 0, out
    lowered = out.lower()
    for required in ("session", "advice", "feasibility", "capital", "daily target",
                     "max daily loss", "round-trip cost", "required gross",
                     "cost rates"):
        assert required in lowered, f"{required!r} missing from the output"


def test_the_documented_command_line_works_exactly_as_written(capsys):
    """The exact invocation from the brief, with no --date or --at."""
    code, out, _ = run(capsys, "morning", "--capital", "100000",
                       "--target-pct", "1.0", "--max-loss-pct", "1.0")
    assert code == 0, out
    assert "MORNING SETUP" in out


def test_missing_capital_is_refused_and_never_defaulted(capsys):
    """argparse's own required-argument path. Capital is never invented."""
    with pytest.raises(SystemExit) as exc:
        morning(capsys, capital=None)
    assert exc.value.code != 0
    assert "capital" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("bad", ["abc", "", "1,00,000"])
def test_an_unparseable_capital_is_rejected_with_a_clear_error(capsys, bad):
    with pytest.raises(SystemExit) as exc:
        morning(capsys, capital=bad)
    assert exc.value.code != 0
    assert "not a number" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_non_positive_capital_is_no_trade(capsys, bad):
    """Parseable but invalid: the module blocks it, and the CLI reports that."""
    code, out, _ = morning(capsys, capital=bad)
    assert code == 0, "a computed NO_TRADE is a valid answer, not a CLI failure"
    assert "NO_TRADE" in out
    assert "capital" in out.lower()


@pytest.mark.parametrize("flag,bad", [
    ("--target-pct", "0"), ("--target-pct", "-1"), ("--target-pct", "500"),
    ("--max-loss-pct", "0"), ("--max-loss-pct", "-2"), ("--max-loss-pct", "500"),
])
def test_an_invalid_percentage_is_no_trade_with_a_named_blocker(capsys, flag, bad):
    code, out, _ = morning(capsys, flag, bad)
    assert code == 0
    assert "NO_TRADE" in out
    word = "target" if "target" in flag else "loss"
    assert word in out.lower()


@pytest.mark.parametrize("flag", ["--target-pct", "--max-loss-pct"])
def test_an_unparseable_percentage_is_rejected(capsys, flag):
    with pytest.raises(SystemExit) as exc:
        morning(capsys, flag, "abc")
    assert exc.value.code != 0
    assert "not a number" in capsys.readouterr().err.lower()


# ---- the six status words, each distinguishable on screen ----------------------


def test_pre_open_output(capsys):
    code, out, _ = morning(capsys, at="08:30")
    assert code == 0
    assert "PRE_OPEN" in out
    assert "not permission" in out.lower()


def test_open_output(capsys):
    code, out, _ = morning(capsys, at="10:00")
    assert code == 0
    assert "OPEN" in out and "PRE_OPEN" not in out


def test_closed_output(capsys):
    code, out, _ = morning(capsys, at="16:00")
    assert code == 0
    assert "CLOSED" in out
    assert "NO_TRADE" in out


def test_after_square_off_output(capsys):
    code, out, _ = morning(capsys, at="15:20")
    assert code == 0
    assert "AFTER_SQUARE_OFF" in out
    assert "NO_TRADE" in out


def test_holiday_output_names_the_holiday(capsys):
    code, out, _ = morning(capsys, date_=MORNING_HOLIDAY, at="10:00")
    assert code == 0
    assert "NO_TRADE" in out
    assert "republic day" in out.lower()


def test_weekend_output(capsys):
    code, out, _ = morning(capsys, date_=MORNING_SAT, at="10:00")
    assert code == 0
    assert "NO_TRADE" in out
    assert "weekend" in out.lower()


def test_not_applicable_is_shown_for_a_shut_market(capsys):
    code, out, _ = morning(capsys, date_=MORNING_SAT, at="10:00")
    assert "NOT_APPLICABLE" in out
    assert "POSSIBLE" not in out, "a shut market must not read as feasible"


def test_infeasible_is_shown_when_costs_exceed_the_loss_budget(capsys):
    code, out, _ = morning(capsys, "--max-loss-pct", "0.01")
    assert code == 0
    assert "INFEASIBLE" in out
    assert "NO_TRADE" in out
    assert "round trip" in out.lower()


def test_unknown_is_shown_when_capital_cannot_be_used(capsys):
    code, out, _ = morning(capsys, capital="0")
    assert code == 0
    assert "UNKNOWN" in out


def test_possible_is_labelled_as_arithmetic_not_probability(capsys):
    code, out, _ = morning(capsys)
    assert "POSSIBLE" in out
    lowered = out.lower()
    assert "arithmetic" in lowered
    assert "not a probability" in lowered or "not a prediction" in lowered


# ---- numbers -------------------------------------------------------------------


def test_the_unverified_cost_warning_is_printed(capsys):
    code, out, _ = morning(capsys)
    assert "UNVERIFIED" in out
    assert "contract note" in out.lower()


def test_the_gross_hurdle_and_cost_estimate_are_printed(capsys):
    code, out, _ = morning(capsys)
    assert "182.68" in out, "the round-trip cost estimate must be on screen"
    assert "1.1827" in out, "the required gross percentage must be on screen"
    assert "100.00" in out, "slippage must be itemised"


def test_money_is_printed_without_binary_float_artefacts(capsys):
    code, out, _ = morning(capsys, "--target-pct", "0.7", capital="33333.33")
    assert code == 0
    assert "0000000" not in out and "9999999" not in out, out


def test_the_disclaimer_is_printed(capsys):
    code, out, _ = morning(capsys)
    lowered = out.lower()
    assert "planning guidance only" in lowered
    assert "not a prediction" in lowered
    assert "not a recommendation" in lowered
    assert "no order" in lowered


def test_the_output_is_deterministic_for_the_same_inputs(capsys):
    first = morning(capsys)[1]
    second = morning(capsys)[1]
    assert first == second


# ---- JSON ----------------------------------------------------------------------


def test_json_output_is_valid_and_behind_an_explicit_flag(capsys):
    import json

    code, out, _ = morning(capsys, "--json")
    assert code == 0
    payload = json.loads(out)
    for key in ("session_state", "trade_date", "capital", "configured_daily_target",
                "configured_max_loss", "estimated_round_trip_cost",
                "estimated_slippage", "feasibility_status", "blockers", "warnings",
                "advice", "generated_at", "cost_configuration_verified"):
        assert key in payload, key
    assert payload["capital"] == "100000.00"
    assert isinstance(payload["capital"], str), "money must not be a JSON float"


def test_json_carries_the_disclaimer_so_it_cannot_be_lost(capsys):
    import json

    payload = json.loads(morning(capsys, "--json")[1])
    text = payload["disclaimer"].lower()
    assert "not a prediction" in text and "no order" in text


def test_without_the_flag_the_output_is_not_json(capsys):
    import json

    with pytest.raises(json.JSONDecodeError):
        json.loads(morning(capsys)[1])


# ---- the honesty constraints ---------------------------------------------------


def test_the_morning_command_reads_no_broker_token(capsys, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("the morning command read the broker token")

    monkeypatch.setattr("src.data.fyers_auth.FyersAuth.load", explode)
    monkeypatch.setattr("src.data.fyers_auth.require_token", explode)
    assert morning(capsys)[0] == 0


def test_the_morning_command_opens_no_socket(capsys, monkeypatch):
    import socket

    def explode(*a, **k):
        raise AssertionError("the morning command opened a socket")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    assert morning(capsys)[0] == 0


def test_the_output_carries_no_symbol_ranking_or_prediction(capsys):
    """Scoped to the REPORT BODY, not the disclaimer.

    The disclaimer necessarily contains the words it disclaims -- "not a recommendation
    to buy or sell" -- so scanning the whole output flags the guard's own safety text.
    Same scoping as the bulk-check table, whose surrounding prose says "not a ranking".
    """
    from src.cli import MORNING_DISCLAIMER

    out = morning(capsys)[1]
    body = out.split(MORNING_DISCLAIMER[:40])[0].lower()
    assert len(body) > 200, "the split removed the report as well as the disclaimer"

    # NOTE: "probability" is deliberately NOT banned. The report prints "not a
    # probability" beside the feasibility, which is required, and which
    # test_possible_is_labelled_as_arithmetic_not_probability asserts. The test is
    # about CLAIMS, not vocabulary: "probability: 0.72" would be a claim, "not a
    # probability" is the opposite of one.
    for banned in ("symbol", "candidate", "ranking", "rank ", "score",
                   "confidence", "best ", "buy ", "sell ", "forecast"):
        assert banned not in body, f"the morning report implies {banned!r}"


def test_the_command_adds_no_business_logic_of_its_own():
    """A thin presenter: it must not recompute anything the module decides."""
    import inspect

    from src import cli

    source = inspect.getsource(cli.cmd_morning)
    for banned in ("round_trip_costs", "is_trading_day", "holiday_name",
                   "Feasibility.", "SessionState.", "square_off_time",
                   "rates_verified"):
        assert banned not in source, (
            f"cmd_morning recomputes {banned!r} -- that belongs in src/morning/setup.py")
    assert "morning_setup(" in source


def test_morning_reaches_no_order_api():
    import inspect

    from src import cli

    source = inspect.getsource(cli.cmd_morning)
    for banned in ("place_order", "modify_order", "cancel_order", "exit_positions"):
        assert banned not in source


# ---- existing commands unchanged -----------------------------------------------


def test_adding_morning_did_not_change_the_existing_verbs(capsys):
    """Every previously-registered subcommand still parses and still runs."""
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    verbs = set(actions[0].choices)
    for verb in ("add-position", "list-positions", "book-partial", "close-position",
                 "watch", "replay", "journal", "check-entry", "auth", "backfill",
                 "backfill-status", "clear-positions", "dashboard", "session",
                 "collect-announcements"):
        assert verb in verbs, f"{verb} disappeared from the CLI"
    assert "morning" in verbs


def test_session_command_is_behaviourally_unchanged(capsys):
    code, out, _ = run(capsys, "session")
    assert code == 0
    assert "fno-advisor" in out


# --------------------------------------------------------------------------------
# What limits --symbols
#
# The socket takes 5000; the startup history seed is one paced REST call per symbol
# and is what actually binds. The guard exists to catch a typo, not to keep the F&O
# universe out -- ~180 names must fit.
# --------------------------------------------------------------------------------


def test_the_whole_fno_universe_fits_under_the_guard():
    from src.api.serve import MAX_SYMBOLS, SOCKET_SYMBOL_CAP
    from src.data.backfill import LIQUID_FNO_UNIVERSE

    assert len(LIQUID_FNO_UNIVERSE) <= MAX_SYMBOLS
    assert MAX_SYMBOLS < SOCKET_SYMBOL_CAP, \
        "the guard is a practical one, well under the broker's"


def test_a_hundred_and_eighty_symbols_are_accepted(tmp_path):
    from src.api.serve import validate_request
    from src.positions.store import PositionStore

    names = [f"SYM{i:03d}" for i in range(180)]
    got = validate_request(PositionStore(tmp_path), mode="live", symbols=names)
    assert len(got) == 180


def test_past_the_guard_the_error_names_the_real_constraint(tmp_path):
    from src.api.serve import MAX_SYMBOLS, validate_request
    from src.positions.store import PositionStore

    names = [f"SYM{i:04d}" for i in range(MAX_SYMBOLS + 1)]
    with pytest.raises(ValueError) as exc:
        validate_request(PositionStore(tmp_path), mode="live", symbols=names)
    text = str(exc.value)
    assert str(MAX_SYMBOLS) in text
    assert "REST history call at startup" in text, \
        "the error must say what binds, not just that a limit exists"
    assert "5000" in text, "and that the socket is not it"


def test_the_seed_estimate_matches_the_pacing_that_produces_it():
    """A stale estimate is worse than none -- it is a promise the code breaks."""
    from src.api.serve import seed_seconds_estimate
    from src.data.backfill import RateLimiter

    limiter = RateLimiter()
    assert limiter.min_interval == pytest.approx(1.0 / 3.0)
    assert limiter.max_per_minute == 100

    assert seed_seconds_estimate(0) == 0.0
    assert seed_seconds_estimate(1) == 0.0
    # The per-MINUTE cap binds at every count above one -- 0.6s apart beats 0.33s --
    # so the estimate is ~0.6s per symbol, not the per-second cap's 0.33s.
    assert seed_seconds_estimate(6) == pytest.approx(3.0, abs=0.01)
    assert seed_seconds_estimate(180) == pytest.approx(107.4, abs=0.5)
    assert seed_seconds_estimate(250) == pytest.approx(149.4, abs=0.5)


def test_duplicate_symbols_are_collapsed():
    """Two identical rows are indistinguishable from one row rendered twice."""
    from src.api.serve import resolve_symbols

    assert resolve_symbols("RELIANCE,reliance, RELIANCE ") == ["RELIANCE"]
    assert resolve_symbols("INFY,RELIANCE,INFY") == ["INFY", "RELIANCE"]


def test_an_empty_symbol_list_is_refused(tmp_path):
    from src.api.serve import validate_request
    from src.positions.store import PositionStore

    with pytest.raises(ValueError, match="resolved to nothing"):
        validate_request(PositionStore(tmp_path), mode="live", symbols=" , ,")


def test_the_dashboard_help_states_the_limit(capsys):
    import src.cli as cli

    with pytest.raises(SystemExit):
        cli.main(["dashboard", "--help"])
    out = capsys.readouterr().out
    assert "5000" in out and "180" in out and "250" in out


# --------------------------------------------------------------------------------
# clear-positions: destructive, so it must be hard to do by accident
# --------------------------------------------------------------------------------


def test_clear_positions_does_nothing_without_confirm(capsys, data_dir):
    add(capsys, data_dir)
    code, out, _ = run(capsys, "--data-dir", data_dir, "clear-positions",
                       "--date", str(TRADE_DATE))
    assert code == 1
    assert "Nothing was deleted" in out
    _, listed, _ = run(capsys, "--data-dir", data_dir, "list-positions",
                       "--date", str(TRADE_DATE))
    assert "p-cli" in listed


def test_clear_positions_lists_what_it_would_delete_before_asking(capsys, data_dir):
    add(capsys, data_dir)
    _, out, _ = run(capsys, "--data-dir", data_dir, "clear-positions",
                    "--date", str(TRADE_DATE))
    assert "p-cli" in out and "RELIANCE" in out
    assert "never touches a broker" in out


def test_clear_positions_with_confirm_deletes(capsys, data_dir):
    add(capsys, data_dir)
    code, out, _ = run(capsys, "--data-dir", data_dir, "clear-positions",
                       "--date", str(TRADE_DATE), "--confirm")
    assert code == 0
    assert "Deleted 1 position" in out
    _, listed, _ = run(capsys, "--data-dir", data_dir, "list-positions",
                       "--date", str(TRADE_DATE))
    assert "p-cli" not in listed


def test_clear_positions_can_target_one_id(capsys, data_dir):
    add(capsys, data_dir, position_id="keep-me")
    add(capsys, data_dir, position_id="drop-me", price="1001.00")
    code, _, _ = run(capsys, "--data-dir", data_dir, "clear-positions",
                     "--date", str(TRADE_DATE), "--position-id", "drop-me", "--confirm")
    assert code == 0
    _, listed, _ = run(capsys, "--data-dir", data_dir, "list-positions",
                       "--date", str(TRADE_DATE))
    assert "keep-me" in listed and "drop-me" not in listed


def test_clear_positions_on_an_empty_date_is_not_an_error(capsys, data_dir):
    code, out, _ = run(capsys, "--data-dir", data_dir, "clear-positions",
                       "--date", "2020-01-02", "--confirm")
    assert code == 0
    assert "Nothing to clear" in out


def test_clear_positions_also_drops_runtime_state(capsys, data_dir):
    """A cleared position must not leave a trailing stop to be resumed against."""
    from src.positions.store import PositionStore

    add(capsys, data_dir)
    store = PositionStore(data_dir)
    assert store.load_runtime("p-cli", TRADE_DATE) is not None
    run(capsys, "--data-dir", data_dir, "clear-positions",
        "--date", str(TRADE_DATE), "--confirm")
    assert store.load_runtime("p-cli", TRADE_DATE) is None


def test_both_front_doors_share_one_implementation():
    """The CLI verb and the script must not be two copies of the wiring."""
    import inspect

    import scripts.serve_dashboard as script
    import src.cli as cli

    assert "src.api.serve" in inspect.getsource(cli.cmd_dashboard)
    assert "from src.api.serve import serve" in inspect.getsource(script)
