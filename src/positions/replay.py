"""Replay driver: run the exit engine across a historical session.

A requirement, not a convenience. Advice that has never been examined against past
data is advice with no evidence behind it, and this is the only honest way to look at
what the rules would have said before trusting them with money.

Reproducibility is the property that makes it useful. :func:`replay_session` reads no
clock and no network; the same bars plus the same config produce byte-identical advice
every run. If a replay result changes, something in the code or the config changed --
that is a signal, not noise.

Replay is NOT a backtest of profitability. It shows what the engine would have said
and when. Turning that into a P&L claim would need the entry rule, the fills the user
would actually have got, and honest slippage -- none of which live here.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from config.settings import CostSettings, ExitEngineSettings, get_cost_settings
from src.costs import realised_pnl
from src.market.calendar import IST, ensure_ist
from src.positions.exit_engine import evaluate
from src.positions.feed import ReplayFeed
from src.positions.models import (
    Bar,
    ExitAdvice,
    ExitRule,
    Position,
    PositionRuntime,
    PositionState,
    completed_bars,
)

log = logging.getLogger("fno.replay")

__all__ = [
    "ReplayResult",
    "replay_session",
    "SessionOutcome",
    "batch_replay",
    "outcomes_to_dataframe",
    "write_outcomes_csv",
    "DEFAULT_ENTRY_TIME",
    "DEFAULT_NOTIONAL",
]

#: Entry time for batch replay. Late enough that ATR14/RSI14/EMA21 are warm (30 bars
#: from the 09:15 open) and past the opening auction noise.
DEFAULT_ENTRY_TIME = time(9, 45)

#: Fixed rupee notional per simulated position, so P&L is comparable across symbols
#: with very different share prices. Quantity is floor(notional / entry_price).
DEFAULT_NOTIONAL = Decimal("100000")

#: States after which a user acting on the advice would be flat, so there is nothing
#: further to advise on.
TERMINAL_STATES = frozenset(
    {PositionState.EXIT_NOW, PositionState.TIME_EXIT, PositionState.FORCE_EXIT}
)


@dataclass
class ReplayResult:
    """Everything one replayed session produced."""

    position: Position
    runtime: PositionRuntime
    advices: list[ExitAdvice] = field(default_factory=list)
    #: Advice at each point the state CHANGED. The useful summary of a session --
    #: 375 bars of HOLD is not a story, four transitions is.
    transitions: list[ExitAdvice] = field(default_factory=list)
    exited_at: datetime | None = None
    exit_state: PositionState | None = None

    @property
    def final(self) -> ExitAdvice | None:
        return self.advices[-1] if self.advices else None

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in self.advices:
            counts[a.state.value] = counts.get(a.state.value, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def summary(self) -> str:
        """A short human-readable account of the session."""
        lines = [
            f"{self.position.symbol}  {len(self.advices)} evaluations, "
            f"{len(self.transitions)} state changes",
            "  " + ", ".join(f"{k}={v}" for k, v in self.state_counts().items()),
        ]
        if self.exited_at is not None:
            lines.append(
                f"  exit advised {self.exit_state.value} at "
                f"{self.exited_at.strftime('%H:%M:%S')} IST"
            )
        else:
            lines.append("  no exit advised -- position still open at the last bar")
        if self.final is not None and self.final.pnl is not None:
            lines.append(f"  final P&L: {self.final.pnl.summary()}")
        return "\n".join(lines)


def replay_session(
    position: Position,
    bars: Sequence[Bar],
    *,
    runtime: PositionRuntime | None = None,
    engine_cfg: ExitEngineSettings | None = None,
    cost_cfg: CostSettings | None = None,
    stop_at_exit: bool = True,
) -> ReplayResult:
    """Run the engine over a session's bars, one evaluation per completed bar.

    Evaluation points are bar close times -- the moments a live run would have woken
    up. Bars before ``position.entry_time`` are still fed in (indicators need the
    warm-up) but produce no advice: you cannot be advised on a position you do not yet
    hold.

    Data-availability boundary:
        At each step the engine sees only bars with ``ts + BAR_INTERVAL <= step``.
        The bar being closed at that instant IS included -- it is complete. The next
        one is not. ``last_tick_at`` is the most recent moment the feed HAD DATA -- the
        same meaning it carries live -- so a gap in the historical data surfaces as
        STALE_DATA rather than being silently bridged. At a bar close time, which is the
        only instant this driver evaluates at, that is the bar's own close, so nothing
        measured by a batch replay moved when the definition was unified.

    Args:
        stop_at_exit: stop after the first terminal exit state. True by default,
            because a user acting on EXIT_NOW is flat and everything after it is
            fiction. Set False to see what the rules would have said all day.

    Returns:
        A :class:`ReplayResult`. Deterministic for a given (bars, config).
    """
    runtime = runtime or PositionRuntime.initial(position)
    feed = ReplayFeed(position.symbol, bars)
    result = ReplayResult(position=position, runtime=runtime)

    previous: PositionState | None = None
    for step in feed.steps():
        if step < position.entry_time:
            continue
        feed.advance_to(step)
        advice = evaluate(
            position,
            runtime,
            feed.bars(),
            step,
            feed.last_tick_at(),
            engine_cfg=engine_cfg,
            cost_cfg=cost_cfg,
        )
        result.advices.append(advice)
        if advice.state is not previous:
            result.transitions.append(advice)
            previous = advice.state

        if advice.state in TERMINAL_STATES:
            result.exited_at = advice.asof
            result.exit_state = advice.state
            if stop_at_exit:
                break

    return result


# --------------------------------------------------------------------------------
# Batch replay over many real sessions
# --------------------------------------------------------------------------------


@dataclass
class SessionOutcome:
    """One simulated position: how it was entered, how it ended, and how far it swung.

    MAE and MFE are the excursion pair. They answer a question raw P&L cannot: was a
    losing exit unlucky or predictable, and did a winner give back most of its move
    before the rule noticed. A rule that exits with a large MFE behind it is cutting
    winners; one with a large MAE ahead of a profitable exit is taking heat it did not
    need to.
    """

    symbol: str
    trade_date: date
    entry_time: datetime
    entry_price: float
    quantity: int

    exit_state: str
    exit_rule: str
    exit_time: datetime | None
    exit_price: float | None
    exit_reason: str

    gross_pnl: float
    net_pnl: float
    gross_pct: float
    net_pct: float
    costs: float

    #: Max favourable excursion: highest HIGH between entry and exit, minus entry.
    mfe: float
    mfe_pct: float
    #: Max adverse excursion: entry minus lowest LOW between entry and exit.
    mae: float
    mae_pct: float

    minutes_held: float
    bars_held: int
    evaluations: int
    #: True when the position was still open at the last bar of the session.
    unresolved: bool

    def as_row(self) -> dict:
        row = asdict(self)
        row["trade_date"] = self.trade_date.isoformat()
        row["entry_time"] = self.entry_time.isoformat()
        row["exit_time"] = None if self.exit_time is None else self.exit_time.isoformat()
        return row


def _excursions(
    bars: Sequence[Bar],
    entry_time: datetime,
    exit_time: datetime | None,
    entry_price: float,
) -> tuple[float, float, int]:
    """(MFE, MAE, bars_held) over the holding window.

    Uses bar HIGH and LOW, not close: the excursion is what price actually reached
    while the position was open, which is what a stop would have felt.

    Data-availability boundary:
        Bars with ``entry_time <= ts`` and, when an exit happened, ``ts < exit_time``.
        The exit bar itself is excluded -- its extremes occurred at or after the
        moment the exit was advised, so counting them would attribute post-exit
        movement to the holding period.
    """
    held = [
        b for b in bars
        if b.ts >= entry_time and (exit_time is None or b.ts < exit_time)
    ]
    if not held:
        return 0.0, 0.0, 0
    mfe = max(b.high for b in held) - entry_price
    mae = entry_price - min(b.low for b in held)
    return max(0.0, mfe), max(0.0, mae), len(held)


def replay_one_session(
    symbol: str,
    trade_date: date,
    bars: Sequence[Bar],
    *,
    entry_time: time = DEFAULT_ENTRY_TIME,
    notional: Decimal = DEFAULT_NOTIONAL,
    engine_cfg: ExitEngineSettings | None = None,
    cost_cfg: CostSettings | None = None,
    stop_loss: Decimal | None = None,
    target: Decimal | None = None,
) -> SessionOutcome | None:
    """Simulate one entry on one session and record the outcome.

    Entry price is the CLOSE OF THE LAST BAR COMPLETED AT ``entry_time`` -- the most
    recent price that had actually printed. Using the entry bar's open or its high
    would be look-ahead, assuming a fill at a price not yet known.

    Returns None when the session has no bar completed by ``entry_time`` (a short or
    broken day), rather than inventing a fill.
    """
    cost_cfg = cost_cfg or get_cost_settings()
    entry_at = datetime.combine(trade_date, entry_time, tzinfo=IST)

    known = completed_bars(bars, entry_at)
    if not known:
        log.warning("%s %s: no completed bar at %s, skipping", symbol, trade_date, entry_time)
        return None

    entry_price = known[-1].close
    quantity = int(notional / Decimal(str(entry_price)))
    if quantity <= 0:
        log.warning("%s %s: notional too small for price %.2f", symbol, trade_date, entry_price)
        return None

    price_dec = Decimal(str(entry_price)).quantize(Decimal("0.01"))
    # A stop at or above the fill, or a target at or below it, cannot be honoured --
    # Position rejects both. That is a signal-side error; drop the level and note it
    # rather than crash a 30,000-session sweep.
    if stop_loss is not None and stop_loss >= price_dec:
        log.warning("%s %s: stop %s not below fill %s, dropping it",
                    symbol, trade_date, stop_loss, price_dec)
        stop_loss = None
    if target is not None and target <= price_dec:
        target = None

    position = Position(
        position_id=f"{symbol}-{trade_date:%Y%m%d}",
        symbol=symbol,
        quantity=quantity,
        entry_price=price_dec,
        entry_time=entry_at,
        stop_loss=stop_loss,
        target=target,
    )
    result = replay_session(
        position, bars, engine_cfg=engine_cfg, cost_cfg=cost_cfg, stop_at_exit=True
    )
    if not result.advices:
        return None

    final = result.advices[-1]
    unresolved = result.exit_state is None
    exit_price = final.last_price if final.last_price is not None else entry_price
    exit_time = result.exited_at or final.asof

    pnl = realised_pnl(position.entry_price, exit_price, quantity, cost_cfg)
    mfe, mae, bars_held = _excursions(bars, entry_at, result.exited_at, entry_price)

    return SessionOutcome(
        symbol=symbol,
        trade_date=trade_date,
        entry_time=entry_at,
        entry_price=entry_price,
        quantity=quantity,
        exit_state=(result.exit_state or final.state).value,
        exit_rule=final.rule.value,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=final.reason,
        gross_pnl=float(pnl.gross_pnl),
        net_pnl=float(pnl.net_pnl),
        gross_pct=float(pnl.gross_pct),
        net_pct=float(pnl.net_pct),
        costs=float(pnl.costs.total),
        mfe=mfe,
        mfe_pct=100.0 * mfe / entry_price,
        mae=mae,
        mae_pct=100.0 * mae / entry_price,
        minutes_held=(exit_time - entry_at).total_seconds() / 60.0,
        bars_held=bars_held,
        evaluations=len(result.advices),
        unresolved=unresolved,
    )


def batch_replay(
    store,
    symbols: Sequence[str],
    trade_dates: Sequence[date],
    *,
    entry_time: time = DEFAULT_ENTRY_TIME,
    notional: Decimal = DEFAULT_NOTIONAL,
    engine_cfg: ExitEngineSettings | None = None,
    cost_cfg: CostSettings | None = None,
) -> list[SessionOutcome]:
    """Run the exit engine across N sessions x M symbols.

    Symbol-days with no stored bars are skipped and logged, not treated as flat days.
    A missing day is missing data; counting it as a no-trade would understate activity
    and quietly bias every aggregate computed downstream.
    """
    outcomes: list[SessionOutcome] = []
    missing = 0
    for symbol in symbols:
        for trade_date in trade_dates:
            if not store.has_bars(symbol, trade_date):
                missing += 1
                continue
            bars = store.load_bars(symbol, trade_date)
            outcome = replay_one_session(
                symbol, trade_date, bars,
                entry_time=entry_time, notional=notional,
                engine_cfg=engine_cfg, cost_cfg=cost_cfg,
            )
            if outcome is not None:
                outcomes.append(outcome)

    log.info(
        "batch replay: %d outcomes from %d symbol-days (%d missing, %d unusable)",
        len(outcomes), len(symbols) * len(trade_dates), missing,
        len(symbols) * len(trade_dates) - missing - len(outcomes),
    )
    return outcomes


def outcomes_to_dataframe(outcomes: Sequence[SessionOutcome]):
    """Outcomes as a pandas DataFrame. Empty input yields an empty, correctly-typed frame."""
    import pandas as pd  # noqa: PLC0415 - heavy import, only needed for analysis

    if not outcomes:
        return pd.DataFrame(columns=[f.name for f in SessionOutcome.__dataclass_fields__.values()])
    frame = pd.DataFrame([o.as_row() for o in outcomes])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    for column in ("entry_time", "exit_time"):
        frame[column] = pd.to_datetime(frame[column], format="ISO8601", utc=True)
        frame[column] = frame[column].dt.tz_convert("Asia/Kolkata")
    return frame


def write_outcomes_csv(outcomes: Sequence[SessionOutcome], path: Path | str) -> Path:
    """Write outcomes to CSV, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    outcomes_to_dataframe(outcomes).to_csv(path, index=False)
    log.info("wrote %d outcomes to %s", len(outcomes), path)
    return path
