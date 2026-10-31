"""Data models for Module B -- the Signal Engine.

Every candidate carries not just a score but WHICH components produced it and which
argued against. A ranked list with no dissent recorded is unauditable: when a signal
fails you need to know whether the engine was confident or split.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

__all__ = [
    "Direction",
    "ComponentScore",
    "Candidate",
    "SignalDecision",
    "Rejection",
]


class Direction:
    """Only LONG exists.

    Module C's exit logic is long-specific throughout -- it trails below the running
    high and exits on losing VWAP from above. A short routed through it would receive
    inverted, confidently wrong advice, so the signal engine cannot produce one. See
    CLAUDE.md section 2.2.
    """

    LONG = "LONG"


@dataclass(frozen=True)
class ComponentScore:
    """One scoring component's contribution.

    ``score`` is normalised to [-1, +1] from the long side: positive supports the
    trade, negative argues against it. ``None`` means the component could not be
    computed and is excluded from the weighted average rather than counted as zero --
    an unknown is not a neutral opinion.
    """

    name: str
    score: float | None
    weight: float
    raw: float | None
    detail: str

    @property
    def available(self) -> bool:
        return self.score is not None

    @property
    def fired(self) -> bool:
        """Supported the trade."""
        return self.score is not None and self.score > 0.15

    @property
    def against(self) -> bool:
        """Argued against the trade."""
        return self.score is not None and self.score < -0.15

    @property
    def contribution(self) -> float:
        return 0.0 if self.score is None else self.score * self.weight


@dataclass(frozen=True)
class Candidate:
    """One surfaced trade idea, with its levels and its reasoning."""

    symbol: str
    direction: str
    score: float
    asof: datetime
    trade_date: date

    last_price: float
    atr: float
    entry_low: Decimal
    entry_high: Decimal
    stop: Decimal
    t1: Decimal
    t2: Decimal

    components: tuple[ComponentScore, ...] = ()
    sector: str = "UNKNOWN"

    @property
    def entry_mid(self) -> Decimal:
        return (self.entry_low + self.entry_high) / 2

    @property
    def risk_per_share(self) -> Decimal:
        return self.entry_mid - self.stop

    @property
    def risk_reward_t1(self) -> float:
        risk = self.risk_per_share
        return float((self.t1 - self.entry_mid) / risk) if risk > 0 else 0.0

    @property
    def risk_reward_t2(self) -> float:
        risk = self.risk_per_share
        return float((self.t2 - self.entry_mid) / risk) if risk > 0 else 0.0

    @property
    def fired(self) -> tuple[ComponentScore, ...]:
        return tuple(c for c in self.components if c.fired)

    @property
    def against(self) -> tuple[ComponentScore, ...]:
        return tuple(c for c in self.components if c.against)

    @property
    def unavailable(self) -> tuple[ComponentScore, ...]:
        return tuple(c for c in self.components if not c.available)

    def reason(self) -> str:
        """What fired, what argued against, and what could not be judged."""
        parts = [
            f"{self.symbol} LONG, score {self.score:+.2f}. "
            f"FOR: {', '.join(c.detail for c in self.fired) or 'nothing'}."
        ]
        if self.against:
            parts.append(f"AGAINST: {', '.join(c.detail for c in self.against)}.")
        else:
            parts.append("AGAINST: nothing.")
        if self.unavailable:
            parts.append(
                f"Not judged: {', '.join(c.name for c in self.unavailable)}."
            )
        parts.append(
            f"Entry {self.entry_low}-{self.entry_high}, stop {self.stop}, "
            f"T1 {self.t1} (R:R {self.risk_reward_t1:.1f}), "
            f"T2 {self.t2} (R:R {self.risk_reward_t2:.1f})."
        )
        return " ".join(parts)

    def render(self) -> str:
        lines = [
            f"[{self.asof:%H:%M} IST] {self.symbol}  LONG  score {self.score:+.2f}  "
            f"({self.sector})",
            f"  entry  : {self.entry_low} - {self.entry_high}   (last {self.last_price:.2f})",
            f"  stop   : {self.stop}   ATR {self.atr:.2f}",
            f"  T1/T2  : {self.t1} / {self.t2}   R:R {self.risk_reward_t1:.1f} / "
            f"{self.risk_reward_t2:.1f}",
            "  for    : " + (", ".join(c.detail for c in self.fired) or "nothing"),
            "  against: " + (", ".join(c.detail for c in self.against) or "nothing"),
        ]
        if self.unavailable:
            lines.append("  unknown: " + ", ".join(c.name for c in self.unavailable))
        return "\n".join(lines)

    def as_row(self) -> dict:
        return {
            "asof": self.asof.isoformat(),
            "trade_date": self.trade_date.isoformat(),
            "symbol": self.symbol,
            "direction": self.direction,
            "sector": self.sector,
            "score": round(self.score, 4),
            "last_price": round(self.last_price, 2),
            "atr": round(self.atr, 4),
            "entry_low": str(self.entry_low),
            "entry_high": str(self.entry_high),
            "stop": str(self.stop),
            "t1": str(self.t1),
            "t2": str(self.t2),
            "risk_reward_t1": round(self.risk_reward_t1, 3),
            "risk_reward_t2": round(self.risk_reward_t2, 3),
            "reason": self.reason(),
        }


@dataclass(frozen=True)
class Rejection:
    """Why a symbol did not make it. Kept so a NO_SIGNAL day can be explained."""

    symbol: str
    stage: str  # ban | liquidity | atr | score | risk_reward | entry_check | sector | cap
    detail: str


@dataclass
class SignalDecision:
    """Everything one evaluation produced, including the case for nothing."""

    asof: datetime
    trade_date: date
    candidates: list[Candidate] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    considered: int = 0
    notes: list[str] = field(default_factory=list)
    #: Every symbol that produced a composite score, whether or not it cleared the
    #: bar. Without this the score bar can only be judged against the scores that
    #: passed it, which says nothing about whether it discriminates.
    all_scores: list[tuple[str, float]] = field(default_factory=list)

    @property
    def is_no_signal(self) -> bool:
        return not self.candidates

    def rejections_by_stage(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rejections:
            out[r.stage] = out.get(r.stage, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def render(self) -> str:
        head = f"[{self.asof:%H:%M} IST] {self.trade_date}  considered {self.considered}"
        if self.is_no_signal:
            body = [
                head,
                "  NO_SIGNAL -- nothing cleared the bar. This is a normal outcome, "
                "not a failure.",
                "  rejected: " + (
                    ", ".join(f"{k}={v}" for k, v in self.rejections_by_stage().items())
                    or "none"
                ),
            ]
        else:
            body = [head, ""] + [c.render() for c in self.candidates]
        for note in self.notes:
            body.append(f"  note: {note}")
        return "\n".join(body)
