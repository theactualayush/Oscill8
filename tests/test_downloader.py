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

import numpy as np
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

from core import config
from core import downloader
from core.config import BarInterval  # noqa: E402


@pytest.fixture(autouse=True)
def reset_session_state():
    """Ensure each test starts with a clean session flag and fresh mocks."""
    downloader._session_open = False
    fake_lseg_data.open_session.reset_mock(side_effect=True)
    fake_lseg_data.close_session.reset_mock()
    fake_lseg_data.get_history.reset_mock(side_effect=True)
    # Default to a healthy session state; individual tests override this
    # return_value to simulate a non-open state.
    fake_lseg_data.open_session.return_value = SimpleNamespace(open_state="OpenState.Opened")
    yield


def _make_ld_error(message: str) -> Exception:
    """Build an exception shaped exactly like the real lseg.data._errors.
    LDError observed in live Module 5B validation: module
    "lseg.data._errors", class name "LDError", a `.message` attribute,
    `.code` is None, and `.args` is empty -- confirmed empirically (see
    the Module 5B.1 design review) rather than assumed. No real
    lseg.data import needed to construct this -- __module__ is set
    directly on the locally-defined class."""

    class LDError(Exception):
        def __init__(self, msg: str):
            super().__init__()
            self.message = msg
            self.code = None

    LDError.__module__ = "lseg.data._errors"
    return LDError(message)


_UNIVERSE_NOT_FOUND_MESSAGE = (
    "No data to return, please check errors: ERROR: No successful response. "
    "(TS.Interday.UserRequestError.70005, The universe is not found)"
)


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


def test_open_session_logs_success_when_state_is_opened(mocker):
    mock_logger = mocker.patch.object(downloader, "logger")
    downloader.open_lseg_session()

    assert any(
        call.args and "opened successfully" in call.args[0]
        for call in mock_logger.info.call_args_list
    )
    assert mock_logger.warning.call_count == 0


def test_open_session_logs_warning_when_state_is_not_opened(mocker):
    fake_lseg_data.open_session.return_value = SimpleNamespace(open_state="OpenState.Closed")
    mock_logger = mocker.patch.object(downloader, "logger")

    downloader.open_lseg_session()

    assert mock_logger.warning.call_count == 1
    assert not any(
        call.args and "opened successfully" in call.args[0]
        for call in mock_logger.info.call_args_list
    )
    # Idempotency is unaffected: open_session() itself didn't raise, so we
    # still shouldn't call it again on the next download.
    assert downloader._session_open is True


def test_open_session_logs_success_when_state_unavailable(mocker):
    # Some session objects may not expose open_state at all -- fall back
    # to treating "no exception raised" as success rather than warning.
    fake_lseg_data.open_session.return_value = object()
    mock_logger = mocker.patch.object(downloader, "logger")

    downloader.open_lseg_session()

    assert mock_logger.warning.call_count == 0
    assert any(
        call.args and "opened successfully" in call.args[0]
        for call in mock_logger.info.call_args_list
    )


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


def test_normalize_columns_srax26_live_field_names():
    # Exact field names observed from a live LSEG Workspace pull for
    # SRAZ26 -- it rejects the generic OPEN/HIGH/LOW/CLOSE/VOLUME names.
    raw = _make_lseg_df(["2026-01-01", "2026-01-02"]).rename(
        columns={
            "OPEN": "OPEN_PRC",
            "HIGH": "HIGH_1",
            "LOW": "LOW_1",
            "CLOSE": "TRDPRC_1",
            "VOLUME": "ACVOL_UNS",
        }
    )
    out = downloader._normalize_columns(raw)
    assert list(out.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert len(out) == 2


def test_normalize_columns_missing_field_raises_clear_error():
    raw = _make_lseg_df(["2026-07-01"]).drop(columns=["CLOSE"])
    with pytest.raises(ValueError, match="Could not find a column for 'Close'"):
        downloader._normalize_columns(raw)


def test_normalize_columns_output_is_plain_float64():
    # Regression: every OHLCV column must be plain numpy float64, never a
    # pandas nullable extension dtype (which would carry pd.NA through to
    # database.cache.insert_bars and crash it).
    raw = _make_lseg_df(["2026-07-01", "2026-07-02"])
    out = downloader._normalize_columns(raw)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert out[col].dtype == np.float64
        assert not pd.api.types.is_extension_array_dtype(out[col])


def test_normalize_columns_pd_na_becomes_np_nan_float64():
    # Regression for the real HOURLY SRAZ26 bug: a thin intraday bar can
    # legitimately have a missing OHLC field. If LSEG (or pandas) hands
    # that back via a nullable extension dtype, the value arrives as
    # pd.NA -- normalization must convert it to np.nan in a plain
    # float64 column, not pass pd.NA through untouched.
    raw = _make_lseg_df(["2026-07-01", "2026-07-02"])
    raw["OPEN"] = pd.array([raw["OPEN"].iloc[0], pd.NA], dtype="Float64")

    out = downloader._normalize_columns(raw)

    assert out["Open"].dtype == np.float64
    assert np.isnan(out["Open"].iloc[1])
    assert not isinstance(out["Open"].iloc[1], type(pd.NA))


def test_normalize_columns_preserves_missing_value_does_not_fill_or_drop():
    raw = _make_lseg_df(["2026-07-01", "2026-07-02"])
    raw["OPEN"] = pd.array([raw["OPEN"].iloc[0], pd.NA], dtype="Float64")

    out = downloader._normalize_columns(raw)

    assert len(out) == 2  # the bar with a missing Open is not dropped
    assert np.isnan(out["Open"].iloc[1])  # not filled with a fabricated value
    assert not np.isnan(out["Close"].iloc[1])  # other fields on that bar are untouched


# ---------------------------------------------------------------------
# Column normalization: DAILY SETTLE -> Close fallback
#
# Live LSEG testing found SONIA (SONU6) returns TRDPRC_1/OPEN_PRC/
# HIGH_1/LOW_1/BID/ASK entirely NA at DAILY, but SETTLE populated with
# the real daily price. SOFR/Fed Funds have both TRDPRC_1 and SETTLE
# populated (and they can differ slightly) -- their existing
# TRDPRC_1-derived Close must be completely unaffected.
# ---------------------------------------------------------------------

def _make_sonia_like_df(dates: list[str], settle: list[float]) -> pd.DataFrame:
    """Shaped like the live SONU6 DAILY response: TRDPRC_1/OPEN_PRC/
    HIGH_1/LOW_1 entirely NA, SETTLE populated."""
    idx = pd.to_datetime(dates)
    n = len(dates)
    return pd.DataFrame(
        {
            "OPEN_PRC": [float("nan")] * n,
            "HIGH_1": [float("nan")] * n,
            "LOW_1": [float("nan")] * n,
            "TRDPRC_1": [float("nan")] * n,
            "ACVOL_UNS": [0] * n,
            "SETTLE": settle,
        },
        index=idx,
    )


def test_normalize_columns_settle_fills_missing_daily_close():
    raw = _make_sonia_like_df(["2026-07-02", "2026-07-03"], [96.235, 96.240])
    out = downloader._normalize_columns(raw, settle_fallback_for_close=True)

    assert out["Close"].tolist() == pytest.approx([96.235, 96.240])
    assert out["Close"].dtype == np.float64


def test_normalize_columns_settle_fallback_does_not_override_populated_close():
    # SOFR-like: TRDPRC_1 and SETTLE both populated and slightly
    # different -- existing TRDPRC_1-derived Close must win, unchanged.
    raw = _make_lseg_df(["2026-07-06", "2026-07-09"]).rename(columns={"CLOSE": "TRDPRC_1"})
    raw["TRDPRC_1"] = [96.135, 96.120]
    raw["SETTLE"] = [96.130, 96.125]

    out = downloader._normalize_columns(raw, settle_fallback_for_close=True)

    assert out["Close"].tolist() == pytest.approx([96.135, 96.120])


def test_normalize_columns_settle_fallback_partial_row_missingness():
    # Row-wise: some rows have a populated primary Close, others don't --
    # each row is resolved independently, not an all-or-nothing choice.
    raw = _make_lseg_df(["2026-07-01", "2026-07-02", "2026-07-03"]).rename(
        columns={"CLOSE": "TRDPRC_1"}
    )
    raw["TRDPRC_1"] = [100.02, float("nan"), 100.04]
    raw["SETTLE"] = [200.0, 96.240, 200.0]

    out = downloader._normalize_columns(raw, settle_fallback_for_close=True)

    assert out["Close"].tolist() == pytest.approx([100.02, 96.240, 100.04])


def test_normalize_columns_settle_fallback_disabled_leaves_close_nan():
    # Intraday path: even if SETTLE is present, it must never be
    # consulted unless settle_fallback_for_close=True is explicitly
    # passed (i.e. never for HOURLY/4H).
    raw = _make_sonia_like_df(["2026-07-02"], [96.235])
    out = downloader._normalize_columns(raw)  # default False

    assert np.isnan(out["Close"].iloc[0])


def test_normalize_columns_settle_fallback_no_settle_column_present():
    # Absence of both the primary Close source and SETTLE must preserve
    # existing behaviour -- Close stays NaN, no crash.
    raw = _make_lseg_df(["2026-07-01"]).rename(columns={"CLOSE": "TRDPRC_1"})
    raw["TRDPRC_1"] = [float("nan")]

    out = downloader._normalize_columns(raw, settle_fallback_for_close=True)

    assert np.isnan(out["Close"].iloc[0])


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
    # Generic field names (e.g. "OPEN") are rejected for some instruments
    # (SRAZ26) -- request LSEG's own default fields instead of a fixed list.
    assert kwargs["fields"] is None


def test_download_history_srax26_live_field_names_end_to_end():
    # Reproduces the real LSEG Workspace response for SRAZ26: a call made
    # without explicit fields returns OPEN_PRC/HIGH_1/LOW_1/TRDPRC_1/
    # ACVOL_UNS rather than the generic OHLCV names.
    raw = _make_lseg_df(["2026-01-01", "2026-01-02"]).rename(
        columns={
            "OPEN": "OPEN_PRC",
            "HIGH": "HIGH_1",
            "LOW": "LOW_1",
            "CLOSE": "TRDPRC_1",
            "VOLUME": "ACVOL_UNS",
        }
    )
    fake_lseg_data.get_history.return_value = raw

    df = downloader.download_history("SRAZ26", "DAILY", "2026-01-01", "2026-01-02")

    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 2
    _, kwargs = fake_lseg_data.get_history.call_args
    assert kwargs["fields"] is None


def test_download_history_empty_result_returns_empty_frame_not_error():
    fake_lseg_data.get_history.return_value = pd.DataFrame()
    df = downloader.download_history("SRAZ26", "DAILY", "2026-07-01", "2026-07-03")
    assert df.empty
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]


def test_download_history_invalid_date_range_raises():
    with pytest.raises(ValueError, match="must be <="):
        downloader.download_history("SRAZ26", "DAILY", "2026-07-10", "2026-07-01")


def test_download_history_daily_sonia_settle_fallback_end_to_end():
    # Reproduces the live SONU6 DAILY bug report end-to-end: TRDPRC_1/
    # OHLC entirely NA, SETTLE populated -- download_history's Close
    # must come from SETTLE, not NaN.
    fake_lseg_data.get_history.return_value = _make_sonia_like_df(
        ["2026-07-02", "2026-07-03"], [96.235, 96.240]
    )
    df = downloader.download_history("SONU6", "DAILY", "2026-07-02", "2026-07-03")

    assert df["Close"].tolist() == pytest.approx([96.235, 96.240])


def test_download_history_hourly_settle_present_does_not_fill_close():
    # Same shape as the DAILY SONIA case, but requested at HOURLY --
    # intraday semantics must be completely unchanged: SETTLE is never
    # consulted, Close stays NaN.
    fake_lseg_data.get_history.return_value = _make_sonia_like_df(
        ["2026-07-02"], [96.235]
    )
    df = downloader.download_history("SONU6", "HOURLY", "2026-07-02", "2026-07-02")

    assert np.isnan(df["Close"].iloc[0])


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
# _is_confirmed_universe_not_found: narrow classification (Module 5B.1)
# ---------------------------------------------------------------------

def test_is_confirmed_universe_not_found_true_for_exact_match():
    exc = _make_ld_error(_UNIVERSE_NOT_FOUND_MESSAGE)
    assert downloader._is_confirmed_universe_not_found(exc) is True


def test_is_confirmed_universe_not_found_false_for_generic_no_data_message():
    # "No data to return" alone, without the specific code+phrase, must
    # NOT be classified.
    exc = _make_ld_error("No data to return, please check errors: ERROR: No successful response.")
    assert downloader._is_confirmed_universe_not_found(exc) is False


def test_is_confirmed_universe_not_found_false_for_different_error_code():
    exc = _make_ld_error(
        "No data to return (TS.Interday.UserRequestError.99999, The universe is not found)"
    )
    assert downloader._is_confirmed_universe_not_found(exc) is False


def test_is_confirmed_universe_not_found_false_for_matching_code_different_phrase():
    exc = _make_ld_error("(TS.Interday.UserRequestError.70005, Some unrelated reason)")
    assert downloader._is_confirmed_universe_not_found(exc) is False


def test_is_confirmed_universe_not_found_false_for_generic_universe_mention():
    # "universe" alone, without the specific code+phrase, must NOT be
    # classified.
    exc = _make_ld_error("Some unrelated universe configuration issue")
    assert downloader._is_confirmed_universe_not_found(exc) is False


def test_is_confirmed_universe_not_found_false_for_matching_message_wrong_type():
    # Even the exact code+phrase must NOT be classified unless the
    # exception is actually LSEG's LDError type.
    exc = RuntimeError(_UNIVERSE_NOT_FOUND_MESSAGE)
    assert downloader._is_confirmed_universe_not_found(exc) is False


# ---------------------------------------------------------------------
# download_history: MarketDataUnavailableError translation + retry bypass
# ---------------------------------------------------------------------

def test_download_history_confirmed_universe_not_found_raises_typed_error_not_retried():
    fake_lseg_data.get_history.side_effect = _make_ld_error(_UNIVERSE_NOT_FOUND_MESSAGE)

    with pytest.raises(downloader.MarketDataUnavailableError) as exc_info:
        downloader.download_history("SRAH26", "DAILY", "2026-01-01", "2026-01-05")

    assert exc_info.value.ric == "SRAH26"
    assert "70005" in exc_info.value.message
    # Confirmed-permanent condition -- must NOT be retried.
    assert fake_lseg_data.get_history.call_count == 1


def test_download_history_generic_ld_error_is_not_translated_and_still_retries():
    fake_lseg_data.get_history.side_effect = _make_ld_error(
        "No data to return, please check errors: ERROR: No successful response."
    )

    with pytest.raises(Exception) as exc_info:
        downloader.download_history("SRAH26", "DAILY", "2026-01-01", "2026-01-05")

    assert not isinstance(exc_info.value, downloader.MarketDataUnavailableError)
    assert fake_lseg_data.get_history.call_count == 3  # ordinary retry behaviour retained


def test_download_history_unrelated_ld_error_is_not_translated_and_still_retries():
    fake_lseg_data.get_history.side_effect = _make_ld_error(
        "(TS.Interday.SomeOtherError.12345, A completely different problem)"
    )

    with pytest.raises(Exception) as exc_info:
        downloader.download_history("SRAH26", "DAILY", "2026-01-01", "2026-01-05")

    assert not isinstance(exc_info.value, downloader.MarketDataUnavailableError)
    assert fake_lseg_data.get_history.call_count == 3


def test_download_history_ordinary_connection_error_retry_behaviour_unaffected():
    # Regression: the retry-predicate change (excluding only
    # MarketDataUnavailableError) must not alter behaviour for any other
    # exception type, including the pre-existing retry-then-succeed and
    # exhausts-retries cases exercised above.
    fake_lseg_data.get_history.side_effect = [
        ConnectionError("transient network blip"),
        _make_lseg_df(["2026-07-01"]),
    ]
    df = downloader.download_history("SRAZ26", "DAILY", "2026-07-01", "2026-07-01")
    assert len(df) == 1
    assert fake_lseg_data.get_history.call_count == 2


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
