"""India VIX: the publication-lag boundary, and the one mis-stamped file in the archive.

The store had no tests. It acquired these when the F&O feature work found that
``ind_close_all_10042023.csv`` -- the file NSE serves at the 2023-04-10 URL -- carries an
internal ``Index Date`` of 2023-10-04, and was silently overwriting a genuine session.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.data.india_vix import (
    VIX_INDEX_NAME,
    VixRow,
    VixStore,
    asof_row,
    date_from_filename,
    parse_index_close,
)

HEADER = ("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
          "Closing Index Value,Points Change,Change(%)\n")


def csv_bytes(stamped: str, close: float, *, name: str = "India VIX") -> bytes:
    return (HEADER
            + f"Nifty 50,{stamped},100,101,99,100.5,0.5,0.5\n"
            + f"{name},{stamped},11.0,12.0,10.0,{close},-0.07,-0.57\n"
            ).encode("utf-8")


# --------------------------------------------------------------------------------
# The publication lag
# --------------------------------------------------------------------------------


def test_asof_row_is_strictly_before_the_decision_date():
    """The file for session D publishes AFTER the close of D.

    ``<=`` here would hand an intraday decision the closing volatility of the session it
    is still inside. That is the whole reason this accessor exists.
    """
    rows = {
        date(2024, 1, 10): VixRow(date(2024, 1, 10), 11.0, 12.0, 10.0, 11.5),
        date(2024, 1, 11): VixRow(date(2024, 1, 11), 12.0, 13.0, 11.0, 12.5),
    }
    assert asof_row(rows, date(2024, 1, 11)).trade_date == date(2024, 1, 10)
    assert asof_row(rows, date(2024, 1, 12)).trade_date == date(2024, 1, 11)
    assert asof_row(rows, date(2024, 1, 10)) is None


def test_asof_row_does_not_reach_forward_when_nothing_precedes():
    rows = {date(2024, 1, 10): VixRow(date(2024, 1, 10), 11.0, 12.0, 10.0, 11.5)}
    assert asof_row(rows, date(2024, 1, 1)) is None


# --------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------


def test_parse_picks_the_india_vix_row():
    row = parse_index_close(csv_bytes("19-08-2026", 11.32))
    assert row == VixRow(date(2026, 8, 19), 11.0, 12.0, 10.0, 11.32)


def test_parse_returns_none_when_the_file_carries_no_vix_row():
    """A fact about the date, not an error -- and distinct from 'no file'."""
    assert parse_index_close(csv_bytes("19-08-2026", 11.32, name="Nifty Bank")) is None


def test_parse_does_not_fuzzily_match_a_similar_index_name():
    """"India VIX" must not silently match some future "India VIX 30"."""
    assert parse_index_close(csv_bytes("19-08-2026", 11.32, name="India VIX 30")) is None
    assert VIX_INDEX_NAME == "india vix"


def test_parse_rejects_an_unrecognised_header():
    with pytest.raises(ValueError, match="unrecognised index-close header"):
        parse_index_close(b"Some,Other,Columns\n1,2,3\n")


# --------------------------------------------------------------------------------
# The mis-stamped file
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("ind_close_all_04102023.csv", date(2023, 10, 4)),
    ("ind_close_all_10042023.csv", date(2023, 4, 10)),
    ("ind_close_all_01012020.csv", date(2020, 1, 1)),
    ("something_else.csv", None),
    ("ind_close_all_notadate.csv", None),
    ("ind_close_all_99999999.csv", None),
])
def test_date_from_filename(name, expected):
    assert date_from_filename(name) == expected


def test_a_us_order_stamp_does_not_overwrite_a_genuine_session(tmp_path):
    """The real defect, reproduced with the real filenames and the real values.

    Both files' ``Index Date`` columns parse to 2023-10-04. The April one sorts later, so
    before the fix it won: 2023-10-04 took April's close of 12.27 instead of its own
    11.66, and 2023-04-10 disappeared from the series with no error anywhere.

    Keying by filename keeps BOTH sessions and gives each its own data.
    """
    store = VixStore(tmp_path)
    year = store.raw_dir / "2023"
    year.mkdir(parents=True)
    # Genuine 4-October file: name and internal stamp agree.
    (year / "ind_close_all_04102023.csv").write_bytes(csv_bytes("04-10-2023", 11.66))
    # The file served at the 10-April URL, stamped in US order as 04-10-2023.
    (year / "ind_close_all_10042023.csv").write_bytes(csv_bytes("04-10-2023", 12.27))

    rows = store.load_all()

    assert len(rows) == 2, "a session was lost to the collision"
    assert rows[date(2023, 10, 4)].close == 11.66
    assert rows[date(2023, 4, 10)].close == 12.27
    assert rows[date(2023, 4, 10)].trade_date == date(2023, 4, 10), (
        "the row must be restamped to the session it belongs to, not left inconsistent"
    )


def test_ordering_cannot_decide_the_winner(tmp_path):
    """The original bug was decided by sort order. Writing the files in the opposite
    order must now produce the identical result."""
    store = VixStore(tmp_path)
    year = store.raw_dir / "2023"
    year.mkdir(parents=True)
    (year / "ind_close_all_10042023.csv").write_bytes(csv_bytes("04-10-2023", 12.27))
    (year / "ind_close_all_04102023.csv").write_bytes(csv_bytes("04-10-2023", 11.66))
    rows = store.load_all()
    assert rows[date(2023, 10, 4)].close == 11.66
    assert rows[date(2023, 4, 10)].close == 12.27


def test_a_consistent_file_is_kept(tmp_path):
    """Guard against the fix throwing away good data."""
    store = VixStore(tmp_path)
    year = store.raw_dir / "2024"
    year.mkdir(parents=True)
    (year / "ind_close_all_15012024.csv").write_bytes(csv_bytes("15-01-2024", 14.2))
    rows = store.load_all()
    assert rows[date(2024, 1, 15)].close == 14.2


def test_the_real_archive_has_exactly_the_three_documented_us_order_files():
    """Pins the claim made in the module docstring against the data on disk.

    Skips when the backfill has not been run, so a fresh clone is not failed for
    missing data it was never given.
    """
    from pathlib import Path

    from config.settings import get_settings

    store = VixStore(Path(get_settings().data_dir))
    paths = sorted(store.raw_dir.rglob("ind_close_all_*.csv"))
    if len(paths) < 100:
        pytest.skip("India VIX archive not backfilled on this machine")

    mismatched = {}
    for path in paths:
        row = parse_index_close(path.read_bytes())
        stamped = date_from_filename(path.name)
        if row is not None and stamped is not None and stamped != row.trade_date:
            mismatched[path.name] = (stamped, row.trade_date)

    assert mismatched == {
        "ind_close_all_06042023.csv": (date(2023, 4, 6), date(2023, 6, 4)),
        "ind_close_all_10042023.csv": (date(2023, 4, 10), date(2023, 10, 4)),
        "ind_close_all_11042023.csv": (date(2023, 4, 11), date(2023, 11, 4)),
    }, mismatched

    # Every one is a day/month swap, which is what identifies it as a format quirk
    # rather than the wrong file being served.
    for stamped, internal in mismatched.values():
        assert (stamped.day, stamped.month) == (internal.month, internal.day)


def test_the_real_archive_loses_no_session_to_the_collision():
    """1640 files must yield 1640 rows. The original bug showed up as 1639."""
    from pathlib import Path

    from config.settings import get_settings

    store = VixStore(Path(get_settings().data_dir))
    paths = sorted(store.raw_dir.rglob("ind_close_all_*.csv"))
    if len(paths) < 100:
        pytest.skip("India VIX archive not backfilled on this machine")

    rows = store.load_all()
    assert len(rows) == len(paths), (
        f"{len(paths)} files produced {len(rows)} rows -- a session was overwritten"
    )
    # The two sessions the collision used to destroy.
    assert rows[date(2023, 10, 4)].close == 11.66
    assert rows[date(2023, 4, 10)].close == 12.27
