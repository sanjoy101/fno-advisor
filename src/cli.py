"""Command line interface for Module C -- Position Tracker.

    python -m src.cli check-entry --symbol RELIANCE --price 1234.50
    python -m src.cli add-position --symbol RELIANCE --qty 100 --price 1234.50
    python -m src.cli list-positions
    python -m src.cli watch --replay-demo
    python -m src.cli replay --symbol RELIANCE --position-id p-1
    python -m src.cli book-partial --position-id p-1 --qty 50
    python -m src.cli close-position --position-id p-1 --price 1250.10
    python -m src.cli journal

ADVISORY ONLY. Nothing here places, modifies, or cancels an order. ``book-partial``
and ``close-position`` RECORD what you already did in your broker app; they do not
do it for you.

Uses argparse rather than a CLI framework -- no dependency beyond the standard
library, and this is a personal tool with eight verbs.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time as _time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Sequence

from config.settings import get_cost_settings, get_exit_engine_settings
from src.market.calendar import (
    IST,
    ensure_ist,
    is_trading_day,
    now_ist,
    session_close,
    square_off_time,
)
from src.positions.exit_engine import evaluate
from src.positions.feed import ReplayFeed, synthetic_session
from src.positions.models import Position, PositionRuntime, PositionState
from src.positions.replay import replay_session
from src.positions.store import PositionStore

BANNER = "fno-advisor  |  ADVISORY ONLY -- this tool never places orders"

#: Terminal colours by state. Falls back to plain text when stdout is not a tty.
_COLOURS = {
    PositionState.INVALID_ENTRY: "\033[1;33m",
    PositionState.FORCE_EXIT: "\033[1;97;41m",
    PositionState.EXIT_NOW: "\033[1;31m",
    PositionState.TIME_EXIT: "\033[33m",
    PositionState.PARTIAL_EXIT: "\033[36m",
    PositionState.TRAIL: "\033[32m",
    PositionState.HOLD: "\033[90m",
    PositionState.STALE_DATA: "\033[1;35m",
}
_RESET = "\033[0m"


def _colour(state: PositionState, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{_COLOURS.get(state, '')}{text}{_RESET}"


def _parse_time(
    value: str | None,
    default: datetime | None = None,
    on: date | None = None,
) -> datetime:
    """Parse HH:MM or an ISO datetime into tz-aware IST.

    A bare ``HH:MM`` is resolved against ``on`` -- the command's ``--date`` -- not
    against today. Resolving it against today silently pointed every past-date query
    at the wrong session and returned a confidently wrong answer.
    """
    if value is None:
        if default is None:
            raise argparse.ArgumentTypeError("a time is required")
        return default
    try:
        if len(value) <= 5 and ":" in value:
            hh, mm = value.split(":")
            day = on or date.today()
            return datetime(day.year, day.month, day.day, int(hh), int(mm), tzinfo=IST)
        return ensure_ist(datetime.fromisoformat(value))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"could not parse time {value!r}: {exc}") from exc


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc


def _trade_date(args) -> date:
    return date.fromisoformat(args.date) if getattr(args, "date", None) else date.today()


# --------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------


def cmd_add_position(args, store: PositionStore) -> int:
    """Record a position you have ALREADY bought in your broker app."""
    entry_time = _parse_time(args.entry_time, default=now_ist())
    position = Position(
        position_id=args.position_id or f"{args.symbol.upper()}-{uuid.uuid4().hex[:8]}",
        symbol=args.symbol,
        quantity=args.qty,
        entry_price=args.price,
        entry_time=entry_time,
        stop_loss=args.stop,
        target=args.target,
        notes=args.notes,
    )
    store.save_position(position)
    store.save_runtime(PositionRuntime.initial(position), position.trade_date)

    print(f"Recorded {position.position_id}")
    print(f"  {position.symbol}  {position.quantity} @ {position.entry_price}")
    print(f"  entered {position.entry_time:%Y-%m-%d %H:%M:%S} IST")
    print(f"  stop {position.stop_loss or '(none -- ATR trail will set one)'}")
    print(f"  target {position.target or '(none -- T1 defaults to 1.5x ATR)'}")
    if not is_trading_day(entry_time):
        print("  WARNING: that is not an NSE trading day. Check the date.")
    return 0


def cmd_list_positions(args, store: PositionStore) -> int:
    trade_date = _trade_date(args)
    positions = store.list_positions(trade_date, status=None if args.all else "OPEN")
    if not positions:
        print(f"No positions on {trade_date}.")
        return 0
    print(f"Positions on {trade_date}:")
    for p in positions:
        status = store.position_status(p.position_id, trade_date)
        rt = store.load_runtime(p.position_id, trade_date)
        stop = rt.current_stop if rt else p.stop_loss
        print(
            f"  {p.position_id:<24} {p.symbol:<12} {p.quantity:>6} @ {p.entry_price:>10}"
            f"  stop {str(stop or '-'):>10}  [{status}]"
        )
    return 0


def cmd_book_partial(args, store: PositionStore) -> int:
    """Record a partial exit you ALREADY executed manually."""
    trade_date = _trade_date(args)
    rt = store.load_runtime(args.position_id, trade_date)
    if rt is None:
        print(f"No runtime state for {args.position_id} on {trade_date}.", file=sys.stderr)
        return 1
    if args.qty >= rt.remaining_quantity:
        print(
            f"Booking {args.qty} would leave nothing held (remaining "
            f"{rt.remaining_quantity}). Use close-position instead.",
            file=sys.stderr,
        )
        return 1
    rt.remaining_quantity -= args.qty
    rt.partial_advised = True
    store.save_runtime(rt, trade_date)
    print(f"Recorded partial exit of {args.qty}. Still held: {rt.remaining_quantity}.")
    return 0


def cmd_close_position(args, store: PositionStore) -> int:
    """Record a full exit you ALREADY executed manually, with the real fill price."""
    from src.costs import realised_pnl

    trade_date = _trade_date(args)
    position = store.get_position(args.position_id, trade_date)
    if position is None:
        print(f"No position {args.position_id} on {trade_date}.", file=sys.stderr)
        return 1
    rt = store.load_runtime(args.position_id, trade_date)
    quantity = rt.remaining_quantity if rt else position.quantity

    exit_time = _parse_time(args.exit_time, default=now_ist(), on=trade_date)
    store.close_position(args.position_id, trade_date, args.price, exit_time)

    pnl = realised_pnl(position.entry_price, args.price, quantity, get_cost_settings())
    print(f"Closed {args.position_id}: {quantity} @ {args.price}")
    print(f"  {pnl.summary()}")
    print(f"  costs: {pnl.costs.as_dict()['total']} (both legs, slippage included)")
    return 0


def _advise_once(store: PositionStore, position: Position, bars, now, last_tick_at, *, log: bool):
    rt = store.load_or_init_runtime(position)
    advice = evaluate(
        position, rt, bars, now, last_tick_at,
        engine_cfg=get_exit_engine_settings(), cost_cfg=get_cost_settings(),
    )
    store.save_runtime(rt, position.trade_date)
    if log:
        store.log_advice(advice)
    return advice


def cmd_watch(args, store: PositionStore) -> int:
    """Print live advice for every open position as each 1-minute bar completes.

    ``--replay-demo`` drives it from a deterministic synthetic session instead of a
    broker connection, so the loop can be seen working before any credentials or real
    bars exist. It is a fixture, not market data -- never judge a rule by it.

    A live feed is NOT wired up yet: :class:`~src.positions.feed.FyersLiveFeed` needs
    credentials and a market session and is untested. Until then, watch runs on stored
    or synthetic bars.
    """
    trade_date = _trade_date(args)
    positions = store.list_positions(trade_date, status="OPEN")
    if not positions:
        print(f"No open positions on {trade_date}. Nothing to watch.")
        return 0

    print(BANNER)
    if not is_trading_day(trade_date):
        print(f"{trade_date} is not an NSE trading day -- the market is closed.")

    for position in positions:
        if args.replay_demo:
            bars = synthetic_session(position.symbol, trade_date)
        elif store.has_bars(position.symbol, trade_date):
            bars = store.load_bars(position.symbol, trade_date)
        else:
            print(
                f"No stored bars for {position.symbol} on {trade_date}. "
                f"Use --replay-demo, or backfill bars first.",
                file=sys.stderr,
            )
            return 1

        feed = ReplayFeed(position.symbol, bars)
        previous: PositionState | None = None
        print(f"\n=== {position.symbol} {position.quantity} @ {position.entry_price} ===")
        for step in feed.steps():
            if step < position.entry_time:
                continue
            feed.advance_to(step)
            advice = _advise_once(
                store, position, feed.bars(), step, feed.last_tick_at(), log=not args.no_log
            )
            if advice.state is not previous or args.verbose:
                print(_colour(advice.state, advice.render()))
                previous = advice.state
            if advice.state in {PositionState.EXIT_NOW, PositionState.FORCE_EXIT,
                                PositionState.TIME_EXIT}:
                print("  -> exit advised. Execute in your broker app, then run "
                      "close-position to record the fill.")
                break
            if args.interval:
                _time.sleep(args.interval)
    return 0


def cmd_replay(args, store: PositionStore) -> int:
    """Run the engine across a historical session and print what it would have said."""
    trade_date = _trade_date(args)
    position = store.get_position(args.position_id, trade_date) if args.position_id else None
    if position is None:
        print(
            f"No position {args.position_id!r} on {trade_date}. "
            f"Record it first with add-position.",
            file=sys.stderr,
        )
        return 1

    if args.demo:
        bars = synthetic_session(position.symbol, trade_date)
    elif store.has_bars(position.symbol, trade_date):
        bars = store.load_bars(position.symbol, trade_date)
    else:
        print(
            f"No stored bars for {position.symbol} on {trade_date}. Use --demo to "
            f"exercise replay against a synthetic session.",
            file=sys.stderr,
        )
        return 1

    result = replay_session(
        position, bars,
        engine_cfg=get_exit_engine_settings(),
        cost_cfg=get_cost_settings(),
        stop_at_exit=not args.full_day,
    )
    print(BANNER)
    print(f"\nReplay {position.symbol} on {trade_date}"
          + ("  (SYNTHETIC fixture data -- not market data)" if args.demo else ""))
    print("\nState changes:")
    for advice in result.transitions:
        print(_colour(advice.state, advice.render()))
    print("\n" + result.summary())
    if args.log:
        for advice in result.advices:
            store.log_advice(advice)
        print(f"\nLogged {len(result.advices)} evaluations to the journal.")
    return 0


def cmd_journal(args, store: PositionStore) -> int:
    """Summarise the advice journal with DuckDB."""
    # "asof" and first/last are all DuckDB keywords -- quote the column, alias the rest.
    rows = store.query(
        'SELECT state, count(*) AS n, min("asof") AS first_at, max("asof") AS last_at '
        "FROM advice GROUP BY state ORDER BY n DESC"
    )
    if not rows:
        print("Advice journal is empty.")
        return 0
    print(f"{'state':<14}{'count':>7}  {'first':<10}{'last':<10}")
    for state, n, first_at, last_at in rows:
        print(f"{state:<14}{n:>7}  {first_at:%H:%M:%S}  {last_at:%H:%M:%S}")
    return 0


def cmd_check_entry(args, store: PositionStore) -> int:
    """Ask whether an entry is sound. Records NOTHING.

    This is the "should I buy this" question, answered before you buy. It creates no
    position, writes no runtime state, and touches no journal -- run it as often as
    you like.
    """
    from src.positions.exit_engine import check_hypothetical_entry

    trade_date = _trade_date(args)
    symbol = args.symbol.strip().upper()

    if args.demo:
        bars = synthetic_session(symbol, trade_date)
    elif store.has_bars(symbol, trade_date):
        bars = store.load_bars(symbol, trade_date)
    else:
        print(
            f"No stored bars for {symbol} on {trade_date}. Backfill first, or use "
            f"--demo to exercise the check against a synthetic session.",
            file=sys.stderr,
        )
        return 1

    at = (_parse_time(args.at, on=trade_date) if args.at else bars[-1].close_time)
    check = check_hypothetical_entry(
        bars, at, args.price, args.stop,
        session_date=trade_date, engine_cfg=get_exit_engine_settings(),
    )
    if check is None:
        print(
            f"No completed bar for {symbol} at {at:%H:%M:%S} IST -- nothing to judge "
            f"the entry against.",
            file=sys.stderr,
        )
        return 1

    print(BANNER)
    if args.demo:
        print("  (SYNTHETIC fixture data -- not market data)")
    print()
    state = PositionState.HOLD if check.valid else PositionState.INVALID_ENTRY
    print(_colour(state, check.render(symbol)))
    print()
    print("  Nothing was recorded. Use add-position if you go ahead and buy.")
    # Non-zero on an unsound entry so this is usable in a shell guard.
    return 0 if check.valid else 2


def cmd_auth(args, store: PositionStore) -> int:
    """Run the manual Fyers OAuth + 2FA flow and cache the token.

    Interactive and blocking by design. The 2FA step is completed by a human in a
    browser; nothing here automates it.
    """
    from config.settings import get_fyers_settings
    from src.data.fyers_auth import AuthError, FyersAuth

    try:
        settings = get_fyers_settings()
    except Exception as exc:
        print(
            f"error: Fyers credentials are not configured ({exc}).\n"
            f"  Copy .env.example to .env and fill in FYERS_APP_ID, FYERS_SECRET,\n"
            f"  FYERS_REDIRECT_URI from https://myapi.fyers.in/dashboard",
            file=sys.stderr,
        )
        return 1

    auth = FyersAuth(settings)
    if args.status:
        print(auth.status())
        return 0

    existing = auth.load_if_valid()
    if existing is not None and not args.force:
        print(auth.status())
        print("Token is still valid today. Use --force to regenerate anyway.")
        return 0

    try:
        auth.interactive_login()
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_backfill(args, store: PositionStore) -> int:
    """Fetch historical 1-minute bars. Resumable -- safe to interrupt and re-run."""
    from config.settings import get_fyers_settings
    from src.data.backfill import LIQUID_FNO_20, Backfiller, FyersHistoryClient
    from src.data.fyers_auth import AuthError, require_token

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(
        days=args.months * 31
    )
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols \
        else list(LIQUID_FNO_20)

    try:
        settings = get_fyers_settings()
        token = require_token(settings)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: Fyers credentials not configured ({exc}). See .env.example",
              file=sys.stderr)
        return 1

    client = FyersHistoryClient(token.access_token, settings.app_id)
    backfiller = Backfiller(client, store, max_days_per_request=args.chunk_days)

    print(BANNER)
    print(f"Backfilling {len(symbols)} symbols, {start} to {end}")
    summary = backfiller.run(symbols, start, end, force=args.force)
    print("\n" + summary.render())

    quarantined = backfiller.ledger.quarantined()
    if quarantined:
        print(f"\n{len(quarantined)} day(s) QUARANTINED -- inspect them, do not ignore:")
        for entry in quarantined[:20]:
            print(f"  {entry.symbol:<12} {entry.trade_date}  {entry.reason}")
        if len(quarantined) > 20:
            print(f"  ... and {len(quarantined) - 20} more")
        print(f"  Files under {store.root / 'quarantine' / 'bars'}")
    return 0


def cmd_backfill_status(args, store: PositionStore) -> int:
    """Show what the backfill ledger has settled so far."""
    from src.data.backfill import BackfillLedger

    ledger = BackfillLedger(store.root / "raw" / "_backfill_ledger.json")
    counts = ledger.counts()
    if not counts:
        print("Backfill ledger is empty -- nothing has been fetched yet.")
        return 0
    print("Backfill ledger:")
    for status, n in sorted(counts.items()):
        print(f"  {status:<14}{n:>7}")
    quarantined = ledger.quarantined()
    if quarantined:
        print(f"\nQuarantined ({len(quarantined)}):")
        for entry in quarantined[:20]:
            print(f"  {entry.symbol:<12} {entry.trade_date}  {entry.reason}")
    return 0


def cmd_refresh_universe(args, store: PositionStore) -> int:
    """Refresh and validate the dated read-only FYERS NSE F&O master."""
    from src.data.fno_universe import refresh_snapshot

    snapshot = refresh_snapshot(store.root)
    print(f"F&O universe refreshed: {len(snapshot.symbols)} stock underlyings, "
          f"{len(snapshot.indices)} index symbols")
    print(f"As of {snapshot.as_of}; SHA-256 {snapshot.sha256}")
    return 0


def cmd_clear_positions(args, store: PositionStore) -> int:
    """Delete recorded positions for a date. Destructive, so --confirm is required."""
    trade_date = _trade_date(args)
    ids = [i.strip() for i in args.position_id.split(",")] if args.position_id else None

    existing = store.list_positions(trade_date, status=None)
    if ids is not None:
        existing = [p for p in existing if p.position_id in set(ids)]
    if not existing:
        which = f" matching {args.position_id}" if ids else ""
        print(f"Nothing to clear: no positions recorded for {trade_date}{which}.")
        return 0

    print(BANNER)
    print(f"About to DELETE {len(existing)} position(s) recorded on {trade_date}:")
    for p in existing:
        status = store.position_status(p.position_id, trade_date)
        print(f"  {p.position_id:<22} {p.symbol:<12} x{p.quantity:<6} "
              f"@ {p.entry_price}  {p.entry_time:%H:%M:%S}  [{status}]")
    print()
    print("This deletes the RECORD, including closed positions and their runtime")
    print("state. It does not close anything and it never touches a broker.")

    if not args.confirm:
        print()
        print("Nothing was deleted. Re-run with --confirm to go ahead.")
        return 1

    removed = store.clear_positions(trade_date, position_ids=ids)
    print()
    print(f"Deleted {removed} position(s) and their runtime state.")
    return 0


def cmd_dashboard(args, store: PositionStore) -> int:
    """Serve the web dashboard. REPLAY unless --mode live is asked for."""
    from src.api.serve import serve

    trade_date = date.fromisoformat(args.date) if args.date else None
    return serve(
        store,
        mode=args.mode,
        trade_date=trade_date,
        speed=args.speed,
        symbols=args.symbols,
        host=args.host,
        port=args.port,
    )


def cmd_collect_announcements(args, store: PositionStore) -> int:
    """Poll NSE for corporate announcements. Forward-collect only, never backwards.

    This is what makes the dashboard's ELEVATED_EVENT_RISK panel say anything other
    than UNKNOWN. Run it on a schedule through the session -- every 15 minutes is
    enough for the 30-minute staleness limit and leaves room for one missed run.
    """
    from config.settings import get_settings
    from src.data.announcements import (
        AnnouncementStore,
        NseAnnouncementClient,
        collect_once,
    )
    from src.data.backfill import LIQUID_FNO_UNIVERSE
    from src.market.event_risk import classify, unclassified_subjects

    day = date.fromisoformat(args.date) if args.date else now_ist().date()
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else list(LIQUID_FNO_UNIVERSE))

    ann_store = AnnouncementStore(args.data_dir or get_settings().data_dir)
    client = NseAnnouncementClient()

    rounds = max(1, args.repeat)
    for i in range(rounds):
        if i:
            print(f"  sleeping {args.interval}s ...")
            _time.sleep(args.interval)
        result = collect_once(client, ann_store, symbols, day)
        stamp = result["observed_at"][11:19]
        if result.get("recorded") is None:
            print(f"[{stamp}] BOTH FEEDS FAILED -- event risk stays UNKNOWN, which is "
                  f"the correct state. {'; '.join(result['errors'])}")
            continue
        rec = result["recorded"]
        print(f"[{stamp}] announcements {result.get('announcements_ours', 0)}"
              f"/{result.get('announcements_all', 0)} ours, "
              f"calendar {result.get('calendar_ours', 0)}"
              f"/{result.get('calendar_all', 0)} ours -> "
              f"new {rec['new']}, revised {rec['revised']}, repeat {rec['repeated']}")
        for err in result["errors"]:
            print(f"          PARTIAL: {err}")

    observations = ann_store.observations()
    ours = [o for o in observations.values() if o.symbol in set(symbols)]
    material = [o for o in ours if classify(o.subject)]
    print(f"\nledger: {len(observations)} observations, {len(ours)} for our symbols, "
          f"{len(material)} material")
    if material:
        print("material observations on file:")
        for obs in sorted(material, key=lambda o: o.first_seen_at)[-15:]:
            cats = ",".join(classify(obs.subject))
            print(f"  {obs.first_seen_at:%Y-%m-%d %H:%M}  {obs.symbol:<12} "
                  f"{cats:<28} {obs.subject}")

    # Announcements only. A calendar row is a board-meeting intimation and flags on its
    # scheduled date whatever its stated purpose, so listing "Other business matters"
    # here would claim a gap that does not exist.
    unmatched = unclassified_subjects(
        o.subject for o in ours if o.scheduled_for is None)
    if unmatched:
        print(f"\n{len(unmatched)} announcement subject(s) matched NO material category "
              f"and are therefore being treated as immaterial. Review this list -- a gap "
              f"here renders a symbol CLEAR:")
        for subject, n in list(unmatched.items())[:20]:
            print(f"  {n:>3}  {subject}")
    return 0


#: Printed under every morning setup, and carried in the JSON so it cannot be lost by
#: piping. The module answers an arithmetic question; this says what that is not.
MORNING_DISCLAIMER = (
    "Planning guidance only. This is NOT a prediction, NOT a recommendation to buy or "
    "sell anything, and no order is placed by this command or by any other part of "
    "this tool."
)


def cmd_morning(args, store: PositionStore) -> int:
    """Module A: is today worth trading, given capital, limits and costs?

    A THIN PRESENTER. Every rule -- the calendar, the cost arithmetic, the blockers,
    the verdict -- lives in :func:`src.morning.setup.morning_setup`. Nothing is decided
    here, and ``test_the_command_adds_no_business_logic_of_its_own`` fails if this
    function starts recomputing anything the module already answers.

    Reads no broker token and opens no socket: the module is pure arithmetic over the
    NSE calendar and the cost model.

    Exit code follows the existing convention (see ``cmd_session``, which returns 0 on
    a non-trading day): a successfully computed answer is 0 whatever the advice, because
    NO_TRADE is a first-class result and not an error (CLAUDE.md 1.5). A malformed
    argument exits through argparse, and an unexpected exception through ``main``.
    """
    from src.morning.setup import morning_setup

    trade_date = date.fromisoformat(args.date) if args.date else None
    # A bare --at HH:MM is resolved against --date, which is the convention _parse_time
    # documents: resolving it against today silently pointed every past-date query at
    # the wrong session. With neither flag both stay None and the module reads the
    # clock -- so the default is always NOW, never a historical date.
    now = _parse_time(args.at, on=trade_date) if args.at else None

    result = morning_setup(
        capital=args.capital,
        daily_target_pct=args.target_pct,
        max_daily_loss_pct=args.max_loss_pct,
        for_date=trade_date,
        now=now,
    )

    if args.json:
        import json

        print(json.dumps(result.as_dict() | {"disclaimer": MORNING_DISCLAIMER},
                         indent=2))
        return 0

    def money(value) -> str:
        """Decimal formatting only. Never float -- 1000.55 must not print as .5500001."""
        return "--" if value is None else f"{value:,.2f}"

    def pct(value, places: int = 4) -> str:
        return "--" if value is None else f"{value:.{places}f}%"

    print(BANNER)
    print()
    print(f"MORNING SETUP  {result.trade_date}"
          f"        generated {result.generated_at:%H:%M:%S} IST")
    print()
    print(f"  advice         {result.advice.value}")
    print(f"  session        {result.session_state.value}")
    # Padded so the note keeps its column whichever status word appears -- the longest
    # is NOT_APPLICABLE.
    print(f"  feasibility    {result.feasibility_status.value:<16}"
          f"  arithmetic only -- not a probability")
    print()
    print(f"  capital                  {money(result.capital):>14}")
    print(f"  daily target   {pct(result.configured_daily_target_pct, 3):>9}"
          f" {money(result.configured_daily_target):>14}")
    print(f"  max daily loss {pct(result.configured_max_loss_pct, 3):>9}"
          f" {money(result.configured_max_loss):>14}")
    print()
    print(f"  round-trip cost          {money(result.estimated_round_trip_cost):>14}"
          f"   {pct(result.cost_pct_of_capital)} of capital")
    print(f"    of which slippage      {money(result.estimated_slippage):>14}")
    print(f"  required gross           {pct(result.required_gross_pct):>14}"
          f"   to NET the target after costs")
    if result.round_trips_within_loss_budget is not None:
        print(f"  loss budget absorbs      "
              f"{result.round_trips_within_loss_budget:>14,.2f}"
              f"   round trips in costs alone")
    print()
    print(f"  cost rates     "
          f"{'VERIFIED' if result.cost_configuration_verified else 'UNVERIFIED'}")

    if result.blockers:
        print()
        print("  BLOCKERS")
        for item in result.blockers:
            print(f"    ! {item}")
    if result.warnings:
        print()
        print("  WARNINGS")
        for item in result.warnings:
            print(f"    - {item}")

    print()
    print(f"  {MORNING_DISCLAIMER}")
    return 0


def cmd_session(args, store: PositionStore) -> int:
    """Pre-flight: is the market open, and how long until square-off."""
    now = now_ist()
    print(BANNER)
    print(f"Now: {now:%Y-%m-%d %H:%M:%S} IST")
    if not is_trading_day(now):
        print("NOT a trading day -- NSE is closed.")
        return 0
    deadline = square_off_time(now)
    print(f"Session closes {session_close(now):%H:%M} IST")
    if now < deadline:
        mins = (deadline - now).total_seconds() / 60
        print(f"Intraday square-off at {deadline:%H:%M} IST -- {mins:.0f} min away.")
    else:
        print(f"PAST the {deadline:%H:%M} square-off. Intraday positions should be flat.")
    return 0


# --------------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description=BANNER)
    parser.add_argument("--data-dir", help="override the data root (default: config)")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-position", help="record a manual buy you already made")
    add.add_argument("--symbol", required=True)
    add.add_argument("--qty", required=True, type=int)
    add.add_argument("--price", required=True, type=_decimal)
    add.add_argument("--entry-time", help="HH:MM or ISO datetime (default: now)")
    add.add_argument("--stop", type=_decimal, help="optional initial stop loss")
    add.add_argument("--target", type=_decimal, help="optional target (becomes T1)")
    add.add_argument("--position-id", help="default: SYMBOL-<random>")
    add.add_argument("--notes")
    add.set_defaults(func=cmd_add_position)

    ls = sub.add_parser("list-positions", help="show positions for a date")
    ls.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ls.add_argument("--all", action="store_true", help="include closed positions")
    ls.set_defaults(func=cmd_list_positions)

    watch = sub.add_parser("watch", help="print live exit advice as bars complete")
    watch.add_argument("--date", help="YYYY-MM-DD (default: today)")
    watch.add_argument("--replay-demo", action="store_true",
                       help="drive from a synthetic session instead of stored bars")
    watch.add_argument("--interval", type=float, default=0.0,
                       help="seconds to sleep between evaluations")
    watch.add_argument("--verbose", action="store_true", help="print every bar, not just changes")
    watch.add_argument("--no-log", action="store_true", help="do not write to the journal")
    watch.set_defaults(func=cmd_watch)

    rep = sub.add_parser("replay", help="run the engine over a historical session")
    rep.add_argument("--position-id", required=True)
    rep.add_argument("--date", help="YYYY-MM-DD (default: today)")
    rep.add_argument("--demo", action="store_true", help="use a synthetic session")
    rep.add_argument("--full-day", action="store_true", help="keep going past the first exit")
    rep.add_argument("--log", action="store_true", help="write results to the journal")
    rep.set_defaults(func=cmd_replay)

    part = sub.add_parser("book-partial", help="record a partial exit you already made")
    part.add_argument("--position-id", required=True)
    part.add_argument("--qty", required=True, type=int)
    part.add_argument("--date", help="YYYY-MM-DD (default: today)")
    part.set_defaults(func=cmd_book_partial)

    close = sub.add_parser("close-position", help="record a full exit you already made")
    close.add_argument("--position-id", required=True)
    close.add_argument("--price", required=True, type=_decimal, help="your ACTUAL fill price")
    close.add_argument("--exit-time", help="HH:MM or ISO datetime (default: now)")
    close.add_argument("--date", help="YYYY-MM-DD (default: today)")
    close.set_defaults(func=cmd_close_position)

    jr = sub.add_parser("journal", help="summarise the advice journal")
    jr.set_defaults(func=cmd_journal)

    sess = sub.add_parser("session", help="pre-flight session status")
    sess.set_defaults(func=cmd_session)

    ann = sub.add_parser(
        "collect-announcements",
        help="poll NSE corporate announcements (forward-collect only)",
        description=(
            "Polls the NSE corporate-announcements and board-meeting feeds and appends "
            "what it sees to a forward-only ledger. This feeds the dashboard's "
            "ELEVATED_EVENT_RISK filter, which is an EXCLUSION, not a signal: no "
            "sentiment, no direction, no claim that it improves returns. There is "
            "deliberately no historical backfill -- retroactively edited filings cannot "
            "be used to reconstruct what was knowable at the time."
        ),
    )
    ann.add_argument("--date", help="session to collect (default: today)")
    ann.add_argument("--symbols", help="comma-separated (default: the F&O universe)")
    ann.add_argument("--repeat", type=int, default=1,
                     help="number of polls in this run (default 1)")
    ann.add_argument("--interval", type=float, default=900.0,
                     help="seconds between polls when --repeat > 1 (default 900)")
    ann.set_defaults(func=cmd_collect_announcements)

    clr = sub.add_parser("clear-positions",
                         help="DELETE recorded positions for a date (needs --confirm)")
    clr.add_argument("--date", help="YYYY-MM-DD (default: today)")
    clr.add_argument("--position-id", help="comma-separated ids; default is ALL for the date")
    clr.add_argument("--confirm", action="store_true",
                     help="required; without it this only lists what would go")
    clr.set_defaults(func=cmd_clear_positions)

    dash = sub.add_parser(
        "dashboard",
        help="serve the web dashboard (REPLAY by default; LIVE must be asked for)",
    )
    # REPLAY is the default deliberately: it needs no token, cannot be mistaken for
    # today's market once the header renders, and a mistyped command must not open a
    # page showing real money.
    dash.add_argument("--mode", choices=["replay", "live"], default="replay",
                      help="default: replay")
    dash.add_argument("--date", help="YYYY-MM-DD, the session to replay (replay mode)")
    dash.add_argument("--speed", type=float, default=60.0,
                      help="replay speed multiplier (60 = one minute per second)")
    dash.add_argument("--symbols",
                      help="comma-separated; default is maintained liquid subset in "
                           "REPLAY, and a recent refreshed F&O master in LIVE (subset "
                           "fallback). Each symbol costs one paced startup history call")
    dash.add_argument("--host", default="127.0.0.1", help="loopback by default; there "
                                                          "is no auth on this")
    dash.add_argument("--port", type=int, default=8000)
    dash.set_defaults(func=cmd_dashboard)

    ce = sub.add_parser("check-entry",
                        help="is this a valid entry right now? records nothing")
    ce.add_argument("--symbol", required=True)
    ce.add_argument("--price", type=_decimal,
                    help="proposed entry price (default: last completed close)")
    ce.add_argument("--stop", type=_decimal, help="proposed stop loss")
    ce.add_argument("--at", help="HH:MM or ISO datetime (default: last completed bar)")
    ce.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ce.add_argument("--demo", action="store_true", help="use a synthetic session")
    ce.set_defaults(func=cmd_check_entry)

    auth = sub.add_parser("auth", help="run the manual Fyers login (2FA is by hand)")
    auth.add_argument("--status", action="store_true", help="just report token state")
    auth.add_argument("--force", action="store_true", help="regenerate even if still valid")
    auth.set_defaults(func=cmd_auth)

    bf = sub.add_parser("backfill", help="fetch historical 1-minute bars (resumable)")
    bf.add_argument("--symbols", help="comma-separated (default: the 20 liquid F&O names)")
    bf.add_argument("--months", type=int, default=6, help="lookback when --start is omitted")
    bf.add_argument("--start", help="YYYY-MM-DD")
    bf.add_argument("--end", help="YYYY-MM-DD (default: today)")
    bf.add_argument("--chunk-days", type=int, default=30, help="days per API request")
    bf.add_argument("--force", action="store_true", help="refetch already-settled days")
    bf.add_argument("--verbose", action="store_true")
    bf.set_defaults(func=cmd_backfill)

    bfs = sub.add_parser("backfill-status", help="what the backfill ledger has settled")
    bfs.set_defaults(func=cmd_backfill_status)

    univ = sub.add_parser("refresh-universe", help="refresh the dated FYERS NSE F&O symbol master")
    univ.set_defaults(func=cmd_refresh_universe)

    morn = sub.add_parser(
        "morning",
        help="Module A: is today worth trading, given capital, limits and costs?")
    # Required on purpose: capital is never defaulted and never inferred. See
    # CLAUDE.md 1.5 -- a setup built on assumed capital is a setup built on nothing.
    morn.add_argument("--capital", required=True, type=_decimal,
                      help="money available today. Never assumed; you must state it")
    morn.add_argument("--target-pct", type=_decimal,
                      help="daily target as a percent of capital (default: config)")
    morn.add_argument("--max-loss-pct", type=_decimal,
                      help="maximum daily loss as a percent of capital (default: config)")
    morn.add_argument("--date", help="YYYY-MM-DD; plan a session other than today's")
    morn.add_argument("--at", help="HH:MM or ISO datetime, resolved against --date. "
                                   "Makes the answer deterministic for testing")
    morn.add_argument("--json", action="store_true",
                      help="machine-readable output instead of the printed report")
    morn.set_defaults(func=cmd_morning)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = PositionStore(args.data_dir) if args.data_dir else PositionStore()
    try:
        return args.func(args, store)
    except Exception as exc:  # surfaced, never swallowed
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
