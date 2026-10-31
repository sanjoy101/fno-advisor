"""Does the single-symbol entry check discriminate, or is it decoration?

The complaint that prompted this: the check reports UNSOUND on nearly everything, and
from the panel there is no way to tell whether that is an accurate description of the
market or a threshold firing on noise.

Four questions, in the order they have to be answered:

1. **How often does it fire?** A check that says UNSOUND 95% of the time carries almost
   no information whatever its predicates are, because the answer barely depends on the
   input.
2. **Which predicate fires?** Keyed off :class:`~src.positions.exit_engine.EntryProblem`
   tags, never off the prose -- see CLAUDE.md 2.2 on why classifying by reason string
   silently changes every aggregate the next time someone rewords a message.
3. **By how much?** A count cannot distinguish "price is meaningfully below VWAP" from
   "price is one tick below VWAP". The deviation is reported as a distribution, in
   percent AND in ATR units, because ATR is the only scale on which "this is noise" is
   a statement about the symbol rather than about its price level.
4. **Does it predict anything?** Forward return after SOUND versus after UNSOUND. This
   is the one that decides whether the other three matter.

**Question 4 has a trap, and it is the same one the swing experiment fell into
(CLAUDE.md 2.3): most of a stock's next hour is the market's next hour.** On a falling
morning nearly every symbol is below VWAP and nearly every symbol falls, so a raw
SOUND-vs-UNSOUND gap can be large, significant, and entirely beta. The primary measure
here is therefore **cross-sectionally demeaned**: each observation's forward return
minus the mean forward return of every symbol sampled at that same session and slot.
That asks the only question the panel's user cares about -- given that you are buying
something at 11:00 today, does the verdict pick a better-than-average something? The
raw figure is reported beside it, and where the two disagree the raw one is beta.

Data-availability boundary:
    Every observation is computed from ``completed_bars(bars, slot_time)`` -- the bar
    containing the sample instant is excluded, so no snapshot sees the minute it is
    deciding in. The forward return reads bars STRICTLY after that boundary and is
    never fed back into the verdict. Forward windows are capped at the 15:15 square-off
    because a hold this system could not have held is not a result it can claim; the
    actual horizon used is recorded per observation and slots with different horizons
    are never pooled.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Iterable, Sequence

from config.settings import ExitEngineSettings, get_exit_engine_settings
from src.analysis.momentum import Stat, minimum_detectable_effect, summarise
from src.market.calendar import IST, square_off_time
from src.positions.exit_engine import EntryProblem, entry_findings
from src.positions.indicators import compute_indicators
from src.positions.models import Bar, completed_bars

__all__ = [
    "SAMPLE_TIMES",
    "FORWARD_MINUTES",
    "EntryObservation",
    "observe_session",
    "verdict_distribution",
    "problem_breakdown",
    "vwap_deviation_profile",
    "demean_by_slot",
    "forward_comparison",
    "GroupDiff",
    "slot_label",
]

#: When the diagnostic samples. Spread across the session so a result cannot be an
#: artifact of one time of day, and spaced more than FORWARD_MINUTES apart so no two
#: forward windows for the same symbol overlap.
SAMPLE_TIMES: tuple[time, ...] = (time(9, 45), time(11, 0), time(13, 0), time(14, 30))

#: Forward window, matching the momentum analysis so the two are comparable.
FORWARD_MINUTES = 60


def slot_label(t: time) -> str:
    return t.strftime("%H:%M")


@dataclass(frozen=True)
class EntryObservation:
    """One check-entry evaluation, plus what happened next."""

    symbol: str
    session: date
    slot: str
    asof: datetime
    valid: bool
    codes: tuple[EntryProblem, ...]
    unevaluated: tuple[str, ...]
    price: float
    vwap: float | None
    atr14: float | None
    #: (price - VWAP) / VWAP as a percent. NEGATIVE means below VWAP.
    vwap_dev_pct: float | None
    #: (price - VWAP) / ATR. Negative means below. The scale on which "noise" means
    #: something: a 0.1% gap is huge for a quiet symbol and nothing for a volatile one.
    vwap_dev_atr: float | None
    #: Percent return from ``price`` to the close of the last completed bar at the end
    #: of the forward window. None when the window had no bar to close on.
    forward_pct: float | None
    #: Minutes actually used. Below FORWARD_MINUTES when the square-off truncated it.
    forward_minutes: int

    @property
    def fired(self) -> frozenset[EntryProblem]:
        return frozenset(self.codes)


def _forward_window_end(session: date, at: datetime) -> tuple[datetime, int]:
    """Where the forward window ends, and how many minutes that really is.

    Capped at the 15:15 intraday square-off. A 60-minute hold from 14:30 finishes at
    15:30, which this system could never have held -- reporting it as a 60-minute
    result would credit the check with a return no one could have taken.
    """
    wanted = at + timedelta(minutes=FORWARD_MINUTES)
    capped = min(wanted, square_off_time(session))
    return capped, max(0, int((capped - at).total_seconds() // 60))


def observe_session(
    symbol: str,
    session: date,
    bars: Sequence[Bar],
    *,
    slots: Sequence[time] = SAMPLE_TIMES,
    stop_loss: Decimal | None = None,
    cfg: ExitEngineSettings | None = None,
) -> list[EntryObservation]:
    """Evaluate the entry check at each slot of one symbol-session.

    ``stop_loss`` defaults to None because that is what the panel sends: the user asks
    "is this sound" without proposing a stop. The consequence is measured rather than
    assumed -- see :func:`problem_breakdown`.

    Data-availability boundary:
        Each slot's snapshot uses ``completed_bars(bars, slot)``: the bar whose minute
        contains the slot is still forming and is excluded. The forward price is read
        from bars completed at the window end, which is strictly later, and never
        reaches the verdict.
    """
    cfg = cfg or get_exit_engine_settings()
    out: list[EntryObservation] = []

    for slot in slots:
        at = datetime.combine(session, slot, tzinfo=IST)
        done = completed_bars(bars, at)
        if not done:
            continue
        snap = compute_indicators(done, session_date=session)
        if snap is None:
            continue

        price = snap.last_price
        findings, unevaluated = entry_findings(
            Decimal(str(price)), stop_loss, snap, cfg
        )

        dev_pct = dev_atr = None
        if snap.vwap:
            dev_pct = 100.0 * (price - snap.vwap) / snap.vwap
            if snap.atr14:
                dev_atr = (price - snap.vwap) / snap.atr14

        end, minutes = _forward_window_end(session, at)
        ahead = completed_bars(bars, end)
        forward = None
        if minutes > 0 and ahead and ahead[-1].ts > done[-1].ts and price > 0:
            forward = 100.0 * (ahead[-1].close - price) / price

        out.append(EntryObservation(
            symbol=symbol,
            session=session,
            slot=slot_label(slot),
            asof=done[-1].close_time,
            valid=not findings,
            codes=tuple(c for c, _ in findings),
            unevaluated=unevaluated,
            price=price,
            vwap=snap.vwap,
            atr14=snap.atr14,
            vwap_dev_pct=dev_pct,
            vwap_dev_atr=dev_atr,
            forward_pct=forward,
            forward_minutes=minutes,
        ))
    return out


# --------------------------------------------------------------------------------
# 1. How often does it fire?
# --------------------------------------------------------------------------------


def verdict_distribution(
    observations: Sequence[EntryObservation],
    *,
    by_slot: bool = False,
) -> list[dict]:
    """SOUND / UNSOUND share, overall or per slot.

    A check that answers the same way almost always is carrying almost no information,
    whatever its predicates say -- so the share is the first thing to look at, before
    any question about whether the answer is right.
    """
    buckets: dict[str, list[EntryObservation]] = defaultdict(list)
    for o in observations:
        buckets[o.slot if by_slot else "all"].append(o)

    rows = []
    for key in sorted(buckets):
        group = buckets[key]
        sound = sum(1 for o in group if o.valid)
        rows.append({
            "slot": key,
            "n": len(group),
            "sound": sound,
            "unsound": len(group) - sound,
            "sound_pct": round(100.0 * sound / len(group), 2) if group else 0.0,
            "partly_unevaluated": sum(1 for o in group if o.unevaluated),
        })
    return rows


# --------------------------------------------------------------------------------
# 2. Which predicate fired?
# --------------------------------------------------------------------------------


def problem_breakdown(observations: Sequence[EntryObservation]) -> list[dict]:
    """Share of UNSOUND verdicts each predicate is present in, and alone responsible for.

    Both columns matter. "Present in" double-counts when two conditions fire together;
    "sole cause" is what would change if the predicate were removed. A predicate that
    is never the sole cause is not doing any work.

    Every :class:`EntryProblem` member appears even at zero, because a zero here is a
    finding -- STOP_ALREADY_HIT cannot fire at all when no stop is supplied, which is
    exactly how the panel calls it.
    """
    unsound = [o for o in observations if not o.valid]
    total = len(unsound)
    rows = []
    for problem in EntryProblem:
        present = sum(1 for o in unsound if problem in o.fired)
        sole = sum(1 for o in unsound if o.fired == {problem})
        rows.append({
            "problem": problem.value,
            "present_n": present,
            "present_pct": round(100.0 * present / total, 2) if total else 0.0,
            "sole_cause_n": sole,
            "sole_cause_pct": round(100.0 * sole / total, 2) if total else 0.0,
        })
    return sorted(rows, key=lambda r: -r["present_n"])


def combination_breakdown(observations: Sequence[EntryObservation]) -> list[dict]:
    """Exact combinations of predicates, so overlap is visible rather than inferred."""
    unsound = [o for o in observations if not o.valid]
    counts: dict[tuple[str, ...], int] = defaultdict(int)
    for o in unsound:
        counts[tuple(sorted(c.value for c in o.fired))] += 1
    total = len(unsound)
    return sorted(
        (
            {"combination": " + ".join(k), "n": v,
             "pct": round(100.0 * v / total, 2) if total else 0.0}
            for k, v in counts.items()
        ),
        key=lambda r: -r["n"],
    )


# --------------------------------------------------------------------------------
# 3. By how much?
# --------------------------------------------------------------------------------


def _quantiles(values: Sequence[float], qs: Iterable[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    out = {}
    for q in qs:
        idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        out[f"p{int(q * 100)}"] = round(ordered[idx], 4)
    return out


def vwap_deviation_profile(observations: Sequence[EntryObservation]) -> dict:
    """How far below VWAP a BELOW_VWAP verdict typically is, as a distribution.

    Reported two ways on purpose. Percent is what the user reads on screen; ATR
    multiples are what decides whether it is noise, because 0.2% is a wide move for
    a quiet symbol and inside the bid-ask chatter for a volatile one. The share inside
    0.25 ATR is the "is this noise" number: a gap smaller than a quarter of the
    average bar range is not a market being below its average, it is a tick.
    """
    fired = [
        o for o in observations
        if EntryProblem.BELOW_VWAP in o.fired and o.vwap_dev_pct is not None
    ]
    below_pct = [-o.vwap_dev_pct for o in fired]  # positive = this far BELOW
    below_atr = [-o.vwap_dev_atr for o in fired if o.vwap_dev_atr is not None]

    def share(values, limit):
        return round(100.0 * sum(1 for v in values if v < limit) / len(values), 2) \
            if values else 0.0

    return {
        "n": len(fired),
        "pct_below_vwap": _quantiles(below_pct, (0.1, 0.25, 0.5, 0.75, 0.9, 0.95)),
        "atr_below_vwap": _quantiles(below_atr, (0.1, 0.25, 0.5, 0.75, 0.9, 0.95)),
        "n_with_atr": len(below_atr),
        "within_0.10pct": share(below_pct, 0.10),
        "within_0.25pct": share(below_pct, 0.25),
        "within_0.50pct": share(below_pct, 0.50),
        "within_0.25atr": share(below_atr, 0.25),
        "within_0.50atr": share(below_atr, 0.50),
        "within_1.00atr": share(below_atr, 1.00),
    }


# --------------------------------------------------------------------------------
# 4. Does the verdict predict anything?
# --------------------------------------------------------------------------------


def demean_by_slot(observations: Sequence[EntryObservation]) -> dict[int, float]:
    """Forward return minus the mean forward return of that session+slot cross-section.

    The naive comparator CLAUDE.md 1.2 and 2.3 both insist on. Without it, a verdict
    that fires on down mornings scores a large forward-return gap purely because down
    mornings keep falling -- an observation about the market, not about the check.

    Keyed by ``id()`` of the observation so a caller can attach it without the
    observations having to be mutable. Cells with a single symbol are dropped: a
    cross-section of one has no cross-section to subtract.
    """
    cells: dict[tuple[date, str], list[EntryObservation]] = defaultdict(list)
    for o in observations:
        if o.forward_pct is not None:
            cells[(o.session, o.slot)].append(o)

    out: dict[int, float] = {}
    for members in cells.values():
        if len(members) < 2:
            continue
        mean = sum(o.forward_pct for o in members) / len(members)
        for o in members:
            out[id(o)] = o.forward_pct - mean
    return out


@dataclass(frozen=True)
class GroupDiff:
    """A difference of two means, with everything needed to disbelieve it."""

    label: str
    sound: Stat
    unsound: Stat
    #: Independent cells (session x slot) behind the comparison, not observation count.
    effective_n: int

    @property
    def diff(self) -> float:
        """SOUND minus UNSOUND. Positive means the check picked better outcomes."""
        return self.sound.mean - self.unsound.mean

    @property
    def stderr(self) -> float:
        return math.sqrt(self.sound.stderr ** 2 + self.unsound.stderr ** 2)

    @property
    def ci95(self) -> tuple[float, float]:
        half = 1.96 * self.stderr
        return self.diff - half, self.diff + half

    @property
    def effective_stderr(self) -> float:
        """Standard error rescaled to the independent-cell count.

        Same correction the swing experiment needed: 20 symbols sampled in one minute
        share that minute's market move, so they are one draw with 20 readings, not 20
        draws. Treating them as 20 understates the interval by ~sqrt(20).
        """
        n = self.sound.n + self.unsound.n
        if self.effective_n < 2 or n < 2:
            return float("inf")
        return self.stderr * math.sqrt(n / self.effective_n)

    @property
    def effective_ci95(self) -> tuple[float, float]:
        half = 1.96 * self.effective_stderr
        return self.diff - half, self.diff + half

    @property
    def mde(self) -> float:
        pooled_sd = math.sqrt((self.sound.sd ** 2 + self.unsound.sd ** 2) / 2)
        return minimum_detectable_effect(self.effective_n, pooled_sd)

    def verdict(self) -> str:
        """A plain reading. Refuses to call an underpowered null a null."""
        if min(self.sound.n, self.unsound.n) < 30:
            return (
                f"NO CLAIM POSSIBLE (SOUND n={self.sound.n}, "
                f"UNSOUND n={self.unsound.n})."
            )
        low, high = self.effective_ci95
        if low <= 0.0 <= high:
            detectable = abs(self.diff) >= self.mde
            tail = (
                "The sample could have detected an effect this size, so this is a "
                "measured null, not an absent measurement."
                if detectable else
                f"The sample can only detect +/-{self.mde:.3f}pp, and the measured gap "
                f"is smaller than that -- this is NOT a measurement of zero, it is an "
                f"absence of measurement."
            )
            return (
                f"INDISTINGUISHABLE (diff {self.diff:+.4f}pp, 95% CI "
                f"[{low:+.4f}, {high:+.4f}] on {self.effective_n} independent cells). "
                f"{tail}"
            )
        direction = "SOUND outperforms" if self.diff > 0 else "SOUND UNDERPERFORMS"
        return (
            f"{direction} (diff {self.diff:+.4f}pp, 95% CI [{low:+.4f}, {high:+.4f}] "
            f"on {self.effective_n} independent cells)"
        )

    def as_row(self) -> dict:
        low, high = self.effective_ci95
        raw_low, raw_high = self.ci95
        return {
            "label": self.label,
            "sound_n": self.sound.n,
            "sound_mean": round(self.sound.mean, 4),
            "unsound_n": self.unsound.n,
            "unsound_mean": round(self.unsound.mean, 4),
            "diff": round(self.diff, 4),
            "ci95_low": round(low, 4),
            "ci95_high": round(high, 4),
            "naive_ci95_low": round(raw_low, 4),
            "naive_ci95_high": round(raw_high, 4),
            "effective_n": self.effective_n,
            "mde": round(self.mde, 4),
        }


def forward_comparison(
    observations: Sequence[EntryObservation],
    *,
    label: str,
    demeaned: bool = True,
) -> GroupDiff:
    """Forward return after SOUND versus after UNSOUND.

    ``demeaned=True`` (the default, and the one that answers the question) subtracts
    each session+slot cross-sectional mean first, so the comparison is about symbol
    selection rather than about which days the check happened to fire on.
    """
    excess = demean_by_slot(observations) if demeaned else {}
    sound_vals: list[float] = []
    unsound_vals: list[float] = []
    cells: set[tuple[date, str]] = set()

    for o in observations:
        if o.forward_pct is None:
            continue
        value = excess.get(id(o)) if demeaned else o.forward_pct
        if value is None:
            continue
        (sound_vals if o.valid else unsound_vals).append(value)
        cells.add((o.session, o.slot))

    return GroupDiff(
        label=label,
        sound=summarise(f"{label} / SOUND", sound_vals),
        unsound=summarise(f"{label} / UNSOUND", unsound_vals),
        effective_n=len(cells),
    )
