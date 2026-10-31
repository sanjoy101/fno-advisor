"""Append-only manual decision notes for the trading journal."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from src.market.calendar import ensure_ist, now_ist

log = logging.getLogger("fno.journal")


class DecisionNote(BaseModel):
    """A human-entered note about a taken or skipped market opportunity."""

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    symbol: str = Field(min_length=1, max_length=32)
    decision: Literal["TAKEN", "SKIPPED"]
    position_id: str | None = Field(default=None, max_length=80)
    recorded_at: datetime = Field(default_factory=now_ist)
    reference_price: float | None = Field(default=None, gt=0)
    note: str = Field(default="", max_length=1200)
    setup_tag: str = Field(default="UNSPECIFIED", max_length=64)
    habit_tags: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("recorded_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return ensure_ist(value)

    @field_validator("setup_tag")
    @classmethod
    def normalize_setup_tag(cls, value: str) -> str:
        return value.strip() or "UNSPECIFIED"

    @field_validator("habit_tags")
    @classmethod
    def normalize_habit_tags(cls, values: list[str]) -> list[str]:
        normalized = (value.strip().upper()[:32] for value in values if value.strip())
        return list(dict.fromkeys(normalized))[:6]


class JournalStore:
    """Small JSONL ledger. Corrections are new records; existing rows are untouched."""

    def __init__(self, root: Path | str) -> None:
        self.path = Path(root) / "journal" / "decisions.jsonl"
        self.advice_path = Path(root) / "journal" / "exit_advice.jsonl"

    def append(self, note: DecisionNote) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(note.model_dump_json() + "\n")
            stream.flush()

    def entries(self, limit: int = 300) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
                if payload.get("kind") not in (None, "DECISION"):
                    continue
                rows.append(DecisionNote.model_validate(payload).model_dump(mode="json"))
            except Exception as exc:  # noqa: BLE001 - keep other journal rows readable
                log.warning("skipping invalid journal row in %s: %s", self.path, exc)
        return sorted(rows, key=lambda row: row["recorded_at"], reverse=True)[:limit]

    def append_advice(self, payload: dict) -> None:
        self.advice_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"kind": "EXIT_ADVICE", **payload}
        with self.advice_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            stream.flush()

    def advice_entries(self, limit: int = 300) -> list[dict]:
        if not self.advice_path.exists():
            return []
        rows = []
        for line in self.advice_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if row.get("kind") == "EXIT_ADVICE":
                    rows.append(row)
            except json.JSONDecodeError:
                log.warning("skipping unparseable advice row in %s", self.advice_path)
        rows.sort(key=lambda row: row.get("asof", ""), reverse=True)
        return rows[:limit]
