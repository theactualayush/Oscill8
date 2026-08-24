"""
tests/test_quanthub.py

Unit tests for core/quanthub.py: QH instrument construction (namespace
independence from LSEG RICs), response normalization, batching, count
estimation, 4H resampling, credential handling, and error propagation
(an HTTP 500 must NOT be classified as MarketDataUnavailableError --
that classification is LSEG-specific, see core/downloader.py).

requests.get is mocked throughout -- no live QuantHub network access.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests

from core import config, quanthub
from core.config import BarInterval
from core.downloader import MarketDataUnavailableError


# ---------------------------------------------------------------------
# build_instrument: QH namespace, never derived from a RIC
# ---------------------------------------------------------------------

def test_build_instrument_sofr_matches_verified_live_example():
    # The one directly-verified example from live QuantHub testing.
    assert quanthub.build_instrument("SRA", 3, 2024) == "SRAH24"


def test_build_instrument_sonia_uses_two_digit_year_independent_of_lseg():
    # SONIA's LSEG ric_year_digits is 1 (e.g. "SONH6") -- QuantHub's own
    # verified example (SONH26) uses 2 digits regardless. build_instrument
    # must never consult core.config.MARKETS/ric_year_digits.
    assert quanthub.build_instrument("SON", 3, 2026) == "SONH26"


def test_build_instrument_euribor_uses_qh_root_not_reuters_root():
    # Part 9 item 7: FEIH26 (Reuters-derived) must never be produced or
    # used where ERH26 (the verified QuantHub identifier) is required.
    instrument = quanthub.build_instrument("ER", 3, 2026)
    assert instrument == "ERH26"
    assert instrument != "FEIH26"


@pytest.mark.parametrize(
    "qh_root, month, year, expected",
    [
        ("FSR", 3, 2026, "FSRH26"),   # SARON
        ("YBA", 3, 2026, "YBAH26"),   # Australia 90 Day Bank Bill
        ("FER", 3, 2026, "FERH26"),   # ICE Europe ESTR
    ],
)
def test_build_instrument_matches_verified_examples(qh_root, month, year, expected):
    assert quanthub.build_instrument(qh_root, month, year) == expected


def test_build_instrument_invalid_month_raises():
    with pytest.raises(ValueError, match="month must be 1-12"):
        quanthub.build_instrument("SRA", 13, 2026)


def test_build_instrument_empty_root_raises():
    with pytest.raises(ValueError, match="qh_root"):
        quanthub.build_instrument("", 3, 2026)


# ---------------------------------------------------------------------
# Month-code assumption: only "H" (March) is live-verified against the
# real QuantHub API. The other 11 letters are carried over from the
# universal futures month-code convention, never independently
# confirmed. This boundary must stay explicit and tested, not silently
# assumed to generalize.
# ---------------------------------------------------------------------

def test_only_h_is_in_the_live_verified_month_code_set():
    assert quanthub.LIVE_VERIFIED_QUANTHUB_MONTH_CODES == frozenset({"H"})


def test_build_instrument_for_verified_month_does_not_log_assumption_warning(caplog):
    with caplog.at_level("DEBUG", logger="core.quanthub"):
        quanthub.build_instrument("SRA", 3, 2024)  # March = "H", live-verified
    assert not any("not independently confirmed" in r.message for r in caplog.records)


@pytest.mark.parametrize("month, month_code", [(1, "F"), (6, "M"), (12, "Z")])
def test_build_instrument_for_unverified_month_logs_the_assumption(caplog, month, month_code):
    # Mechanically still works (a market needs its full listing cycle to
    # be scannable -- this is documentation/logging, not a restriction)
    # but must be traceable as an assumed, not live-confirmed, mapping.
    with caplog.at_level("DEBUG", logger="core.quanthub"):
        instrument = quanthub.build_instrument("SRA", month, 2026)
    assert instrument == f"SRA{month_code}26"
    assert any("not independently confirmed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------

def _ms(date_str: str) -> int:
    # .value is int64 nanoseconds since epoch, treating a naive Timestamp
    # as UTC directly (no local-tz conversion) -- deterministic regardless
    # of the machine's local timezone, unlike .timestamp().
    return int(pd.Timestamp(date_str).value // 10**6)


_SAMPLE_RECORDS = [
    {
        "product": "SONH26",
        "time": _ms("2026-06-13"),
        "open": 96.2525,
        "high": 96.2550,
        "low": 96.2525,
        "close": 96.2550,
        "volume": 233,
    },
    {
        "product": "SONH26",
        "time": _ms("2026-06-14"),
        "open": 96.2550,
        "high": 96.2600,
        "low": 96.2500,
        "close": 96.2580,
        "volume": 410,
    },
]


def test_normalize_quanthub_records_basic_shape_and_dtypes():
    df = quanthub._normalize_quanthub_records(_SAMPLE_RECORDS)
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert str(df["Date"].dtype).startswith("datetime64")
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert str(df[col].dtype) == "float64"
    assert len(df) == 2
    assert df.iloc[0]["Close"] == 96.2550
    assert df["Date"].is_monotonic_increasing


def test_normalize_quanthub_records_empty_list_returns_empty_canonical_df():
    df = quanthub._normalize_quanthub_records([])
    assert df.empty
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]


def test_normalize_quanthub_records_unix_ms_timestamp_decoded_correctly():
    df = quanthub._normalize_quanthub_records([_SAMPLE_RECORDS[0]])
    assert df.iloc[0]["Date"] == pd.Timestamp("2026-06-13")


# ---------------------------------------------------------------------
# HTTP fetch: response-shape handling, batching, error propagation
# ---------------------------------------------------------------------

def _mock_response(mocker, *, json_body=None, status_code=200, raise_exc=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    if raise_exc is not None:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    return mocker.patch("core.quanthub.requests.get", return_value=resp)


@pytest.fixture(autouse=True)
def _quanthub_token(monkeypatch):
    monkeypatch.setattr(config, "QUANTHUB_TOKEN", "test-token")


def test_fetch_records_handles_bare_list_response(mocker):
    mock_get = _mock_response(mocker, json_body=_SAMPLE_RECORDS)
    grouped = quanthub._fetch_quanthub_records(["SONH26"], "1D", 5)
    assert grouped == {"SONH26": _SAMPLE_RECORDS}
    mock_get.assert_called_once()


def test_fetch_records_handles_wrapped_empty_response(mocker):
    _mock_response(mocker, json_body={"status": "SUCCESS", "data": []})
    grouped = quanthub._fetch_quanthub_records(["ERH26"], "1D", 5)
    assert grouped == {}


def test_fetch_records_sends_bearer_auth_header_and_params(mocker):
    mock_get = _mock_response(mocker, json_body=[])
    quanthub._fetch_quanthub_records(["SRAH24"], "1D", 5)
    _, kwargs = mock_get.call_args
    assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
    assert kwargs["params"] == {"instruments": "SRAH24", "interval": "1D", "count": 5}


def test_fetch_records_batches_multiple_instruments_in_one_call(mocker):
    batch_records = [
        {"product": "SONH26", "time": 1781568000000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"product": "ERH26", "time": 1781568000000, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
        {"product": "FSRH26", "time": 1781568000000, "open": 3, "high": 3, "low": 3, "close": 3, "volume": 3},
    ]
    mock_get = _mock_response(mocker, json_body=batch_records)

    grouped = quanthub._fetch_quanthub_records(["SONH26", "ERH26", "FSRH26"], "1D", 5)

    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["instruments"] == "SONH26,ERH26,FSRH26"
    assert set(grouped) == {"SONH26", "ERH26", "FSRH26"}
    assert grouped["ERH26"][0]["close"] == 2


def test_fetch_records_reproduces_the_exact_live_verified_batch(mocker):
    # The trader's own live test against the real QuantHub API: a single
    # batched request for these 4 instruments returned HTTP 200 with
    # real OHLC data for each. Reproduced here as a mocked regression
    # test locking down the request shape that was actually verified.
    live_verified_instruments = ["ERH26", "FSRH26", "YBAH26", "FERH26"]
    batch_records = [
        {
            "product": instr,
            "time": _ms("2026-03-16"),
            "open": 96.0 + i,
            "high": 96.1 + i,
            "low": 95.9 + i,
            "close": 96.05 + i,
            "volume": 100 + i,
        }
        for i, instr in enumerate(live_verified_instruments)
    ]
    mock_get = _mock_response(mocker, json_body=batch_records)

    grouped = quanthub._fetch_quanthub_records(live_verified_instruments, "1D", 5)

    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["instruments"] == "ERH26,FSRH26,YBAH26,FERH26"
    assert set(grouped) == set(live_verified_instruments)
    for instr in live_verified_instruments:
        assert len(grouped[instr]) == 1
        assert grouped[instr][0]["product"] == instr


def test_fetch_batch_normalizes_each_instrument_independently(mocker):
    batch_records = [
        {"product": "SONH26", "time": 1781568000000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"product": "ERH26", "time": 1781568000000, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
    ]
    _mock_response(mocker, json_body=batch_records)

    result = quanthub.fetch_batch(["SONH26", "ERH26"], "DAILY", 5)

    assert set(result) == {"SONH26", "ERH26"}
    assert result["SONH26"].iloc[0]["Close"] == 1.0
    assert result["ERH26"].iloc[0]["Close"] == 2.0


def test_fetch_records_http_500_propagates_as_plain_http_error_not_market_data_unavailable(mocker):
    _mock_response(mocker, status_code=500, raise_exc=requests.exceptions.HTTPError("500 Server Error"))
    with pytest.raises(requests.exceptions.HTTPError):
        quanthub._fetch_quanthub_records(["SONH26"], "1D", 5)
    # Confirm this is NOT (and never becomes) the narrow LSEG classification.


def test_missing_credentials_raises_before_any_http_call(mocker, monkeypatch):
    monkeypatch.setattr(config, "QUANTHUB_TOKEN", "")
    mock_get = mocker.patch("core.quanthub.requests.get")
    with pytest.raises(quanthub.QuantHubCredentialsMissingError):
        quanthub._fetch_quanthub_records(["SONH26"], "1D", 5)
    mock_get.assert_not_called()


def test_quanthub_credentials_missing_error_is_not_market_data_unavailable_error():
    assert not issubclass(quanthub.QuantHubCredentialsMissingError, MarketDataUnavailableError)


# ---------------------------------------------------------------------
# download_history: end-to-end (mocked HTTP), date filtering, 4H resample
# ---------------------------------------------------------------------

def _records_for_dates(product: str, dates: list[str]) -> list[dict]:
    return [
        {
            "product": product,
            "time": _ms(d),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10,
        }
        for d in dates
    ]


def test_download_history_daily_filters_to_requested_range(mocker):
    records = _records_for_dates(
        "SONH26", ["2026-01-01", "2026-01-05", "2026-01-10", "2026-01-20"]
    )
    _mock_response(mocker, json_body=records)

    df = quanthub.download_history("SONH26", "DAILY", "2026-01-03", "2026-01-15")

    assert list(df["Date"].dt.strftime("%Y-%m-%d")) == ["2026-01-05", "2026-01-10"]


def test_download_history_unknown_instrument_returns_empty_canonical_df(mocker):
    _mock_response(mocker, json_body={"status": "SUCCESS", "data": []})
    df = quanthub.download_history("NOPE99", "DAILY", "2026-01-01", "2026-01-05")
    assert df.empty
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]


def test_download_history_start_after_end_raises():
    with pytest.raises(ValueError, match="start .* must be <= end"):
        quanthub.download_history("SONH26", "DAILY", "2026-01-10", "2026-01-01")


def test_download_history_four_hour_requests_native_1h_and_resamples(mocker):
    # 8 consecutive hourly bars -> should resample to 2 four-hour bars.
    hourly_dates = pd.date_range("2026-01-05 00:00", periods=8, freq="1h")
    records = [
        {
            "product": "SONH26",
            "time": int(ts.value // 10**6),
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 10,
        }
        for i, ts in enumerate(hourly_dates)
    ]
    mock_get = _mock_response(mocker, json_body=records)

    df = quanthub.download_history("SONH26", "4H", "2026-01-05", "2026-01-05")

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["interval"] == "1H"
    assert len(df) == 2
    # First 4H bar: Open = first hourly Open, Close = 4th hourly Close.
    assert df.iloc[0]["Open"] == 100.0
    assert df.iloc[0]["Close"] == 103.5


def test_download_history_logs_truncation_warning_when_count_hit_and_gap_remains(mocker, caplog):
    # Ask for a wide range but only return exactly `count` bars, none of
    # which reach back to the requested start -- a real truncation signal.
    dates = pd.date_range("2026-01-25", periods=10, freq="1D")
    records = [
        {
            "product": "SONH26",
            "time": int(ts.value // 10**6),
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10,
        }
        for ts in dates
    ]
    _mock_response(mocker, json_body=records)
    mocker.patch("core.quanthub._estimate_count", return_value=10)

    with caplog.at_level("WARNING"):
        quanthub.download_history("SONH26", "DAILY", "2026-01-01", "2026-02-01")

    assert any("count was insufficient" in r.message for r in caplog.records)


# ---------------------------------------------------------------------
# _estimate_count: pure heuristic, no network
# ---------------------------------------------------------------------

def test_estimate_count_daily_covers_full_calendar_span_plus_buffer():
    start = datetime(2026, 1, 1)
    end = datetime(2026, 1, 10)
    count = quanthub._estimate_count("1D", start, end)
    assert count >= 10


def test_estimate_count_hourly_generously_covers_span():
    start = datetime(2026, 1, 1)
    end = datetime(2026, 1, 2)
    count = quanthub._estimate_count("1H", start, end)
    assert count >= 24


def test_estimate_count_unknown_interval_raises():
    with pytest.raises(ValueError, match="No count-estimation rule"):
        quanthub._estimate_count("5M", datetime(2026, 1, 1), datetime(2026, 1, 2))
