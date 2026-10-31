"""Parquet + DuckDB persistence for Module C.

No database server. Positions, engine runtime state, and the advice log are Parquet
files under ``data/``, partitioned by trade date, queried with DuckDB.

Layout::

    data/positions/trade_date=YYYY-MM-DD/positions.parquet
    data/positions/trade_date=YYYY-MM-DD/runtime.parquet
    data/journal/advice/trade_date=YYYY-MM-DD/advice.parquet
    data/raw/bars/symbol=SYM/trade_date=YYYY-MM-DD/bars.parquet

Why runtime state is persisted at all: the trailing stop is a ratchet built up over a
session. If the process restarts at 14:00 and the runtime is lost, the stop resets to
its initial level and hands back risk the position had already locked in. Reloading it
is what makes a restart a non-event.

Timestamps are written tz-aware **UTC** and converted to IST on read. Parquet
round-trips UTC reliably; local-zone round-tripping is implementation-dependent.

Money is written as ``decimal128(18, 4)``, never float. A float column is how
1000.55 comes back as 1000.5500000000001 and a P&L reconciliation starts failing by a
paisa. Values are quantised to 4dp on write.

Reads that reconstruct models go through pyarrow, so types survive exactly. DuckDB is
used for the analytical queries the journal exists for -- ``store.query(...)`` runs
SQL straight over the Parquet tree.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from config.settings import get_settings
from src.market.calendar import IST, ensure_ist
from src.positions.models import (
    Bar,
    ExitAdvice,
    Position,
    PositionRuntime,
    PositionState,
    Side,
)

__all__ = ["PositionStore", "MONEY", "PRICE_SCALE"]

#: Scale used for every money column. 4dp comfortably covers NSE tick sizes.
PRICE_SCALE = 4
MONEY = pa.decimal128(18, PRICE_SCALE)
_QUANTUM = Decimal(1).scaleb(-PRICE_SCALE)
_TS = pa.timestamp("us", tz="UTC")


def _dec(value: Decimal | float | int | None) -> Decimal | None:
    """Quantise to the money scale, or None. Floats go via str to stay exact."""
    if value is None:
        return None
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return d.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def _utc(value: datetime | None) -> datetime | None:
    """Normalise to UTC for storage. Rejects naive datetimes via ensure_ist."""
    if value is None:
        return None
    return ensure_ist(value).astimezone(timezone.utc)


def _ist(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(IST)


POSITION_SCHEMA = pa.schema(
    [
        ("position_id", pa.string()),
        ("symbol", pa.string()),
        ("quantity", pa.int64()),
        ("entry_price", MONEY),
        ("entry_time", _TS),
        ("stop_loss", MONEY),
        ("target", MONEY),
        ("side", pa.string()),
        ("segment", pa.string()),
        ("notes", pa.string()),
        ("status", pa.string()),
        ("exit_price", MONEY),
        ("exit_time", _TS),
    ]
)

RUNTIME_SCHEMA = pa.schema(
    [
        ("position_id", pa.string()),
        ("highest_price", pa.float64()),
        ("current_stop", MONEY),
        ("breakeven_armed", pa.bool_()),
        ("partial_advised", pa.bool_()),
        ("entry_checked", pa.bool_()),
        ("entry_verdict", pa.string()),
        ("remaining_quantity", pa.int64()),
        ("last_state", pa.string()),
        ("last_evaluated_at", _TS),
    ]
)

ADVICE_SCHEMA = pa.schema(
    [
        ("asof", _TS),
        ("position_id", pa.string()),
        ("symbol", pa.string()),
        ("state", pa.string()),
        ("reason", pa.string()),
        ("last_price", pa.float64()),
        ("stop_price", MONEY),
        ("target1", MONEY),
        ("suggested_exit_quantity", pa.int64()),
        ("gross_pnl", MONEY),
        ("net_pnl", MONEY),
        ("costs_total", MONEY),
        ("gross_pct", MONEY),
        ("net_pct", MONEY),
        ("feed_stale", pa.bool_()),
        ("stale_seconds", pa.float64()),
        ("vwap", pa.float64()),
        ("atr14", pa.float64()),
        ("ema9", pa.float64()),
        ("ema21", pa.float64()),
        ("rsi14", pa.float64()),
        ("volume_ratio", pa.float64()),
    ]
)

BAR_SCHEMA = pa.schema(
    [
        ("ts", _TS),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
    ]
)


class PositionStore:
    """Parquet-backed store for positions, runtime state, and the advice journal."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else get_settings().data_dir
        self.positions_dir = self.root / "positions"
        self.advice_dir = self.root / "journal" / "advice"
        self.bars_dir = self.root / "raw" / "bars"

    # ---- paths ----------------------------------------------------------------

    def _pos_path(self, trade_date: date) -> Path:
        return self.positions_dir / f"trade_date={trade_date:%Y-%m-%d}" / "positions.parquet"

    def _runtime_path(self, trade_date: date) -> Path:
        return self.positions_dir / f"trade_date={trade_date:%Y-%m-%d}" / "runtime.parquet"

    def _advice_path(self, trade_date: date) -> Path:
        return self.advice_dir / f"trade_date={trade_date:%Y-%m-%d}" / "advice.parquet"

    def _bars_path(self, symbol: str, trade_date: date) -> Path:
        return (
            self.bars_dir
            / f"symbol={symbol.upper()}"
            / f"trade_date={trade_date:%Y-%m-%d}"
            / "bars.parquet"
        )

    @staticmethod
    def _write(path: Path, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows, schema=schema)
        pq.write_table(table, path)

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return pq.read_table(path).to_pylist()

    # ---- positions ------------------------------------------------------------

    def save_position(self, position: Position, *, status: str = "OPEN") -> None:
        """Insert or replace a position in its trade date's file.

        Rewrites the whole (small) day file. That is an upsert of one logical row,
        not a mutation of history: closed positions keep their recorded entry, and
        the advice journal is separate and never rewritten in place.
        """
        rows = self._read(self._pos_path(position.trade_date))
        rows = [r for r in rows if r["position_id"] != position.position_id]
        rows.append(
            {
                "position_id": position.position_id,
                "symbol": position.symbol,
                "quantity": position.quantity,
                "entry_price": _dec(position.entry_price),
                "entry_time": _utc(position.entry_time),
                "stop_loss": _dec(position.stop_loss),
                "target": _dec(position.target),
                "side": position.side.value,
                "segment": position.segment,
                "notes": position.notes,
                "status": status,
                "exit_price": None,
                "exit_time": None,
            }
        )
        rows.sort(key=lambda r: r["entry_time"])
        self._write(self._pos_path(position.trade_date), rows, POSITION_SCHEMA)

    @staticmethod
    def _to_position(row: dict[str, Any]) -> Position:
        return Position(
            position_id=row["position_id"],
            symbol=row["symbol"],
            quantity=row["quantity"],
            entry_price=row["entry_price"],
            entry_time=_ist(row["entry_time"]),
            stop_loss=row["stop_loss"],
            target=row["target"],
            side=Side(row["side"]),
            segment=row["segment"],
            notes=row["notes"],
        )

    def list_positions(
        self,
        trade_date: date,
        *,
        status: str | None = "OPEN",
    ) -> list[Position]:
        """Positions recorded on ``trade_date``, optionally filtered by status."""
        rows = self._read(self._pos_path(trade_date))
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        return [self._to_position(r) for r in rows]

    def clear_positions(self, trade_date: date, *, position_ids: list[str] | None = None
                        ) -> int:
        """Delete recorded positions for a trade date. Returns how many were removed.

        This is a DELETION, not a close. A closed position keeps its recorded entry and
        exit and stays part of the record; a cleared one is gone. It exists for
        cleaning up test or mistaken entries, and it also drops the matching runtime
        rows so a cleared position cannot leave a trailing stop behind to be resumed
        against a position that no longer exists.

        Positions are decision records, not raw market data, so removing them does not
        violate the append-only rule on ``data/raw`` -- but it is still destructive and
        every caller must confirm it explicitly.
        """
        pos_path = self._pos_path(trade_date)
        rows = self._read(pos_path)
        if not rows:
            return 0

        keep = ([r for r in rows if r["position_id"] not in set(position_ids)]
                if position_ids is not None else [])
        removed = len(rows) - len(keep)
        if not removed:
            return 0

        if keep:
            self._write(pos_path, keep, POSITION_SCHEMA)
        else:
            pos_path.unlink(missing_ok=True)

        gone = ({r["position_id"] for r in rows} - {r["position_id"] for r in keep})
        rt_path = self._runtime_path(trade_date)
        rt_rows = [r for r in self._read(rt_path) if r["position_id"] not in gone]
        if rt_rows:
            self._write(rt_path, rt_rows, RUNTIME_SCHEMA)
        else:
            rt_path.unlink(missing_ok=True)
        return removed

    def get_position(self, position_id: str, trade_date: date) -> Position | None:
        for row in self._read(self._pos_path(trade_date)):
            if row["position_id"] == position_id:
                return self._to_position(row)
        return None

    def position_status(self, position_id: str, trade_date: date) -> str | None:
        for row in self._read(self._pos_path(trade_date)):
            if row["position_id"] == position_id:
                return row["status"]
        return None

    def close_position(
        self,
        position_id: str,
        trade_date: date,
        exit_price: Decimal | float,
        exit_time: datetime,
    ) -> None:
        """Mark a position closed with the price the user ACTUALLY filled at.

        Record the real fill, not the price the engine advised at. The gap between
        the two is real slippage, and recording it is the only way the modelled
        slippage assumption in the cost config ever gets checked against reality.

        Raises:
            KeyError: if the position is not on file for that date.
        """
        path = self._pos_path(trade_date)
        rows = self._read(path)
        for row in rows:
            if row["position_id"] == position_id:
                row["status"] = "CLOSED"
                row["exit_price"] = _dec(exit_price)
                row["exit_time"] = _utc(exit_time)
                self._write(path, rows, POSITION_SCHEMA)
                return
        raise KeyError(f"no position {position_id!r} on {trade_date}")

    # ---- runtime state --------------------------------------------------------

    def save_runtime(self, runtime: PositionRuntime, trade_date: date) -> None:
        """Persist engine state so a restart does not reset the trail."""
        path = self._runtime_path(trade_date)
        rows = [r for r in self._read(path) if r["position_id"] != runtime.position_id]
        rows.append(
            {
                "position_id": runtime.position_id,
                "highest_price": runtime.highest_price,
                "current_stop": _dec(runtime.current_stop),
                "breakeven_armed": runtime.breakeven_armed,
                "partial_advised": runtime.partial_advised,
                "entry_checked": runtime.entry_checked,
                "entry_verdict": runtime.entry_verdict,
                "remaining_quantity": runtime.remaining_quantity,
                "last_state": None if runtime.last_state is None else runtime.last_state.value,
                "last_evaluated_at": _utc(runtime.last_evaluated_at),
            }
        )
        self._write(path, rows, RUNTIME_SCHEMA)

    def load_runtime(self, position_id: str, trade_date: date) -> PositionRuntime | None:
        for row in self._read(self._runtime_path(trade_date)):
            if row["position_id"] == position_id:
                return PositionRuntime(
                    position_id=row["position_id"],
                    highest_price=row["highest_price"],
                    current_stop=row["current_stop"],
                    breakeven_armed=row["breakeven_armed"],
                    partial_advised=row["partial_advised"],
                    # Older runtime files predate the entry check; absent means
                    # "never checked", which is the correct default.
                    entry_checked=bool(row.get("entry_checked") or False),
                    entry_verdict=row.get("entry_verdict"),
                    remaining_quantity=row["remaining_quantity"],
                    last_state=(
                        None if row["last_state"] is None else PositionState(row["last_state"])
                    ),
                    last_evaluated_at=_ist(row["last_evaluated_at"]),
                )
        return None

    def load_or_init_runtime(self, position: Position) -> PositionRuntime:
        """Resume a position's engine state, or start fresh if it has none."""
        existing = self.load_runtime(position.position_id, position.trade_date)
        return existing if existing is not None else PositionRuntime.initial(position)

    # ---- advice journal -------------------------------------------------------

    def log_advice(self, advice: ExitAdvice) -> None:
        """Append one evaluation to the day's advice journal.

        Append-only: existing rows are never modified, only added to. This is the
        record that lets a decision be re-examined later -- what the engine said, on
        what indicator values, and what the P&L was at that moment.
        """
        trade_date = advice.asof.astimezone(IST).date()
        path = self._advice_path(trade_date)
        rows = self._read(path)
        pnl = advice.pnl
        snap = advice.indicators
        rows.append(
            {
                "asof": _utc(advice.asof),
                "position_id": advice.position_id,
                "symbol": advice.symbol,
                "state": advice.state.value,
                "reason": advice.reason,
                "last_price": advice.last_price,
                "stop_price": _dec(advice.stop_price),
                "target1": _dec(advice.target1),
                "suggested_exit_quantity": advice.suggested_exit_quantity,
                "gross_pnl": None if pnl is None else _dec(pnl.gross_pnl),
                "net_pnl": None if pnl is None else _dec(pnl.net_pnl),
                "costs_total": None if pnl is None else _dec(pnl.costs.total),
                "gross_pct": None if pnl is None else _dec(pnl.gross_pct),
                "net_pct": None if pnl is None else _dec(pnl.net_pct),
                "feed_stale": advice.feed_stale,
                "stale_seconds": advice.stale_seconds,
                "vwap": None if snap is None else snap.vwap,
                "atr14": None if snap is None else snap.atr14,
                "ema9": None if snap is None else snap.ema9,
                "ema21": None if snap is None else snap.ema21,
                "rsi14": None if snap is None else snap.rsi14,
                "volume_ratio": None if snap is None else snap.volume_ratio,
            }
        )
        self._write(path, rows, ADVICE_SCHEMA)

    def read_advice(
        self,
        trade_date: date,
        *,
        position_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Advice rows for a date, ascending, with IST timestamps."""
        rows = self._read(self._advice_path(trade_date))
        if position_id is not None:
            rows = [r for r in rows if r["position_id"] == position_id]
        for r in rows:
            r["asof"] = _ist(r["asof"])
        return sorted(rows, key=lambda r: r["asof"])

    # ---- bars -----------------------------------------------------------------

    def save_bars(self, symbol: str, trade_date: date, bars: Sequence[Bar]) -> None:
        """Persist a session's 1-minute bars, for replay and for indicator warm-up."""
        rows = [
            {
                "ts": _utc(b.ts),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]
        self._write(self._bars_path(symbol, trade_date), rows, BAR_SCHEMA)

    def load_bars(self, symbol: str, trade_date: date) -> list[Bar]:
        """Load a session's bars, ascending, with IST timestamps."""
        rows = self._read(self._bars_path(symbol, trade_date))
        bars = [
            Bar(
                ts=_ist(r["ts"]),
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
            for r in rows
        ]
        return sorted(bars, key=lambda b: b.ts)

    def has_bars(self, symbol: str, trade_date: date) -> bool:
        return self._bars_path(symbol, trade_date).exists()

    # ---- DuckDB ---------------------------------------------------------------

    def connect(self) -> duckdb.DuckDBPyConnection:
        """A DuckDB connection with IST as the session timezone.

        Without the timezone set, TIMESTAMPTZ columns render in whatever zone the
        machine happens to be in, which makes a query result look like a different
        time of day than the journal it came from.
        """
        con = duckdb.connect()
        con.execute("SET TimeZone='Asia/Kolkata'")
        return con

    def query(self, sql: str, params: Iterable[Any] | None = None) -> list[tuple]:
        """Run SQL over the Parquet tree.

        Views registered for you: ``positions``, ``runtime``, ``advice``. They glob
        every trade date, so ``SELECT state, count(*) FROM advice GROUP BY 1`` works
        across the whole history without loading anything into memory first.

        Gotcha: ``asof`` is a DuckDB keyword (ASOF JOIN), so it must be quoted --
        ``min("asof")``, not ``min(asof)``, which is a parse error. Same for ``first``
        and ``last`` as column aliases.
        """
        con = self.connect()
        try:
            self._register_views(con)
            return con.execute(sql, list(params) if params else None).fetchall()
        finally:
            con.close()

    def _register_views(self, con: duckdb.DuckDBPyConnection) -> None:
        sources = {
            "positions": (self.positions_dir, "*/positions.parquet", POSITION_SCHEMA),
            "runtime": (self.positions_dir, "*/runtime.parquet", RUNTIME_SCHEMA),
            "advice": (self.advice_dir, "*/advice.parquet", ADVICE_SCHEMA),
        }
        for name, (base, pattern, schema) in sources.items():
            if not base.exists() or not any(base.glob(pattern)):
                # An empty table with the REAL schema, not a missing one. A query on a
                # day with no data should return no rows -- not "table not found", and
                # not "column state does not exist" either.
                con.register(f"_empty_{name}", pa.Table.from_pylist([], schema=schema))
                con.execute(f"CREATE VIEW {name} AS SELECT * FROM _empty_{name}")
                continue
            # CREATE VIEW cannot take a prepared parameter in DuckDB, so the path is
            # inlined. It is built from self.root and fixed literals -- never from
            # user input -- and single quotes are escaped regardless.
            glob = str(base / pattern).replace("\\", "/").replace("'", "''")
            con.execute(
                f"CREATE VIEW {name} AS "
                f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
            )
