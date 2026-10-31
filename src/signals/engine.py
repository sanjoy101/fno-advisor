"""Module B -- the Signal Engine.

Ranks the F&O universe at fixed times and surfaces UP TO two long candidates.

Order of operations, and why:

1. **Hard gates first** -- ban, liquidity, minimum ATR. A gated symbol is ABSENT, not
   ranked low. Ranking a banned symbol third still leaves it one quiet day from being
   surfaced.
2. **Panel median second.** Relative strength is cross-sectional, so every surviving
   symbol's return has to exist before any of them can be scored.
3. **Score, then rank.**
4. **Walk the ranking** applying the remaining gates -- risk:reward, Module C's
   entry check, one-per-sector -- taking candidates until the cap.

The cap is a CEILING. Zero candidates is a normal, expected, frequent result and the
engine never relaxes anything to reach two. See CLAUDE.md section 1.5.

Look-ahead: every feature reads bars through
:func:`~src.positions.models.completed_bars` and is proved causal in
``tests/test_signal_leak.py``. The engine adds one cross-sectional dependency -- the
panel median -- which is computed from the same completed bars at the same instant, so
it inherits the same boundary.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from statistics import median
from typing import Mapping, Sequence

from config.settings import (
    ExitEngineSettings,
    SignalEngineSettings,
    get_exit_engine_settings,
    get_signal_engine_settings,
)
from src.market.calendar import IST, ensure_ist, is_trading_day
from src.positions.exit_engine import check_hypothetical_entry
from src.positions.models import Bar
from src.signals import features as F
from src.signals.models import Candidate, Direction, Rejection, SignalDecision
from src.signals.scoring import (
    composite,
    score_ema_trend,
    score_or_breakout,
    score_or_hold,
    score_relative_strength,
    score_roc,
    score_rsi,
    score_volume,
    score_vwap_persistence,
    score_vwap_trend,
)
from src.signals.universe import FnoBanList, UniverseFilters, sector_of

log = logging.getLogger("fno.signals")

__all__ = ["signal_times", "generate_signals", "SymbolView", "build_symbol_view"]

PAISA = Decimal("0.01")


def signal_times(trade_date: date, cfg: SignalEngineSettings | None = None) -> list[datetime]:
    """Evaluation instants: 09:30, then every 5 minutes, through 14:30.

    09:30 because the 15-minute opening range is only complete then. 14:30 because
    past that there is too little session left to reach a target before the 15:15
    square-off -- a signal you cannot see through is not a signal.
    """
    cfg = cfg or get_signal_engine_settings()
    first = time.fromisoformat(cfg.first_signal_time)
    last = time.fromisoformat(cfg.last_signal_time)
    cursor = datetime.combine(trade_date, first, tzinfo=IST)
    end = datetime.combine(trade_date, last, tzinfo=IST)
    out = []
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(minutes=cfg.interval_minutes)
    return out


class SymbolView:
    """Every feature for one symbol at one instant, computed once.

    Built before ranking so each feature is evaluated a single time per symbol per
    instant. Nothing here reads a bar that has not completed.
    """

    __slots__ = (
        "symbol", "bars", "at", "last_price", "atr_pct", "atr_abs", "turnover",
        "or_state", "or_atr_units", "or_held", "ema_gap", "vwap_gap", "vwap_bars",
        "rsi", "roc", "rel_volume", "session_return",
    )

    def __init__(self, symbol: str, bars: Sequence[Bar], at: datetime,
                 profile: Mapping[time, float], cfg: SignalEngineSettings):
        self.symbol = symbol
        self.bars = bars
        self.at = at
        done = F.session_bars(bars, at)
        self.last_price = F.last_price_at(bars, at)
        self.atr_pct = F.atr_pct(bars, at)
        self.atr_abs = (
            self.atr_pct * self.last_price / 100.0
            if self.atr_pct is not None and self.last_price else None
        )
        self.turnover = sum(b.close * b.volume for b in done) if done else None
        self.or_state = F.breakout_state(bars, at)
        self.or_atr_units = F.breakout_atr_units(bars, at)
        self.or_held = F.breakout_held_bars(bars, at)
        self.ema_gap = F.ema_alignment_pct(bars, at)
        self.vwap_gap = F.price_vs_vwap_pct(bars, at)
        self.vwap_bars = F.bars_above_vwap(bars, at)
        self.rsi = F.rsi_at(bars, at)
        self.roc = F.rate_of_change_pct(bars, at, lookback=cfg.roc_lookback_bars)
        self.rel_volume = F.relative_volume(bars, at, profile)
        self.session_return = F.session_return_pct(bars, at)


def build_symbol_view(symbol, bars, at, profile, cfg) -> SymbolView:
    return SymbolView(symbol, bars, at, profile, cfg)


def _quantize(value: float, rounding=ROUND_HALF_UP) -> Decimal:
    return Decimal(str(value)).quantize(PAISA, rounding=rounding)


def _levels(view: SymbolView, cfg: SignalEngineSettings):
    """Entry zone, stop and targets, all in ATRs off the last completed close."""
    price, atr = view.last_price, view.atr_abs
    half = float(cfg.entry_zone_atr) * atr
    return (
        _quantize(price - half, ROUND_DOWN),
        _quantize(price + half, ROUND_UP),
        _quantize(price - float(cfg.stop_atr_multiple) * atr, ROUND_DOWN),
        _quantize(price + float(cfg.t1_atr_multiple) * atr, ROUND_UP),
        _quantize(price + float(cfg.t2_atr_multiple) * atr, ROUND_UP),
    )


def generate_signals(
    store,
    symbols: Sequence[str],
    trade_date: date,
    at: datetime,
    all_sessions: Sequence[date],
    *,
    cfg: SignalEngineSettings | None = None,
    exit_cfg: ExitEngineSettings | None = None,
    ban_list: FnoBanList | None = None,
    filters: UniverseFilters | None = None,
    profiles: Mapping[str, Mapping[time, float]] | None = None,
) -> SignalDecision:
    """Rank the universe at ``at`` and return up to ``max_candidates`` longs.

    Data-availability boundary:
        Every feature reads bars with ``ts + BAR_INTERVAL <= at``. The volume baseline
        comes from sessions strictly before ``trade_date``. The panel median is
        computed from the same completed bars at the same instant. Nothing here can
        see the bar forming at ``at``, let alone a later one.
    """
    cfg = cfg or get_signal_engine_settings()
    exit_cfg = exit_cfg or get_exit_engine_settings()
    ban_list = ban_list or FnoBanList()
    filters = filters or UniverseFilters()
    at = ensure_ist(at)

    decision = SignalDecision(asof=at, trade_date=trade_date)
    if ban_list.warning():
        decision.notes.append(ban_list.warning())

    if not is_trading_day(trade_date):
        decision.notes.append(f"{trade_date} is not an NSE trading day")
        return decision

    window = signal_times(trade_date, cfg)
    if at < window[0] or at > window[-1]:
        decision.notes.append(
            f"{at:%H:%M} is outside the signal window "
            f"{cfg.first_signal_time}-{cfg.last_signal_time}; no signal is generated"
        )
        return decision

    # ---- pass 1: hard gates and feature computation ---------------------------
    views: list[SymbolView] = []
    for symbol in symbols:
        symbol = symbol.strip().upper()
        decision.considered += 1

        if ban_list.is_banned(symbol, trade_date):
            decision.rejections.append(
                Rejection(symbol, "ban", "in the F&O ban period -- no new positions")
            )
            continue
        if not store.has_bars(symbol, trade_date):
            decision.rejections.append(Rejection(symbol, "data", "no bars stored"))
            continue

        bars = store.load_bars(symbol, trade_date)
        profile = (profiles or {}).get(symbol)
        if profile is None:
            profile = F.volume_profile_for(
                store, symbol, all_sessions, trade_date,
                lookback=cfg.volume_profile_lookback,
            )
        view = build_symbol_view(symbol, bars, at, profile, cfg)

        if view.last_price is None or view.atr_abs is None or view.atr_abs <= 0:
            decision.rejections.append(
                Rejection(symbol, "data", "price or ATR not computable yet")
            )
            continue
        reject = filters.liquidity_reject(view.turnover)
        if reject:
            decision.rejections.append(Rejection(symbol, "liquidity", reject))
            continue
        reject = filters.atr_reject(view.atr_pct)
        if reject:
            decision.rejections.append(Rejection(symbol, "atr", reject))
            continue
        views.append(view)

    if not views:
        decision.notes.append("no symbol cleared the hard gates")
        return decision

    # ---- pass 2: cross-sectional relative strength ----------------------------
    returns = [v.session_return for v in views if v.session_return is not None]
    panel_median = median(returns) if returns else None
    if panel_median is None:
        decision.notes.append("panel median unavailable; relative strength not scored")

    # ---- pass 3: score --------------------------------------------------------
    scored: list[tuple[float, SymbolView, tuple]] = []
    for view in views:
        components = (
            score_or_breakout(view.or_state, view.or_atr_units, cfg),
            score_or_hold(view.or_held, cfg),
            score_ema_trend(view.ema_gap, cfg),
            score_vwap_trend(view.vwap_gap, cfg),
            score_vwap_persistence(view.vwap_bars, cfg),
            score_rsi(view.rsi, cfg),
            score_roc(view.roc, cfg),
            score_volume(view.rel_volume, cfg),
            score_relative_strength(view.session_return, panel_median, cfg),
        )
        total = composite(components)
        if total is None:
            decision.rejections.append(
                Rejection(view.symbol, "score", "no component could be computed")
            )
            continue
        decision.all_scores.append((view.symbol, total))
        if total < float(cfg.min_composite_score):
            decision.rejections.append(Rejection(
                view.symbol, "score",
                f"score {total:+.2f} below the {float(cfg.min_composite_score):+.2f} bar",
            ))
            continue
        scored.append((total, view, components))

    scored.sort(key=lambda t: (-t[0], t[1].symbol))

    # ---- pass 4: walk the ranking ---------------------------------------------
    for total, view, components in scored:
        if len(decision.candidates) >= cfg.max_candidates:
            decision.rejections.append(Rejection(
                view.symbol, "cap",
                f"score {total:+.2f} cleared the bar but the {cfg.max_candidates}-"
                f"candidate ceiling was already full",
            ))
            continue

        entry_low, entry_high, stop, t1, t2 = _levels(view, cfg)
        candidate = Candidate(
            symbol=view.symbol, direction=Direction.LONG, score=total, asof=at,
            trade_date=trade_date, last_price=view.last_price, atr=view.atr_abs,
            entry_low=entry_low, entry_high=entry_high, stop=stop, t1=t1, t2=t2,
            components=components, sector=sector_of(view.symbol),
        )

        if candidate.risk_reward_t1 < float(cfg.min_risk_reward):
            decision.rejections.append(Rejection(
                view.symbol, "risk_reward",
                f"R:R to T1 is {candidate.risk_reward_t1:.2f}, below the "
                f"{float(cfg.min_risk_reward):.2f} floor",
            ))
            continue

        conflict = filters.sector_conflict(
            view.symbol, [c.symbol for c in decision.candidates]
        )
        if conflict:
            decision.rejections.append(Rejection(view.symbol, "sector", conflict))
            continue

        # Module C has the final word. A signal its entry check would immediately
        # flag as INVALID_ENTRY is a bug in this engine, not a trade.
        check = check_hypothetical_entry(
            view.bars, at, candidate.entry_mid, stop,
            session_date=trade_date, engine_cfg=exit_cfg,
        )
        if check is not None and not check.valid:
            decision.rejections.append(Rejection(
                view.symbol, "entry_check",
                "Module C would flag this entry: " + "; ".join(check.problems),
            ))
            continue

        decision.candidates.append(candidate)

    return decision
