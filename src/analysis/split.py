"""Chronological DEV/HOLDOUT split, with a guard that makes peeking hard.

The protocol this enforces:

  * The split is **chronological**, never random. Time-ordered data shuffled into
    train/test leaks the future into the past -- CLAUDE.md 1.2.
  * Diagnostics and sweeps see **DEV only**. :meth:`HoldoutGuard.assert_dev_only`
    raises if a holdout date reaches a function that should not have it.
  * The holdout is evaluated **once**, for **one** config. The claim is recorded to
    disk, so a second peek from a fresh process still fails.

Why a file and not a variable: the obvious way to defeat an in-memory guard is to
run the script again with a different setting. Persisting the claim means the second
run has to actively delete evidence rather than just re-run.

Re-running the SAME config is allowed and is not a peek. It yields no information
that was not already obtained. Running a DIFFERENT config is a second look, and the
guard refuses it.

What this cannot stop: someone reading the holdout parquet directly, or deleting the
claim file. It is a guard rail on an honest process, not a security boundary. The
discipline still has to be wanted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.market.calendar import IST

__all__ = [
    "DevHoldoutSplit",
    "HoldoutGuard",
    "HoldoutLeakError",
    "HoldoutSpentError",
    "Panel",
    "PANEL_FULL_HISTORY",
    "PANEL_2026_SPENT",
    "PANEL_SWING",
    "SWING_DEV_END",
    "swing_split",
    "GLOBAL_EXCLUDED_SESSIONS",
    "SPENT_REGIONS",
    "REGIMES",
    "regime_of",
    "config_fingerprint",
]

#: Sessions dropped from every analysis for data-quality reasons.
#: 2026-08-20: the vendor returns it twice with conflicting high/low/volume.
GLOBAL_EXCLUDED_SESSIONS: tuple[date, ...] = (date(2026, 8, 20),)

#: Ranges whose DEV *and* holdout have already been examined, so they can never serve
#: as a holdout again. Data that has been looked at is development data forever --
#: the information is out, and no amount of re-splitting puts it back.
SPENT_REGIONS: tuple[tuple[date, date, str], ...] = (
    (date(2026, 2, 16), date(2026, 8, 19),
     "momentum analysis 2026-08-21: sweeps on its DEV, one holdout evaluation"),
)

#: Coarse regime labels, used to report how much variety a split actually spans.
#: Six months was one regime; the point of the six-year panel is that it is not.
REGIMES: tuple[tuple[str, date, date], ...] = (
    ("2020 crash & recovery", date(2020, 1, 1), date(2020, 12, 31)),
    ("2021-22 trending/bear", date(2021, 1, 1), date(2022, 12, 31)),
    ("2023-24 recovery", date(2023, 1, 1), date(2024, 12, 31)),
    ("2025-26", date(2025, 1, 1), date(2026, 12, 31)),
)


def regime_of(day: date) -> str:
    for label, start, end in REGIMES:
        if start <= day <= end:
            return label
    return "unlabelled"


class HoldoutLeakError(RuntimeError):
    """A holdout session reached something that may only see DEV."""


class HoldoutSpentError(RuntimeError):
    """The holdout has already been used, for a different config."""


def config_fingerprint(config: Any) -> str:
    """A stable short hash of a config, used to tell one evaluation from another.

    Two runs of the same settings produce the same fingerprint, so an idempotent
    re-run is recognised as such rather than counted as a second peek.
    """
    if hasattr(config, "model_dump"):
        payload = config.model_dump()
    elif isinstance(config, dict):
        payload = config
    else:
        payload = {"repr": repr(config)}
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class DevHoldoutSplit:
    """A chronological split of trading sessions."""

    dev: tuple[date, ...]
    holdout: tuple[date, ...]
    dev_fraction: float

    @classmethod
    def chronological(
        cls,
        sessions: Iterable[date],
        dev_fraction: float = 0.8,
    ) -> "DevHoldoutSplit":
        """First ``dev_fraction`` of sessions by DATE to DEV, the remainder to HOLDOUT.

        No shuffling and no interleaving. The holdout is strictly LATER than every
        DEV session, which is the only arrangement that answers the question actually
        being asked: would a rule chosen on what I had then have worked on what came
        next?

        Raises:
            ValueError: if the split would leave either side empty.
        """
        ordered = sorted(set(sessions))
        if not 0.0 < dev_fraction < 1.0:
            raise ValueError(f"dev_fraction must be in (0, 1), got {dev_fraction}")
        cut = int(len(ordered) * dev_fraction)
        dev, holdout = ordered[:cut], ordered[cut:]
        if not dev or not holdout:
            raise ValueError(
                f"{len(ordered)} sessions at dev_fraction={dev_fraction} gives "
                f"{len(dev)} DEV / {len(holdout)} HOLDOUT -- one side is empty"
            )
        return cls(tuple(dev), tuple(holdout), dev_fraction)

    @property
    def boundary(self) -> date:
        """The first holdout session. Everything before it is DEV."""
        return self.holdout[0]

    def describe(self) -> str:
        return (
            f"DEV {self.dev[0]}..{self.dev[-1]} ({len(self.dev)} sessions) | "
            f"HOLDOUT {self.holdout[0]}..{self.holdout[-1]} "
            f"({len(self.holdout)} sessions)"
        )

    def is_holdout(self, day: date) -> bool:
        return day >= self.boundary


@dataclass(frozen=True)
class Panel:
    """A named analysis window with its split and its own holdout claim file.

    Separate claim files per panel are the point: the 2026 panel's holdout is spent,
    and that must not silently permit or forbid anything about a different panel.
    """

    name: str
    start: date
    end: date
    dev_fraction: float
    claim_path: Path
    excluded: tuple[date, ...] = GLOBAL_EXCLUDED_SESSIONS
    spent: tuple[tuple[date, date, str], ...] = SPENT_REGIONS
    note: str = ""

    def sessions(self, calendar_sessions: Iterable[date] | None = None) -> list[date]:
        """Sessions in the panel, minus exclusions and minus any spent region.

        Spent ranges are removed entirely rather than pushed into DEV. Putting looked-at
        data in DEV would be defensible, but it also quietly re-weights the training
        window toward one already-mined period; keeping the panel clean is simpler to
        reason about and costs little here.
        """
        if calendar_sessions is None:
            from src.market.calendar import trading_days_between  # local: avoids a cycle

            calendar_sessions = trading_days_between(self.start, self.end)
        out = []
        for day in calendar_sessions:
            if not (self.start <= day <= self.end):
                continue
            if day in self.excluded:
                continue
            if any(lo <= day <= hi for lo, hi, _ in self.spent):
                continue
            out.append(day)
        return out

    def split(self, sessions: Iterable[date] | None = None) -> DevHoldoutSplit:
        return DevHoldoutSplit.chronological(
            self.sessions(sessions), self.dev_fraction
        )

    def guard(self, sessions: Iterable[date] | None = None) -> "HoldoutGuard":
        return HoldoutGuard(self.split(sessions), self.claim_path)

    def regime_coverage(self, sessions: Iterable[date] | None = None) -> list[dict]:
        """Sessions per regime on each side of the split.

        A holdout that sits entirely inside one regime tests one regime, whatever its
        session count says.
        """
        s = self.split(sessions)
        rows = []
        for label, _, _ in REGIMES:
            dev_n = sum(1 for d in s.dev if regime_of(d) == label)
            hold_n = sum(1 for d in s.holdout if regime_of(d) == label)
            if dev_n or hold_n:
                rows.append({"regime": label, "dev_sessions": dev_n,
                             "holdout_sessions": hold_n})
        return rows


#: The six-year panel. DEV spans the 2020 crash, the 2021-22 trend and 2023-24;
#: HOLDOUT is the most recent untouched block. 2026-02-16..2026-08-19 is excluded as
#: spent -- see SPENT_REGIONS.
PANEL_FULL_HISTORY = Panel(
    name="full-history-2020",
    start=date(2020, 1, 1),
    end=date(2026, 2, 15),
    dev_fraction=0.8,
    claim_path=Path("reports/.holdout_claim_full_history.json"),
    note="six-year panel; holdout genuinely untouched as of 2026-08-21",
)

#: The swing experiment's panel. A DIFFERENT question on the same data, so it gets a
#: different split and its own claim file: DEV to the end of 2024, HOLDOUT 2025
#: onward. The intraday holdout is spent; this one is untouched and must stay so until
#: there is a configuration worth a single evaluation.
#:
#: Note the boundary differs from PANEL_FULL_HISTORY's (2024-11-27). That is
#: deliberate -- a calendar-year cut is easier to reason about, and the two panels
#: answer different questions so their splits need not align.
PANEL_SWING = Panel(
    name="swing-2020-2026",
    start=date(2020, 1, 1),
    end=date(2026, 8, 19),
    dev_fraction=0.8,  # overridden by the explicit boundary below
    claim_path=Path("reports/.holdout_claim_swing.json"),
    spent=(),
    note="multi-day swing horizon; DEV to 2024-12-31, HOLDOUT 2025-01-01 onward",
)

#: Explicit calendar boundary for the swing panel, in place of a fraction.
SWING_DEV_END = date(2024, 12, 31)


def swing_split(sessions: Iterable[date]) -> DevHoldoutSplit:
    """Chronological split at the calendar boundary rather than at a fraction."""
    ordered = sorted(
        d for d in sessions
        if PANEL_SWING.start <= d <= PANEL_SWING.end
        and d not in GLOBAL_EXCLUDED_SESSIONS
    )
    dev = [d for d in ordered if d <= SWING_DEV_END]
    holdout = [d for d in ordered if d > SWING_DEV_END]
    if not dev or not holdout:
        raise ValueError(f"{len(dev)} DEV / {len(holdout)} HOLDOUT -- one side is empty")
    fraction = len(dev) / len(ordered)
    return DevHoldoutSplit(tuple(dev), tuple(holdout), fraction)


#: The original six-month panel, retained only so its spent status is discoverable.
#: Do not evaluate anything on it.
PANEL_2026_SPENT = Panel(
    name="2026-h1-SPENT",
    start=date(2026, 2, 16),
    end=date(2026, 8, 19),
    dev_fraction=0.8,
    claim_path=Path("reports/.holdout_claim.json"),
    spent=(),
    note="SPENT 2026-08-21. Kept for provenance only.",
)


class HoldoutGuard:
    """Gates access to the holdout and records the single evaluation it permits."""

    def __init__(self, split: DevHoldoutSplit, claim_path: Path | str) -> None:
        self.split = split
        self.claim_path = Path(claim_path)

    # ---- DEV side: always open -------------------------------------------------

    @property
    def dev(self) -> list[date]:
        """DEV sessions. Diagnostics and sweeps use these and only these."""
        return list(self.split.dev)

    def assert_dev_only(self, sessions: Sequence[date], what: str = "this analysis") -> None:
        """Raise if any holdout session is present.

        Called at the entry to every diagnostic and sweep. Catches the realistic
        mistake -- passing the full session list out of habit -- rather than
        deliberate circumvention.
        """
        leaked = sorted(d for d in sessions if self.split.is_holdout(d))
        if leaked:
            raise HoldoutLeakError(
                f"{what} was given {len(leaked)} HOLDOUT session(s), first "
                f"{leaked[0]}. Diagnostics and sweeps must run on DEV only "
                f"(through {self.split.dev[-1]}). Use guard.dev."
            )

    # ---- HOLDOUT side: one claim ----------------------------------------------

    def existing_claim(self) -> dict | None:
        if not self.claim_path.exists():
            return None
        try:
            return json.loads(self.claim_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def claim(self, config: Any, note: str = "") -> list[date]:
        """Claim the holdout for ``config`` and return its sessions.

        Idempotent for the same config; refuses a different one.

        Raises:
            HoldoutSpentError: the holdout was already used for another config.
        """
        fingerprint = config_fingerprint(config)
        prior = self.existing_claim()

        if prior is not None and prior.get("fingerprint") != fingerprint:
            raise HoldoutSpentError(
                f"The holdout has already been evaluated, on "
                f"{prior.get('claimed_at')}, for config {prior.get('fingerprint')} "
                f"({prior.get('note')}).\n"
                f"You are now asking to evaluate a DIFFERENT config "
                f"({fingerprint}). That is a second look, and it turns the holdout "
                f"into another development set -- whatever it then reports is no "
                f"longer an out-of-sample result.\n"
                f"If the first evaluation was a genuine mistake, delete\n"
                f"    {self.claim_path}\n"
                f"and record WHY in the report. Do not do it to get a better number."
            )

        if prior is None:
            self.claim_path.parent.mkdir(parents=True, exist_ok=True)
            self.claim_path.write_text(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "note": note,
                        "claimed_at": datetime.now(IST).isoformat(timespec="seconds"),
                        "holdout_from": self.split.holdout[0].isoformat(),
                        "holdout_to": self.split.holdout[-1].isoformat(),
                        "holdout_sessions": len(self.split.holdout),
                        "config": (
                            config.model_dump(mode="json")
                            if hasattr(config, "model_dump") else str(config)
                        ),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return list(self.split.holdout)

    def status(self) -> str:
        prior = self.existing_claim()
        if prior is None:
            return "HOLDOUT UNUSED -- available for exactly one evaluation."
        return (
            f"HOLDOUT SPENT on {prior.get('claimed_at')} for config "
            f"{prior.get('fingerprint')} ({prior.get('note')})."
        )

    def reset(self) -> None:
        """Discard the claim. For tests, and for a documented, explained mistake."""
        self.claim_path.unlink(missing_ok=True)
