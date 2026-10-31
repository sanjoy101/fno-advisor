"""Composite scoring. Each component is a pure function of one feature value.

Every component maps a raw feature onto ``[-1, +1]`` from the long side, so the
weighted average is comparable across symbols and across the day. Two rules hold
throughout:

* An **unavailable** feature scores ``None``, not ``0.0``. It is then dropped from the
  weighted average along with its weight, rather than diluting the score toward
  neutral. "I could not measure this" and "this is neutral" are different claims, and
  conflating them lets a half-warm symbol outrank a fully-measured one.
* A component may score **negative**. A ranking built only from positives cannot tell
  a strong setup from a mixed one, because everything that fails to help scores zero.

The shaping constants live in :class:`config.settings.SignalEngineSettings`. They are
defaults chosen to be defensible in advance, NOT values fitted to any result.
"""

from __future__ import annotations

from typing import Sequence

from config.settings import SignalEngineSettings
from src.signals.models import ComponentScore

__all__ = [
    "ramp",
    "score_or_breakout",
    "score_or_hold",
    "score_ema_trend",
    "score_vwap_trend",
    "score_vwap_persistence",
    "score_rsi",
    "score_roc",
    "score_volume",
    "score_relative_strength",
    "composite",
]


def ramp(value: float | None, full: float, *, floor: float = -1.0, cap: float = 1.0) -> float | None:
    """Linear ramp: ``0`` at zero, ``+1`` at ``full``, clamped to [floor, cap].

    Symmetric for negatives, so a move against the trade scores as strongly against as
    the same move in favour scores for.
    """
    if value is None:
        return None
    if full <= 0:
        return 0.0
    return max(floor, min(cap, value / full))


# --------------------------------------------------------------------------------
# Opening range
# --------------------------------------------------------------------------------


def score_or_breakout(
    state: str | None,
    atr_units: float | None,
    cfg: SignalEngineSettings,
) -> ComponentScore:
    """Breakout past the 15-minute opening range, measured in ATRs.

    Below the range scores negative: for a long, that is not a weak setup, it is the
    wrong side of the day's first battle.
    """
    weight = float(cfg.weight_or_breakout)
    if state is None or atr_units is None:
        return ComponentScore("or_breakout", None, weight, None,
                              "opening range not formed yet")
    score = ramp(atr_units, float(cfg.breakout_atr_full))
    if state == "ABOVE":
        detail = f"broke the opening range by {atr_units:.2f} ATR"
    elif state == "BELOW":
        detail = f"below the opening range by {abs(atr_units):.2f} ATR"
    else:
        detail = "still inside the opening range"
    return ComponentScore("or_breakout", score, weight, atr_units, detail)


def score_or_hold(held: int | None, cfg: SignalEngineSettings) -> ComponentScore:
    """How many consecutive bars the breakout has held.

    Only ever non-negative: not having broken out is already penalised by
    :func:`score_or_breakout`, and charging for it twice would double-count.
    """
    weight = float(cfg.weight_or_hold)
    if held is None:
        return ComponentScore("or_hold", None, weight, None,
                              "opening range not formed yet")
    score = ramp(float(held), float(cfg.hold_bars_full), floor=0.0)
    detail = (
        f"breakout held {held} consecutive bars" if held
        else "breakout has not held a single bar"
    )
    return ComponentScore("or_hold", score, weight, float(held), detail)


# --------------------------------------------------------------------------------
# Trend
# --------------------------------------------------------------------------------


def score_ema_trend(gap_pct: float | None, cfg: SignalEngineSettings) -> ComponentScore:
    weight = float(cfg.weight_ema_trend)
    if gap_pct is None:
        return ComponentScore("ema_trend", None, weight, None, "EMA21 not warm yet")
    score = ramp(gap_pct, float(cfg.ema_gap_full_pct))
    detail = (
        f"EMA9 above EMA21 by {gap_pct:.2f}%" if gap_pct > 0
        else f"EMA9 BELOW EMA21 by {abs(gap_pct):.2f}%"
    )
    return ComponentScore("ema_trend", score, weight, gap_pct, detail)


def score_vwap_trend(gap_pct: float | None, cfg: SignalEngineSettings) -> ComponentScore:
    weight = float(cfg.weight_vwap_trend)
    if gap_pct is None:
        return ComponentScore("vwap_trend", None, weight, None, "no VWAP yet")
    score = ramp(gap_pct, float(cfg.vwap_gap_full_pct))
    detail = (
        f"price {gap_pct:.2f}% above session VWAP" if gap_pct > 0
        else f"price {abs(gap_pct):.2f}% BELOW session VWAP"
    )
    return ComponentScore("vwap_trend", score, weight, gap_pct, detail)


def score_vwap_persistence(bars: int | None, cfg: SignalEngineSettings) -> ComponentScore:
    """Consecutive bars closing above VWAP. Non-negative, same reasoning as or_hold."""
    weight = float(cfg.weight_vwap_persistence)
    if bars is None:
        return ComponentScore("vwap_persistence", None, weight, None, "no VWAP yet")
    score = ramp(float(bars), float(cfg.vwap_bars_full), floor=0.0)
    detail = (
        f"held above VWAP for {bars} bars" if bars
        else "not holding above VWAP"
    )
    return ComponentScore("vwap_persistence", score, weight, float(bars), detail)


# --------------------------------------------------------------------------------
# Momentum
# --------------------------------------------------------------------------------


def score_rsi(value: float | None, cfg: SignalEngineSettings) -> ComponentScore:
    """RSI as a BAND, not a threshold.

    Full marks between ``rsi_ideal_low`` and ``rsi_ideal_high``. Below 50 scores
    negative. Above ``rsi_overbought`` the score falls back toward zero and then
    negative: buying a stock that has already gone vertical is buying the exhaustion,
    and a naive "RSI > 50 is good" rule ranks exactly that highest.
    """
    weight = float(cfg.weight_rsi)
    if value is None:
        return ComponentScore("rsi", None, weight, None, "RSI not warm yet")

    low, high, hot = float(cfg.rsi_ideal_low), float(cfg.rsi_ideal_high), float(cfg.rsi_overbought)
    if value < 50.0:
        score = max(-1.0, (value - 50.0) / 25.0)
        detail = f"RSI {value:.0f} is below 50"
    elif value < low:
        score = (value - 50.0) / (low - 50.0) if low > 50 else 1.0
        detail = f"RSI {value:.0f} is rising but not yet strong"
    elif value <= high:
        score = 1.0
        detail = f"RSI {value:.0f} is in the {low:.0f}-{high:.0f} band"
    elif value <= hot:
        score = 1.0 - (value - high) / (hot - high) if hot > high else 0.0
        detail = f"RSI {value:.0f} is getting extended"
    else:
        score = max(-1.0, -(value - hot) / (100.0 - hot) if hot < 100 else -1.0)
        detail = f"RSI {value:.0f} is overbought past {hot:.0f}"
    return ComponentScore("rsi", score, weight, value, detail)


def score_roc(pct: float | None, cfg: SignalEngineSettings) -> ComponentScore:
    weight = float(cfg.weight_roc)
    if pct is None:
        return ComponentScore("roc", None, weight, None, "not enough bars for ROC")
    score = ramp(pct, float(cfg.roc_full_pct))
    detail = (
        f"up {pct:.2f}% over the last {cfg.roc_lookback_bars} bars" if pct > 0
        else f"DOWN {abs(pct):.2f}% over the last {cfg.roc_lookback_bars} bars"
    )
    return ComponentScore("roc", score, weight, pct, detail)


# --------------------------------------------------------------------------------
# Volume
# --------------------------------------------------------------------------------


def score_volume(ratio: float | None, cfg: SignalEngineSettings) -> ComponentScore:
    """Volume against its own time-of-day baseline.

    ``1.0`` is an ordinary minute for that time of day and scores zero. Below-average
    participation scores negative -- a breakout nobody is trading is not a breakout.
    """
    weight = float(cfg.weight_volume)
    if ratio is None:
        return ComponentScore("volume", None, weight, None,
                              "no time-of-day volume baseline")
    full = float(cfg.relative_volume_full)
    score = ramp(ratio - 1.0, max(1e-9, full - 1.0))
    detail = (
        f"volume {ratio:.1f}x its time-of-day average" if ratio >= 1.0
        else f"volume only {ratio:.1f}x its time-of-day average"
    )
    return ComponentScore("volume", score, weight, ratio, detail)


# --------------------------------------------------------------------------------
# Relative strength
# --------------------------------------------------------------------------------


def score_relative_strength(
    own_return_pct: float | None,
    panel_median_pct: float | None,
    cfg: SignalEngineSettings,
) -> ComponentScore:
    """Return against the panel median so far today.

    A stock up 0.5% on a day when the panel is up 0.8% is WEAK, and an absolute-return
    ranking would call it strong. This is the component that stops the engine simply
    ranking beta on an up day.
    """
    weight = float(cfg.weight_relative_strength)
    if own_return_pct is None or panel_median_pct is None:
        return ComponentScore("relative_strength", None, weight, None,
                              "panel median not available")
    excess = own_return_pct - panel_median_pct
    score = ramp(excess, float(cfg.relative_strength_full_pct))
    context = f"({own_return_pct:+.2f}% vs median {panel_median_pct:+.2f}%)"
    if abs(excess) < 0.005:
        detail = f"moving with the panel, no relative edge {context}"
    elif excess > 0:
        detail = f"outperforming the panel by {excess:.2f}pp {context}"
    else:
        detail = f"UNDERPERFORMING the panel by {abs(excess):.2f}pp {context}"
    return ComponentScore("relative_strength", score, weight, excess, detail)


# --------------------------------------------------------------------------------
# Composite
# --------------------------------------------------------------------------------


def composite(components: Sequence[ComponentScore]) -> float | None:
    """Weight-normalised average over the AVAILABLE components.

    Unavailable components are dropped with their weight, so a symbol is scored on
    what could actually be measured rather than penalised for a cold indicator.
    Returns None when nothing at all could be measured -- which is not a score of
    zero, and must not be ranked as one.
    """
    usable = [c for c in components if c.available]
    total_weight = sum(c.weight for c in usable)
    if not usable or total_weight <= 0:
        return None
    return sum(c.contribution for c in usable) / total_weight
