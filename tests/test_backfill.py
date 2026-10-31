"""Tests for the historical backfill.

The valuable properties are the recovery ones: a run that dies halfway must resume
without refetching, and a bad day must land somewhere you can look at it rather than
being dropped or stored as if it were fine.

A fake history client stands in for Fyers -- no network, no credentials.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from src.data.backfill import (
    DECISION_FIELDS,
    FULL_SESSION_BARS,
    LIQUID_FNO_20,
    LIQUID_FNO_UNIVERSE,
    RETIRED_SYMBOLS,
    BackfillLedger,
    Backfiller,
    FyersHistoryClient,
    RateLimiter,
    candles_to_bars,
    dedupe_bars,
    last_completed_session,
    to_fyers_symbol,
    trim_to_session,
    validate_session_bars,
)
from src.market.calendar import IST
from src.positions.models import Bar
from src.positions.store import PositionStore

DAY = date(2026, 8, 20)  # Thursday, a trading day
DAY2 = date(2026, 8, 21)  # Friday


def session_candles(
    trade_date: date = DAY,
    *,
    count: int = FULL_SESSION_BARS,
    price: float = 1000.0,
    start_minute: int = 0,
    volume: int = 5000,
) -> list[list]:
    """Well-formed raw candles for a session, as the API would return them."""
    open_ts = datetime.combine(trade_date, time(9, 15), tzinfo=IST)
    rows = []
    for i in range(count):
        ts = open_ts + timedelta(minutes=i + start_minute)
        epoch = int(ts.astimezone(timezone.utc).timestamp())
        rows.append([epoch, price, price + 1, price - 1, price, volume])
    return rows


class FakeClient:
    """Returns canned candles and records every call."""

    def __init__(self, by_symbol=None, fail_times: int = 0, error=RuntimeError("boom")):
        self.by_symbol = by_symbol or {}
        self.calls: list[tuple[str, date, date]] = []
        self.fail_times = fail_times
        self.error = error

    def fetch(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        rows = self.by_symbol.get(symbol.upper(), [])
        return [
            r for r in rows
            if start <= datetime.fromtimestamp(r[0], tz=timezone.utc).astimezone(IST).date() <= end
        ]


@pytest.fixture
def store(tmp_path) -> PositionStore:
    return PositionStore(tmp_path)


@pytest.fixture
def no_sleep():
    """A rate limiter that records pauses instead of taking them."""
    slept: list[float] = []
    return RateLimiter(sleep=slept.append, clock=lambda: 0.0), slept


# --------------------------------------------------------------------------------
# Universe / symbols
# --------------------------------------------------------------------------------


def test_starting_universe_covers_the_requested_names():
    """21 after the Tata Motors demerger split TATAMOTORS into TMPV and TMCV."""
    assert len(LIQUID_FNO_UNIVERSE) == 21
    for name in ("RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ONGC"):
        assert name in LIQUID_FNO_UNIVERSE


@pytest.mark.parametrize(
    "raw,expected",
    [("RELIANCE", "NSE:RELIANCE-EQ"), (" infy ", "NSE:INFY-EQ"),
     ("NSE:TCS-EQ", "NSE:TCS-EQ")],
)
def test_symbol_qualification(raw, expected):
    assert to_fyers_symbol(raw) == expected


# --------------------------------------------------------------------------------
# candles_to_bars
# --------------------------------------------------------------------------------


def test_candles_convert_to_ist_bars():
    bars = candles_to_bars(session_candles(count=3))
    assert len(bars) == 3
    assert bars[0].ts == datetime.combine(DAY, time(9, 15), tzinfo=IST)
    assert bars[0].ts.utcoffset() == timedelta(hours=5, minutes=30)


def test_unparseable_candles_are_skipped_not_fatal():
    rows = session_candles(count=2) + [["bad", 1, 2, 3, 4, 5]]
    assert len(candles_to_bars(rows)) == 2


def test_candles_preserve_ohlcv():
    bars = candles_to_bars([[int(datetime.combine(DAY, time(9, 15),
                                                  tzinfo=IST).timestamp()),
                             100.5, 102.0, 99.0, 101.0, 1234]])
    b = bars[0]
    assert (b.open, b.high, b.low, b.close, b.volume) == (100.5, 102.0, 99.0, 101.0, 1234)


# --------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------


def test_a_clean_session_validates():
    report = validate_session_bars("RELIANCE", DAY, candles_to_bars(session_candles()))
    assert report.ok
    assert report.rows == FULL_SESSION_BARS
    assert report.coverage_pct == pytest.approx(100.0)


def test_empty_day_is_a_problem():
    report = validate_session_bars("RELIANCE", DAY, [])
    assert not report.ok
    assert "no bars" in report.problems[0]


def test_duplicate_timestamps_are_caught():
    bars = candles_to_bars(session_candles(count=10))
    report = validate_session_bars("RELIANCE", DAY, bars + [bars[3]])
    assert not report.ok
    assert any("duplicate" in p for p in report.problems)


def test_gap_over_five_minutes_is_caught():
    early = candles_to_bars(session_candles(count=10))
    late = candles_to_bars(session_candles(count=10, start_minute=30))
    report = validate_session_bars("RELIANCE", DAY, early + late)
    assert not report.ok
    assert any("gap of" in p for p in report.problems)
    assert report.largest_gap_minutes == pytest.approx(21.0)


def test_gap_of_exactly_five_minutes_is_tolerated():
    early = candles_to_bars(session_candles(count=5))
    late = candles_to_bars(session_candles(count=5, start_minute=9))
    report = validate_session_bars("RELIANCE", DAY, early + late)
    assert report.largest_gap_minutes == pytest.approx(5.0)
    assert report.ok


def test_ohlc_violation_is_caught_without_raising():
    """A broker payload that breaks OHLC quarantines the DAY, not the run."""
    bars = candles_to_bars(session_candles(count=5))
    bad = Bar.model_construct(
        ts=bars[-1].ts + timedelta(minutes=1),
        open=100.0, high=95.0, low=99.0, close=100.0, volume=10,
    )
    report = validate_session_bars("RELIANCE", DAY, bars + [bad])
    assert not report.ok
    assert any("OHLC sanity" in p for p in report.problems)


def test_negative_volume_is_caught():
    bars = candles_to_bars(session_candles(count=5))
    bad = Bar.model_construct(
        ts=bars[-1].ts + timedelta(minutes=1),
        open=100.0, high=101.0, low=99.0, close=100.0, volume=-5,
    )
    report = validate_session_bars("RELIANCE", DAY, bars + [bad])
    assert any("negative volume" in p for p in report.problems)


def test_bars_outside_the_session_are_caught():
    after_hours = candles_to_bars(session_candles(count=3, start_minute=400))  # ~15:55
    report = validate_session_bars("RELIANCE", DAY, after_hours)
    assert not report.ok
    assert any("outside 09:15-15:30" in p for p in report.problems)


# ================================================================================
# REGRESSION: one boundary bar must not discard a whole session
#
# 10 dates across 2020-2023 lost ~136 otherwise-complete symbol-days because the
# vendor emitted a single extra bar stamped 15:30. The module always claimed to
# reject out-of-session BARS; the validator was rejecting the DAY.
# ================================================================================


def test_trim_keeps_the_session_and_returns_the_strays():
    inside = candles_to_bars(session_candles())
    stray = candles_to_bars(session_candles(count=1, start_minute=375))  # 15:30
    keep, drop = trim_to_session(inside + stray, DAY)
    assert len(keep) == FULL_SESSION_BARS
    assert len(drop) == 1
    assert drop[0].ts.strftime("%H:%M") == "15:30"


def test_the_bar_stamped_at_the_close_is_outside():
    """15:30 covers [15:30, 15:31), which is after the session ends."""
    bar = candles_to_bars(session_candles(count=1, start_minute=375))
    keep, drop = trim_to_session(bar, DAY)
    assert keep == [] and len(drop) == 1


def test_trim_drops_pre_open_prints():
    early = candles_to_bars(session_candles(count=2, start_minute=-8))  # 09:07
    keep, drop = trim_to_session(early, DAY)
    assert len(drop) >= 1
    assert all(b.ts.strftime("%H:%M") >= "09:15" for b in keep)


def test_trim_drops_bars_from_another_date():
    other = candles_to_bars(session_candles(DAY2, count=3))
    keep, drop = trim_to_session(other, DAY)
    assert keep == [] and len(drop) == 3


def test_a_single_1530_bar_no_longer_quarantines_the_day(store, tmp_path):
    """The exact production signature from 2020-03-20 and nine other dates."""
    rows = session_candles() + session_candles(count=1, start_minute=375)
    client = FakeClient({"RELIANCE": rows})
    summary = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert summary.quarantined == 0
    assert summary.written == 1
    assert summary.trimmed_out_of_session_bars == 1
    assert len(store.load_bars("RELIANCE", DAY)) == FULL_SESSION_BARS


def test_the_trim_is_recorded_in_the_ledger(store, tmp_path):
    """Silently fixing a vendor quirk is still worth being able to find later."""
    rows = session_candles() + session_candles(count=1, start_minute=375)
    b = make_backfiller(store, FakeClient({"RELIANCE": rows}), tmp_path)
    b.run(["RELIANCE"], DAY, DAY)
    entry = b.ledger.entries()[0]
    assert entry.status == "OK"
    assert "trimmed 1 bar" in entry.reason
    assert "15:30" in entry.reason


def test_trimming_does_not_rescue_a_genuinely_broken_day(store, tmp_path):
    """A real intrasession halt still quarantines -- the gap rule is unchanged."""
    rows = (session_candles(count=100)
            + session_candles(count=100, start_minute=160)
            + session_candles(count=1, start_minute=375))
    summary = make_backfiller(store, FakeClient({"RELIANCE": rows}), tmp_path).run(
        ["RELIANCE"], DAY, DAY
    )
    assert summary.quarantined == 1


def test_a_day_that_is_entirely_out_of_window_is_empty_not_written(store, tmp_path):
    rows = session_candles(count=5, start_minute=400)
    summary = make_backfiller(store, FakeClient({"RELIANCE": rows}), tmp_path).run(
        ["RELIANCE"], DAY, DAY
    )
    assert summary.written == 0
    assert summary.empty == 1


def test_bars_from_the_wrong_date_are_caught():
    report = validate_session_bars("RELIANCE", DAY, candles_to_bars(session_candles(DAY2, count=5)))
    assert not report.ok
    assert any("not on" in p for p in report.problems)


def test_coverage_is_reported_for_a_short_day():
    report = validate_session_bars("RELIANCE", DAY, candles_to_bars(session_candles(count=100)))
    assert report.coverage_pct == pytest.approx(100 * 100 / FULL_SESSION_BARS)


# --------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------


def test_ledger_records_and_reloads(tmp_path):
    path = tmp_path / "ledger.json"
    BackfillLedger(path).record("RELIANCE", DAY, "OK", 375)
    assert BackfillLedger(path).is_done("RELIANCE", DAY) is True


def test_ledger_is_flushed_after_every_entry_not_at_the_end(tmp_path):
    """A ledger written only on clean exit is useless after a crash."""
    ledger = BackfillLedger(tmp_path / "ledger.json")
    ledger.record("RELIANCE", DAY, "OK", 375)
    lines = [l for l in ledger.path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, "not persisted immediately"
    assert json.loads(lines[0])["symbol"] == "RELIANCE"


def test_ledger_appends_rather_than_rewriting(tmp_path):
    """Rewriting a growing ledger per symbol-day is quadratic.

    A six-year backfill settles ~32,000 symbol-days; re-serialising a ~6 MB file on
    each one is hundreds of GB of writes. Appending keeps it constant time.
    """
    ledger = BackfillLedger(tmp_path / "ledger.json")
    for i in range(50):
        ledger.record(f"SYM{i}", DAY, "OK", 375)
    lines = [l for l in ledger.path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 50


def test_later_ledger_lines_supersede_earlier_ones(tmp_path):
    path = tmp_path / "ledger.json"
    first = BackfillLedger(path)
    first.record("RELIANCE", DAY, "QUARANTINED", 750, "duplicated")
    first.record("RELIANCE", DAY, "OK", 375)
    reloaded = BackfillLedger(path)
    assert reloaded.counts() == {"OK": 1}


def test_a_truncated_final_line_does_not_break_resume(tmp_path):
    """A crash mid-append leaves a partial line. Skip it; that day gets refetched."""
    path = tmp_path / "ledger.json"
    ledger = BackfillLedger(path)
    ledger.record("RELIANCE", DAY, "OK", 375)
    ledger.record("INFY", DAY, "OK", 375)
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write('{"symbol": "TCS", "trade_da')
    reloaded = BackfillLedger(path)
    assert reloaded.counts() == {"OK": 2}
    assert reloaded.is_done("RELIANCE", DAY)


def test_legacy_json_ledger_is_migrated(tmp_path):
    """The 2026 backfill wrote the old format; it must not be lost."""
    legacy = tmp_path / "ledger.json"
    legacy.write_text(json.dumps({"entries": [{
        "symbol": "RELIANCE", "trade_date": DAY.isoformat(), "status": "OK",
        "rows": 375, "reason": None, "recorded_at": "2026-08-21T00:00:00+05:30",
    }]}), encoding="utf-8")

    ledger = BackfillLedger(legacy)
    assert ledger.is_done("RELIANCE", DAY)
    assert ledger.path.suffix == ".jsonl"
    assert not legacy.exists(), "legacy file should be renamed once migrated"
    assert (tmp_path / "ledger.json.migrated").exists()


def test_ledger_is_case_insensitive_on_symbol(tmp_path):
    ledger = BackfillLedger(tmp_path / "l.json")
    ledger.record("reliance", DAY, "OK", 1)
    assert ledger.is_done("RELIANCE", DAY)


def test_ledger_counts_by_status(tmp_path):
    ledger = BackfillLedger(tmp_path / "l.json")
    ledger.record("A", DAY, "OK", 375)
    ledger.record("B", DAY, "QUARANTINED", 10, "gap")
    ledger.record("C", DAY, "EMPTY", 0)
    assert ledger.counts() == {"OK": 1, "QUARANTINED": 1, "EMPTY": 1}


def test_ledger_lists_quarantined_with_reasons(tmp_path):
    ledger = BackfillLedger(tmp_path / "l.json")
    ledger.record("B", DAY, "QUARANTINED", 10, "gap of 21 min")
    assert ledger.quarantined()[0].reason == "gap of 21 min"


def test_ledger_forget_allows_a_retry(tmp_path):
    ledger = BackfillLedger(tmp_path / "l.json")
    ledger.record("A", DAY, "QUARANTINED", 1, "x")
    ledger.forget("A", DAY)
    assert ledger.is_done("A", DAY) is False


def test_corrupt_ledger_starts_fresh_rather_than_crashing(tmp_path):
    path = tmp_path / "l.json"
    path.write_text("{not json")
    assert BackfillLedger(path).counts() == {}


# --------------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------------


def test_limiter_spaces_requests(no_sleep):
    limiter, slept = no_sleep
    limiter.acquire()
    limiter.acquire()
    assert slept and slept[0] == pytest.approx(1 / 3.0)


def test_limiter_enforces_the_per_minute_cap():
    slept: list[float] = []
    limiter = RateLimiter(max_per_second=1000, max_per_minute=3,
                          sleep=slept.append, clock=lambda: 0.0)
    for _ in range(4):
        limiter.acquire()
    assert any(s > 50 for s in slept), "should have paused for the minute window"


def test_backoff_grows_exponentially_and_is_capped():
    slept: list[float] = []
    limiter = RateLimiter(sleep=slept.append, clock=lambda: 0.0)
    delays = [limiter.backoff(i) for i in range(8)]
    assert delays[0] < delays[1] < delays[2]
    assert max(delays) <= 60.0


# --------------------------------------------------------------------------------
# Backfiller
# --------------------------------------------------------------------------------


#: A fixed "now" well after every session these tests reference, so the
#: not-yet-closed clamp does not depend on when the suite happens to run.
FIXED_NOW = datetime.combine(date(2026, 8, 25), time(16, 0), tzinfo=IST)


def make_backfiller(store, client, tmp_path, **kw):
    kw.setdefault("now", lambda: FIXED_NOW)
    return Backfiller(
        client, store,
        ledger=BackfillLedger(tmp_path / "ledger.json"),
        limiter=RateLimiter(sleep=lambda _: None, clock=lambda: 0.0),
        clock=lambda: 0.0,
        **kw,
    )


def test_backfill_writes_a_clean_session(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles()})
    summary = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert summary.written == 1
    assert len(store.load_bars("RELIANCE", DAY)) == FULL_SESSION_BARS


def test_backfill_only_requests_trading_days(store, tmp_path):
    """Saturday and Sunday must not be requested at all."""
    client = FakeClient({"RELIANCE": session_candles()})
    summary = make_backfiller(store, client, tmp_path).run(
        ["RELIANCE"], date(2026, 8, 22), date(2026, 8, 23)  # Sat + Sun
    )
    assert summary.requested_symbol_days == 0
    assert client.calls == []


def test_backfill_skips_a_listed_holiday(store, tmp_path):
    client = FakeClient({"RELIANCE": []})
    summary = make_backfiller(store, client, tmp_path).run(
        ["RELIANCE"], date(2026, 1, 26), date(2026, 1, 26)  # Republic Day
    )
    assert summary.requested_symbol_days == 0


# ---- resumability -----------------------------------------------------------


def test_backfill_is_resumable(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles()})
    first = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert first.written == 1
    calls_after_first = len(client.calls)

    second = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert second.written == 0
    assert second.skipped_already_done == 1
    assert len(client.calls) == calls_after_first, "refetched an already-settled day"


def test_resume_after_a_crash_only_fetches_what_is_missing(store, tmp_path):
    """Simulates dying after day one of two."""
    ledger = BackfillLedger(tmp_path / "ledger.json")
    ledger.record("RELIANCE", DAY, "OK", 375)

    client = FakeClient({"RELIANCE": session_candles(DAY) + session_candles(DAY2)})
    backfiller = Backfiller(
        client, store, ledger=ledger,
        limiter=RateLimiter(sleep=lambda _: None, clock=lambda: 0.0), clock=lambda: 0.0,
        now=lambda: FIXED_NOW,
    )
    summary = backfiller.run(["RELIANCE"], DAY, DAY2)
    assert summary.skipped_already_done == 1
    assert summary.written == 1
    assert store.has_bars("RELIANCE", DAY2)


def test_force_refetches_settled_days(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles()})
    b = make_backfiller(store, client, tmp_path)
    b.run(["RELIANCE"], DAY, DAY)
    summary = b.run(["RELIANCE"], DAY, DAY, force=True)
    assert summary.written == 1


def test_quarantined_days_are_not_retried_without_forget(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles(count=5)
                         + session_candles(count=5, start_minute=60)})
    b = make_backfiller(store, client, tmp_path)
    assert b.run(["RELIANCE"], DAY, DAY).quarantined == 1
    assert b.run(["RELIANCE"], DAY, DAY).skipped_already_done == 1


# ---- quarantine -------------------------------------------------------------


def test_bad_day_is_quarantined_not_stored(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles(count=5)
                         + session_candles(count=5, start_minute=60)})
    summary = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert summary.quarantined == 1
    assert summary.written == 0
    assert not store.has_bars("RELIANCE", DAY), "bad data must not reach the main store"


def test_quarantine_keeps_the_data_and_the_reason(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles(count=5)
                         + session_candles(count=5, start_minute=60)})
    make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    folder = (Path(store.root) / "quarantine" / "bars" / "symbol=RELIANCE"
              / f"trade_date={DAY:%Y-%m-%d}")
    assert folder.exists()
    reason = json.loads((folder / "reason.json").read_text())
    assert any("gap of" in p for p in reason["problems"])
    assert reason["rows"] == 10
    assert (folder / "bars.parquet").exists(), "quarantined bars must be inspectable"


def test_quarantine_is_recorded_in_the_ledger(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles(count=5)
                         + session_candles(count=5, start_minute=60)})
    b = make_backfiller(store, client, tmp_path)
    b.run(["RELIANCE"], DAY, DAY)
    entry = b.ledger.quarantined()[0]
    assert entry.symbol == "RELIANCE"
    assert "gap of" in entry.reason


def test_empty_day_is_recorded_rather_than_silently_ignored(store, tmp_path):
    client = FakeClient({"RELIANCE": []})
    b = make_backfiller(store, client, tmp_path)
    summary = b.run(["RELIANCE"], DAY, DAY)
    assert summary.empty == 1
    assert b.ledger.is_done("RELIANCE", DAY)
    assert b.ledger.entries()[0].status == "EMPTY"


# ---- retries and logging ----------------------------------------------------


def test_transient_failure_is_retried_then_succeeds(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles()}, fail_times=2)
    summary = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert summary.written == 1
    assert len(client.calls) == 3


def test_persistent_failure_abandons_the_chunk_without_killing_the_run(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles(), "INFY": session_candles()},
                        fail_times=99)
    summary = make_backfiller(store, client, tmp_path, max_attempts=2).run(
        ["RELIANCE", "INFY"], DAY, DAY
    )
    assert summary.failed_requests == 2
    assert summary.written == 0


def test_a_failing_symbol_does_not_block_the_others(store, tmp_path):
    class OneBadSymbol(FakeClient):
        def fetch(self, symbol, start, end):
            if symbol.upper() == "RELIANCE":
                raise RuntimeError("nope")
            return super().fetch(symbol, start, end)

    client = OneBadSymbol({"INFY": session_candles()})
    summary = make_backfiller(store, client, tmp_path, max_attempts=2).run(
        ["RELIANCE", "INFY"], DAY, DAY
    )
    assert summary.written == 1
    assert store.has_bars("INFY", DAY)


def test_every_request_is_logged(store, tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="fno.backfill")
    client = FakeClient({"RELIANCE": session_candles()})
    make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    text = caplog.text
    assert "request ok" in text
    assert "symbol=RELIANCE" in text
    assert "rows=" in text


def test_failures_are_logged_with_the_attempt_number(store, tmp_path, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="fno.backfill")
    client = FakeClient({"RELIANCE": session_candles()}, fail_times=1)
    make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert "request FAILED" in caplog.text
    assert "attempt=1/4" in caplog.text


# --------------------------------------------------------------------------------
# The Fyers history response itself
#
# The first live run reported "Fyers history failed (code 200)" with an empty message
# for every symbol. 200 is a SUCCESS code: the API answered fine and the range simply
# had no candles. Reporting that as a failure invents an outage.
# --------------------------------------------------------------------------------


class FakeHistoryApi:
    """Stands in for fyersModel.FyersModel. Records what it was asked for."""

    def __init__(self, response):
        self.response = response
        self.requests: list[dict] = []

    def history(self, data):
        self.requests.append(data)
        return self.response


def history_client(response):
    client = FyersHistoryClient("tok", "APP-100")
    client._client = FakeHistoryApi(response)  # no network, no credentials
    return client


def test_no_data_at_code_200_is_an_empty_series_not_a_failure(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="fno.backfill")
    client = history_client({"s": "no_data", "code": 200, "message": ""})
    assert client.fetch("RELIANCE", DAY, DAY) == []
    assert "no candles" in caplog.text
    assert "NSE:RELIANCE-EQ" in caplog.text, "the request must be in the log line"


def test_any_code_200_response_is_treated_as_success_with_no_candles():
    """The exact shape seen live: code 200, an unhelpful status, an empty message."""
    client = history_client({"s": "", "code": 200, "message": ""})
    assert client.fetch("RELIANCE", DAY, DAY) == []


def test_a_real_api_failure_still_raises_and_names_the_request():
    client = history_client({"s": "error", "code": -300,
                             "message": "Invalid symbol provided"})
    with pytest.raises(RuntimeError) as exc:
        client.fetch("TATAMOTORS", DAY, DAY)
    text = str(exc.value)
    assert "Invalid symbol provided" in text
    assert "NSE:TATAMOTORS-EQ" in text
    assert str(DAY) in text


def test_an_error_with_no_message_still_says_something():
    client = history_client({"s": "error", "code": -99, "message": ""})
    with pytest.raises(RuntimeError, match="empty message"):
        client.fetch("RELIANCE", DAY, DAY)


def test_a_non_dict_response_raises():
    client = history_client(None)
    with pytest.raises(RuntimeError, match="NoneType"):
        client.fetch("RELIANCE", DAY, DAY)


def test_ok_returns_the_candles():
    candles = session_candles(count=3)
    client = history_client({"s": "ok", "code": 200, "candles": candles})
    assert client.fetch("RELIANCE", DAY, DAY) == candles


def test_the_request_carries_one_minute_resolution_for_the_asked_range():
    client = history_client({"s": "ok", "code": 200, "candles": []})
    client.fetch("RELIANCE", DAY, DAY2)
    sent = client._client.requests[0]
    assert sent["resolution"] == "1"
    assert sent["range_from"] == DAY.isoformat()
    assert sent["range_to"] == DAY2.isoformat()


# ---- chunking ---------------------------------------------------------------


def test_long_ranges_are_split_into_chunks(store, tmp_path):
    client = FakeClient({"RELIANCE": []})
    b = make_backfiller(store, client, tmp_path, max_days_per_request=10)
    b.run(["RELIANCE"], date(2026, 6, 1), date(2026, 6, 30))
    assert len(client.calls) >= 3
    for _, start, end in client.calls:
        assert (end - start).days < 10


def test_chunks_cover_the_whole_range_without_overlap(store, tmp_path):
    client = FakeClient({"RELIANCE": []})
    b = make_backfiller(store, client, tmp_path, max_days_per_request=7)
    b.run(["RELIANCE"], date(2026, 6, 1), date(2026, 6, 30))
    ranges = sorted((s, e) for _, s, e in client.calls)
    for (_, prev_end), (next_start, _) in zip(ranges, ranges[1:]):
        assert next_start == prev_end + timedelta(days=1)


def test_multiple_symbols_and_days(store, tmp_path):
    client = FakeClient({
        "RELIANCE": session_candles(DAY) + session_candles(DAY2),
        "INFY": session_candles(DAY) + session_candles(DAY2),
    })
    summary = make_backfiller(store, client, tmp_path).run(["RELIANCE", "INFY"], DAY, DAY2)
    assert summary.written == 4
    assert store.has_bars("INFY", DAY2)


def test_summary_renders_the_counts(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles()})
    summary = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert "written 1" in summary.render()


# ================================================================================
# REGRESSION: the 2026-08-20 duplication
#
# Fyers returned every 2026-08-20 session TWICE, for every symbol -- 750 candles
# where there are 375 minutes. A single-day request reproduced it, so it is the
# vendor's store, not chunking or retry. 366 of the 375 pairs were byte-identical;
# 9 differed, and only in `open`.
# ================================================================================


def duplicated_session(trade_date: date = DAY, *, conflicts: int = 9) -> list[list]:
    """The exact production signature: a session repeated, differing only in `open`."""
    first = session_candles(trade_date)
    second = [list(row) for row in first]
    for i in range(conflicts):
        second[i * 7][1] = round(second[i * 7][1] - 1.0, 2)  # nudge `open` only
    return first + second


def test_dedupe_collapses_a_repeated_session():
    bars = candles_to_bars(duplicated_session())
    assert len(bars) == 750
    deduped, report = dedupe_bars(bars)
    assert len(deduped) == FULL_SESSION_BARS
    assert report.dropped == 375
    assert report.identical == 366
    assert report.conflicting == 9


def test_dedupe_reports_which_fields_conflicted():
    _, report = dedupe_bars(candles_to_bars(duplicated_session()))
    assert report.conflict_fields == {"open": 9}
    assert report.conflicts_decision_fields is False


def test_dedupe_is_deterministic():
    """Same response in, same bars out, every run. A replay depends on it."""
    bars = candles_to_bars(duplicated_session())
    a, ra = dedupe_bars(bars)
    b, rb = dedupe_bars(bars)
    assert a == b
    assert ra.conflict_fields == rb.conflict_fields


def test_dedupe_keeps_the_first_copy_in_response_order():
    """Which copy wins is arbitrary but must be defined, not incidental."""
    first = session_candles(count=3)
    second = [list(r) for r in first]
    second[1][1] = 999.0  # a distinguishable `open` on the second copy
    bars, report = dedupe_bars(candles_to_bars(first + second))
    assert report.conflicting == 1
    assert bars[1].open == first[1][1], "the first copy must win"


def test_dedupe_leaves_clean_data_untouched():
    bars = candles_to_bars(session_candles())
    deduped, report = dedupe_bars(bars)
    assert deduped == bars
    assert report.had_duplicates is False
    assert report.describe() == "no duplicate timestamps"


def test_duplicated_day_is_now_written_not_quarantined(store, tmp_path):
    """The end-to-end regression: this day used to land in quarantine at 750 rows."""
    client = FakeClient({"RELIANCE": duplicated_session()})
    summary = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert summary.quarantined == 0
    assert summary.written == 1
    assert summary.deduped_days == 1
    assert summary.dropped_duplicate_rows == 375
    assert len(store.load_bars("RELIANCE", DAY)) == FULL_SESSION_BARS


def test_dedupe_note_is_recorded_in_the_ledger(store, tmp_path):
    """A silently-fixed vendor defect is still a defect worth being able to find."""
    client = FakeClient({"RELIANCE": duplicated_session()})
    b = make_backfiller(store, client, tmp_path)
    b.run(["RELIANCE"], DAY, DAY)
    entry = b.ledger.entries()[0]
    assert entry.status == "OK"
    assert "duplicate" in entry.reason
    assert "open x9" in entry.reason.replace("openx9", "open x9")


def test_conflict_in_a_decision_field_still_quarantines(store, tmp_path):
    """A disagreement on close is a disagreement about the market. Do not pick one."""
    first = session_candles()
    second = [list(r) for r in first]
    second[10][4] = second[10][4] + 0.5  # close, kept inside the bar's own high/low
    client = FakeClient({"RELIANCE": first + second})
    summary = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert summary.quarantined == 1
    assert summary.written == 0
    folder = (Path(store.root) / "quarantine" / "bars" / "symbol=RELIANCE"
              / f"trade_date={DAY:%Y-%m-%d}")
    reason = json.loads((folder / "reason.json").read_text())
    assert any("decision fields" in p for p in reason["problems"])


@pytest.mark.parametrize(
    "field_index,field,delta",
    [(2, "high", +7), (3, "low", -7), (4, "close", +0.5), (5, "volume", +7)],
)
def test_every_decision_field_conflict_quarantines(store, tmp_path, field_index, field, delta):
    """Deltas keep each bar internally valid, so the conflict is what is tested."""
    first = session_candles()
    second = [list(r) for r in first]
    second[10][field_index] = second[10][field_index] + delta
    client = FakeClient({"RELIANCE": first + second})
    assert make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY).quarantined == 1


def test_no_indicator_or_rule_reads_bar_open():
    """Pins the premise that an `open`-only conflict is harmless.

    If this ever fails, DECISION_FIELDS must gain "open" and the 2026-08-20 day
    must go back to quarantine.
    """
    import inspect

    from src.positions import exit_engine, indicators, replay

    for module in (indicators, exit_engine, replay):
        source = inspect.getsource(module)
        for pattern in (".open", "bar.open", "b.open"):
            offenders = [
                line.strip()
                for line in source.splitlines()
                if pattern in line and not line.strip().startswith("#")
                and "open_" not in line and "session_open" not in line
                and "PRE_OPEN" not in line and "SESSION_OPEN" not in line
                and "openp" not in line
            ]
            assert not offenders, f"{module.__name__} reads bar.open: {offenders}"


def test_retry_returns_one_response_and_never_accumulates(store, tmp_path):
    """Explicitly rules out the retry-append hypothesis for the duplication."""
    calls = {"n": 0}

    class FailThenSucceed(FakeClient):
        def fetch(self, symbol, start, end):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("transient")
            return session_candles()

    summary = make_backfiller(store, FailThenSucceed(), tmp_path).run(["RELIANCE"], DAY, DAY)
    assert calls["n"] == 3
    assert summary.written == 1
    assert len(store.load_bars("RELIANCE", DAY)) == FULL_SESSION_BARS, \
        "two failed attempts must not add rows"


# ================================================================================
# REGRESSION: requesting a session that has not closed yet
# ================================================================================


def test_sessions_after_the_last_close_are_excluded(store, tmp_path):
    """Reproduces the original run: started 00:57 on DAY2, asked for DAY2 anyway."""
    early_on_day2 = datetime.combine(DAY2, time(0, 57), tzinfo=IST)
    client = FakeClient({"RELIANCE": session_candles(DAY)})
    b = make_backfiller(store, client, tmp_path, now=lambda: early_on_day2)
    summary = b.run(["RELIANCE"], DAY, DAY2)
    assert summary.clamped_future_sessions == 1
    assert summary.empty == 0, "an unopened session must not be booked as EMPTY"
    assert not b.ledger.is_done("RELIANCE", DAY2)
    assert b.ledger.is_done("RELIANCE", DAY), "the completed day is still fetched"


def test_a_closed_session_is_not_excluded(store, tmp_path):
    after_day2_close = datetime.combine(DAY2, time(15, 31), tzinfo=IST)
    client = FakeClient({"RELIANCE": session_candles(DAY) + session_candles(DAY2)})
    b = make_backfiller(store, client, tmp_path, now=lambda: after_day2_close)
    summary = b.run(["RELIANCE"], DAY, DAY2)
    assert summary.clamped_future_sessions == 0
    assert summary.written == 2


def test_last_completed_session_waits_for_the_close():
    before = datetime.combine(DAY, time(9, 0), tzinfo=IST)
    after = datetime.combine(DAY, time(15, 31), tzinfo=IST)
    assert last_completed_session(DAY, before) < DAY
    assert last_completed_session(DAY, after) == DAY


def test_last_completed_session_skips_weekends():
    monday = datetime.combine(date(2026, 8, 24), time(16, 0), tzinfo=IST)
    assert last_completed_session(date(2026, 8, 23), monday) == date(2026, 8, 21)


# ================================================================================
# REGRESSION: the retired TATAMOTORS ticker
# ================================================================================


def test_retired_symbol_is_reported_not_retried(store, tmp_path, caplog):
    """7 failed requests told us nothing. The ticker was gone, not the network."""
    import logging

    caplog.set_level(logging.ERROR, logger="fno.backfill")
    client = FakeClient({})
    summary = make_backfiller(store, client, tmp_path).run(["TATAMOTORS"], DAY, DAY)
    assert client.calls == [], "a known-dead ticker must not be requested at all"
    assert summary.failed_requests == 0
    assert "RETIRED" in caplog.text
    assert "TMPV" in caplog.text


def test_pre_listing_sessions_are_skipped_not_retried(store, tmp_path, caplog):
    """TMCV listed at the Tata Motors demerger; asking for 2020 burnt 4 retries a chunk."""
    import logging

    from src.data.backfill import SYMBOL_LISTED_FROM

    caplog.set_level(logging.INFO, logger="fno.backfill")
    client = FakeClient({})
    b = make_backfiller(store, client, tmp_path)
    summary = b.run(["TMCV"], date(2020, 1, 6), date(2020, 1, 10))
    assert client.calls == [], "a symbol that did not exist must not be requested"
    assert summary.failed_requests == 0
    assert summary.skipped_pre_listing == 5
    assert "not listed before" in caplog.text
    assert str(SYMBOL_LISTED_FROM["TMCV"]) in caplog.text


def test_post_listing_sessions_are_still_fetched(store, tmp_path):
    client = FakeClient({"TMCV": session_candles(DAY)})
    summary = make_backfiller(store, client, tmp_path).run(["TMCV"], DAY, DAY)
    assert summary.written == 1
    assert summary.skipped_pre_listing == 0


def test_symbols_without_a_listing_date_are_unaffected(store, tmp_path):
    client = FakeClient({"RELIANCE": session_candles(DAY)})
    summary = make_backfiller(store, client, tmp_path).run(["RELIANCE"], DAY, DAY)
    assert summary.skipped_pre_listing == 0
    assert summary.written == 1


def test_universe_no_longer_contains_the_dead_ticker():
    assert "TATAMOTORS" not in LIQUID_FNO_UNIVERSE
    assert "TMPV" in LIQUID_FNO_UNIVERSE
    assert "TMCV" in LIQUID_FNO_UNIVERSE


def test_universe_has_no_duplicates():
    assert len(set(LIQUID_FNO_UNIVERSE)) == len(LIQUID_FNO_UNIVERSE)


# ---- read-only --------------------------------------------------------------


def test_backfill_module_calls_no_order_api():
    import inspect

    import src.data.backfill as mod

    source = inspect.getsource(mod)
    for token in ("place_order", "modify_order", "cancel_order", "exit_positions"):
        assert token not in source
