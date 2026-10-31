"""Scoring the F&O features into one composite, on the long side.

Every constant in this module is a DESIGN choice fixed before any P&L was computed, and
each one is justified from the nature of the quantity rather than read off an outcome.
That is the only thing that makes the composite's absolute bar meaningful: a threshold
tuned until the equity curve looked good is not a threshold, it is a fitted parameter
wearing one.

The scale is the project's existing one -- :class:`~src.signals.scoring.ComponentScore`
in [-1, +1], positive supporting a long, ``None`` for "could not be measured", and
:func:`~src.signals.scoring.composite` averaging over the AVAILABLE components with
their weights renormalised. An unmeasurable component is dropped, never counted as
neutral: an unknown is not an opinion.

Two of the nine components -- both India VIX -- are MARKET-WIDE and take the same value
for every symbol on a given session. That is intentional. In a single-shot design where
the day's best candidate is compared against an absolute bar, a market-wide component is
the only thing that can push an entire session below the bar and produce NO_TRADE. A
purely cross-sectional score always has a best candidate and can never decline to trade.

WHY ABSOLUTE SCALING AND NOT CROSS-SECTIONAL RANKING
-----------------------------------------------------
Ranking each feature within the day's universe needs no constants and is tempting for
that reason. It was not used, because it makes the absolute bar toothless: the top
candidate holds top ranks by construction, so its composite is high almost every day and
the bar would bind on essentially nothing but the market-wide terms. Absolute scaling
costs a stated constant per feature and keeps NO_TRADE reachable on its merits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence

from src.signals import fno_features as F
from src.signals.scoring import ComponentScore, composite

__all__ = [
    "WEIGHTS",
    "OI_STATE_SCORE",
    "OI_SATURATION_PCT",
    "CARRY_CENTRE_PCT",
    "CARRY_SPAN_PCT",
    "BASIS_ROC_SATURATION_PP",
    "VIX_CHANGE_SATURATION_PCT",
    "ROLLOVER_SATURATION",
    "FnoCandidate",
    "clamp",
    "score_oi_price_state",
    "score_oi_buildup",
    "score_basis_carry",
    "score_basis_roc",
    "score_vix_percentile",
    "score_vix_change",
    "score_rollover",
    "score_symbol_fno",
    "rank_universe_fno",
]

#: Component weights. Ordered by how directly each measures F&O POSITIONING, which is
#: the hypothesis under test: the one-day OI/price state is the canonical statement of
#: who is entering, the multi-day builds are the same idea at longer lookbacks and
#: individually weaker, basis is a price-based read on the same positioning, and India
#: VIX is market context rather than a statement about this symbol.
WEIGHTS: dict[str, float] = {
    "oi_price_state_1d": 3.0,
    "oi_buildup_5d": 2.0,
    "oi_buildup_15d": 1.0,
    "oi_buildup_30d": 1.0,
    "basis_carry": 2.0,
    "basis_roc": 2.0,
    "vix_percentile": 1.5,
    "vix_change": 1.0,
    "rollover_relative": 1.0,
}

#: The four states, scored on the long side. Buildups are full-strength because a new
#: position is a fresh commitment of capital; the two unwind states are damped to 0.3
#: because a price move driven by positions CLOSING has no one behind it and is the
#: weaker reading of the same move. Signs follow standard market-structure convention
#: and are not free parameters.
OI_STATE_SCORE: dict[str, float] = {
    F.OI_STATE_LONG_BUILDUP: +1.0,
    F.OI_STATE_SHORT_COVERING: +0.3,
    F.OI_STATE_LONG_UNWINDING: -0.3,
    F.OI_STATE_SHORT_BUILDUP: -1.0,
}

#: OI percent change at which a multi-day build is treated as unambiguously large.
#: Scaled with the window because OI accumulates: a 10% build in a week is a marked
#: change in participation, and a month needs proportionately more to say the same.
OI_SATURATION_PCT: dict[int, float] = {5: 10.0, 15: 20.0, 30: 30.0}

#: Annualised basis, in percent, treated as the neutral cost of carry -- roughly the
#: Indian short-term risk-free rate over this period. A futures premium at this level is
#: what arbitrage-free pricing predicts and carries no directional information.
CARRY_CENTRE_PCT = 6.0
#: Deviation from the carry centre at which the score saturates. Set equal to the centre
#: so a flat basis (0% annualised) scores -1 and twice fair carry scores +1.
CARRY_SPAN_PCT = 6.0

#: One-day basis change, in percentage points, treated as large. A stock future's basis
#: lives in a band well under one point, so a fifth of a point in a session is a real
#: move in the carry rather than noise.
BASIS_ROC_SATURATION_PP = 0.20

#: One-day India VIX percent change treated as large. VIX routinely moves a few percent;
#: ten is a regime-shifting session.
VIX_CHANGE_SATURATION_PCT = 10.0

#: Deviation of the rollover ratio from its same-days-to-expiry norm treated as large.
#: Ten points of rollover above or below normal is a marked change in how much of the
#: open position is being carried forward.
ROLLOVER_SATURATION = 0.10


def clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class FnoCandidate:
    """One symbol scored on one decision date."""

    symbol: str
    trade_date: date
    score: float
    components: tuple[ComponentScore, ...]

    def component(self, name: str) -> ComponentScore | None:
        return next((c for c in self.components if c.name == name), None)


# --------------------------------------------------------------------------------
# Component scorers
# --------------------------------------------------------------------------------


def score_oi_price_state(state: str | None) -> ComponentScore:
    """The four-state OI/price classification, mapped to the long side."""
    value = OI_STATE_SCORE.get(state) if state else None
    return ComponentScore(
        name="oi_price_state_1d",
        score=value,
        weight=WEIGHTS["oi_price_state_1d"],
        raw=None,
        detail=(state or "unavailable (roll, corporate action, or flat leg)"),
    )


def score_oi_buildup(lookback: int, oi_pct: float | None,
                     price_pct: float | None) -> ComponentScore:
    """A multi-day OI build, signed by the same four-state logic and scaled by size.

    The state supplies the sign, the magnitude supplies the confidence. An OI build of
    0.1% and one of 30% both point the same way and should not score the same, which is
    what a bare state mapping would do.
    """
    name = f"oi_buildup_{lookback}d"
    if oi_pct is None or price_pct is None or oi_pct == 0.0 or price_pct == 0.0:
        return ComponentScore(name=name, score=None, weight=WEIGHTS[name], raw=oi_pct,
                              detail="unavailable (roll, corporate action, or flat leg)")
    if price_pct > 0:
        base = OI_STATE_SCORE[F.OI_STATE_LONG_BUILDUP if oi_pct > 0
                              else F.OI_STATE_SHORT_COVERING]
    else:
        base = OI_STATE_SCORE[F.OI_STATE_SHORT_BUILDUP if oi_pct > 0
                              else F.OI_STATE_LONG_UNWINDING]
    magnitude = min(1.0, abs(oi_pct) / OI_SATURATION_PCT[lookback])
    return ComponentScore(
        name=name, score=clamp(base * magnitude), weight=WEIGHTS[name], raw=oi_pct,
        detail=f"OI {oi_pct:+.2f}% / price {price_pct:+.2f}% over {lookback}d",
    )


def score_basis_carry(annualised: float | None) -> ComponentScore:
    """Annualised basis against fair carry. A rich premium is the bullish read."""
    score = (None if annualised is None
             else clamp((annualised - CARRY_CENTRE_PCT) / CARRY_SPAN_PCT))
    return ComponentScore(
        name="basis_carry", score=score, weight=WEIGHTS["basis_carry"], raw=annualised,
        detail=("unavailable (too near expiry)" if annualised is None
                else f"{annualised:+.2f}% annualised vs {CARRY_CENTRE_PCT}% fair carry"),
    )


def score_basis_roc(roc_pp: float | None) -> ComponentScore:
    """One-day change in the basis. An expanding premium is the bullish read."""
    score = None if roc_pp is None else clamp(roc_pp / BASIS_ROC_SATURATION_PP)
    return ComponentScore(
        name="basis_roc", score=score, weight=WEIGHTS["basis_roc"], raw=roc_pp,
        detail=("unavailable (roll or corporate action)" if roc_pp is None
                else f"{roc_pp:+.3f}pp in one session"),
    )


def score_vix_percentile(pct: float | None) -> ComponentScore:
    """Where volatility sits in its trailing year. Calm favours a long.

    MARKET-WIDE: identical for every symbol on a session.
    """
    score = None if pct is None else clamp(1.0 - 2.0 * pct)
    return ComponentScore(
        name="vix_percentile", score=score, weight=WEIGHTS["vix_percentile"], raw=pct,
        detail=("unavailable (insufficient VIX history)" if pct is None
                else f"VIX at the {100.0 * pct:.0f}th percentile of its trailing year"),
    )


def score_vix_change(change_pct: float | None) -> ComponentScore:
    """One-session VIX change. Falling volatility is risk-on.

    MARKET-WIDE: identical for every symbol on a session.
    """
    score = (None if change_pct is None
             else clamp(-change_pct / VIX_CHANGE_SATURATION_PCT))
    return ComponentScore(
        name="vix_change", score=score, weight=WEIGHTS["vix_change"], raw=change_pct,
        detail=("unavailable" if change_pct is None else f"VIX {change_pct:+.2f}%"),
    )


def score_rollover(relative: float | None) -> ComponentScore:
    """Rollover against its norm at the same distance from expiry.

    Above-normal rollover means an unusually large share of the open position is being
    carried into the next contract rather than closed, which is the conviction reading.
    """
    score = None if relative is None else clamp(relative / ROLLOVER_SATURATION)
    return ComponentScore(
        name="rollover_relative", score=score, weight=WEIGHTS["rollover_relative"],
        raw=relative,
        detail=("unavailable (no next contract, or fewer than 3 prior cycles)"
                if relative is None
                else f"{100.0 * relative:+.1f}pp vs the same-DTE norm"),
    )


# --------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------


def score_symbol_fno(
    records: Mapping[date, F.FnoDaily],
    vix_by_date: Mapping[date, float],
    sessions: Sequence[date],
    upto: date,
    *,
    basis_usable: bool = True,
) -> tuple[float | None, tuple[ComponentScore, ...]]:
    """Composite F&O score for one symbol at the decision moment on ``upto``.

    Data-availability boundary:
        Every component routes through :func:`src.signals.fno_features.fno_series` or
        the VIX accessors, all of which apply a strict ``trade_date < upto``. Nothing
        here is handed a pre-filtered list, which is what allows
        ``assert_session_causal`` to prove the boundary holds rather than assume it.

    ``basis_usable=False`` marks both basis components unavailable for the whole run,
    for a symbol whose price scale carries a residual offset that no candidate factor can
    express -- see :func:`src.signals.fno_features.basis_is_plausible`. They then drop
    out with their weight through the ordinary "could not be measured" path rather than
    contributing a constant fake premium. It is a per-SYMBOL constant, fixed before the
    first decision date and identical on every one of them, so it cannot encode anything
    about a particular session.

    Returns (composite, components). The composite is None when NOTHING could be
    measured, which is not a score of zero and must not be ranked as one.
    """
    components = (
        score_oi_price_state(
            F.oi_price_state(records, sessions, upto, lookback=1, use_total=False)),
        *(
            score_oi_buildup(
                k,
                F.oi_change_pct(records, sessions, upto, lookback=k, use_total=True),
                F.spot_return_pct(records, sessions, upto, lookback=k),
            )
            for k in (5, 15, 30)
        ),
        score_basis_carry(F.basis_annualised_pct(records, sessions, upto)
                          if basis_usable else None),
        score_basis_roc(F.basis_roc_pp(records, sessions, upto)
                        if basis_usable else None),
        score_vix_percentile(F.vix_percentile(vix_by_date, sessions, upto)),
        score_vix_change(F.vix_change_pct(vix_by_date, sessions, upto)),
        score_rollover(F.rollover_relative(records, sessions, upto)),
    )
    return composite(components), components


def rank_universe_fno(
    records_by_symbol: Mapping[str, Mapping[date, F.FnoDaily]],
    vix_by_date: Mapping[date, float],
    sessions: Sequence[date],
    upto: date,
    *,
    eligible: Sequence[str] | None = None,
    basis_usable: Mapping[str, bool] | None = None,
) -> list[FnoCandidate]:
    """Score every eligible symbol on ``upto`` and return them ranked, best first.

    ``eligible`` is the caller's liquidity/volatility screen, applied BEFORE scoring so
    that a name which would never be traded cannot occupy the top of the ranking.

    ``basis_usable`` maps symbol to whether its basis is on a trustworthy price scale;
    a symbol absent from the mapping defaults to True.

    Ties break on symbol name, so the ordering is deterministic and a rerun cannot
    quietly select a different candidate.
    """
    names = list(records_by_symbol) if eligible is None else list(eligible)
    usable = basis_usable or {}
    out: list[FnoCandidate] = []
    for symbol in names:
        records = records_by_symbol.get(symbol)
        if not records:
            continue
        score, components = score_symbol_fno(
            records, vix_by_date, sessions, upto,
            basis_usable=usable.get(symbol, True))
        if score is None:
            continue
        out.append(FnoCandidate(symbol=symbol, trade_date=upto, score=score,
                                components=components))
    out.sort(key=lambda c: (-c.score, c.symbol))
    return out
