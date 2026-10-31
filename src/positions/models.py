"""Data models for Module C -- Position Tracker.

Every datetime here is tz-aware IST. Naive input is rejected at validation, not
silently localised.

Money vs market data:
    Prices that are *money* (entry, stop, target) are ``Decimal``. Prices that feed
    *indicators* (bar OHLC) are ``float`` -- indicators are ratios and smoothing, and
    Decimal there buys nothing but slowness. Conversion happens at exactly one place,
    :func:`src.costs.to_decimal`, which goes via ``str`` so no binary float error
    leaks into a currency figure.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.market.calendar import IST, ensure_ist

__all__ = [
    "BAR_INTERVAL",
    "PositionState",
    "ExitRule",
    "Side",
    "Bar",
    "Position",
    "PositionRuntime",
    "IndicatorSnapshot",
    "ExitAdvice",
    "completed_bars",
]

#: The engine evaluates on 1-minute bars.
BAR_INTERVAL = timedelta(minutes=1)


class PositionState(str, Enum):
    """The one state emitted per position per evaluation.

    Ordered here by descending urgency, which is also the precedence order the state
    machine resolves in -- see :func:`src.positions.exit_engine.evaluate`.
    """

    #: 15:15 IST intraday square-off. Time-driven, unconditional, beats everything.
    FORCE_EXIT = "FORCE_EXIT"
    #: Feed has gone quiet. Advice suppressed; last-known P&L shown but labelled.
    STALE_DATA = "STALE_DATA"
    #: The entry conditions were ALREADY violated when the position was opened.
    #: Diagnostic, fired once at the first completed bar after entry. It reports that
    #: the position should not exist -- it is not an exit instruction, and it does not
    #: suppress anything: normal advice resumes on the next bar.
    INVALID_ENTRY = "INVALID_ENTRY"
    #: Stop hit, VWAP lost on above-average volume, or momentum reversed.
    EXIT_NOW = "EXIT_NOW"
    #: Held long enough with no movement -- the edge has decayed.
    TIME_EXIT = "TIME_EXIT"
    #: Target 1 reached; book part of the position and trail the rest.
    PARTIAL_EXIT = "PARTIAL_EXIT"
    #: In profit past the breakeven trigger; the stop is being trailed up.
    TRAIL = "TRAIL"
    #: Trend intact, nothing to do.
    HOLD = "HOLD"

    @property
    def is_actionable(self) -> bool:
        """False when the state carries no price-based recommendation to act on."""
        return self is not PositionState.STALE_DATA

    @property
    def suggests_exit(self) -> bool:
        """True when the advice is to reduce or close the position.

        INVALID_ENTRY is deliberately absent. It says the position should not have been
        opened, which is a different claim from "get out now" -- and if the exit rules
        do want you out, they say so on the very next bar in their own words.
        """
        return self in {
            PositionState.FORCE_EXIT,
            PositionState.EXIT_NOW,
            PositionState.TIME_EXIT,
            PositionState.PARTIAL_EXIT,
        }


class ExitRule(str, Enum):
    """Which specific rule produced a state.

    ``PositionState`` says *what to do*; this says *why*, as a machine-readable tag.
    Three different conditions all surface as EXIT_NOW, and aggregating them requires
    telling them apart -- grepping the prose reason string for "Momentum reversal"
    works right up until someone rewords it.
    """

    STOP_HIT = "STOP_HIT"
    VWAP_LOSS = "VWAP_LOSS"
    #: The entry itself was unsound. See PositionState.INVALID_ENTRY.
    ENTRY_ALREADY_INVALID = "ENTRY_ALREADY_INVALID"
    MOMENTUM_REVERSAL = "MOMENTUM_REVERSAL"
    TIME_DECAY = "TIME_DECAY"
    TARGET_1 = "TARGET_1"
    SQUARE_OFF = "SQUARE_OFF"
    MARKET_CLOSED = "MARKET_CLOSED"
    STALE_FEED = "STALE_FEED"
    NO_DATA = "NO_DATA"
    #: HOLD / TRAIL -- no rule fired.
    NONE = "NONE"


class Side(str, Enum):
    """Direction of the recorded position.

    Only LONG is supported. The exit logic is long-specific throughout (trail *below*
    the running high, exit on losing VWAP *from above*), so a short position would be
    evaluated with inverted rules and silently wrong advice. It is rejected instead.
    """

    LONG = "LONG"


def _require_ist(value: datetime) -> datetime:
    """Validator helper: reject naive datetimes, normalise to IST."""
    return ensure_ist(value)


class Bar(BaseModel):
    """One completed OHLCV bar.

    ``ts`` is the bar's OPEN time, matching the Fyers historical convention. A bar
    stamped 09:15 covers [09:15, 09:16) and is not complete until 09:16. Comparisons
    against a decision time must therefore use ``ts + BAR_INTERVAL``, never ``ts`` --
    see :func:`completed_bars`.
    """

    model_config = ConfigDict(frozen=True)

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = Field(ge=0)

    _norm_ts = field_validator("ts")(_require_ist)

    @property
    def close_time(self) -> datetime:
        """When this bar finished forming, i.e. when its values became usable."""
        return self.ts + BAR_INTERVAL

    @property
    def typical_price(self) -> float:
        """(H + L + C) / 3 -- the price VWAP weights by volume."""
        return (self.high + self.low + self.close) / 3.0

    @model_validator(mode="after")
    def _check_ohlc_consistency(self) -> "Bar":
        if self.high < self.low:
            raise ValueError(f"bar {self.ts}: high {self.high} < low {self.low}")
        if self.high < max(self.open, self.close):
            raise ValueError(f"bar {self.ts}: high {self.high} below open/close")
        if self.low > min(self.open, self.close):
            raise ValueError(f"bar {self.ts}: low {self.low} above open/close")
        return self


class Position(BaseModel):
    """A position the user has ALREADY bought manually in their broker app.

    This system did not open it and cannot close it. It only advises.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    symbol: str
    quantity: int = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    entry_time: datetime
    stop_loss: Decimal | None = None
    target: Decimal | None = None
    side: Side = Side.LONG
    segment: str = "EQ"
    notes: str | None = None

    _norm_entry_time = field_validator("entry_time")(_require_ist)

    @field_validator("symbol")
    @classmethod
    def _normalise_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must not be empty")
        return v

    @model_validator(mode="after")
    def _check_levels(self) -> "Position":
        if self.stop_loss is not None and self.stop_loss >= self.entry_price:
            raise ValueError(
                f"stop_loss {self.stop_loss} must be below entry {self.entry_price} for a long"
            )
        if self.target is not None and self.target <= self.entry_price:
            raise ValueError(
                f"target {self.target} must be above entry {self.entry_price} for a long"
            )
        return self

    @property
    def notional(self) -> Decimal:
        return self.entry_price * self.quantity

    @property
    def trade_date(self):
        """IST calendar date the position was opened on."""
        return self.entry_time.date()


class PositionRuntime(BaseModel):
    """Mutable engine state for one position.

    Persisted between evaluations so a restart mid-session does not reset the trail
    and hand back risk the position had already locked in. See
    :class:`src.positions.store.PositionStore`.
    """

    position_id: str
    #: Highest completed-bar close seen since entry. Drives the trailing stop.
    highest_price: float
    #: Current advisory stop. MONOTONIC: :meth:`raise_stop` is the only mutator and
    #: it refuses to lower. A stop that can move down is not a stop.
    current_stop: Decimal | None = None
    #: Set once price has travelled `breakeven_atr_multiple` ATRs in profit.
    breakeven_armed: bool = False
    #: Set once PARTIAL_EXIT has been advised, so it fires once rather than every bar.
    partial_advised: bool = False
    #: Set once the entry-validity check has run. It is a one-shot diagnostic about a
    #: moment that has passed; repeating it every bar would be noise, and re-running it
    #: on later data would be answering a different question.
    entry_checked: bool = False
    #: What the entry check concluded, kept so it stays visible after the bar it fired
    #: on. None means "not yet checked"; empty string means "checked, entry was clean".
    entry_verdict: str | None = None
    #: Quantity still held. Updated when the user records an actual partial fill.
    remaining_quantity: int = Field(gt=0)
    last_state: PositionState | None = None
    last_evaluated_at: datetime | None = None

    @field_validator("last_evaluated_at")
    @classmethod
    def _norm_eval_time(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_ist(v)

    @classmethod
    def initial(cls, position: Position) -> "PositionRuntime":
        """Fresh runtime state for a newly recorded position.

        ``highest_price`` seeds at the entry price, not at zero: before any bar has
        printed, the best price seen is the one paid.
        """
        return cls(
            position_id=position.position_id,
            highest_price=float(position.entry_price),
            current_stop=position.stop_loss,
            remaining_quantity=position.quantity,
        )

    def raise_stop(self, candidate: Decimal | None) -> bool:
        """Move the stop up to ``candidate`` if that is an improvement.

        Returns True if the stop moved. A candidate at or below the current stop is
        ignored -- the stop NEVER moves down, which is what makes a trail a ratchet
        rather than a way to keep giving back profit.
        """
        if candidate is None:
            return False
        if self.current_stop is None or candidate > self.current_stop:
            self.current_stop = candidate
            return True
        return False

    def update_high(self, price: float) -> bool:
        """Record a new running high if ``price`` exceeds it. Returns True if raised.

        Must only be called with a price from a completed bar on a live feed. Feeding
        a stale price in would let the trail ratchet on data that is no longer true.
        """
        if price > self.highest_price:
            self.highest_price = price
            return True
        return False


class IndicatorSnapshot(BaseModel):
    """Indicator values as of one completed bar.

    Any field may be None when there is not enough history yet. None means "unknown",
    and the engine must not treat it as a passing or failing condition.
    """

    model_config = ConfigDict(frozen=True)

    #: Close time of the last completed bar -- the moment these values became known.
    asof: datetime
    last_price: float
    bars_used: int
    vwap: float | None = None
    atr14: float | None = None
    ema9: float | None = None
    ema21: float | None = None
    rsi14: float | None = None
    volume: int | None = None
    avg_volume20: float | None = None
    volume_ratio: float | None = None

    _norm_asof = field_validator("asof")(_require_ist)

    @property
    def has_core_indicators(self) -> bool:
        """True when ATR and VWAP are both available -- the minimum to advise on."""
        return self.atr14 is not None and self.vwap is not None


class ExitAdvice(BaseModel):
    """One evaluation's output: exactly one state, with the reasoning spelled out.

    The user executes manually and must be able to disagree, so ``reason`` carries the
    actual numbers that produced the state, not a category label.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    symbol: str
    state: PositionState
    #: Which rule fired. Use this for analysis, never the reason string.
    rule: ExitRule = ExitRule.NONE
    reason: str
    asof: datetime
    last_price: float | None = None
    stop_price: Decimal | None = None
    target1: Decimal | None = None
    #: How much to sell if acting on this advice. 0 for HOLD/TRAIL/STALE_DATA.
    suggested_exit_quantity: int = 0
    #: Always present, always carries gross AND net. Never rendered gross-only.
    pnl: object | None = None  # src.costs.PnLBreakdown; untyped to avoid a cycle
    indicators: IndicatorSnapshot | None = None
    feed_stale: bool = False
    stale_seconds: float | None = None

    _norm_asof = field_validator("asof")(_require_ist)

    @property
    def actionable(self) -> bool:
        """False when the advice is informational only (stale feed)."""
        return self.state.is_actionable

    def render(self) -> str:
        """Terminal-friendly multi-line rendering, P&L always gross AND net."""
        lines = [
            f"[{self.asof.strftime('%H:%M:%S')} IST] {self.symbol}  {self.state.value}",
            f"  reason : {self.reason}",
        ]
        if self.last_price is not None:
            lines.append(f"  price  : {self.last_price:.2f}")
        if self.stop_price is not None:
            lines.append(f"  stop   : {self.stop_price}")
        if self.pnl is not None:
            lines.append(f"  P&L    : {self.pnl.summary()}")
        if self.suggested_exit_quantity:
            lines.append(f"  action : sell {self.suggested_exit_quantity} (you, manually)")
        return "\n".join(lines)


def completed_bars(
    bars: Sequence[Bar],
    asof: datetime,
    interval: timedelta = BAR_INTERVAL,
) -> list[Bar]:
    """Bars fully formed at or before ``asof``. The look-ahead guard.

    Data-availability boundary:
        Returns bars where ``ts + interval <= asof``. The bar whose interval CONTAINS
        ``asof`` is EXCLUDED -- at 09:17 the 09:15 bar is complete but the 09:17 bar
        is still forming, and only its open is known. Using its high, low, or close
        is look-ahead: it encodes what price did after the decision moment.

        Every indicator and every state-machine rule reads bars through this function.
        Nothing reads the raw list.

    Raises:
        NaiveDatetimeError: if ``asof`` is naive.
    """
    asof = ensure_ist(asof)
    return [b for b in bars if b.ts + interval <= asof]


def bars_for_session(bars: Sequence[Bar], session_date) -> list[Bar]:
    """Bars belonging to one IST trading session.

    Used to anchor VWAP to the session rather than letting it roll across days.
    """
    return [b for b in bars if b.ts.astimezone(IST).date() == session_date]
