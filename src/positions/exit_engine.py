"""Module C -- the exit advisory state machine.

Emits exactly one :class:`~src.positions.models.PositionState` per evaluation, with
a reason string carrying the numbers that produced it. The user executes manually
and must be able to disagree, so "EXIT_NOW because stop hit" is not enough -- the
reason says which stop, where it was, and why it was there.

**Driveable by replay.** :func:`evaluate` is a pure function of its arguments. It
never reads the clock, never opens a socket, and never touches the store. ``now``
and ``last_tick_at`` are injected. That is what lets the same code run against a
historical day and against a live feed, and it is why a replayed result is
reproducible bit-for-bit.

Precedence
----------
Exactly one state wins, resolved in this order::

    1. FORCE_EXIT     15:15 IST square-off, or market closed with a position open
    2. STALE_DATA     feed quiet longer than the threshold
    3. INVALID_ENTRY  first bar after entry, and the entry was already unsound
    4. EXIT_NOW       stop hit / VWAP lost on volume / momentum reversed
    5. TIME_EXIT      held past the decay window with nothing to show
    6. PARTIAL_EXIT   Target 1 reached, first time
    7. TRAIL          in profit past the breakeven trigger
    8. HOLD           nothing to do

INVALID_ENTRY outranks EXIT_NOW on the ONE bar it can fire, and only there. When an
exit condition was already true at entry, both would fire on the same bar and they
carry the same information -- but "you entered while momentum was already down" is
the useful sentence and "momentum reversal" is the misleading one. From the next bar
onward the entry check is spent and the exit rules speak for themselves, so nothing
is suppressed: a position that should be closed still says EXIT_NOW one minute later.

FORCE_EXIT sits above STALE_DATA deliberately. Square-off is driven by the clock,
not by a price, so a dead feed does not make the reminder wrong -- and suppressing
it would go silent at exactly the moment it matters most. The P&L attached to that
advice is labelled stale rather than presented as current.

Everything below FORCE_EXIT is price-based and is therefore suppressed by a stale
feed, per the "failing loud beats failing silent" rule.

Long only. See :class:`~src.positions.models.Side`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Sequence

from config.settings import (
    CostSettings,
    ExitEngineSettings,
    get_cost_settings,
    get_exit_engine_settings,
)
from src.costs import PAISA, unrealised_pnl
from src.market.calendar import ensure_ist, is_trading_day, square_off_time
from src.positions.indicators import compute_indicators, ema, rsi
from src.positions.models import (
    BAR_INTERVAL,
    Bar,
    ExitAdvice,
    ExitRule,
    IndicatorSnapshot,
    Position,
    PositionRuntime,
    PositionState,
    completed_bars,
)

__all__ = [
    "evaluate",
    "target_one",
    "PRECEDENCE",
    "EntryCheck",
    "EntryProblem",
    "check_entry",
    "check_hypothetical_entry",
    "entry_findings",
    "evaluate_entry_conditions",
    "feed_staleness",
    "bars_have_stopped",
    "square_off_reason",
]

#: Documented precedence, highest first. Mirrored by the test suite so a reordering
#: of the state machine cannot pass silently.
PRECEDENCE: tuple[PositionState, ...] = (
    PositionState.FORCE_EXIT,
    PositionState.STALE_DATA,
    PositionState.INVALID_ENTRY,
    PositionState.EXIT_NOW,
    PositionState.TIME_EXIT,
    PositionState.PARTIAL_EXIT,
    PositionState.TRAIL,
    PositionState.HOLD,
)


def _round_paisa(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(PAISA, rounding=ROUND_HALF_UP)


def target_one(
    position: Position,
    atr_value: float | None,
    cfg: ExitEngineSettings,
) -> Decimal | None:
    """Target 1: the explicit target if recorded, else an ATR multiple above entry.

    Falling back to ``entry + t1_atr_multiple * ATR`` scales the target to the
    symbol's own volatility, so a quiet stock gets a near target and a wild one a far
    target. A fixed percentage would be trivial on one and unreachable on the other.

    Returns None when no target was set and ATR is not yet available -- in which case
    PARTIAL_EXIT simply cannot fire, which is the honest outcome.
    """
    if position.target is not None:
        return position.target
    if atr_value is None:
        return None
    return _round_paisa(float(position.entry_price) + float(cfg.t1_atr_multiple) * atr_value)


def _stop_kind(runtime: PositionRuntime, position: Position) -> str:
    if runtime.current_stop is None:
        return "none"
    if runtime.breakeven_armed and runtime.current_stop > position.entry_price:
        return "trailing"
    if runtime.current_stop == position.entry_price:
        return "breakeven"
    return "initial"


def _update_stop(
    position: Position,
    runtime: PositionRuntime,
    done: Sequence[Bar],
    snap: IndicatorSnapshot,
    cfg: ExitEngineSettings,
) -> None:
    """Advance the running high and ratchet the stop upward. Never downward.

    Two stages, per the trailing rule:

      1. Once the running high is ``breakeven_atr_multiple`` ATRs above entry, arm
         breakeven and pull the stop up to the entry price.
      2. Once armed, trail ``trail_atr_multiple`` ATRs below the running high.

    Stage 2's candidate can be below entry early on -- the monotonic ratchet in
    :meth:`PositionRuntime.raise_stop` discards it, which is exactly why breakeven
    can never be given back. It also protects against ATR *widening*: a bigger ATR
    computes a lower trail, and without the ratchet the stop would slide back down
    every time volatility picked up.

    The running high is recomputed from every completed bar since entry, not just
    from the close of the bar being evaluated. That makes the trail correct on a
    single-shot evaluation -- a replay driver, or a process that restarted and
    missed bars -- rather than only when the engine has observed every tick in
    sequence.

    Data-availability boundary:
        Reads bars with ``ts >= position.entry_time`` from ``done`` (already filtered
        to completed bars). Bars from before entry are excluded: the position was not
        held then, so their highs are not part of its excursion.

    Called only when the feed is fresh. Ratcheting on a stale price would lock the
    stop onto a level the market may have long since left.
    """
    for bar in done:
        if bar.ts >= position.entry_time:
            runtime.update_high(bar.close)
    runtime.update_high(snap.last_price)
    if snap.atr14 is None:
        return

    entry = float(position.entry_price)
    breakeven_trigger = entry + float(cfg.breakeven_atr_multiple) * snap.atr14
    if runtime.highest_price >= breakeven_trigger:
        runtime.breakeven_armed = True
        runtime.raise_stop(position.entry_price)

    if runtime.breakeven_armed:
        trail = runtime.highest_price - float(cfg.trail_atr_multiple) * snap.atr14
        runtime.raise_stop(_round_paisa(trail))


def _momentum_reversed_at(bars: Sequence[Bar], cfg: ExitEngineSettings) -> bool:
    """Was momentum reversed as of the last bar in ``bars``?

    EMA9 below EMA21 and RSI14 below the reversal level. Recomputed from the bar
    window rather than read off a snapshot, so it can be asked about earlier bars.

    Data-availability boundary:
        Uses only ``bars``, which the caller has already filtered to completed bars.
        Returns False when any input indicator is unavailable -- "unknown" must never
        read as "reversed".
    """
    e9 = ema(bars, 9)
    e21 = ema(bars, 21)
    r = rsi(bars, 14)
    if e9 is None or e21 is None or r is None:
        return False
    return e9 < e21 and r < float(cfg.rsi_reversal_level)


def _momentum_confirmed(
    done: Sequence[Bar],
    snap: IndicatorSnapshot,
    cfg: ExitEngineSettings,
) -> bool:
    """True when momentum has been reversed for N consecutive completed bars.

    ``momentum_confirmation_bars = 1`` is the original single-bar rule: fire the
    instant EMA9 crosses below EMA21 with RSI under the level. Higher N demands the
    condition persist, trading responsiveness for fewer whipsaw exits.

    Which N is right is an EMPIRICAL question. It is a swept parameter in
    reports/momentum_analysis.md, not something to pick by intuition.

    Data-availability boundary:
        Bar k back is evaluated on ``done[:len(done)-k]`` -- only bars completed at or
        before that bar's close. No future bar leaks into a past bar's verdict.
    """
    n = max(1, cfg.momentum_confirmation_bars)
    if n == 1:
        if snap.ema9 is None or snap.ema21 is None or snap.rsi14 is None:
            return False
        return snap.ema9 < snap.ema21 and snap.rsi14 < float(cfg.rsi_reversal_level)
    if len(done) < n:
        return False
    return all(_momentum_reversed_at(done[: len(done) - k], cfg) for k in range(n))


class EntryProblem(str, Enum):
    """Which entry predicate fired, as a stable tag rather than as prose.

    Same rule as :class:`ExitRule`: analysis keys off the tag. Never classify a finding
    by grepping the human-readable string -- it gets reworded, and every aggregate that
    depended on the old wording silently changes without anything failing.
    """

    MOMENTUM_REVERSED = "MOMENTUM_REVERSED"
    BELOW_VWAP = "BELOW_VWAP"
    STOP_ALREADY_HIT = "STOP_ALREADY_HIT"


@dataclass(frozen=True)
class EntryCheck:
    """Whether an entry was sound, judged on what was knowable at the entry bar.

    The wording matters and is the whole point of this class. The exit rules already
    detected these conditions -- 48% of positions in the momentum analysis exited on
    the first completed bar because the exit condition was true before the position
    existed. Reporting that as "momentum reversal" describes the market; reporting it
    as "you entered while momentum was already down" describes the mistake.
    """

    valid: bool
    #: Past-tense statements about the entry, e.g. "EMA9 1032.16 was already below
    #: EMA21 1036.02 with RSI14 43.2".
    problems: tuple[str, ...]
    #: Checks that could not run, usually for want of indicator history. Never
    #: silently counted as passing.
    unevaluated: tuple[str, ...]
    asof: datetime
    entry_price: Decimal
    price_at_check: float
    indicators: IndicatorSnapshot | None
    #: The same findings as ``problems``, tagged. Aligned index for index -- a test
    #: pins that, so analysis can classify without reading the prose.
    codes: tuple[EntryProblem, ...] = ()

    def summary(self) -> str:
        if self.valid:
            base = "Entry looks sound: no entry condition was already violated."
        else:
            base = "Entry was unsound -- " + "; ".join(self.problems) + "."
        if self.unevaluated:
            base += f" (Not checkable: {', '.join(self.unevaluated)}.)"
        return base

    def render(self, symbol: str) -> str:
        mark = "OK" if self.valid else "INVALID"
        lines = [
            f"[{self.asof.strftime('%H:%M:%S')} IST] {symbol}  ENTRY {mark}",
            f"  entry price : {self.entry_price}",
            f"  price at check: {self.price_at_check:.2f}",
        ]
        snap = self.indicators
        if snap is not None:
            def fmt(v, spec=".2f"):
                return "n/a" if v is None else format(v, spec)
            lines.append(
                f"  VWAP {fmt(snap.vwap)}  EMA9 {fmt(snap.ema9)}  "
                f"EMA21 {fmt(snap.ema21)}  RSI14 {fmt(snap.rsi14, '.1f')}  "
                f"ATR {fmt(snap.atr14)}"
            )
        for problem in self.problems:
            lines.append(f"  ! {problem}")
        for skipped in self.unevaluated:
            lines.append(f"  ? not checkable: {skipped}")
        if self.valid:
            lines.append("  no entry condition was already violated")
        return "\n".join(lines)


def feed_staleness(
    now: datetime,
    last_tick_at: datetime | None,
    cfg: ExitEngineSettings,
) -> tuple[bool, float | None]:
    """(is_stale, age_seconds) by the exit engine's rule. The single definition.

    Never received data counts as stale. This is the rule that suppresses every
    price-based exit state, and it exists as a function so nothing has to re-derive it
    from ``last_tick_at`` and risk drifting from it.
    """
    age: float | None = None
    if last_tick_at is not None:
        age = (ensure_ist(now) - ensure_ist(last_tick_at)).total_seconds()
    return (last_tick_at is None or age > float(cfg.stale_feed_seconds)), age


def bars_have_stopped(
    now: datetime,
    last_tick_at: datetime | None,
    cfg: ExitEngineSettings,
) -> tuple[bool, float | None]:
    """Have COMPLETED BARS stopped arriving? A different question from feed_staleness.

    Both ask "is the data dead", but they measure different things and the difference is
    a whole bar:

    * :func:`feed_staleness` measures TICK recency. On a live socket a 30-second silence
      is a real fault, and it is what suppresses exit advice.
    * A completed bar only appears once a minute BY CONSTRUCTION. Measuring bar recency
      against a 30-second threshold therefore reports "stale" for the second half of
      every single minute, whatever the feed is doing.

    That is not hypothetical. In REPLAY ``last_tick_at`` IS the last completed bar's
    close, so the exit engine's rule flips a healthy replayed position
    HOLD -> STALE_DATA -> HOLD every minute; the common speed settings only dodge it
    because their clock steps land exactly on :00 and :30. See
    ``test_replay_staleness_flickers_within_a_minute``, which pins the behaviour as it
    is rather than as it should be.

    The entry check consumes ONLY completed bars, so this is the question it has to ask,
    and the threshold is derived rather than picked: one bar interval (the expected gap
    between completed bars) plus the configured tolerance on top of it.
    """
    age: float | None = None
    if last_tick_at is not None:
        age = (ensure_ist(now) - ensure_ist(last_tick_at)).total_seconds()
    limit = BAR_INTERVAL.total_seconds() + float(cfg.stale_feed_seconds)
    return (last_tick_at is None or age > limit), age


def entry_findings(
    entry_price: Decimal,
    stop_loss: Decimal | None,
    snap: IndicatorSnapshot,
    cfg: ExitEngineSettings,
) -> tuple[tuple[tuple[EntryProblem, str], ...], tuple[str, ...]]:
    """The three entry predicates, ONCE, as ((code, prose), ...) and unevaluated.

    The single implementation. :func:`evaluate_entry_conditions` projects the prose out
    of this and analysis projects the codes, so the two can never disagree about
    whether a condition fired -- which is what a second copy of these three ``if``s
    would eventually do.

    See :func:`evaluate_entry_conditions` for what each predicate means and why.
    """
    findings: list[tuple[EntryProblem, str]] = []
    unevaluated: list[str] = []
    price = snap.last_price

    if snap.ema9 is None or snap.ema21 is None or snap.rsi14 is None:
        missing = [
            n for n, v in (("EMA9", snap.ema9), ("EMA21", snap.ema21), ("RSI14", snap.rsi14))
            if v is None
        ]
        unevaluated.append(f"momentum ({', '.join(missing)} not computable yet)")
    elif snap.ema9 < snap.ema21 and snap.rsi14 < float(cfg.rsi_reversal_level):
        findings.append((
            EntryProblem.MOMENTUM_REVERSED,
            f"EMA9 {snap.ema9:.2f} was already below EMA21 {snap.ema21:.2f} "
            f"and RSI14 was already {snap.rsi14:.1f} "
            f"(below {cfg.rsi_reversal_level}) -- momentum was against this trade "
            f"before it was opened",
        ))

    if snap.vwap is None:
        unevaluated.append("VWAP (no session volume yet)")
    elif price < snap.vwap:
        findings.append((
            EntryProblem.BELOW_VWAP,
            f"price {price:.2f} was already below session VWAP {snap.vwap:.2f} "
            f"(by {snap.vwap - price:.2f}) -- bought into a market trading below its "
            f"own volume-weighted average",
        ))

    if stop_loss is not None:
        if entry_price <= stop_loss:
            findings.append((
                EntryProblem.STOP_ALREADY_HIT,
                f"entry price {entry_price} was already at or below the stop "
                f"{stop_loss} -- the position had no room before it started",
            ))
        elif price <= float(stop_loss):
            findings.append((
                EntryProblem.STOP_ALREADY_HIT,
                f"price {price:.2f} was already at or through the stop {stop_loss} "
                f"by the first completed bar -- the position was stopped out before "
                f"it was first evaluated",
            ))

    return tuple(findings), tuple(unevaluated)


def evaluate_entry_conditions(
    entry_price: Decimal,
    stop_loss: Decimal | None,
    snap: IndicatorSnapshot,
    cfg: ExitEngineSettings,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The three entry tests, as (problems, unevaluated). Past tense, about the entry.

    1. Momentum already reversed: EMA9 below EMA21 AND RSI below the reversal level.
       The same pair the EXIT_NOW rule uses, but with no confirmation-bar requirement:
       for an entry, one bar of "already broken" is enough to say so.
    2. Price already below session VWAP. Deliberately WITHOUT the above-average-volume
       gate the exit rule applies. Confirmation matters when deciding to abandon a
       position you hold; for a position you have not yet justified, being on the wrong
       side of VWAP at all is the finding.
    3. Price already at or through the stop. A position born past its own stop had no
       room to work.

    A condition whose indicator is unavailable is listed as unevaluated, never treated
    as passing.

    Projects the prose out of :func:`entry_findings`, which holds the predicates.
    """
    findings, unevaluated = entry_findings(entry_price, stop_loss, snap, cfg)
    return tuple(prose for _, prose in findings), unevaluated


def _first_bar_after_entry(done: Sequence[Bar], entry_time: datetime) -> Bar | None:
    for bar in done:
        if bar.ts >= entry_time:
            return bar
    return None


def check_entry(
    position: Position,
    bars: Sequence[Bar],
    now: datetime,
    *,
    engine_cfg: ExitEngineSettings | None = None,
) -> EntryCheck | None:
    """Judge a recorded position's entry, as of the FIRST completed bar after entry.

    Returns None when no bar at or after ``entry_time`` has completed yet -- there is
    nothing to judge.

    Data-availability boundary:
        Indicators are computed on bars up to and including the first bar whose
        ``ts >= entry_time``, NOT on everything known by ``now``. Judging an entry with
        later data would answer a different question -- "did it work out" rather than
        "was it defensible at the time" -- and would be look-ahead.
    """
    engine_cfg = engine_cfg or get_exit_engine_settings()
    done = completed_bars(bars, ensure_ist(now))
    first = _first_bar_after_entry(done, position.entry_time)
    if first is None:
        return None

    upto = [b for b in done if b.ts <= first.ts]
    snap = compute_indicators(upto, session_date=position.trade_date)
    if snap is None:
        return None

    findings, unevaluated = entry_findings(
        position.entry_price, position.stop_loss, snap, engine_cfg
    )
    return EntryCheck(
        valid=not findings,
        problems=tuple(p for _, p in findings),
        codes=tuple(c for c, _ in findings),
        unevaluated=unevaluated,
        asof=first.close_time,
        entry_price=position.entry_price,
        price_at_check=snap.last_price,
        indicators=snap,
    )


def check_hypothetical_entry(
    bars: Sequence[Bar],
    at: datetime,
    entry_price: Decimal | float | None = None,
    stop_loss: Decimal | None = None,
    *,
    session_date: date | None = None,
    engine_cfg: ExitEngineSettings | None = None,
) -> EntryCheck | None:
    """Judge an entry you have NOT made, as of the last completed bar at ``at``.

    Backs the ``check-entry`` CLI verb: ask before buying, record nothing. Defaults
    ``entry_price`` to the last completed close, which is the only price that has
    actually printed.

    Data-availability boundary:
        Uses bars with ``ts + BAR_INTERVAL <= at``. The forming bar is excluded, so
        this answers the question with exactly what a live screen would show.
    """
    engine_cfg = engine_cfg or get_exit_engine_settings()
    at = ensure_ist(at)
    done = completed_bars(bars, at)
    if not done:
        return None
    snap = compute_indicators(done, session_date=session_date or at.date())
    if snap is None:
        return None

    price = Decimal(str(snap.last_price)) if entry_price is None else Decimal(str(entry_price))
    findings, unevaluated = entry_findings(price, stop_loss, snap, engine_cfg)
    problems = tuple(p for _, p in findings)
    return EntryCheck(
        valid=not problems,
        problems=problems,
        codes=tuple(c for c, _ in findings),
        unevaluated=unevaluated,
        asof=done[-1].close_time,
        entry_price=price,
        price_at_check=snap.last_price,
        indicators=snap,
    )


def _exit_now_reason(
    position: Position,
    runtime: PositionRuntime,
    snap: IndicatorSnapshot,
    cfg: ExitEngineSettings,
    done: Sequence[Bar] = (),
) -> tuple[ExitRule, str] | None:
    """First EXIT_NOW condition that fires, as (rule, reason), or None.

    Conditions requiring an indicator that is not yet available are skipped, never
    treated as passing or failing. A missing RSI must not read as "momentum fine".
    """
    price = snap.last_price

    if runtime.current_stop is not None and price <= float(runtime.current_stop):
        kind = _stop_kind(runtime, position)
        detail = ""
        if kind == "trailing":
            # ATR can be None here even though a trailing stop exists: the stop is
            # persisted runtime state that outlives a restart, while ATR needs 15
            # completed bars in the CURRENT session. Resuming into an early session
            # hits exactly that. This string is cosmetic -- formatting None used to
            # raise TypeError out of evaluate(), which 500'd /positions AND the
            # websocket snapshot, blanking the whole dashboard over a label.
            atr_txt = f"{snap.atr14:.2f}" if snap.atr14 is not None else "not yet available"
            high_txt = (f"{runtime.highest_price:.2f}"
                        if runtime.highest_price is not None else "unknown")
            detail = (
                f", trailing {cfg.trail_atr_multiple}x ATR ({atr_txt}) below "
                f"session high {high_txt}"
            )
        elif kind == "breakeven":
            detail = ", stop was moved to breakeven after 1x ATR of profit"
        return ExitRule.STOP_HIT, (
            f"Stop hit: last {price:.2f} is at or below the {kind} stop "
            f"{runtime.current_stop}{detail}."
        )

    if (
        snap.vwap is not None
        and snap.volume_ratio is not None
        and price < snap.vwap
        and snap.volume_ratio > float(cfg.vwap_loss_volume_ratio)
    ):
        return ExitRule.VWAP_LOSS, (
            f"VWAP lost on above-average volume: last {price:.2f} below session VWAP "
            f"{snap.vwap:.2f} with volume {snap.volume_ratio:.2f}x the 20-bar average "
            f"({snap.avg_volume20:,.0f}). Sellers are in control, not drift."
        )

    if _momentum_confirmed(done, snap, cfg):
        n = max(1, cfg.momentum_confirmation_bars)
        held = "" if n == 1 else f", held for {n} consecutive bars"
        return ExitRule.MOMENTUM_REVERSAL, (
            f"Momentum reversal: EMA9 {snap.ema9:.2f} below EMA21 {snap.ema21:.2f} "
            f"and RSI14 {snap.rsi14:.1f} below {cfg.rsi_reversal_level}{held}."
        )

    return None


def _time_exit_reason(
    position: Position,
    snap: IndicatorSnapshot,
    now: datetime,
    gross_pct: Decimal,
    cfg: ExitEngineSettings,
) -> str | None:
    """TIME_EXIT reason if the position has gone nowhere for long enough, else None.

    The decay band is measured on GROSS percentage, not net. The question this rule
    asks is "has the price stopped moving", which is about price; costs are a
    constant drag that would bias every position toward looking like a loser and fire
    this rule early. Both figures are still reported on the advice.
    """
    minutes_held = (now - position.entry_time).total_seconds() / 60.0
    if minutes_held < cfg.time_exit_minutes:
        return None
    if abs(gross_pct) > cfg.time_exit_pnl_pct_band:
        return None
    if snap.atr14 is None:
        return None
    drift = abs(snap.last_price - float(position.entry_price))
    band = float(cfg.time_exit_atr_multiple) * snap.atr14
    if drift > band:
        return None
    return (
        f"Time decay: held {minutes_held:.0f} min (threshold {cfg.time_exit_minutes}), "
        f"gross P&L {gross_pct:+.2f}% inside the +/-{cfg.time_exit_pnl_pct_band}% band, "
        f"and price {snap.last_price:.2f} is {drift:.2f} from entry "
        f"{position.entry_price} -- within {cfg.time_exit_atr_multiple}x ATR "
        f"({band:.2f}). The setup has not worked; the edge has decayed."
    )


def _partial_quantity(runtime: PositionRuntime, cfg: ExitEngineSettings) -> int:
    """How much to book at Target 1.

    Returns 0 when the position cannot be split in a way that leaves a remainder to
    trail -- booking "half" of a single share is not a thing, and the engine should
    fall through to TRAIL rather than emit unfollowable advice.
    """
    qty = int(runtime.remaining_quantity * float(cfg.partial_exit_fraction))
    if qty <= 0 or qty >= runtime.remaining_quantity:
        return 0
    return qty


def square_off_reason(position: Position, now: datetime) -> tuple[ExitRule, str] | None:
    """FORCE_EXIT (rule, reason) if the square-off deadline has passed, else None.

    Purely time-based. Deliberately takes no price argument, which is what makes it
    safe to evaluate ahead of the staleness check -- and what makes it usable when the
    runtime has suppressed price-based advice entirely. A dead feed or a lapsed broker
    token cannot make a clock wrong, and going silent about an open intraday position at
    15:15 is the worst available outcome.

    Knows nothing about credentials and must keep knowing nothing: the caller decides
    whether the rest of the engine may run, and calls this either way.
    """
    if not is_trading_day(now):
        return ExitRule.MARKET_CLOSED, (
            f"Market is closed on {now.date()} (holiday or weekend) and an intraday "
            f"position in {position.symbol} is still open. Square this off."
        )
    deadline = square_off_time(now)
    if now >= deadline:
        return ExitRule.SQUARE_OFF, (
            f"Intraday square-off: {now.strftime('%H:%M:%S')} IST is at or past the "
            f"{deadline.strftime('%H:%M')} deadline. Unconditional -- this overrides "
            f"every other state."
        )
    return None


def evaluate(
    position: Position,
    runtime: PositionRuntime,
    bars: Sequence[Bar],
    now: datetime,
    last_tick_at: datetime | None,
    *,
    engine_cfg: ExitEngineSettings | None = None,
    cost_cfg: CostSettings | None = None,
) -> ExitAdvice:
    """Evaluate one position and return exactly one piece of advice.

    Pure with respect to time and I/O: ``now`` and ``last_tick_at`` are injected, so
    a replayed historical day and a live session run identical code.

    ``runtime`` is MUTATED in place -- the running high, the ratcheted stop, the
    breakeven and partial flags. The caller owns persisting it
    (:class:`src.positions.store.PositionStore`) so a restart mid-session does not
    reset the trail and hand back risk the position had already locked in.

    Data-availability boundary:
        Only bars with ``ts + BAR_INTERVAL <= now`` are read, via
        :func:`~src.positions.models.completed_bars`. The bar containing ``now`` is
        still forming and is excluded -- using its high, low, or close would encode
        what price did after the decision moment. All indicators derive from that
        filtered list. Nothing else reads ``bars``.

    Args:
        position: the position the user bought manually.
        runtime: mutable engine state, mutated in place.
        bars: 1-minute bars for the symbol, ascending. May include the forming bar
            and prior sessions; both are filtered out.
        now: decision timestamp, tz-aware IST.
        last_tick_at: when the feed last delivered data. None means no data has ever
            arrived, which counts as stale.

    Returns:
        An :class:`ExitAdvice` whose ``pnl`` always carries gross AND net.
    """
    engine_cfg = engine_cfg or get_exit_engine_settings()
    cost_cfg = cost_cfg or get_cost_settings()
    now = ensure_ist(now)

    done = completed_bars(bars, now)
    snap = compute_indicators(done, session_date=position.trade_date)

    feed_stale, stale_seconds = feed_staleness(now, last_tick_at, engine_cfg)

    def finish(
        state: PositionState,
        reason: str,
        *,
        rule: ExitRule = ExitRule.NONE,
        exit_qty: int = 0,
        t1: Decimal | None = None,
    ) -> ExitAdvice:
        pnl = None
        if snap is not None:
            pnl = unrealised_pnl(
                position.entry_price,
                snap.last_price,
                runtime.remaining_quantity,
                cost_cfg,
                price_is_stale=feed_stale,
            )
        runtime.last_state = state
        runtime.last_evaluated_at = now
        if state is PositionState.PARTIAL_EXIT:
            runtime.partial_advised = True
        return ExitAdvice(
            position_id=position.position_id,
            symbol=position.symbol,
            state=state,
            rule=rule,
            reason=reason,
            asof=now,
            last_price=None if snap is None else snap.last_price,
            stop_price=runtime.current_stop,
            target1=t1,
            suggested_exit_quantity=exit_qty,
            pnl=pnl,
            indicators=snap,
            feed_stale=feed_stale,
            stale_seconds=stale_seconds,
        )

    stale_note = ""
    if feed_stale:
        age = "never received data" if stale_seconds is None else f"{stale_seconds:.0f}s old"
        stale_note = (
            f" WARNING: price feed is {age} (threshold "
            f"{engine_cfg.stale_feed_seconds}s), so the P&L shown is last-known, not "
            f"current. Verify the price in your broker app before acting."
        )

    # ---- 1. FORCE_EXIT -- time-based, needs no price, beats everything ----------
    force = square_off_reason(position, now)
    if force is not None:
        force_rule, force_reason = force
        return finish(
            PositionState.FORCE_EXIT,
            force_reason + stale_note,
            rule=force_rule,
            exit_qty=runtime.remaining_quantity,
        )

    # ---- 2. STALE_DATA -- suppress every price-based rule below -----------------
    if feed_stale:
        age = "no data has arrived" if stale_seconds is None else f"last tick {stale_seconds:.0f}s ago"
        return finish(
            PositionState.STALE_DATA,
            f"Price feed is stale ({age}, threshold {engine_cfg.stale_feed_seconds}s). "
            f"Exit advice is SUPPRESSED -- acting on a price this old is worse than "
            f"not acting. Any P&L shown is last-known, not current. Check the feed, "
            f"and check the position in your broker app.",
            rule=ExitRule.STALE_FEED,
        )

    if snap is None:
        return finish(
            PositionState.STALE_DATA,
            f"No completed 1-minute bar for {position.symbol} at "
            f"{now.strftime('%H:%M:%S')} IST. Nothing to evaluate -- the first bar of "
            f"the session has not finished forming yet.",
            rule=ExitRule.NO_DATA,
        )

    # ---- Ratchet the stop before any price rule reads it ------------------------
    _update_stop(position, runtime, done, snap, engine_cfg)

    # ---- 3. INVALID_ENTRY -- one shot, on the first bar after entry -------------
    # Only fires when THIS evaluation is at that bar. If the engine starts later
    # (a restart, or a replay that joins mid-session) the moment has passed: the
    # verdict is still recorded on the runtime, but it is not announced as if it
    # were news about now.
    if not runtime.entry_checked:
        first = _first_bar_after_entry(done, position.entry_time)
        if first is not None:
            check = check_entry(position, bars, now, engine_cfg=engine_cfg)
            runtime.entry_checked = True
            runtime.entry_verdict = "" if check is None or check.valid else check.summary()
            at_that_bar = snap.asof == first.close_time
            if check is not None and not check.valid and at_that_bar:
                return finish(
                    PositionState.INVALID_ENTRY,
                    f"This position should not have been opened. {check.summary()} "
                    f"Diagnostic only -- this is not an exit instruction, and normal "
                    f"advice resumes on the next bar.",
                    rule=ExitRule.ENTRY_ALREADY_INVALID,
                    t1=target_one(position, snap.atr14, engine_cfg),
                )

    t1 = target_one(position, snap.atr14, engine_cfg)
    pnl = unrealised_pnl(
        position.entry_price, snap.last_price, runtime.remaining_quantity, cost_cfg
    )

    warmup = ""
    if snap.bars_used < engine_cfg.min_bars_for_advice:
        missing = [
            name
            for name, value in (
                ("ATR14", snap.atr14),
                ("RSI14", snap.rsi14),
                ("EMA9", snap.ema9),
                ("EMA21", snap.ema21),
                ("volume ratio", snap.volume_ratio),
            )
            if value is None
        ]
        if missing:
            warmup = (
                f" (Warming up: {snap.bars_used} bars, need "
                f"{engine_cfg.min_bars_for_advice}. Not yet computable: "
                f"{', '.join(missing)}. Rules depending on those were NOT evaluated.)"
            )

    # ---- 4. EXIT_NOW -----------------------------------------------------------
    exit_hit = _exit_now_reason(position, runtime, snap, engine_cfg, done)
    if exit_hit is not None:
        exit_rule, exit_reason = exit_hit
        return finish(
            PositionState.EXIT_NOW,
            exit_reason + warmup,
            rule=exit_rule,
            exit_qty=runtime.remaining_quantity,
            t1=t1,
        )

    # ---- 5. TIME_EXIT ----------------------------------------------------------
    time_reason = _time_exit_reason(position, snap, now, pnl.gross_pct, engine_cfg)
    if time_reason is not None:
        return finish(
            PositionState.TIME_EXIT,
            time_reason + warmup,
            rule=ExitRule.TIME_DECAY,
            exit_qty=runtime.remaining_quantity,
            t1=t1,
        )

    # ---- 6. PARTIAL_EXIT -------------------------------------------------------
    if not runtime.partial_advised and t1 is not None and snap.last_price >= float(t1):
        qty = _partial_quantity(runtime, engine_cfg)
        if qty > 0:
            source = "your recorded target" if position.target is not None else (
                f"{engine_cfg.t1_atr_multiple}x ATR ({snap.atr14:.2f}) above entry"
            )
            return finish(
                PositionState.PARTIAL_EXIT,
                f"Target 1 reached: last {snap.last_price:.2f} at or above T1 {t1} "
                f"({source}). Book {qty} of {runtime.remaining_quantity} and trail the "
                f"remainder; stop stays at {runtime.current_stop}." + warmup,
                rule=ExitRule.TARGET_1,
                exit_qty=qty,
                t1=t1,
            )

    # ---- 7. TRAIL --------------------------------------------------------------
    if runtime.breakeven_armed:
        return finish(
            PositionState.TRAIL,
            f"In profit and trailing: session high {runtime.highest_price:.2f}, stop "
            f"ratcheted to {runtime.current_stop} "
            f"({_stop_kind(runtime, position)}). Risk on this trade is no longer above "
            f"breakeven. Stop will not move down." + warmup,
            t1=t1,
        )

    # ---- 8. HOLD ---------------------------------------------------------------
    bits: list[str] = []
    if snap.vwap is not None:
        side = "above" if snap.last_price >= snap.vwap else "below"
        bits.append(f"{side} VWAP {snap.vwap:.2f}")
    if snap.atr14 is not None:
        to_breakeven = (
            float(position.entry_price)
            + float(engine_cfg.breakeven_atr_multiple) * snap.atr14
            - snap.last_price
        )
        bits.append(f"{to_breakeven:.2f} from the breakeven trigger")
    if runtime.current_stop is not None:
        bits.append(f"stop {runtime.current_stop} not hit")
    detail = "; ".join(bits) if bits else "no exit condition met"
    caution = ""
    if snap.vwap is not None and snap.last_price < snap.vwap:
        caution = (
            " Note: price is below VWAP but volume is not above average, so this is "
            "drift rather than distribution -- watch it."
        )
    return finish(
        PositionState.HOLD,
        f"Holding: last {snap.last_price:.2f}, {detail}.{caution}" + warmup,
        t1=t1,
    )
