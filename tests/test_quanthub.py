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


# ---------------------------------------------------------------------
# HTTP 429: distinct exception, NOT retried; 5xx/network errors still are.
# ---------------------------------------------------------------------

def test_http_429_raises_quanthub_rate_limit_error_not_generic_http_error(mocker):
    _mock_response(mocker, status_code=429)
    with pytest.raises(quanthub.QuantHubRateLimitError):
        quanthub._fetch_quanthub_records(["YBAH28"], "1H", 4416)


def test_http_429_is_not_retried(mocker):
    # exactly one call, not the usual up-to-3 tenacity attempts.
    mock_get = _mock_response(mocker, status_code=429)
    with pytest.raises(quanthub.QuantHubRateLimitError):
        quanthub._fetch_quanthub_records(["YBAH28"], "1H", 4416)
    assert mock_get.call_count == 1


def test_quanthub_rate_limit_error_is_not_credentials_or_market_data_unavailable_error():
    assert not issubclass(quanthub.QuantHubRateLimitError, quanthub.QuantHubCredentialsMissingError)
    assert not issubclass(quanthub.QuantHubRateLimitError, MarketDataUnavailableError)


def test_http_500_still_retries_up_to_three_attempts(mocker):
    # Transient/5xx errors keep the existing generic retry behaviour --
    # only 429 is excluded.
    mock_get = _mock_response(mocker, status_code=500, raise_exc=requests.exceptions.HTTPError("500 Server Error"))
    with pytest.raises(requests.exceptions.HTTPError):
        quanthub._fetch_quanthub_records(["SONH26"], "1D", 5)
    assert mock_get.call_count == 3


def test_network_failure_still_retries_up_to_three_attempts(mocker):
    mock_get = mocker.patch(
        "core.quanthub.requests.get", side_effect=requests.exceptions.ConnectionError("boom")
    )
    with pytest.raises(requests.exceptions.ConnectionError):
        quanthub._fetch_quanthub_records(["SONH26"], "1D", 5)
    assert mock_get.call_count == 3


def test_download_history_propagates_rate_limit_error_without_retry_or_fallback(mocker):
    mock_get = _mock_response(mocker, status_code=429)
    with pytest.raises(quanthub.QuantHubRateLimitError):
        quanthub.download_history("YBAH28", "HOURLY", "2026-01-01", "2026-06-30")
    assert mock_get.call_count == 1


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


# ---------------------------------------------------------------------
# QUANTHUB_MAX_REQUEST_COUNT: conservative cap, live-tested at 3000
# ---------------------------------------------------------------------

def test_max_request_count_is_3000():
    # Locks the constant itself to the live-tested value -- not a
    # universal QuantHub limit, just the largest value this integration
    # has evidence is safe (see the constant's own docstring).
    assert quanthub.QUANTHUB_MAX_REQUEST_COUNT == 3000


def test_estimate_count_below_cap_is_unchanged():
    # A small window's natural estimate is well under 3000 -- the cap
    # must not alter it.
    start = datetime(2026, 1, 1)
    end = datetime(2026, 1, 10)
    uncapped = 10 * quanthub._HOURLY_BARS_PER_DAY + quanthub._HOURLY_BARS_PER_DAY
    assert uncapped < quanthub.QUANTHUB_MAX_REQUEST_COUNT
    assert quanthub._estimate_count("1H", start, end) == uncapped


def test_estimate_count_above_cap_is_capped_at_3000():
    # Reproduces the exact live scenario: a ~183-day 1H window naturally
    # estimates to 4416 (the count that produced the live HTTP 429) --
    # must now be capped at 3000, never left uncapped.
    start = datetime(2026, 1, 1)
    end = datetime(2026, 7, 3)  # 184 calendar days -> (184)*24+24 = 4440 uncapped
    count = quanthub._estimate_count("1H", start, end)
    assert count == quanthub.QUANTHUB_MAX_REQUEST_COUNT


def test_estimate_count_daily_above_cap_is_also_capped():
    start = datetime(2000, 1, 1)
    end = datetime(2026, 1, 1)  # ~26 years of calendar days, far above 3000
    count = quanthub._estimate_count("1D", start, end)
    assert count == quanthub.QUANTHUB_MAX_REQUEST_COUNT


def test_estimate_count_never_makes_multiple_requests_to_compensate(mocker):
    # A window whose true required count would exceed the cap must
    # still result in exactly ONE HTTP call, never pagination/multiple
    # requests to try to compensate for the cap.
    records = _records_for_dates("YBAH28", ["2026-06-25", "2026-06-26"])
    mock_get = _mock_response(mocker, json_body=records)

    quanthub.download_history("YBAH28", "HOURLY", "2026-01-01", "2026-07-03")

    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["count"] == quanthub.QUANTHUB_MAX_REQUEST_COUNT


def test_fewer_records_than_requested_is_accepted_not_an_error(mocker):
    # Direct reproduction of the live YBAH28 finding: count=3000/4000
    # requested, only 2995 returned, HTTP 200 -- must be accepted as a
    # normal, instrument-specific result, never raised as an exception
    # or padded/fabricated up to the requested count.
    records = _records_for_dates("YBAH28", [f"2026-01-{d:02d}" for d in range(1, 11)])  # 10 records
    _mock_response(mocker, json_body=records)

    df = quanthub.download_history("YBAH28", "DAILY", "2026-01-01", "2026-03-01")

    # No exception raised (the call above completing is itself the
    # assertion); the shorter-than-requested history is returned as-is.
    assert len(df) == 10


# ---------------------------------------------------------------------
# download_history_batch: chunking into QUANTHUB_BATCH_SIZE-sized HTTP
# requests (live-verified: 10 instruments in one request returns 200;
# QUANTHUB_BATCH_SIZE=10 unless a larger batch is separately verified).
# ---------------------------------------------------------------------

def _resp(json_body, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


def test_quanthub_batch_size_is_10():
    assert quanthub.QUANTHUB_BATCH_SIZE == 10


def test_download_history_batch_ten_instruments_issues_one_request(mocker):
    instruments = [f"INST{i}" for i in range(10)]
    records = [
        {"product": instr, "time": _ms("2026-01-05"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        for instr in instruments
    ]
    mock_get = _mock_response(mocker, json_body=records)

    result = quanthub.download_history_batch(instruments, "DAILY", "2026-01-01", "2026-01-10")

    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["instruments"] == ",".join(instruments)
    assert set(result) == set(instruments)


def test_download_history_batch_twenty_one_instruments_issues_three_requests(mocker):
    # 21 instruments -> chunks of 10, 10, 1 -> exactly 3 HTTP requests.
    instruments = [f"INST{i}" for i in range(21)]
    chunks = [instruments[0:10], instruments[10:20], instruments[20:21]]
    responses = [
        _resp(
            [
                {
                    "product": instr, "time": _ms("2026-01-05"),
                    "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
                }
                for instr in chunk
            ]
        )
        for chunk in chunks
    ]
    mock_get = mocker.patch("core.quanthub.requests.get", side_effect=responses)

    result = quanthub.download_history_batch(instruments, "DAILY", "2026-01-01", "2026-01-10")

    assert mock_get.call_count == 3
    call_instrument_params = [c.kwargs["params"]["instruments"] for c in mock_get.call_args_list]
    assert call_instrument_params == [",".join(chunk) for chunk in chunks]
    assert set(result) == set(instruments)


def test_download_history_batch_deduplicates_repeated_instruments(mocker):
    records = [
        {"product": "SONH26", "time": _ms("2026-01-05"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]
    mock_get = _mock_response(mocker, json_body=records)

    result = quanthub.download_history_batch(
        ["SONH26", "ERH26", "SONH26"], "DAILY", "2026-01-01", "2026-01-10"
    )

    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["instruments"] == "SONH26,ERH26"
    assert set(result) == {"SONH26", "ERH26"}


def test_download_history_batch_splits_response_by_product(mocker):
    records = [
        {"product": "SONH26", "time": _ms("2026-01-05"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"product": "ERH26", "time": _ms("2026-01-05"), "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
    ]
    _mock_response(mocker, json_body=records)

    result = quanthub.download_history_batch(["SONH26", "ERH26"], "DAILY", "2026-01-01", "2026-01-10")

    assert result["SONH26"].iloc[0]["Close"] == 1.0
    assert result["ERH26"].iloc[0]["Close"] == 2.0


def test_download_history_batch_four_hour_resamples_each_instrument_independently(mocker):
    hourly_dates = pd.date_range("2026-01-05 00:00", periods=8, freq="1h")
    records = []
    for instr, base in [("SONH26", 100.0), ("ERH26", 200.0)]:
        for i, ts in enumerate(hourly_dates):
            records.append(
                {
                    "product": instr, "time": int(ts.value // 10**6),
                    "open": base + i, "high": base + i + 0.5, "low": base + i - 0.5,
                    "close": base + i + 0.25, "volume": 10,
                }
            )
    mock_get = _mock_response(mocker, json_body=records)

    result = quanthub.download_history_batch(["SONH26", "ERH26"], "4H", "2026-01-05", "2026-01-05")

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["interval"] == "1H"
    assert len(result["SONH26"]) == 2
    assert len(result["ERH26"]) == 2
    assert result["SONH26"].iloc[0]["Open"] == 100.0
    assert result["ERH26"].iloc[0]["Open"] == 200.0


def test_download_history_batch_count_estimated_once_and_shared_across_chunks(mocker):
    # Same count= parameter on every chunked request, capped exactly as
    # a single-instrument download_history() call would be -- batching
    # must never inflate or vary the count per chunk.
    instruments = [f"INST{i}" for i in range(21)]
    mock_get = mocker.patch(
        "core.quanthub.requests.get", side_effect=[_resp([]), _resp([]), _resp([])]
    )

    quanthub.download_history_batch(instruments, "DAILY", "2026-01-01", "2026-01-10")

    counts = [c.kwargs["params"]["count"] for c in mock_get.call_args_list]
    assert mock_get.call_count == 3
    assert len(set(counts)) == 1
    assert counts[0] <= quanthub.QUANTHUB_MAX_REQUEST_COUNT


def test_download_history_batch_partial_history_per_instrument_accepted_without_pagination(mocker):
    # SONH26 returns fewer records than ERH26 in the SAME batched
    # response -- accepted as-is per instrument, no extra request
    # triggered to try to "fill in" the shorter one.
    records = [
        {"product": "SONH26", "time": _ms("2026-01-05"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ] + [
        {
            "product": "ERH26", "time": _ms(f"2026-01-{d:02d}"),
            "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
        }
        for d in range(1, 6)
    ]
    mock_get = _mock_response(mocker, json_body=records)

    result = quanthub.download_history_batch(["SONH26", "ERH26"], "DAILY", "2026-01-01", "2026-01-10")

    assert mock_get.call_count == 1
    assert len(result["SONH26"]) == 1
    assert len(result["ERH26"]) == 5


def test_download_history_delegates_to_download_history_batch(mocker):
    # download_history() is now a thin single-instrument wrapper around
    # download_history_batch() -- locks in that the refactor didn't
    # change its own public contract.
    mock_batch = mocker.patch(
        "core.quanthub.download_history_batch",
        return_value={
            "SONH26": pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        },
    )

    quanthub.download_history("SONH26", "DAILY", "2026-01-01", "2026-01-10")

    mock_batch.assert_called_once_with(["SONH26"], "DAILY", "2026-01-01", "2026-01-10")
