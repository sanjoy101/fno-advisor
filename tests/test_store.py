"""Tests for Parquet/DuckDB persistence.

The property that matters is restart survival: a fresh store object reading from disk
must reconstruct exactly what was written, with Decimals still exact and timestamps
still IST-aware. A trailing stop that comes back as 1068.5400000000001 -- or as a
naive datetime -- is a silent corruption of the ratchet.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.market.calendar import IST
from src.positions.models import Position, PositionRuntime, PositionState
from src.positions.store import PositionStore
from tests.conftest import TRADE_DATE, flat_series, ist


@pytest.fixture
def store(tmp_path) -> PositionStore:
    return PositionStore(tmp_path)


@pytest.fixture
def saved(store, position) -> Position:
    store.save_position(position)
    return position


# --------------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------------


def test_position_round_trips_exactly(store, position):
    store.save_position(position)
    loaded = store.get_position(position.position_id, TRADE_DATE)
    assert loaded == position


def test_position_survives_a_restart(tmp_path, position):
    PositionStore(tmp_path).save_position(position)
    reopened = PositionStore(tmp_path)  # new object, cold read from disk
    assert reopened.get_position(position.position_id, TRADE_DATE) == position


def test_money_comes_back_as_exact_decimal(store):
    pos = Position(
        position_id="p-money",
        symbol="RELIANCE",
        quantity=7,
        entry_price=Decimal("1234.55"),
        entry_time=ist(9, 20),
        stop_loss=Decimal("1200.05"),
        target=Decimal("1299.95"),
    )
    store.save_position(pos)
    loaded = store.get_position("p-money", TRADE_DATE)
    assert loaded.entry_price == Decimal("1234.55")
    assert loaded.stop_loss == Decimal("1200.05")
    assert isinstance(loaded.entry_price, Decimal)


def test_timestamps_come_back_ist_aware(store, position):
    store.save_position(position)
    loaded = store.get_position(position.position_id, TRADE_DATE)
    assert loaded.entry_time.tzinfo is not None
    assert loaded.entry_time.utcoffset() == timedelta(hours=5, minutes=30)
    assert loaded.entry_time == position.entry_time


def test_optional_fields_round_trip_as_none(store, position):
    store.save_position(position)
    loaded = store.get_position(position.position_id, TRADE_DATE)
    assert loaded.stop_loss is None and loaded.target is None and loaded.notes is None


def test_saving_twice_upserts_rather_than_duplicating(store, position):
    store.save_position(position)
    store.save_position(position)
    assert len(store.list_positions(TRADE_DATE)) == 1


def test_multiple_positions_on_one_day(store, position):
    other = position.model_copy(update={"position_id": "p-2", "symbol": "INFY"})
    store.save_position(position)
    store.save_position(other)
    symbols = {p.symbol for p in store.list_positions(TRADE_DATE)}
    assert symbols == {"RELIANCE", "INFY"}


def test_missing_position_returns_none_not_an_error(store):
    assert store.get_position("nope", TRADE_DATE) is None
    assert store.list_positions(date(2026, 8, 19)) == []


def test_close_position_records_the_actual_fill(store, position):
    store.save_position(position)
    store.close_position(position.position_id, TRADE_DATE, Decimal("1012.35"), ist(14, 5))
    assert store.position_status(position.position_id, TRADE_DATE) == "CLOSED"
    assert store.list_positions(TRADE_DATE, status="OPEN") == []
    assert len(store.list_positions(TRADE_DATE, status="CLOSED")) == 1


def test_closing_an_unknown_position_raises(store):
    with pytest.raises(KeyError):
        store.close_position("ghost", TRADE_DATE, Decimal("100"), ist(14, 0))


# --------------------------------------------------------------------------------
# Runtime state -- the reason persistence exists
# --------------------------------------------------------------------------------


def test_runtime_round_trips(store, position):
    rt = PositionRuntime.initial(position)
    rt.highest_price = 1080.0
    rt.raise_stop(Decimal("1068.54"))
    rt.breakeven_armed = True
    rt.partial_advised = True
    rt.remaining_quantity = 50
    rt.last_state = PositionState.TRAIL
    rt.last_evaluated_at = ist(11, 30)
    store.save_runtime(rt, TRADE_DATE)

    loaded = store.load_runtime(position.position_id, TRADE_DATE)
    assert loaded == rt


def test_trailing_stop_is_not_reset_by_a_restart(tmp_path, position):
    """The whole point: a mid-session restart must not hand back locked-in risk."""
    rt = PositionRuntime.initial(position)
    rt.breakeven_armed = True
    rt.raise_stop(Decimal("1068.5400"))
    PositionStore(tmp_path).save_runtime(rt, TRADE_DATE)

    resumed = PositionStore(tmp_path).load_or_init_runtime(position)
    assert resumed.current_stop == Decimal("1068.5400")
    assert resumed.breakeven_armed is True
    assert resumed.current_stop != position.entry_price


def test_load_or_init_starts_fresh_when_there_is_nothing_stored(store, position):
    rt = store.load_or_init_runtime(position)
    assert rt.current_stop is None
    assert rt.highest_price == pytest.approx(float(position.entry_price))
    assert rt.remaining_quantity == position.quantity


def test_runtime_state_enum_round_trips(store, position):
    for state in PositionState:
        rt = PositionRuntime.initial(position)
        rt.last_state = state
        store.save_runtime(rt, TRADE_DATE)
        assert store.load_runtime(position.position_id, TRADE_DATE).last_state is state


def test_runtime_upserts_rather_than_appending(store, position):
    rt = PositionRuntime.initial(position)
    store.save_runtime(rt, TRADE_DATE)
    rt.highest_price = 1099.0
    store.save_runtime(rt, TRADE_DATE)
    assert store.load_runtime(position.position_id, TRADE_DATE).highest_price == 1099.0


# --------------------------------------------------------------------------------
# Advice journal
# --------------------------------------------------------------------------------


def advice_for(position, engine_cfg, bars=None):
    from src.positions.exit_engine import evaluate

    bars = bars or flat_series(1000, 30)
    rt = PositionRuntime.initial(position)
    return evaluate(
        position, rt, bars, bars[-1].close_time, bars[-1].close_time, engine_cfg=engine_cfg
    )


def test_advice_is_logged_with_gross_and_net(store, position, engine_cfg):
    store.log_advice(advice_for(position, engine_cfg))
    rows = store.read_advice(TRADE_DATE)
    assert len(rows) == 1
    assert rows[0]["gross_pnl"] is not None
    assert rows[0]["net_pnl"] is not None
    assert rows[0]["net_pnl"] < rows[0]["gross_pnl"]


def test_advice_log_is_append_only(store, position, engine_cfg):
    first = advice_for(position, engine_cfg)
    store.log_advice(first)
    store.log_advice(advice_for(position, engine_cfg))
    rows = store.read_advice(TRADE_DATE)
    assert len(rows) == 2
    assert rows[0]["reason"] == first.reason


def test_advice_reason_survives_the_round_trip(store, position, engine_cfg):
    advice = advice_for(position, engine_cfg)
    store.log_advice(advice)
    assert store.read_advice(TRADE_DATE)[0]["reason"] == advice.reason


def test_advice_indicators_are_journaled(store, position, engine_cfg):
    store.log_advice(advice_for(position, engine_cfg))
    row = store.read_advice(TRADE_DATE)[0]
    for field in ("vwap", "atr14", "ema9", "ema21", "rsi14", "volume_ratio"):
        assert row[field] is not None, field


def test_advice_can_be_filtered_by_position(store, position, engine_cfg):
    store.log_advice(advice_for(position, engine_cfg))
    other = position.model_copy(update={"position_id": "p-other"})
    store.log_advice(advice_for(other, engine_cfg))
    assert len(store.read_advice(TRADE_DATE, position_id="p-other")) == 1


def test_advice_timestamps_return_ist(store, position, engine_cfg):
    store.log_advice(advice_for(position, engine_cfg))
    asof = store.read_advice(TRADE_DATE)[0]["asof"]
    assert asof.utcoffset() == timedelta(hours=5, minutes=30)


# --------------------------------------------------------------------------------
# Bars
# --------------------------------------------------------------------------------


def test_bars_round_trip(store):
    bars = flat_series(1000, 30, spread=1.5)
    store.save_bars("RELIANCE", TRADE_DATE, bars)
    assert store.load_bars("RELIANCE", TRADE_DATE) == bars


def test_bars_are_partitioned_by_symbol_and_date(store):
    store.save_bars("RELIANCE", TRADE_DATE, flat_series(1000, 5))
    store.save_bars("INFY", TRADE_DATE, flat_series(1500, 5))
    assert store.load_bars("RELIANCE", TRADE_DATE)[0].close == 1000
    assert store.load_bars("INFY", TRADE_DATE)[0].close == 1500
    assert store.load_bars("RELIANCE", date(2026, 8, 19)) == []


def test_has_bars(store):
    assert store.has_bars("RELIANCE", TRADE_DATE) is False
    store.save_bars("RELIANCE", TRADE_DATE, flat_series(1000, 5))
    assert store.has_bars("RELIANCE", TRADE_DATE) is True


# --------------------------------------------------------------------------------
# DuckDB
# --------------------------------------------------------------------------------


def test_duckdb_queries_the_parquet_tree(store, position, engine_cfg):
    store.save_position(position)
    for _ in range(3):
        store.log_advice(advice_for(position, engine_cfg))
    rows = store.query("SELECT state, count(*) FROM advice GROUP BY 1")
    assert rows == [("HOLD", 3)]


def test_duckdb_sees_positions(store, position):
    store.save_position(position)
    rows = store.query("SELECT symbol, quantity FROM positions")
    assert rows == [("RELIANCE", 100)]


def test_duckdb_money_stays_decimal(store, position):
    store.save_position(position)
    (value,), = store.query("SELECT entry_price FROM positions")
    assert value == Decimal("1000.0000")
    assert isinstance(value, Decimal)


def test_duckdb_query_on_an_empty_store_returns_nothing_rather_than_erroring(store):
    assert store.query("SELECT * FROM advice") == []
    assert store.query("SELECT * FROM positions") == []


def test_duckdb_spans_multiple_trade_dates(store, position):
    store.save_position(position)
    older = position.model_copy(
        update={"position_id": "p-old", "entry_time": ist(9, 20, d=date(2026, 8, 19))}
    )
    store.save_position(older)
    (count,), = store.query("SELECT count(*) FROM positions")
    assert count == 2


def test_duckdb_renders_timestamps_in_ist(store, position):
    store.save_position(position)
    (hour,), = store.query("SELECT extract(hour FROM entry_time) FROM positions")
    assert hour == 9, "9:20 IST must not render as 3:50 UTC"
