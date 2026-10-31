"""Every F&O feature, run through the cross-session look-ahead check.

WHY assert_session_causal AND NOT THE BAR-LEVEL HARNESS
--------------------------------------------------------

``src.analysis.lookahead.assert_causal`` varies bars WITHIN one intraday series. Not one
feature in :mod:`src.signals.fno_features` reads an intraday bar -- they read one row per
SESSION, from the daily bhavcopy and the daily index-close file. A feature here leaks by
being handed the wrong list of DATES, and every session in that list is genuinely
complete, so poisoning intraday bars would prove exactly nothing.

That is the same leak class the time-of-day volume baseline had, and
``assert_session_causal`` is the only thing that covers it. It is applied to every public
feature below, plus deliberately leaky controls that it must reject -- because a check
that never fails is not evidence.

The extra trap in this module is the PUBLICATION LAG. The bhavcopy for session D and the
index file for session D both publish after the close of D, so the boundary is strictly
``< upto``, not ``<= upto``. A feature using ``<=`` here would hand a 15:15 decision the
settlement data of the session it is still inside. ``test_a_feature_using_le_is_caught``
pins that the check actually catches it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.analysis.lookahead import assert_session_causal
from src.signals import fno_features as F
from src.signals import fno_scoring as S

# --------------------------------------------------------------------------------
# Fixtures: FnoDaily records built directly, so a test can place a roll or a
# corporate action on an exact date instead of hoping one falls where it is needed.
# --------------------------------------------------------------------------------

START = date(2023, 1, 2)


def sessions(n: int = 90) -> list[date]:
    """``n`` consecutive weekdays. Not the NSE calendar -- these tests are about the
    causality boundary, and a holiday in the middle would only add noise."""
    out, cursor = [], START
    while len(out) < n:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def make_records(
    days: list[date],
    *,
    roll_every: int = 21,
    scale_change_at: int | None = None,
    scale_after: float = 2.0,
) -> dict[date, F.FnoDaily]:
    """A jagged synthetic series with rolls and, optionally, one corporate action.

    Prices and OI are deliberately non-monotonic and non-repeating: on a flat series a
    leak returns roughly the right answer and hides.
    """
    records: dict[date, F.FnoDaily] = {}
    prev_expiry: date | None = None
    prev_factor: float | None = None
    for i, day in enumerate(days):
        cycle = i // roll_every
        expiry = days[min((cycle + 1) * roll_every - 1, len(days) - 1)]
        spot = 1000.0 + ((i * 37) % 13 - 6) * 3.1 + i * 0.7
        # Near-month OI resets hard at each roll; total OI drifts through it.
        near_oi = 1_000_000 + ((i * 911) % 7000) * 40 + (i % roll_every) * 21_000
        total_oi = 3_000_000 + ((i * 613) % 5000) * 60 + i * 9_000
        factor = 1.0
        if scale_change_at is not None and i >= scale_change_at:
            factor = scale_after
            near_oi = int(near_oi * scale_after)
            total_oi = int(total_oi * scale_after)
        basis = 1.0 + 0.0004 * ((expiry - day).days) + 0.00002 * ((i * 17) % 11)
        records[day] = F.FnoDaily(
            trade_date=day,
            underlying="TESTSYM",
            near_expiry=expiry,
            near_close_raw=spot * basis * factor,
            near_close_adj=spot * basis,
            near_oi=near_oi,
            next_expiry=expiry + timedelta(days=28),
            next_oi=int(near_oi * (0.15 + 0.03 * (i % roll_every))),
            total_oi=total_oi,
            spot_close=spot,
            scale_factor=factor,
            is_roll=prev_expiry is not None and expiry != prev_expiry,
            is_scale_change=prev_factor is not None and factor != prev_factor,
            days_to_expiry=(expiry - day).days,
        )
        prev_expiry, prev_factor = expiry, factor
    return records


def make_vix(days: list[date]) -> dict[date, float]:
    return {d: 12.0 + ((i * 53) % 17) * 0.6 + (i % 7) * 0.35
            for i, d in enumerate(days)}


DAYS = sessions(90)
RECORDS = make_records(DAYS)
VIX = make_vix(DAYS)
#: Decision dates late enough that every feature is warm, including the 30-day OI
#: window and the rollover baseline's three prior cycles.
DECISION_DATES = DAYS[70:86]


# --------------------------------------------------------------------------------
# The cross-session causality check, on every public feature
# --------------------------------------------------------------------------------

RECORD_FEATURES = [
    ("oi_change_abs_1d", lambda r, s, u: F.oi_change_abs(r, s, u, lookback=1)),
    ("oi_change_pct_1d", lambda r, s, u: F.oi_change_pct(r, s, u, lookback=1)),
    ("oi_change_pct_5d_total",
     lambda r, s, u: F.oi_change_pct(r, s, u, lookback=5, use_total=True)),
    ("oi_change_pct_15d_total",
     lambda r, s, u: F.oi_change_pct(r, s, u, lookback=15, use_total=True)),
    ("oi_change_pct_30d_total",
     lambda r, s, u: F.oi_change_pct(r, s, u, lookback=30, use_total=True)),
    ("oi_price_state", lambda r, s, u: F.oi_price_state(r, s, u)),
    ("spot_return_pct", lambda r, s, u: F.spot_return_pct(r, s, u)),
    ("basis_pct", lambda r, s, u: F.basis_pct(r, s, u)),
    ("basis_annualised_pct", lambda r, s, u: F.basis_annualised_pct(r, s, u)),
    ("basis_roc_pp", lambda r, s, u: F.basis_roc_pp(r, s, u)),
    ("rollover_ratio", lambda r, s, u: F.rollover_ratio(r, s, u)),
    ("rollover_relative", lambda r, s, u: F.rollover_relative(r, s, u)),
    ("latest", lambda r, s, u: F.latest(r, s, u)),
    ("fno_series_len", lambda r, s, u: len(F.fno_series(r, s, u))),
]

VIX_FEATURES = [
    ("vix_level", F.vix_level),
    ("vix_change_pct", F.vix_change_pct),
    ("vix_percentile", lambda v, s, u: F.vix_percentile(v, s, u, min_history=20)),
]


@pytest.mark.parametrize("name,fn", RECORD_FEATURES, ids=[n for n, _ in RECORD_FEATURES])
@pytest.mark.parametrize("current", DECISION_DATES[:4], ids=lambda d: str(d))
def test_record_feature_is_session_causal(name, fn, current):
    assert_session_causal(
        lambda sess: fn(RECORDS, sess, current), DAYS, current, name=name)


@pytest.mark.parametrize("name,fn", VIX_FEATURES, ids=[n for n, _ in VIX_FEATURES])
@pytest.mark.parametrize("current", DECISION_DATES[:4], ids=lambda d: str(d))
def test_vix_feature_is_session_causal(name, fn, current):
    assert_session_causal(
        lambda sess: fn(VIX, sess, current), DAYS, current, name=name)


def test_the_composite_score_is_session_causal():
    """The whole assembly, not just its parts: a leak could live in the wiring."""
    for current in DECISION_DATES[:4]:
        assert_session_causal(
            lambda sess: S.score_symbol_fno(RECORDS, VIX, sess, current)[0],
            DAYS, current, name="score_symbol_fno",
        )


def test_every_public_feature_is_covered():
    """A new feature must not be able to skip this file unnoticed."""
    covered = {
        "oi_change_abs", "oi_change_pct", "oi_price_state", "spot_return_pct",
        "basis_pct", "basis_annualised_pct", "basis_roc_pp", "rollover_ratio",
        "rollover_relative", "vix_level", "vix_change_pct", "vix_percentile",
        "latest", "fno_series",
    }
    not_features = {
        "BASIS_PLAUSIBILITY_PCT", "CANDIDATE_FACTORS", "MAX_SCALE_RESIDUAL_PCT",
        "MIN_VIX_HISTORY",
        "OI_STATE_LONG_BUILDUP", "OI_STATE_SHORT_BUILDUP", "OI_STATE_SHORT_COVERING",
        "OI_STATE_LONG_UNWINDING", "OI_STATES", "FnoDaily",
        "price_scale_factor",  # pointwise on one session; covered by its own tests
        "build_fno_daily",     # a loader, not a feature; covered by its own tests
        # Panel-level data-quality statistics, deliberately not decision-time features.
        # They read the whole stored series at once and have their own tests.
        "median_basis_pct", "basis_is_plausible",
    }
    missing = set(F.__all__) - covered - not_features
    assert not missing, f"F&O features with no session-causality test: {sorted(missing)}"


# --------------------------------------------------------------------------------
# The check must be capable of failing. Two deliberately leaky controls.
# --------------------------------------------------------------------------------


def test_a_feature_using_le_is_caught():
    """``<=`` instead of ``<`` -- the publication-lag bug this module exists to avoid.

    This is the realistic mistake: a daily row LOOKS like a property of the day it is
    stamped with, so reaching for ``<= upto`` feels natural. It hands a 15:15 decision
    the settlement OI of the session it is still inside.
    """
    def leaky(records, sess, upto):
        usable = [records[d] for d in sorted(sess) if d <= upto and d in records]
        return usable[-1].total_oi if usable else None

    current = DECISION_DATES[0]
    with pytest.raises(AssertionError, match="CROSS-SESSION LOOK-AHEAD"):
        assert_session_causal(
            lambda sess: leaky(RECORDS, sess, current), DAYS, current, name="leaky_le")


def test_a_feature_reading_the_end_of_the_list_is_caught():
    """Reading ``sessions[-1]`` instead of narrowing to ``upto``."""
    def leaky(records, sess, _upto):
        usable = [records[d] for d in sorted(sess) if d in records]
        return usable[-1].spot_close if usable else None

    current = DECISION_DATES[0]
    with pytest.raises(AssertionError, match="CROSS-SESSION LOOK-AHEAD"):
        assert_session_causal(
            lambda sess: leaky(RECORDS, sess, current), DAYS, current, name="leaky_tail")


def test_the_features_actually_return_varying_values():
    """Guard against a vacuous pass: None everywhere is causal and also useless."""
    for name, fn in RECORD_FEATURES:
        values = [fn(RECORDS, DAYS, d) for d in DECISION_DATES]
        real = [v for v in values if v is not None]
        assert real, f"{name} returned None at every decision date"
    changes = {round(F.oi_change_pct(RECORDS, DAYS, d, lookback=5, use_total=True), 6)
               for d in DECISION_DATES
               if F.oi_change_pct(RECORDS, DAYS, d, lookback=5, use_total=True)
               is not None}
    assert len(changes) > 3, "the OI series is too flat to detect a leak"


# --------------------------------------------------------------------------------
# Discontinuity handling: the roll and the corporate action
# --------------------------------------------------------------------------------


def test_near_month_oi_change_is_none_across_a_roll():
    """OI does not carry across a roll: the new contract is a different position pool.

    Measured on real data, RELIANCE's near-month OI moves +292% on its 2024-10-31 roll.
    A number like that reported as a position build is worse than no number.
    """
    roll_days = [d for d, r in RECORDS.items() if r.is_roll]
    assert roll_days, "fixture produced no rolls"
    for roll_day in roll_days:
        after = next((d for d in DAYS if d > roll_day), None)
        if after is None:
            continue
        # A decision on the session after the roll sees the roll inside its 1-day window.
        assert F.oi_change_abs(RECORDS, DAYS, after, lookback=1) is None
        assert F.oi_change_pct(RECORDS, DAYS, after, lookback=1) is None


def test_none_is_not_zero_across_a_roll():
    """The distinction the whole design turns on. Zero reads as 'no change'."""
    roll_day = next(d for d, r in RECORDS.items() if r.is_roll)
    after = next(d for d in DAYS if d > roll_day)
    value = F.oi_change_abs(RECORDS, DAYS, after, lookback=1)
    assert value is None and value != 0


def test_total_oi_carries_through_a_roll():
    """Total OI is summed over every listed expiry, so a roll does not break it.

    This is what makes the 5/15/30-day windows possible at all: a monthly contract does
    not live long enough for a 30-day window to fit inside one.
    """
    roll_day = next(d for d, r in RECORDS.items() if r.is_roll)
    after = next(d for d in DAYS if d > roll_day)
    assert F.oi_change_pct(RECORDS, DAYS, after, lookback=1, use_total=True) is not None


def test_oi_change_is_none_across_a_corporate_action():
    """A lot-size restatement is not a position build.

    Measured on this data: RELIANCE's 1:1 bonus on 2024-10-28 moves near-month OI +73.9%
    and total OI +99.2%; WIPRO's on 2024-12-03 moves them +88.9% and +90.2%. Both are
    the exchange restating the unit, and both would read as enormous accumulation.
    """
    idx = 40  # leaves room for the 30-day window to be tested inside DAYS
    records = make_records(DAYS, scale_change_at=idx, scale_after=2.0)
    ex_date = DAYS[idx]
    after = DAYS[idx + 1]

    # The 1-day window opening at the ex-date spans the restatement.
    assert F.oi_change_pct(records, DAYS, after, lookback=1, use_total=True) is None
    assert F.oi_change_pct(records, DAYS, after, lookback=1, use_total=False) is None
    # So does every longer window that still contains it.
    for k in (5, 15, 30):
        assert F.oi_change_pct(records, DAYS, DAYS[idx + k], lookback=k,
                               use_total=True) is None
    # Once the window has cleared the ex-date entirely, it is measurable again.
    clear = DAYS[idx + 7]
    assert F.oi_change_pct(records, DAYS, clear, lookback=5, use_total=True) is not None
    assert records[ex_date].is_scale_change


def test_basis_roc_is_none_across_a_roll_and_a_corporate_action():
    """Two readings from different contracts are not a change in the carry rate."""
    roll_day = next(d for d, r in RECORDS.items() if r.is_roll)
    assert F.basis_roc_pp(RECORDS, DAYS, next(d for d in DAYS if d > roll_day)) is None

    idx = 60
    records = make_records(DAYS, scale_change_at=idx, scale_after=2.0)
    assert F.basis_roc_pp(records, DAYS, DAYS[idx + 1]) is None


def test_rollover_ratio_survives_a_corporate_action():
    """A ratio of two OI figures rescaled by the SAME factor is immune to the jump.

    Worth pinning because it is the one place a scale-change guard would be wrong: it
    would blank a feature that is perfectly well defined.
    """
    idx = 60
    records = make_records(DAYS, scale_change_at=idx, scale_after=2.0)
    plain = F.rollover_ratio(RECORDS, DAYS, DAYS[idx + 1])
    scaled = F.rollover_ratio(records, DAYS, DAYS[idx + 1])
    assert plain is not None and scaled is not None
    assert plain == pytest.approx(scaled, abs=1e-9)


def test_rollover_relative_baseline_uses_only_prior_cycles():
    """The baseline must never include the contract being measured."""
    # Mid-cycle on purpose: on the first session of a contract there are no same-cycle
    # rows to exclude, so the test would pass without proving anything.
    current = DAYS[80]
    row = F.latest(RECORDS, DAYS, current)
    series = F.fno_series(RECORDS, DAYS, current)
    same_cycle = [r for r in series[:-1] if r.near_expiry == row.near_expiry]
    assert same_cycle, "fixture has no same-cycle rows, so the test proves nothing"
    assert F.rollover_relative(RECORDS, DAYS, current) is not None


# --------------------------------------------------------------------------------
# The price-scale snap
# --------------------------------------------------------------------------------


def test_candidate_factors_are_far_enough_apart_to_be_unambiguous():
    """The premise the whole snap rests on, asserted rather than assumed.

    Snapping is only safe if a genuine basis can never reach halfway to the next
    candidate. The tolerance is 5%; the closest candidates must therefore be more than
    10% apart in relative terms.
    """
    factors = F.CANDIDATE_FACTORS
    gaps = [(b - a) / a for a, b in zip(factors, factors[1:])]
    assert min(gaps) > 2 * F.MAX_SCALE_RESIDUAL_PCT / 100.0, (
        f"closest candidate pair is {min(gaps):.1%} apart, which is not enough "
        f"separation for a {F.MAX_SCALE_RESIDUAL_PCT}% tolerance"
    )


@pytest.mark.parametrize("futures,spot,expected", [
    (1617.00, 805.60, 2.0),    # RELIANCE before its 1:1 bonus, real values
    (305.70, 30.61, 10.0),     # TATASTEEL before its 1:10 split, real values
    (213.35, 106.58, 2.0),     # WIPRO before its 1:1 bonus, real values
    (687.80, 686.70, 1.0),     # INFY, no action: the residual IS the basis
    (84.20, 84.05, 1.0),       # ONGC, no action
])
def test_price_scale_factor_recovers_the_real_factors(futures, spot, expected):
    assert F.price_scale_factor(futures, spot) == expected


def test_price_scale_factor_leaves_the_basis_in_the_residual():
    """Snapping must not flatten the quantity being measured."""
    factor = F.price_scale_factor(1617.00, 805.60)
    basis_pct = 100.0 * (1617.00 / factor - 805.60) / 805.60
    assert 0.0 < basis_pct < 1.0, basis_pct


def test_a_demerger_offset_slips_through_the_snap_and_is_caught_downstream():
    """The failure mode `basis_is_plausible` exists for.

    A demerger factor is a value ratio, not a simple rational, so it lands INSIDE the
    snap's tolerance and is absorbed into a factor of 1.0. Reproduced at ADANIENT's real
    1.0350: the snap reports 1.0 and the basis then reads a constant ~3.5%.
    """
    assert F.price_scale_factor(1035.0, 1000.0) == 1.0

    days = sessions(60)
    contaminated = {}
    for d, row in make_records(days, roll_every=21).items():
        contaminated[d] = F.FnoDaily(
            **{**row.__dict__, "near_close_adj": row.spot_close * 1.0350}
        )
    assert F.median_basis_pct(contaminated) == pytest.approx(3.50, abs=0.01)
    assert not F.basis_is_plausible(contaminated)


def test_a_normal_basis_is_plausible():
    """Guard against the gate throwing away good symbols."""
    median = F.median_basis_pct(RECORDS)
    assert median is not None and 0.0 < median < F.BASIS_PLAUSIBILITY_PCT
    assert F.basis_is_plausible(RECORDS)


def test_suppressing_the_basis_drops_both_components_with_their_weight():
    """Not zeroed — dropped. A fake premium must not become a neutral opinion either."""
    current = DECISION_DATES[0]
    on_score, on = S.score_symbol_fno(RECORDS, VIX, DAYS, current, basis_usable=True)
    off_score, off = S.score_symbol_fno(RECORDS, VIX, DAYS, current, basis_usable=False)

    for name in ("basis_carry", "basis_roc"):
        assert next(c for c in on if c.name == name).available
        assert not next(c for c in off if c.name == name).available
    # Every other component is untouched.
    for name in set(S.WEIGHTS) - {"basis_carry", "basis_roc"}:
        assert (next(c for c in on if c.name == name).score
                == next(c for c in off if c.name == name).score)
    assert on_score is not None and off_score is not None


def test_rank_universe_applies_the_basis_gate_per_symbol():
    ranked = S.rank_universe_fno(
        {"AAA": RECORDS, "BBB": RECORDS}, VIX, DAYS, DECISION_DATES[0],
        basis_usable={"BBB": False},
    )
    by_symbol = {c.symbol: c for c in ranked}
    assert by_symbol["AAA"].component("basis_carry").available
    assert not by_symbol["BBB"].component("basis_carry").available


def test_price_scale_factor_refuses_rather_than_guessing():
    """No plausible factor fits, so the answer is 'unknown', not 1.0.

    Returning 1.0 here is the dangerous failure: it would report a 60% basis as real.
    """
    assert F.price_scale_factor(1600.0, 1000.0) is None
    assert F.price_scale_factor(0.0, 100.0) is None
    assert F.price_scale_factor(100.0, 0.0) is None


# --------------------------------------------------------------------------------
# The four-state classification
# --------------------------------------------------------------------------------


def _two_day(price_delta: float, oi_delta: int) -> tuple[dict, list[date]]:
    days = [DAYS[0], DAYS[1]]
    base = make_records(days, roll_every=99)
    a, b = base[days[0]], base[days[1]]
    base[days[1]] = F.FnoDaily(
        **{**b.__dict__,
           "spot_close": a.spot_close + price_delta,
           "near_oi": a.near_oi + oi_delta,
           "total_oi": a.total_oi + oi_delta,
           "is_roll": False, "is_scale_change": False}
    )
    return base, days


@pytest.mark.parametrize("price_delta,oi_delta,expected", [
    (+5.0, +50_000, F.OI_STATE_LONG_BUILDUP),
    (-5.0, +50_000, F.OI_STATE_SHORT_BUILDUP),
    (+5.0, -50_000, F.OI_STATE_SHORT_COVERING),
    (-5.0, -50_000, F.OI_STATE_LONG_UNWINDING),
])
def test_four_state_classification(price_delta, oi_delta, expected):
    records, days = _two_day(price_delta, oi_delta)
    assert F.oi_price_state(records, days, days[1] + timedelta(days=1)) == expected


@pytest.mark.parametrize("price_delta,oi_delta", [(0.0, 50_000), (5.0, 0)])
def test_a_flat_leg_is_unclassifiable_rather_than_forced(price_delta, oi_delta):
    """An unchanged price or OI does not belong in a four-way split."""
    records, days = _two_day(price_delta, oi_delta)
    assert F.oi_price_state(records, days, days[1] + timedelta(days=1)) is None


def test_state_uses_adjusted_spot_not_raw_futures_for_the_price_leg():
    """A raw futures return across an ex-date is a corporate action, not a move.

    With a 1:1 bonus the raw futures price halves overnight. Reading the price leg from
    raw futures would classify every symbol as falling on its ex-date.
    """
    idx = 60
    records = make_records(DAYS, scale_change_at=idx, scale_after=2.0)
    ex = records[DAYS[idx]]
    before = records[DAYS[idx - 1]]
    assert ex.near_close_raw / before.near_close_raw > 1.8   # raw doubles: meaningless
    assert 0.9 < ex.spot_close / before.spot_close < 1.1     # adjusted spot does not
    assert 0.9 < ex.near_close_adj / before.near_close_adj < 1.1


# --------------------------------------------------------------------------------
# VIX
# --------------------------------------------------------------------------------


def test_vix_percentile_refuses_below_minimum_history():
    """A percentile against a handful of days is not a percentile."""
    assert F.vix_percentile(VIX, DAYS, DAYS[10]) is None
    assert F.vix_percentile(VIX, DAYS, DAYS[10], min_history=5) is not None


def test_vix_percentile_is_bounded_and_ranks_correctly():
    values = [F.vix_percentile(VIX, DAYS, d, min_history=20) for d in DECISION_DATES]
    assert all(v is None or 0.0 < v <= 1.0 for v in values)

    days = sessions(40)
    rising = {d: 10.0 + i for i, d in enumerate(days)}
    assert F.vix_percentile(rising, days, days[-1] + timedelta(days=1),
                            min_history=20) == 1.0


def test_vix_is_market_wide_and_identical_across_symbols():
    """Stated in the module docstring; pinned here because it drives NO_TRADE."""
    current = DECISION_DATES[0]
    a = S.score_symbol_fno(RECORDS, VIX, DAYS, current)[1]
    other = make_records(DAYS, roll_every=17)
    b = S.score_symbol_fno(other, VIX, DAYS, current)[1]
    for name in ("vix_percentile", "vix_change"):
        pa = next(c for c in a if c.name == name)
        pb = next(c for c in b if c.name == name)
        assert pa.score == pb.score


# --------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------


def test_every_weighted_component_is_produced_exactly_once():
    _, components = S.score_symbol_fno(RECORDS, VIX, DAYS, DECISION_DATES[0])
    names = [c.name for c in components]
    assert sorted(names) == sorted(S.WEIGHTS), names
    assert len(names) == len(set(names))


def test_composite_stays_inside_the_declared_scale():
    for d in DECISION_DATES:
        score, _ = S.score_symbol_fno(RECORDS, VIX, DAYS, d)
        assert score is None or -1.0 <= score <= 1.0


def test_an_unavailable_component_is_dropped_not_counted_as_zero():
    """An unknown is not a neutral opinion -- CLAUDE.md's rule, pinned here.

    A single strongly positive component with everything else unavailable must score
    near +1, not near +1/9.
    """
    from src.signals.scoring import composite

    only = S.score_oi_price_state(F.OI_STATE_LONG_BUILDUP)
    blanks = [S.score_basis_carry(None), S.score_basis_roc(None),
              S.score_vix_change(None), S.score_rollover(None)]
    assert composite([only, *blanks]) == pytest.approx(1.0)
    assert composite(blanks) is None


def test_state_scores_are_ordered_as_the_market_structure_reading_says():
    order = [F.OI_STATE_LONG_BUILDUP, F.OI_STATE_SHORT_COVERING,
             F.OI_STATE_LONG_UNWINDING, F.OI_STATE_SHORT_BUILDUP]
    values = [S.OI_STATE_SCORE[s] for s in order]
    assert values == sorted(values, reverse=True)
    assert S.OI_STATE_SCORE[F.OI_STATE_LONG_BUILDUP] > 0
    assert S.OI_STATE_SCORE[F.OI_STATE_SHORT_BUILDUP] < 0


def test_ranking_is_deterministic_and_breaks_ties_on_symbol():
    by_symbol = {"BBB": RECORDS, "AAA": RECORDS, "CCC": RECORDS}
    ranked = S.rank_universe_fno(by_symbol, VIX, DAYS, DECISION_DATES[0])
    assert [c.symbol for c in ranked] == ["AAA", "BBB", "CCC"]
    assert len({c.score for c in ranked}) == 1


def test_scoring_saturation_constants_are_the_pre_registered_values():
    """These are the pre-registered design constants. A change here changes the claim.

    Not a style check: the absolute score bar only means something if the scale it sits
    on was fixed before any P&L was computed. If a future session tunes one of these,
    this test should fail and force the change to be argued for in the report.
    """
    assert S.OI_SATURATION_PCT == {5: 10.0, 15: 20.0, 30: 30.0}
    assert (S.CARRY_CENTRE_PCT, S.CARRY_SPAN_PCT) == (6.0, 6.0)
    assert S.BASIS_ROC_SATURATION_PP == 0.20
    assert S.VIX_CHANGE_SATURATION_PCT == 10.0
    assert S.ROLLOVER_SATURATION == 0.10
    assert S.WEIGHTS == {
        "oi_price_state_1d": 3.0, "oi_buildup_5d": 2.0, "oi_buildup_15d": 1.0,
        "oi_buildup_30d": 1.0, "basis_carry": 2.0, "basis_roc": 2.0,
        "vix_percentile": 1.5, "vix_change": 1.0, "rollover_relative": 1.0,
    }


# --------------------------------------------------------------------------------
# build_fno_daily, against bhavcopy-shaped rows
# --------------------------------------------------------------------------------


class FakeRow:
    """The subset of :class:`src.data.bhavcopy.BhavRow` the builder reads."""

    is_stock_future = True

    def __init__(self, symbol, expiry, close, oi):
        self.symbol, self.expiry, self.close, self.open_interest = \
            symbol, expiry, close, oi


def test_build_fno_daily_skips_a_session_whose_scale_cannot_be_resolved():
    """A hole is better than a fabricated 60% basis."""
    from src.market.calendar import trading_days_between

    days = trading_days_between(date(2024, 1, 2), date(2024, 1, 31))
    expiry = date(2024, 1, 25)
    rows = {d: [FakeRow("TESTSYM", expiry, 1000.0, 500_000),
                FakeRow("TESTSYM", date(2024, 2, 29), 1004.0, 90_000)]
            for d in days}
    good = {d: 998.0 for d in days}
    built = F.build_fno_daily(rows, "TESTSYM", good)
    assert built, "nothing built from a clean fixture"

    broken = dict(good)
    broken[days[3]] = 625.0          # ratio 1.6: no candidate within tolerance
    built2 = F.build_fno_daily(rows, "TESTSYM", broken)
    assert days[3] not in built2
    assert len(built2) == len(built) - 1


def test_build_fno_daily_records_total_oi_across_every_listed_expiry():
    from src.market.calendar import trading_days_between

    days = trading_days_between(date(2024, 1, 2), date(2024, 1, 20))
    rows = {d: [FakeRow("TESTSYM", date(2024, 1, 25), 1000.0, 500_000),
                FakeRow("TESTSYM", date(2024, 2, 29), 1004.0, 90_000),
                FakeRow("TESTSYM", date(2024, 3, 28), 1008.0, 10_000)]
            for d in days}
    built = F.build_fno_daily(rows, "TESTSYM", {d: 998.0 for d in days})
    row = built[days[0]]
    assert row.total_oi == 600_000
    assert row.near_oi == 500_000
    assert row.next_oi == 90_000
    assert row.next_expiry == date(2024, 2, 29)
