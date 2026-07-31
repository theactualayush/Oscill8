"""
tests/test_downloader.py

Unit tests for downloader.py. LSEG's `lseg.data` module is mocked
throughout -- these tests verify our logic (chunking, column
normalization, 4H resampling, retry-then-succeed behaviour) without
requiring a live Workspace session. Live connectivity must still be
verified once on a machine with Workspace running.
"""

from __future__ import annotations

import sys
from datetime import date
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

# ---------------------------------------------------------------------
# Build a fake `lseg.data` module tree BEFORE importing downloader, so
# that `import lseg.data as ld` inside downloader's functions resolves
# to our mock instead of requiring a real Workspace connection.
# ---------------------------------------------------------------------

fake_lseg = ModuleType("lseg")
fake_lseg_data = ModuleType("lseg.data")
fake_lseg.data = fake_lseg_data
sys.modules["lseg"] = fake_lseg
sys.modules["lseg.data"] = fake_lseg_data

fake_lseg_data.open_session = MagicMock()
fake_lseg_data.close_session = MagicMock()
fake_lseg_data.get_history = MagicMock()

import config  # noqa: E402
import downloader  # noqa: E402
from config import BarInterval  # noqa: E402


@pytest.fixture(autouse=True)
def reset_session_state():
    """Ensure each test starts with a clean session flag and fresh mocks."""
    downloader._session_open = False
    fake_lseg_data.open_session.reset_mock(side_effect=True)
    fake_lseg_data.close_session.reset_mock()
    fake_lseg_data.get_history.reset_mock(side_effect=True)
    yield


def _make_lseg_df(dates: list[str], seed: float = 100.0) -> pd.DataFrame:
    """Build a fake DataFrame shaped like what lseg.data.get_history returns."""
    idx = pd.to_datetime(dates)
    n = len(dates)
    return pd.DataFrame(
        {
            "OPEN": [seed + i * 0.01 for i in range(n)],
            "HIGH": [seed + 0.05 + i * 0.01 for i in range(n)],
            "LOW": [seed - 0.05 + i * 0.01 for i in range(n)],
            "CLOSE": [seed + 0.02 + i * 0.01 for i in range(n)],
            "VOLUME": [1000 + i for i in range(n)],
        },
        index=idx,
    )


# ---------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------

def test_open_session_only_opens_once():
    downloader.open_lseg_session()
    downloader.open_lseg_session()
    assert fake_lseg_data.open_session.call_count == 1


def test_close_session_only_closes_if_open():
    downloader.close_lseg_session()  # no-op, never opened
    assert fake_lseg_data.close_session.call_count == 0

    downloader.open_lseg_session()
    downloader.close_lseg_session()
    assert fake_lseg_data.close_session.call_count == 1


# ---------------------------------------------------------------------
# Column normalization
# ---------------------------------------------------------------------

def test_normalize_columns_standard_names():
    raw = _make_lseg_df(["2026-07-01", "2026-07-02"])
    out = downloader._normalize_columns(raw)
    assert list(out.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert len(out) == 2
    assert out["Close"].iloc[0] == pytest.approx(100.02)


def test_normalize_columns_alias_names():
    raw = _make_lseg_df(["2026-07-01"]).rename(
        columns={"OPEN": "OPEN_PRC", "CLOSE": "TRDPRC_1", "VOLUME": "ACVOL_UNS"}
    )
    out = downloader._normalize_columns(raw)
    assert list(out.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]


def test_normalize_columns_missing_field_raises_clear_error():
    raw = _make_lseg_df(["2026-07-01"]).drop(columns=["CLOSE"])
    with pytest.raises(ValueError, match="Could not find a column for 'Close'"):
        downloader._normalize_columns(raw)


# ---------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------

def test_chunk_date_range_single_chunk():
    chunks = downloader._chunk_date_range(date(2026, 1, 1), date(2026, 1, 10), max_days=30)
    assert chunks == [(date(2026, 1, 1), date(2026, 1, 10))]


def test_chunk_date_range_multiple_chunks():
    chunks = downloader._chunk_date_range(date(2026, 1, 1), date(2026, 3, 1), max_days=30)
    assert len(chunks) > 1
    # chunks should be contiguous with no gaps or overlaps
    for i in range(1, len(chunks)):
        prev_end = chunks[i - 1][1]
        cur_start = chunks[i][0]
        assert (cur_start - prev_end).days == 1
    assert chunks[0][0] == date(2026, 1, 1)
    assert chunks[-1][1] == date(2026, 3, 1)


# ---------------------------------------------------------------------
# download_history: daily, single chunk
# ---------------------------------------------------------------------

def test_download_history_daily_basic():
    fake_lseg_data.get_history.return_value = _make_lseg_df(
        ["2026-07-01", "2026-07-02", "2026-07-03"]
    )
    df = downloader.download_history("SRAZ26", "DAILY", "2026-07-01", "2026-07-03")

    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 3
    assert fake_lseg_data.get_history.call_count == 1
    _, kwargs = fake_lseg_data.get_history.call_args
    assert kwargs["universe"] == "SRAZ26"
    assert kwargs["interval"] == "daily"


def test_download_history_empty_result_returns_empty_frame_not_error():
    fake_lseg_data.get_history.return_value = pd.DataFrame()
    df = downloader.download_history("SRAZ26", "DAILY", "2026-07-01", "2026-07-03")
    assert df.empty
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]


def test_download_history_invalid_date_range_raises():
    with pytest.raises(ValueError, match="must be <="):
        downloader.download_history("SRAZ26", "DAILY", "2026-07-10", "2026-07-01")


# ---------------------------------------------------------------------
# download_history: chunked hourly request
# ---------------------------------------------------------------------

def test_download_history_chunks_and_merges(monkeypatch):
    # Force small chunk size so a ~10 day hourly request splits into
    # multiple calls, then verify the results get merged correctly.
    monkeypatch.setitem(config.MAX_LOOKBACK_DAYS, BarInterval.HOURLY, 3)

    call_log = []

    def fake_get_history(universe, interval, start, end, fields):
        call_log.append((start, end))
        # one bar per call, timestamped at the chunk's start
        return _make_lseg_df([start], seed=100.0 + len(call_log))

    fake_lseg_data.get_history.side_effect = fake_get_history

    df = downloader.download_history("SRAZ26", "HOURLY", "2026-07-01", "2026-07-10")

    assert len(call_log) >= 3  # confirms chunking actually happened
    assert len(df) == len(call_log)
    assert df["Date"].is_monotonic_increasing


# ---------------------------------------------------------------------
# download_history: retry-then-succeed
# ---------------------------------------------------------------------

def test_download_history_retries_then_succeeds():
    good_df = _make_lseg_df(["2026-07-01"])
    fake_lseg_data.get_history.side_effect = [
        ConnectionError("transient network blip"),
        ConnectionError("transient network blip"),
        good_df,
    ]

    df = downloader.download_history("SRAZ26", "DAILY", "2026-07-01", "2026-07-01")
    assert len(df) == 1
    assert fake_lseg_data.get_history.call_count == 3


def test_download_history_raises_after_exhausting_retries():
    fake_lseg_data.get_history.side_effect = ConnectionError("down")
    with pytest.raises(ConnectionError):
        downloader.download_history("SRAZ26", "DAILY", "2026-07-01", "2026-07-01")
    assert fake_lseg_data.get_history.call_count == 3  # stop_after_attempt(3)


# ---------------------------------------------------------------------
# download_history: 4H resampling
# ---------------------------------------------------------------------

def test_download_history_4h_resamples_from_hourly():
    # 8 hourly bars -> should collapse into 2 four-hour bars
    hours = pd.date_range("2026-07-01 00:00", periods=8, freq="1h")
    raw = pd.DataFrame(
        {
            "OPEN": range(100, 108),
            "HIGH": [x + 1 for x in range(100, 108)],
            "LOW": [x - 1 for x in range(100, 108)],
            "CLOSE": [x + 0.5 for x in range(100, 108)],
            "VOLUME": [10] * 8,
        },
        index=hours,
    )
    fake_lseg_data.get_history.return_value = raw

    df = downloader.download_history("SRAZ26", "4H", "2026-07-01", "2026-07-01")

    assert len(df) == 2
    # First 4H bar: Open = bar 0's open, High = max of bars 0-3, Close = bar 3's close
    assert df.loc[0, "Open"] == 100
    assert df.loc[0, "High"] == 104  # max(101..104)
    assert df.loc[0, "Low"] == 99    # min(99..102)
    assert df.loc[0, "Close"] == 103.5  # bar index 3's close
    assert df.loc[0, "Volume"] == 40  # sum of 4 bars x 10

    # interval passed to LSEG should still be "hourly", never "4H"
    _, kwargs = fake_lseg_data.get_history.call_args
    assert kwargs["interval"] == "hourly"


def test_download_history_interval_accepts_enum_or_string():
    fake_lseg_data.get_history.return_value = _make_lseg_df(["2026-07-01"])
    df1 = downloader.download_history("SRAZ26", BarInterval.DAILY, "2026-07-01", "2026-07-01")
    df2 = downloader.download_history("SRAZ26", "DAILY", "2026-07-01", "2026-07-01")
    assert len(df1) == len(df2) == 1
