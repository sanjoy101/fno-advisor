"""F&O positioning features: open interest, basis, India VIX, rollover.

WHAT IS AND IS NOT COMPUTABLE FROM THIS DATA
--------------------------------------------

The feature request that motivated this module asked for OI change over 1, 5, 15 and
30 **minutes**. That does not exist and cannot be manufactured:

* Fyers' ``history`` endpoint returns six-field OHLCV and carries **no open interest at
  any resolution** (:mod:`src.data.bhavcopy`, verified during the F&O backfill).
* OI comes from the exchange's end-of-day derivatives bhavcopy, which is daily by
  construction.
* India VIX likewise comes from NSE's daily index-close file -- one OHLC row per
  session, no intraday (:mod:`src.data.india_vix`).
* Futures prices here are daily bhavcopy closes. There are no intraday futures bars, so
  an intraday basis cannot be formed either.

So every lookback in this module is in **trading days**, not minutes, and the module
says so rather than resampling a daily row into minutes and calling it intraday. A
1-minute OI change is not a hard feature to add later; it is a feature the data does
not contain.

THE THREE DISCONTINUITIES, AND WHY EACH ONE IS FATAL IF IGNORED
----------------------------------------------------------------

Three separate boundaries break a naive day-over-day change. All three are handled
explicitly and all three produce ``None``, never zero -- a zero would be read as "no
change", which is exactly the wrong conclusion.

**1. The contract roll.** OI does not carry across a roll: the new contract is a
different pool of positions. Measured on this data, RELIANCE's near-month OI moves
+292% on its 2024-10-31 roll. That is a change of instrument, not of positioning.

**2. Corporate actions.** NSE restates F&O lot size at a split or bonus, so OI counted
in units jumps by the action's factor on the ex-date. Measured here:

===============  ==========  ==================  =================
underlying       ex-date     near-month OI move  total OI move
===============  ==========  ==================  =================
RELIANCE 1:1     2024-10-28  +73.9%              +99.2%
WIPRO 1:1        2024-12-03  +88.9%              +90.2%
===============  ==========  ==================  =================

Neither is a position build. Any OI window spanning an ex-date is measuring the
corporate action, so :func:`oi_change_pct` invalidates it exactly as it invalidates a
roll.

**3. The price-scale mismatch between the two sources.** This one is silent and it
poisons the basis outright. Fyers serves spot history **split/bonus ADJUSTED**
(CLAUDE.md 7); the bhavcopy serves futures **RAW**. Before its bonus, RELIANCE's
futures close divided by its stored spot close is 2.007, not 1.003. A basis computed as
``futures - spot`` on those two numbers is not a mispriced carry, it is a 100% error
that would dwarf every real signal in the panel.

:func:`price_scale_factor` recovers the factor by snapping that ratio to the nearest
plausible corporate-action factor, and refuses (returns ``None``) when nothing fits.
This works only because the quantity being measured is small and the candidates are far
apart: a real basis is well under 5% while the candidate factors are at least 20%
apart, so the snap is unambiguous. That premise is asserted in the tests, not assumed.

**Price CHANGES are taken from the adjusted SPOT series, never from raw futures.** The
adjusted series has no corporate-action discontinuities -- a scan of 21 symbols over six
years found exactly one >20% step and it was the COVID crash. Futures are used for OI,
basis and rollover only.

DATA-AVAILABILITY BOUNDARY (CLAUDE.md 1.2)
-------------------------------------------

**Everything here is strictly ``trade_date < upto``.** The bhavcopy for session D and
the index-close file for session D both publish AFTER the close of D. For a decision
taken at any time during D the newest usable row is D-1. This is the same trap as the
daily bhavcopy in CLAUDE.md 1.2 and it is easy to get wrong, because a daily row looks
like a property of the day it is stamped with.

:func:`fno_series` is the single gate every feature routes through, and its strict
``<`` is the whole guarantee -- the same role :func:`src.signals.universe.trailing_sessions`
plays for the volume profile. That is also the leak class the bar-level poison harness
cannot see, so every feature here is covered by ``assert_session_causal`` in
``tests/test_fno_features.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence

__all__ = [
    "BASIS_PLAUSIBILITY_PCT",
    "CANDIDATE_FACTORS",
    "MAX_SCALE_RESIDUAL_PCT",
    "MIN_VIX_HISTORY",
    "median_basis_pct",
    "basis_is_plausible",
    "OI_STATE_LONG_BUILDUP",
    "OI_STATE_SHORT_BUILDUP",
    "OI_STATE_SHORT_COVERING",
    "OI_STATE_LONG_UNWINDING",
    "OI_STATES",
    "FnoDaily",
    "price_scale_factor",
    "build_fno_daily",
    "fno_series",
    "latest",
    "oi_change_pct",
    "oi_change_abs",
    "oi_price_state",
    "basis_pct",
    "basis_annualised_pct",
    "basis_roc_pp",
    "vix_level",
    "vix_change_pct",
    "vix_percentile",
    "rollover_ratio",
    "rollover_relative",
    "spot_return_pct",
]

#: Plausible cumulative split/bonus factors, used to snap the futures/spot price ratio
#: back onto one scale. Deliberately sparse: the closest pair is 20% apart, and a
#: genuine basis is under 5%, so the snap cannot be ambiguous. Asserted in the tests.
CANDIDATE_FACTORS: tuple[float, ...] = (
    1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0,
    10.0, 12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 100.0,
)

#: Largest residual, in percent, tolerated after snapping. Beyond this nothing
#: plausible fits and the day's price scale is reported as unknown rather than guessed.
MAX_SCALE_RESIDUAL_PCT = 5.0

#: Largest PERSISTENT basis, in percent of spot, that can be real carry on a near-month
#: single-stock future. At a ~6% annual risk-free rate fair monthly carry is about 0.5%;
#: this allows three times that. A symbol whose MEDIAN basis exceeds it over years is not
#: expensive to carry, it is on the wrong price scale -- see :func:`basis_is_plausible`.
BASIS_PLAUSIBILITY_PCT = 1.5

#: Trailing India VIX observations required before a percentile is reported. A
#: percentile against a handful of days is not a percentile. Panel history starts
#: 2020-01-01 and the calendar refuses years before 2020, so a strict 252-session
#: trailing year would blank the whole first year; 120 is a compromise that is stated
#: rather than hidden, and the feature is simply unavailable before it is met.
MIN_VIX_HISTORY = 120

#: Trailing India VIX window, in sessions. One year of trading days.
VIX_WINDOW = 252

OI_STATE_LONG_BUILDUP = "LONG_BUILDUP"        # price up,   OI up
OI_STATE_SHORT_BUILDUP = "SHORT_BUILDUP"      # price down, OI up
OI_STATE_SHORT_COVERING = "SHORT_COVERING"    # price up,   OI down
OI_STATE_LONG_UNWINDING = "LONG_UNWINDING"    # price down, OI down

OI_STATES: tuple[str, ...] = (
    OI_STATE_LONG_BUILDUP,
    OI_STATE_SHORT_BUILDUP,
    OI_STATE_SHORT_COVERING,
    OI_STATE_LONG_UNWINDING,
)


@dataclass(frozen=True)
class FnoDaily:
    """One underlying on one session: futures, OI and the price scale, normalised.

    ``near_close_adj`` is the near-month futures close divided by
    :attr:`scale_factor`, so it sits on the SAME scale as the stored (adjusted) spot
    close and the two can be differenced. ``near_close_raw`` is kept because a raw
    price is what actually traded and discarding it would make the adjustment
    unauditable.
    """

    trade_date: date
    underlying: str
    #: Near-month contract, chosen by the roll rule in :mod:`src.data.futures`.
    near_expiry: date
    near_close_raw: float
    near_close_adj: float
    near_oi: int
    #: Next listed expiry after the near month, when one is listed that day.
    next_expiry: date | None
    next_oi: int | None
    #: OI summed over EVERY listed futures expiry on the underlying. Unlike near-month
    #: OI this carries across a roll -- the pool of futures positions in the underlying
    #: is continuous even as individual contracts expire -- so it is the series the
    #: multi-day OI changes are built from. Futures only; stock options are a different
    #: instrument and pooling them would conflate two things.
    total_oi: int
    spot_close: float
    #: Snapped futures/spot price-scale factor. 1.0 when the two sources agree.
    scale_factor: float
    #: True on the first session of a new near-month contract.
    is_roll: bool
    #: True when ``scale_factor`` differs from the previous session's, i.e. a corporate
    #: action took effect. OI is not comparable across this boundary.
    is_scale_change: bool
    days_to_expiry: int


# --------------------------------------------------------------------------------
# Price scale
# --------------------------------------------------------------------------------


def price_scale_factor(
    futures_close: float,
    spot_close: float,
    *,
    candidates: Sequence[float] = CANDIDATE_FACTORS,
    max_residual_pct: float = MAX_SCALE_RESIDUAL_PCT,
) -> float | None:
    """Factor by which RAW futures must be divided to match ADJUSTED spot.

    The two sources disagree by a cumulative split/bonus factor and by the genuine
    basis. The factor is large and discrete; the basis is small and continuous. Snapping
    the ratio to the nearest candidate therefore recovers the factor and leaves the
    basis in the residual, which is precisely what is wanted.

    Returns None when no candidate fits within ``max_residual_pct``. That is a refusal,
    not a fallback to 1.0: a 1.0 there would silently report a 100% basis.

    Data-availability boundary:
        Pointwise on one session's two closing prices. Reads no other session, so it
        cannot leak; the caller is responsible for only passing it dates it may see.
    """
    if not (futures_close > 0 and spot_close > 0):
        return None
    ratio = futures_close / spot_close
    best = min(candidates, key=lambda f: abs(ratio / f - 1.0))
    return best if abs(ratio / best - 1.0) * 100.0 <= max_residual_pct else None


def median_basis_pct(records: Mapping[date, FnoDaily]) -> float | None:
    """Median basis across a symbol's whole stored series, in percent of spot.

    A DATA-QUALITY statistic, not a feature. It reads every stored session at once and
    must never be used at decision time; it answers "is this symbol's basis on a
    trustworthy scale at all", which is a property of the dataset in the same way
    TMCV having no futures is.
    """
    values = [100.0 * (r.near_close_adj - r.spot_close) / r.spot_close
              for r in records.values() if r.spot_close > 0]
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return (ordered[mid] if len(ordered) % 2
            else 0.5 * (ordered[mid - 1] + ordered[mid]))


def basis_is_plausible(
    records: Mapping[date, FnoDaily], *, bound_pct: float = BASIS_PLAUSIBILITY_PCT
) -> bool:
    """Can this symbol's basis be real carry, or is it a residual price-scale offset?

    :func:`price_scale_factor` snaps to simple rationals because splits and bonuses are
    simple rationals. **Demergers are not.** Their adjustment factor is a value ratio --
    an arbitrary number like 1.035 -- so it lands inside the snap's tolerance, gets
    absorbed into a factor of 1.0, and re-emerges as a constant fake premium that looks
    exactly like an expensive stock to carry.

    Measured in this panel, ADANIENT's futures/spot ratio is **1.0350 in every one of the
    twenty quarters from 2020Q1 to 2024Q4**, including 2023Q1 when the stock lost roughly
    seventy percent. A carry premium cannot hold flat through that; SBIN's sits at 1.002
    and moves. The tell is persistence, not size: a real basis mean-reverts to carry and
    collapses into every expiry, so it cannot have a median three times fair carry.

    Widening :data:`CANDIDATE_FACTORS` to catch 1.035 is the wrong fix -- it would
    destroy the 20% separation that makes the snap unambiguous in the first place, and
    then a genuine 3% basis would be silently snapped away. Refusing the feature for the
    affected symbol keeps both properties.
    """
    median = median_basis_pct(records)
    return median is not None and abs(median) <= bound_pct


# --------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------


def build_fno_daily(
    rows_by_date: Mapping[date, Sequence],
    underlying: str,
    spot_close_by_date: Mapping[date, float],
    *,
    roll_days_before: int | None = None,
) -> dict[date, FnoDaily]:
    """Assemble the per-session F&O record for one underlying.

    Data-availability boundary:
        NONE APPLIED -- this is the raw stored series, keyed by the session the data
        DESCRIBES, not the session it may be used on. Every consumer must go through
        :func:`fno_series`, whose strict ``<`` applies the publication lag. Building the
        map is a data-loading step; applying the boundary is a feature step, and keeping
        them apart is what lets ``assert_session_causal`` catch a feature that forgets.

    A session is skipped entirely when the near-month contract is absent, when spot is
    absent, or when the price scale cannot be resolved. Skipping is deliberate: a
    fabricated row in a price series is worse than a hole in it.
    """
    from src.data.futures import ROLL_DAYS_BEFORE_EXPIRY, active_expiry, expiry_calendar

    roll_days = ROLL_DAYS_BEFORE_EXPIRY if roll_days_before is None else roll_days_before
    symbol = underlying.upper()
    calendar = expiry_calendar(rows_by_date).get(symbol, [])
    if not calendar:
        return {}

    out: dict[date, FnoDaily] = {}
    previous_expiry: date | None = None
    previous_factor: float | None = None

    for day in sorted(rows_by_date):
        mine = [r for r in rows_by_date[day]
                if getattr(r, "is_stock_future", False) and r.symbol == symbol]
        if not mine:
            continue
        near_expiry = active_expiry(day, calendar, roll_days)
        if near_expiry is None:
            continue
        near = next((r for r in mine if r.expiry == near_expiry), None)
        spot = spot_close_by_date.get(day)
        if near is None or spot is None or spot <= 0:
            continue

        factor = price_scale_factor(near.close, spot)
        if factor is None:
            continue

        later = sorted(r for r in {row.expiry for row in mine} if r > near_expiry)
        next_expiry = later[0] if later else None
        next_row = (next((r for r in mine if r.expiry == next_expiry), None)
                    if next_expiry else None)

        out[day] = FnoDaily(
            trade_date=day,
            underlying=symbol,
            near_expiry=near_expiry,
            near_close_raw=near.close,
            near_close_adj=near.close / factor,
            near_oi=near.open_interest,
            next_expiry=next_expiry,
            next_oi=next_row.open_interest if next_row else None,
            total_oi=sum(r.open_interest for r in mine),
            spot_close=spot,
            scale_factor=factor,
            is_roll=previous_expiry is not None and near_expiry != previous_expiry,
            is_scale_change=previous_factor is not None and factor != previous_factor,
            days_to_expiry=(near_expiry - day).days,
        )
        previous_expiry, previous_factor = near_expiry, factor

    return out


# --------------------------------------------------------------------------------
# The causality gate
# --------------------------------------------------------------------------------


def fno_series(
    records: Mapping[date, FnoDaily],
    sessions: Sequence[date],
    upto: date,
) -> list[FnoDaily]:
    """Records for sessions STRICTLY BEFORE ``upto``, ascending. The causality gate.

    Data-availability boundary:
        Strictly ``trade_date < upto``. The F&O bhavcopy for session D publishes after
        the close of D, so D's own row is not available to a decision taken during D --
        including one taken at 15:15, thirteen minutes before the close that produces
        it.

    Every feature in this module goes through here, and none of them is ever handed a
    pre-filtered list. A function given clean input cannot be caught leaking, which is
    the whole reason ``assert_session_causal`` is able to prove anything about these.
    """
    return [records[d] for d in sorted(sessions) if d < upto and d in records]


def latest(records, sessions, upto) -> FnoDaily | None:
    """Most recent usable record, i.e. normally the D-1 row."""
    series = fno_series(records, sessions, upto)
    return series[-1] if series else None


def _spans_discontinuity(window: Sequence[FnoDaily], *, use_total: bool) -> bool:
    """Does an OI window cross a boundary that makes its endpoints incomparable?

    A corporate action rescales OI for every contract at once, so it invalidates both
    the near-month and the total series. A roll changes which contract the near-month
    series points at, so it invalidates that series only -- total OI is summed over all
    listed expiries and is continuous through a roll by construction.

    ``window[0]`` is the earlier endpoint and its own flags describe a boundary that
    fell BEFORE the window opened, so they are not consulted.
    """
    tail = window[1:]
    if any(r.is_scale_change for r in tail):
        return True
    return (not use_total) and any(r.is_roll for r in tail)


def _oi_of(record: FnoDaily, *, use_total: bool) -> int:
    return record.total_oi if use_total else record.near_oi


def oi_change_abs(
    records, sessions, upto, *, lookback: int = 1, use_total: bool = False
) -> int | None:
    """Absolute open-interest change over ``lookback`` TRADING DAYS, or None.

    Data-availability boundary:
        Via :func:`fno_series`, strictly ``trade_date < upto``. With ``lookback=1``
        this is the D-2 to D-1 change, the newest one a decision on D can see.

    Returns None -- never zero -- when the window is short or crosses a roll or a
    corporate action. See :func:`_spans_discontinuity`; zero would read as "no change",
    which is the opposite of "not comparable".

    ``use_total`` selects the series: total OI across every listed futures expiry
    carries through rolls, near-month OI does not. Multi-day lookbacks are only
    meaningful on the total series, because a monthly contract does not live long
    enough to support a 15- or 30-day window inside one roll.
    """
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    series = fno_series(records, sessions, upto)
    if len(series) < lookback + 1:
        return None
    window = series[-(lookback + 1):]
    if _spans_discontinuity(window, use_total=use_total):
        return None
    return _oi_of(window[-1], use_total=use_total) - _oi_of(window[0], use_total=use_total)


def oi_change_pct(
    records, sessions, upto, *, lookback: int = 1, use_total: bool = False
) -> float | None:
    """Open-interest change over ``lookback`` trading days, as a percent.

    Data-availability boundary:
        Identical to :func:`oi_change_abs`, which it delegates the window and every
        discontinuity check to.
    """
    series = fno_series(records, sessions, upto)
    if len(series) < lookback + 1:
        return None
    base = _oi_of(series[-(lookback + 1)], use_total=use_total)
    change = oi_change_abs(records, sessions, upto,
                           lookback=lookback, use_total=use_total)
    if change is None or base <= 0:
        return None
    return 100.0 * change / base


def spot_return_pct(records, sessions, upto, *, lookback: int = 1) -> float | None:
    """Spot return over ``lookback`` trading days, as a percent.

    Data-availability boundary:
        Via :func:`fno_series`, strictly ``trade_date < upto``.

    Taken from the ADJUSTED spot series rather than from raw futures on purpose. A raw
    futures return spanning a split or bonus ex-date is a corporate action, not a market
    move; the adjusted spot series carries no such discontinuity. This is the price leg
    of every OI-divergence classification below.
    """
    series = fno_series(records, sessions, upto)
    if len(series) < lookback + 1:
        return None
    then, now = series[-(lookback + 1)].spot_close, series[-1].spot_close
    return None if then <= 0 else 100.0 * (now - then) / then


def oi_price_state(
    records, sessions, upto, *, lookback: int = 1, use_total: bool = False
) -> str | None:
    """Classify OI against price into the four F&O market-structure states.

    ==================  ==========  =========  ==================================
    state               price       OI         reading
    ==================  ==========  =========  ==================================
    LONG_BUILDUP        up          up         new longs entering
    SHORT_BUILDUP       down        up         new shorts entering
    SHORT_COVERING      up          down       shorts closing, not new demand
    LONG_UNWINDING      down        down       longs closing, not new supply
    ==================  ==========  =========  ==================================

    Data-availability boundary:
        Both legs come through :func:`fno_series`, strictly ``trade_date < upto``.

    None when either leg is unavailable, which includes every window crossing a roll or
    a corporate action, and when either leg is exactly flat -- an unchanged price or an
    unchanged OI does not belong in a four-way classification, and forcing it into one
    would invent a reading the data does not support.
    """
    price = spot_return_pct(records, sessions, upto, lookback=lookback)
    oi = oi_change_pct(records, sessions, upto, lookback=lookback, use_total=use_total)
    if price is None or oi is None or price == 0.0 or oi == 0.0:
        return None
    if price > 0:
        return OI_STATE_LONG_BUILDUP if oi > 0 else OI_STATE_SHORT_COVERING
    return OI_STATE_SHORT_BUILDUP if oi > 0 else OI_STATE_LONG_UNWINDING


# --------------------------------------------------------------------------------
# Basis
# --------------------------------------------------------------------------------


def basis_pct(records, sessions, upto) -> float | None:
    """Near-month futures premium over spot, as a percent of spot.

    Data-availability boundary:
        Via :func:`fno_series`, strictly ``trade_date < upto``. Both legs are the D-1
        session's closes, taken from the SAME session on purpose: pairing a D-1 futures
        close with a D spot price would put a day of spot drift into the basis, which at
        a typical basis of ~0.3% would swamp it.

    Both legs are on the adjusted scale -- see :func:`price_scale_factor` for why the
    raw futures close cannot be differenced against the stored spot close directly.
    """
    row = latest(records, sessions, upto)
    if row is None or row.spot_close <= 0:
        return None
    return 100.0 * (row.near_close_adj - row.spot_close) / row.spot_close


def basis_annualised_pct(records, sessions, upto, *, min_days: int = 2) -> float | None:
    """Basis expressed as an annualised carry rate.

    The raw basis shrinks mechanically to zero as a contract approaches expiry, so
    comparing one symbol six days from expiry against another twenty-six days out
    compares two different quantities. Dividing by the time to expiry removes that
    sawtooth and leaves a rate that is comparable across symbols and across the cycle.

    Data-availability boundary:
        Via :func:`basis_pct` and therefore :func:`fno_series`, strictly
        ``trade_date < upto``. ``days_to_expiry`` is a property of the contract known
        when it was listed, so it reveals nothing about the future.

    None inside ``min_days`` of expiry, where the division amplifies noise without
    limit rather than measuring carry.
    """
    row = latest(records, sessions, upto)
    raw = basis_pct(records, sessions, upto)
    if row is None or raw is None or row.days_to_expiry < min_days:
        return None
    return raw * 365.0 / row.days_to_expiry


def basis_roc_pp(records, sessions, upto) -> float | None:
    """One-day change in the basis, in percentage points.

    Data-availability boundary:
        Via :func:`fno_series`, strictly ``trade_date < upto``. Uses the two most
        recent usable sessions, normally D-2 and D-1.

    None across a roll: the two readings would belong to different contracts with
    different times to expiry, so their difference is a change of instrument. None
    across a corporate action for the same reason the OI changes are -- the basis is
    computed from a rescaled price and the scale changed underneath it.
    """
    series = fno_series(records, sessions, upto)
    if len(series) < 2:
        return None
    window = series[-2:]
    if window[-1].is_roll or window[-1].is_scale_change:
        return None
    out = []
    for row in window:
        if row.spot_close <= 0:
            return None
        out.append(100.0 * (row.near_close_adj - row.spot_close) / row.spot_close)
    return out[-1] - out[0]


# --------------------------------------------------------------------------------
# India VIX -- market-wide, identical for every symbol on a given date
# --------------------------------------------------------------------------------


def vix_level(vix_by_date: Mapping[date, float], sessions, upto) -> float | None:
    """India VIX close on the most recent session before ``upto``.

    Data-availability boundary:
        Strictly ``trade_date < upto``. NSE's index-close file for session D publishes
        after the close of D, so D's own VIX is not available to a decision during D.
    """
    usable = [d for d in sessions if d < upto and d in vix_by_date]
    return vix_by_date[max(usable)] if usable else None


def vix_change_pct(vix_by_date, sessions, upto) -> float | None:
    """One-session percent change in India VIX.

    Data-availability boundary:
        Strictly ``trade_date < upto``; uses the two most recent usable sessions.
    """
    usable = sorted(d for d in sessions if d < upto and d in vix_by_date)
    if len(usable) < 2:
        return None
    then, now = vix_by_date[usable[-2]], vix_by_date[usable[-1]]
    return None if then <= 0 else 100.0 * (now - then) / then


def vix_percentile(
    vix_by_date, sessions, upto, *,
    window: int = VIX_WINDOW, min_history: int = MIN_VIX_HISTORY,
) -> float | None:
    """Where the latest India VIX sits in its trailing year, as a fraction in [0, 1].

    Data-availability boundary:
        Strictly ``trade_date < upto`` for BOTH the observation and every member of the
        window it is ranked against. The trailing window is the ``window`` sessions
        before ``upto``, which includes the observation itself -- it is being placed
        within its own recent history, and excluding it would rank a value against a
        distribution it is not part of.

    None below ``min_history`` observations. A percentile against a handful of days is
    not a percentile, and the alternative -- reporting one anyway -- would be a
    confident number built from nothing.
    """
    usable = sorted(d for d in sessions if d < upto and d in vix_by_date)
    if len(usable) < min_history:
        return None
    values = [vix_by_date[d] for d in usable[-window:]]
    current = values[-1]
    return sum(1 for v in values if v <= current) / len(values)


# --------------------------------------------------------------------------------
# Rollover
# --------------------------------------------------------------------------------


def rollover_ratio(records, sessions, upto) -> float | None:
    """Share of near+next futures OI sitting in the NEXT month, in [0, 1].

    The standard rollover measure: as expiry approaches, positions that intend to stay
    on migrate to the following contract, and the fraction that has moved is what
    "rollover percent" names.

    Data-availability boundary:
        Via :func:`fno_series`, strictly ``trade_date < upto``. Both legs are the same
        D-1 session, so no discontinuity can fall between them -- and because it is a
        RATIO of two OI figures rescaled by the same corporate-action factor, it is
        immune to the lot-size jump that invalidates the OI changes. That immunity is
        the reason this one feature needs no scale-change guard.

    None when no next contract is listed.
    """
    row = latest(records, sessions, upto)
    if row is None or row.next_oi is None:
        return None
    denom = row.near_oi + row.next_oi
    return None if denom <= 0 else row.next_oi / denom


def rollover_relative(
    records, sessions, upto, *, cycles: int = 6, tolerance_days: int = 1
) -> float | None:
    """Rollover ratio against its own normal level at the SAME distance from expiry.

    The raw ratio rises to 1.0 as expiry approaches whatever anyone believes, so its
    level says almost nothing on its own -- comparing a symbol four days out against one
    eighteen days out compares two points on a mechanical curve. What carries
    information is being ABOVE or BELOW the level normally seen at this distance.

    The baseline is the mean ratio at the same ``days_to_expiry`` (within
    ``tolerance_days``) over the previous ``cycles`` expiry cycles.

    Data-availability boundary:
        Via :func:`fno_series`, strictly ``trade_date < upto``, for the observation AND
        for every prior cycle in the baseline. This is precisely the cross-session route
        the volume baseline leaked through -- the bar-level poison harness cannot see
        it, because every session in the baseline is genuinely complete and only the
        LIST OF DATES is wrong. ``assert_session_causal`` is what covers it.

    Returns None rather than a partial baseline below three prior observations.
    """
    series = fno_series(records, sessions, upto)
    if not series:
        return None
    row = series[-1]
    if row.next_oi is None:
        return None
    denom = row.near_oi + row.next_oi
    if denom <= 0:
        return None
    current = row.next_oi / denom

    prior: list[float] = []
    for past in reversed(series[:-1]):
        if past.near_expiry == row.near_expiry:
            continue  # same contract cycle: not an independent prior observation
        if abs(past.days_to_expiry - row.days_to_expiry) > tolerance_days:
            continue
        if past.next_oi is None:
            continue
        d = past.near_oi + past.next_oi
        if d > 0:
            prior.append(past.next_oi / d)
        if len(prior) >= cycles:
            break

    if len(prior) < 3:
        return None
    return current - sum(prior) / len(prior)
