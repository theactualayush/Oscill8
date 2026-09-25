"""
tests/test_quanthub.py

Unit tests for core/quanthub.py: QH instrument construction (namespace
independence from LSEG RICs), response normalization, batching, count
estimation, 4H resampling, credential handling, error propagation
(an HTTP 500 must NOT be classified as MarketDataUnavailableError --
that classification is LSEG-specific, see core/downloader.py), and --
added with the /api/v2/ohlc/ -> /apis/ohlc/ backend migration -- the
OUTGOING REQUEST URL itself.

requests.get is mocked throughout -- no live QuantHub network access.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests

from core import config, quanthub
from core.config import BarInterval
from core.downloader import MarketDataUnavailableError

# The migrated QuantHub OHLC endpoint. QuantHub retired the old /api/
# backend; the old path now returns HTTP 403 for every request.
MIGRATED_OHLC_URL = "https://qh-api.corp.hertshtengroup.com/apis/ohlc/"
RETIRED_OHLC_URL = "https://qh-api.corp.hertshtengroup.com/api/v2/ohlc/"


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

def _mock_response(mocker, *, json_body=None, status_code=200, raise_exc=None,
                   headers=None, text=None):
    """Patch core.quanthub.requests.get with one canned response.

    `headers`/`text` are set to real dict/str values rather than left as
    MagicMock attributes, because the client now READS both: headers for
    Retry-After on a 429, text for the error-body excerpt on a 4xx. A
    MagicMock would make those silently meaningless.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.headers = dict(headers or {})
    resp.text = text if text is not None else ""
    if raise_exc is not None:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    return mocker.patch("core.quanthub.requests.get", return_value=resp)


def _mock_responses(mocker, responses):
    """Patch requests.get with a SEQUENCE of canned responses, for tests
    that need attempt N to differ from attempt N+1 (e.g. 429 then 200)."""
    return mocker.patch("core.quanthub.requests.get", side_effect=responses)


def _response(*, json_body=None, status_code=200, headers=None, text=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.headers = dict(headers or {})
    resp.text = text if text is not None else ""
    resp.raise_for_status.return_value = None
    return resp


@pytest.fixture(autouse=True)
def _neutralise_rate_limiter_sleep(monkeypatch):
    """Keep the client's rate limiter ACTIVE but make its waits free.

    core.quanthub now paces outbound requests to 25/minute -- 2.4s
    between consecutive requests. Left alone, every multi-request test
    in this file (batch chunking, retry counts) would sit in a real
    sleep, turning a fast suite into a multi-minute one.

    A no-op `sleep` is injected rather than the limiter being disabled,
    so the slot-reservation logic still runs on every request and a bug
    in it would still show up here. Only the wall-clock delay is
    removed. The limiter's own timing is verified separately, against a
    fully fake clock, in the rate-limiter section below.
    """
    monkeypatch.setattr(
        quanthub,
        "_RATE_LIMITER",
        quanthub.QuantHubRateLimiter(
            quanthub.QUANTHUB_REQUESTS_PER_MINUTE, sleep=lambda _seconds: None
        ),
    )


@pytest.fixture(autouse=True)
def _quanthub_token(monkeypatch):
    monkeypatch.setattr(config, "QUANTHUB_TOKEN", "test-token")


@pytest.fixture(autouse=True)
def _quanthub_endpoint(monkeypatch):
    """Pin the migrated endpoint for every test in this module.

    core.config reads RBS_QUANTHUB_BASE_URL at IMPORT time, so a
    developer machine that still exports the retired URL would otherwise
    make the outgoing-URL assertions below depend on local environment
    rather than on the code. Pinning it here keeps every request-shape
    test deterministic; that the SHIPPED DEFAULT is itself migrated is
    proven separately, and independently of the environment, by
    test_config_default_endpoint_is_the_migrated_apis_path below.
    """
    monkeypatch.setattr(config, "QUANTHUB_BASE_URL", MIGRATED_OHLC_URL)


# ---------------------------------------------------------------------
# Backend migration: /api/v2/ohlc/ -> /apis/ohlc/
#
# The outgoing URL is passed POSITIONALLY to requests.get, so it lands in
# call_args.args[0]. Every pre-existing test in this file read only
# call_args kwargs (headers/params) and therefore could not have caught
# an endpoint regression -- that gap is what these tests close.
# ---------------------------------------------------------------------

def test_fetch_records_posts_to_the_migrated_apis_ohlc_url(mocker):
    mock_get = _mock_response(mocker, json_body=[])
    quanthub._fetch_quanthub_records(["SONU28"], "1H", 5)

    args, _kwargs = mock_get.call_args
    assert args[0] == MIGRATED_OHLC_URL


def test_fetch_records_never_uses_the_retired_api_v2_url(mocker):
    """Regression guard for the retired backend. The old path returns
    HTTP 403 for every instrument, at every interval and count."""
    mock_get = _mock_response(mocker, json_body=[])
    quanthub._fetch_quanthub_records(["SONU28"], "1H", 5)

    requested_url = mock_get.call_args.args[0]
    assert requested_url != RETIRED_OHLC_URL
    assert quanthub.RETIRED_QUANTHUB_OHLC_PATH not in requested_url
    assert "/apis/ohlc/" in requested_url


def test_request_url_comes_from_config_never_hardcoded_in_the_client(mocker):
    """The client must send whatever core.config resolved, so an
    operator override (RBS_QUANTHUB_BASE_URL) still works and a future
    endpoint change stays a one-line config change."""
    monkeypatched_url = "https://example.invalid/apis/ohlc/"
    mocker.patch.object(config, "QUANTHUB_BASE_URL", monkeypatched_url)
    mock_get = _mock_response(mocker, json_body=[])

    quanthub._fetch_quanthub_records(["SONU28"], "1H", 5)

    assert mock_get.call_args.args[0] == monkeypatched_url


def test_every_batched_request_uses_the_migrated_url(mocker):
    """The URL is per-request, so a multi-chunk download must use the
    migrated endpoint on EVERY chunk, not just the first."""
    mock_get = _mock_response(mocker, json_body=[])
    instruments = [f"SON{m}26" for m in "FGHJKMNQUVXZ"] + ["SONF27"]  # 13 -> 2 chunks

    quanthub.download_history_batch(instruments, "DAILY", "2026-01-01", "2026-01-10")

    assert mock_get.call_count == 2
    assert [c.args[0] for c in mock_get.call_args_list] == [MIGRATED_OHLC_URL] * 2


def test_config_default_endpoint_is_the_migrated_apis_path(monkeypatch, tmp_path):
    """The SHIPPED default in core/config.py -- not whatever this
    machine's environment happens to hold.

    core.config reads RBS_QUANTHUB_BASE_URL once at import, so the
    already-imported module cannot answer this question on a machine
    that sets the variable. A fresh, throwaway copy of the same source
    file is executed with the variable cleared instead; core/config.py
    imports nothing from core, so loading it standalone is safe and
    leaves the real core.config untouched.
    """
    monkeypatch.delenv("RBS_QUANTHUB_BASE_URL", raising=False)

    spec = importlib.util.spec_from_file_location("_fresh_core_config", config.__file__)
    fresh = importlib.util.module_from_spec(spec)
    # config.py declares dataclasses under `from __future__ import
    # annotations`, and dataclasses resolves those annotations via
    # sys.modules[cls.__module__] -- so the throwaway copy has to be
    # registered while it executes. Removed again immediately; the real
    # core.config is never touched.
    sys.modules[spec.name] = fresh
    try:
        spec.loader.exec_module(fresh)
        default_url = fresh.QUANTHUB_BASE_URL
    finally:
        sys.modules.pop(spec.name, None)

    assert default_url == MIGRATED_OHLC_URL
    assert quanthub.RETIRED_QUANTHUB_OHLC_PATH not in default_url
    # The real, already-imported module is unaffected by this probe.
    assert config.__name__ == "core.config"


def test_retired_endpoint_constant_is_recognition_only(monkeypatch, caplog):
    """RETIRED_QUANTHUB_OHLC_PATH exists to RECOGNISE a stale configured
    URL and warn, never to build a request and never to rewrite the
    operator's own setting."""
    monkeypatch.setattr(config, "QUANTHUB_BASE_URL", RETIRED_OHLC_URL)
    with caplog.at_level("WARNING", logger="core.quanthub"):
        quanthub._warn_if_retired_endpoint_configured()

    assert any("RETIRED" in r.message for r in caplog.records)
    # The warning is advisory only -- the configured value is left alone.
    assert config.QUANTHUB_BASE_URL == RETIRED_OHLC_URL


def test_no_warning_when_the_migrated_endpoint_is_configured(caplog):
    with caplog.at_level("WARNING", logger="core.quanthub"):
        quanthub._warn_if_retired_endpoint_configured()
    assert not [r for r in caplog.records if "RETIRED" in r.message]


def test_auth_header_is_unchanged_by_the_migration(mocker):
    """The new backend uses the same Authorization: Bearer <token>
    scheme, with the token still supplied manually via
    RBS_QUANTHUB_TOKEN. Oscill8 performs NO token acquisition, refresh,
    or Microsoft sign-in -- there is no auth client to assert about."""
    mock_get = _mock_response(mocker, json_body=[])
    quanthub._fetch_quanthub_records(["SONU28"], "1H", 5)

    _args, kwargs = mock_get.call_args
    assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
    assert not hasattr(quanthub, "fetch_access_token")


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
# HTTP 429: distinct exception, now RETRIED with a directed cooldown;
# 5xx/network errors keep their own unchanged transient backoff.
# ---------------------------------------------------------------------

def test_http_429_raises_quanthub_rate_limit_error_not_generic_http_error(mocker):
    _mock_response(mocker, status_code=429)
    slept = _record_sleeps()
    with pytest.raises(quanthub.QuantHubRateLimitError):
        _fetch_no_sleep(slept)(["YBAH28"], "1H", 4416)


def test_http_429_is_retried_within_the_shared_attempt_budget(mocker):
    """BEHAVIOUR CHANGE, deliberate. 429 used to be excluded from retry
    entirely, because no cooldown signal had been observed and blind
    exponential retry risked compounding the condition. QuantHub's 429
    body is now known to state its own limit and retry delay, so the
    retry is directed rather than blind -- see _quanthub_wait. The
    attempt budget is the shared 3, not a new one."""
    mock_get = _mock_response(mocker, status_code=429)
    slept = _record_sleeps()

    with pytest.raises(quanthub.QuantHubRateLimitError):
        _fetch_no_sleep(slept)(["YBAH28"], "1H", 4416)

    assert mock_get.call_count == 3


def test_quanthub_rate_limit_error_is_not_credentials_or_market_data_unavailable_error():
    assert not issubclass(quanthub.QuantHubRateLimitError, quanthub.QuantHubCredentialsMissingError)
    assert not issubclass(quanthub.QuantHubRateLimitError, MarketDataUnavailableError)


def test_http_500_still_retries_up_to_three_attempts(mocker):
    # Transient/5xx errors keep the existing generic retry behaviour,
    # untouched by the rate-limit and 400 work.
    mock_get = _mock_response(mocker, status_code=500, raise_exc=requests.exceptions.HTTPError("500 Server Error"))
    slept = _record_sleeps()
    with pytest.raises(requests.exceptions.HTTPError):
        _fetch_no_sleep(slept)(["SONH26"], "1D", 5)
    assert mock_get.call_count == 3


def test_network_failure_still_retries_up_to_three_attempts(mocker):
    mock_get = mocker.patch(
        "core.quanthub.requests.get", side_effect=requests.exceptions.ConnectionError("boom")
    )
    slept = _record_sleeps()
    with pytest.raises(requests.exceptions.ConnectionError):
        _fetch_no_sleep(slept)(["SONH26"], "1D", 5)
    assert mock_get.call_count == 3


def test_download_history_propagates_rate_limit_error_after_exhausting_retries(mocker):
    """The public entry point still surfaces QuantHubRateLimitError --
    it is never swallowed, downgraded, or turned into an empty frame --
    but now only after the retry budget is spent."""
    mock_get = _mock_response(mocker, status_code=429)
    slept = _record_sleeps()
    mocker.patch.object(
        quanthub, "_fetch_quanthub_records", _fetch_no_sleep(slept)
    )

    with pytest.raises(quanthub.QuantHubRateLimitError):
        quanthub.download_history("YBAH28", "HOURLY", "2026-01-01", "2026-06-30")

    assert mock_get.call_count == 3


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


def test_estimate_count_is_not_capped_by_itself():
    # _estimate_count no longer applies any cap of its own -- capping is
    # batch-size-dependent (see QUANTHUB_MAX_ROWS_PER_REQUEST /
    # _max_count_for_batch) and only ever applied by download_history_
    # batch() once the actual per-request instrument count is known.
    start = datetime(1970, 1, 1)
    end = datetime(2026, 1, 1)  # ~56 years of calendar days
    uncapped_daily = (end.date() - start.date()).days + 1 + quanthub._DAILY_COUNT_BUFFER
    assert quanthub._estimate_count("1D", start, end) == uncapped_daily
    assert uncapped_daily > 10_000  # far above even the single-instrument row cap


# ---------------------------------------------------------------------
# QUANTHUB_MAX_ROWS_PER_REQUEST: live-verified TOTAL-ROW cap (not a flat
# per-request `count` cap) -- 8 EURIBOR instruments x count=1000 (8000
# rows) -> HTTP 200; the same 8 x count=2000 (16000 rows) -> HTTP 400
# "Max row limit exceeded (10000)". See _max_count_for_batch(), applied
# fresh per request in download_history_batch() since the permissible
# count depends on how many instruments share that specific request.
# ---------------------------------------------------------------------

def test_max_rows_per_request_is_10000():
    # Locks the constant itself to the live-tested value.
    assert quanthub.QUANTHUB_MAX_ROWS_PER_REQUEST == 10_000


@pytest.mark.parametrize(
    "batch_size, expected_max_count",
    [
        (10, 1000),
        (8, 1250),
        (6, 1666),
        (4, 2500),
        (1, 10_000),
    ],
)
def test_max_count_for_batch_matches_live_verified_examples(batch_size, expected_max_count):
    assert quanthub._max_count_for_batch(batch_size) == expected_max_count


def test_estimate_count_never_makes_multiple_requests_to_compensate(mocker):
    # A window whose true required count would exceed the (single-
    # instrument) per-batch cap must still result in exactly ONE HTTP
    # call, never pagination/multiple requests to try to compensate.
    # A ~2-year HOURLY span naturally estimates well above 10,000 bars
    # (731 days x 24 + 24 > 10,000) -- single instrument, so the cap
    # applied is _max_count_for_batch(1) == QUANTHUB_MAX_ROWS_PER_REQUEST.
    records = _records_for_dates("YBAH28", ["2026-06-25", "2026-06-26"])
    mock_get = _mock_response(mocker, json_body=records)

    quanthub.download_history("YBAH28", "HOURLY", "2026-01-01", "2028-01-01")

    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["count"] == quanthub.QUANTHUB_MAX_ROWS_PER_REQUEST


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


def test_download_history_batch_count_computed_per_chunk_not_shared_across_chunks(mocker):
    # 21 instruments -> chunks of 10, 10, 1. QuantHub's limit is on TOTAL
    # ROWS per request (instruments_in_request x count <=
    # QUANTHUB_MAX_ROWS_PER_REQUEST), so a smaller trailing chunk (1
    # instrument) legitimately gets a HIGHER count than a full
    # QUANTHUB_BATCH_SIZE-sized chunk (10 instruments) -- the count must
    # never be computed once and shared verbatim across differently-
    # sized chunks.
    instruments = [f"INST{i}" for i in range(21)]
    mocker.patch("core.quanthub._estimate_count", return_value=3000)
    mock_get = mocker.patch(
        "core.quanthub.requests.get", side_effect=[_resp([]), _resp([]), _resp([])]
    )

    quanthub.download_history_batch(instruments, "DAILY", "2026-01-01", "2026-01-10")

    counts = [c.kwargs["params"]["count"] for c in mock_get.call_args_list]
    assert mock_get.call_count == 3
    # First two chunks: 10 instruments each -> max_count_for_batch(10) ==
    # 1000, which is below the 3000 estimate, so count is capped to 1000.
    assert counts[0] == 1000
    assert counts[1] == 1000
    # Third chunk: 1 instrument -> max_count_for_batch(1) == 10000, well
    # above the 3000 estimate, so the estimate itself is unchanged.
    assert counts[2] == 3000


@pytest.mark.parametrize(
    "num_instruments, estimated_count, expected_count",
    [
        (8, 3000, 1250),   # live example: 8 instruments, estimate 3000 -> capped to 1250
        (10, 3000, 1000),  # live example: 10 instruments, estimate 3000 -> capped to 1000
        (6, 3000, 1666),   # live example: 6 instruments, estimate 3000 -> capped to 1666
        (2, 200, 200),     # estimate already below the per-batch cap -> unchanged
    ],
)
def test_download_history_batch_row_limit_examples(
    mocker, num_instruments, estimated_count, expected_count
):
    instruments = [f"INST{i}" for i in range(num_instruments)]
    mocker.patch("core.quanthub._estimate_count", return_value=estimated_count)
    mock_get = mocker.patch("core.quanthub.requests.get", return_value=_resp([]))

    quanthub.download_history_batch(instruments, "DAILY", "2026-01-01", "2026-01-10")

    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["count"] == expected_count


@pytest.mark.parametrize("num_instruments", [1, 2, 4, 6, 8, 10])
def test_download_history_batch_never_exceeds_total_row_limit(mocker, num_instruments):
    # However large the raw estimate, instruments_in_request x count must
    # never exceed QUANTHUB_MAX_ROWS_PER_REQUEST for any batch size.
    instruments = [f"INST{i}" for i in range(num_instruments)]
    mocker.patch("core.quanthub._estimate_count", return_value=50_000)
    mock_get = mocker.patch("core.quanthub.requests.get", return_value=_resp([]))

    quanthub.download_history_batch(instruments, "DAILY", "2026-01-01", "2026-01-10")

    _, kwargs = mock_get.call_args
    total_rows = num_instruments * kwargs["params"]["count"]
    assert total_rows <= quanthub.QUANTHUB_MAX_ROWS_PER_REQUEST


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

    # use_date_range is asserted explicitly rather than dropped from the
    # assertion: its DEFAULT is part of the contract this test locks in
    # -- a caller that passes nothing must still get the count shape.
    mock_batch.assert_called_once_with(
        ["SONH26"], "DAILY", "2026-01-01", "2026-01-10", use_date_range=False
    )


def test_download_history_forwards_use_date_range_to_the_batch_function(mocker):
    """The single-instrument wrapper must not swallow the request-shape
    choice -- otherwise a caller asking for a date range would silently
    get a count request instead."""
    mock_batch = mocker.patch(
        "core.quanthub.download_history_batch",
        return_value={
            "SONH26": pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        },
    )

    quanthub.download_history("SONH26", "DAILY", "2026-01-01", "2026-01-10", use_date_range=True)

    mock_batch.assert_called_once_with(
        ["SONH26"], "DAILY", "2026-01-01", "2026-01-10", use_date_range=True
    )


# ---------------------------------------------------------------------
# start/end date-range request shape
#
# Live-established against the MIGRATED /apis/ohlc/ backend (see
# tools/qh_stress_test.py and core/quanthub.py's module docstring):
#   - start/end work, but ONLY as unix SECONDS
#     (unix ms -> 0 rows silently; "YYYY-MM-DD" and ISO-8601 -> HTTP 500)
#   - `count` must be OMITTED when both bounds are sent
#     ("Only two of start or end or count should be provided")
#
# Expected unix-second values below are always COMPUTED from a datetime,
# never written as a literal, so a wrong constant cannot masquerade as a
# passing test.
# ---------------------------------------------------------------------

def _expected_unix_seconds(ts: str) -> int:
    """Unix seconds for a naive timestamp read as UTC.

    Mirrors _ms() above, which documents why .value (int64 nanoseconds
    since epoch, naive == UTC) is used rather than .timestamp(): the
    latter would interpret a naive Timestamp in the MACHINE's local
    timezone and make these assertions machine-dependent.
    """
    return int(pd.Timestamp(ts).value // 10**9)


# -- the conversion itself --------------------------------------------

def test_to_unix_seconds_naive_datetime_is_read_as_utc():
    assert quanthub._to_unix_seconds(datetime(2026, 9, 22, 0, 0, 0)) == _expected_unix_seconds(
        "2026-09-22 00:00:00"
    )


def test_to_unix_seconds_is_seconds_not_milliseconds():
    """The failure mode this guards is silent: QuantHub accepts unix
    milliseconds and returns ZERO ROWS rather than an error."""
    seconds = quanthub._to_unix_seconds(datetime(2026, 9, 22))
    milliseconds = _ms("2026-09-22")

    assert seconds == milliseconds // 1000
    assert seconds != milliseconds
    # ~1.78e9 for 2026; a millisecond value would be ~1.78e12.
    assert 1_000_000_000 < seconds < 10_000_000_000


def test_to_unix_seconds_timezone_aware_datetime_is_converted_to_utc():
    """A tz-aware value must be CONVERTED, not stripped: 02:00+02:00 is
    midnight UTC and must encode identically to a naive midnight."""
    aware = pd.Timestamp("2026-09-22 02:00:00", tz="Europe/Berlin")  # = 00:00 UTC
    assert quanthub._to_unix_seconds(aware) == _expected_unix_seconds("2026-09-22 00:00:00")


def test_to_unix_seconds_utc_aware_and_naive_agree():
    naive = datetime(2026, 9, 22, 13, 45, 0)
    aware = pd.Timestamp("2026-09-22 13:45:00", tz="UTC")
    assert quanthub._to_unix_seconds(naive) == quanthub._to_unix_seconds(aware)


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-22",
        datetime(2026, 9, 22),
        pd.Timestamp("2026-09-22"),
        datetime(2026, 9, 22).date(),
    ],
)
def test_to_unix_seconds_accepts_every_datelike_the_module_already_supports(value):
    """download_history_batch() accepts str/date/datetime (core.utils.
    DateLike); the encoder must handle the same set, not a narrower one."""
    assert quanthub._to_unix_seconds(value) == _expected_unix_seconds("2026-09-22")


def test_to_unix_seconds_floors_sub_second_precision():
    assert quanthub._to_unix_seconds(
        datetime(2026, 9, 22, 0, 0, 0, 999_999)
    ) == _expected_unix_seconds("2026-09-22 00:00:00")


# -- request-shape construction and validation ------------------------

def test_fetch_records_start_end_sends_unix_seconds_and_no_count(mocker):
    mock_get = _mock_response(mocker, json_body=[])

    quanthub._fetch_quanthub_records(
        ["SRAH24"], "1H",
        start=datetime(2026, 9, 1, 0, 0, 0),
        end=datetime(2026, 9, 22, 23, 59, 59),
    )

    params = mock_get.call_args.kwargs["params"]
    assert params == {
        "instruments": "SRAH24",
        "interval": "1H",
        "start": _expected_unix_seconds("2026-09-01 00:00:00"),
        "end": _expected_unix_seconds("2026-09-22 23:59:59"),
    }
    assert "count" not in params


def test_fetch_records_count_only_still_sends_count_and_no_date_range(mocker):
    """Regression guard for every existing caller."""
    mock_get = _mock_response(mocker, json_body=[])

    quanthub._fetch_quanthub_records(["SRAH24"], "1D", 5)

    params = mock_get.call_args.kwargs["params"]
    assert params == {"instruments": "SRAH24", "interval": "1D", "count": 5}
    assert "start" not in params
    assert "end" not in params


def test_fetch_records_start_end_and_count_rejected_before_any_http_call(mocker):
    """QuantHub answers this combination with HTTP 400 'Only two of start
    or end or count should be provided'. Catch it locally instead of
    spending a round trip to be told."""
    mock_get = _mock_response(mocker, json_body=[])

    with pytest.raises(ValueError, match="never both"):
        quanthub._fetch_quanthub_records(
            ["SRAH24"], "1D", 5,
            start=datetime(2026, 9, 1), end=datetime(2026, 9, 22),
        )

    mock_get.assert_not_called()


def test_invalid_request_shape_is_not_retried(mocker):
    """A deterministic parameter error cannot succeed on attempt two --
    it must not consume three attempts the way a 5xx legitimately does."""
    mock_get = _mock_response(mocker, json_body=[])

    with pytest.raises(ValueError):
        quanthub._fetch_quanthub_records(
            ["SRAH24"], "1D", 5, start=datetime(2026, 9, 1), end=datetime(2026, 9, 22)
        )

    assert mock_get.call_count == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": datetime(2026, 9, 1)},   # start without end
        {"end": datetime(2026, 9, 22)},    # end without start
    ],
)
def test_fetch_records_half_a_date_range_is_rejected(mocker, kwargs):
    """QuantHub does accept end+count, but Oscill8 does not use that
    shape; rejecting is better than shipping untested semantics."""
    mock_get = _mock_response(mocker, json_body=[])

    with pytest.raises(ValueError, match="BOTH start and end"):
        quanthub._fetch_quanthub_records(["SRAH24"], "1D", **kwargs)

    mock_get.assert_not_called()


def test_fetch_records_with_neither_count_nor_date_range_is_rejected(mocker):
    mock_get = _mock_response(mocker, json_body=[])

    with pytest.raises(ValueError, match="either count"):
        quanthub._fetch_quanthub_records(["SRAH24"], "1D")

    mock_get.assert_not_called()


def test_build_request_params_is_the_single_place_encoding_happens():
    """Both shapes come out of one builder, so there is exactly one
    place the unix-seconds decision can be got wrong."""
    count_params = quanthub._build_request_params(["A", "B"], "1D", 5, None, None)
    range_params = quanthub._build_request_params(
        ["A", "B"], "1D", None, datetime(2026, 9, 1), datetime(2026, 9, 22)
    )

    assert count_params == {"instruments": "A,B", "interval": "1D", "count": 5}
    assert range_params == {
        "instruments": "A,B",
        "interval": "1D",
        "start": _expected_unix_seconds("2026-09-01"),
        "end": _expected_unix_seconds("2026-09-22"),
    }


# -- download_history_batch: shape selection --------------------------

def test_download_history_batch_date_range_sends_start_end_covering_whole_days(mocker):
    """The end bound must cover the END DAY, not stop at its midnight --
    otherwise a request ending "today" would silently drop today's bars.
    """
    mock_get = _mock_response(mocker, json_body=[])

    quanthub.download_history_batch(
        ["SRAH24"], "HOURLY", "2026-09-01", "2026-09-22", use_date_range=True
    )

    params = mock_get.call_args.kwargs["params"]
    assert params["start"] == _expected_unix_seconds("2026-09-01 00:00:00")
    assert params["end"] == _expected_unix_seconds("2026-09-22 23:59:59")
    assert "count" not in params


def test_download_history_batch_defaults_to_the_count_shape(mocker):
    """No caller that omits the flag may have its request shape change."""
    mock_get = _mock_response(mocker, json_body=[])

    quanthub.download_history_batch(["SRAH24"], "DAILY", "2026-01-01", "2026-01-10")

    params = mock_get.call_args.kwargs["params"]
    assert "count" in params
    assert "start" not in params and "end" not in params


def test_download_history_batch_date_range_never_derives_or_sends_a_count(mocker):
    """A date-range request must not produce a `count` parameter.

    This previously asserted that _estimate_count was never CALLED.
    Automatic date chunking reuses that estimator as its row-density
    model -- deliberately, so the module has one density model rather
    than two -- so the call itself is no longer the right thing to
    assert. What actually matters, and what the original test was
    protecting, is that no count reaches the wire.
    """
    mock_get = _mock_response(mocker, json_body=[])

    quanthub.download_history_batch(
        ["SRAH24"], "HOURLY", "2026-03-23", "2026-09-22", use_date_range=True
    )

    for call in mock_get.call_args_list:
        params = call.kwargs["params"]
        assert "count" not in params
        assert "start" in params and "end" in params


def test_download_history_batch_date_range_batches_instruments_the_same_way(mocker):
    """Chunking is orthogonal to request shape: 13 instruments still
    means 2 requests, each carrying the same date range."""
    mock_get = _mock_response(mocker, json_body=[])
    instruments = [f"SON{m}26" for m in "FGHJKMNQUVXZ"] + ["SONF27"]  # 13 -> 2 chunks

    quanthub.download_history_batch(
        instruments, "DAILY", "2026-01-01", "2026-01-10", use_date_range=True
    )

    assert mock_get.call_count == 2
    sent = [c.kwargs["params"] for c in mock_get.call_args_list]
    assert {p["start"] for p in sent} == {_expected_unix_seconds("2026-01-01 00:00:00")}
    assert {p["end"] for p in sent} == {_expected_unix_seconds("2026-01-10 23:59:59")}
    assert all("count" not in p for p in sent)


def test_six_month_hourly_range_is_a_single_request(mocker):
    """The primary production case. Six months of hourly data measured
    1,378-2,989 rows per instrument live -- far inside the 10,000-row
    ceiling -- so it must take ONE request, with no chunking (which is a
    later task) and no count fallback."""
    mock_get = _mock_response(mocker, json_body=[])

    quanthub.download_history_batch(
        ["SRAU26"], "HOURLY", "2026-03-23", "2026-09-22", use_date_range=True
    )

    assert mock_get.call_count == 1
    params = mock_get.call_args.kwargs["params"]
    assert params["instruments"] == "SRAU26"
    assert params["interval"] == "1H"
    assert params["start"] == _expected_unix_seconds("2026-03-23 00:00:00")
    assert params["end"] == _expected_unix_seconds("2026-09-22 23:59:59")
    assert "count" not in params


# -- response parsing: identical output from both shapes ---------------

def test_date_range_response_parses_identically_to_a_count_response(mocker):
    """Same mocked payload through both shapes must produce the same
    normalized frame -- type, schema, dtypes and values."""
    records = _records_for_dates("SONH26", ["2026-01-05", "2026-01-06", "2026-01-07"])

    _mock_response(mocker, json_body=records)
    by_count = quanthub.download_history("SONH26", "DAILY", "2026-01-01", "2026-01-10")

    _mock_response(mocker, json_body=records)
    by_range = quanthub.download_history(
        "SONH26", "DAILY", "2026-01-01", "2026-01-10", use_date_range=True
    )

    pd.testing.assert_frame_equal(by_count, by_range)
    assert list(by_range.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert str(by_range["Date"].dtype).startswith("datetime64")
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        assert str(by_range[col].dtype) == "float64"


def test_date_range_response_is_still_filtered_and_four_hour_resampled(mocker):
    """The date-range shape reuses the existing normalize -> resample ->
    filter pipeline; it is not a second, parallel code path."""
    hourly = pd.date_range("2026-01-05 00:00", periods=8, freq="1h")
    records = [
        {
            "product": "SONH26", "time": int(ts.value // 10**6),
            "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
            "close": 100.5 + i, "volume": 10,
        }
        for i, ts in enumerate(hourly)
    ]
    mock_get = _mock_response(mocker, json_body=records)

    df = quanthub.download_history(
        "SONH26", "4H", "2026-01-05", "2026-01-05", use_date_range=True
    )

    # Fetched natively hourly, exactly as the count shape does.
    assert mock_get.call_args.kwargs["params"]["interval"] == "1H"
    assert len(df) == 2  # 8 hourly bars -> two 4H bars
    assert df.iloc[0]["Open"] == 100.0
    assert df.iloc[0]["Close"] == 103.5


# -- the 10,000-row ceiling, unchanged at this stage -------------------

def test_date_range_row_limit_error_propagates_uncaught(mocker):
    """A date range wide enough to exceed the shared 10,000-row ceiling
    gets QuantHub's own HTTP 400. At this stage that error is surfaced,
    NOT worked around: automatic chunking is a later task, and silently
    returning a truncated window would hide the condition chunking needs
    to detect."""
    _mock_response(mocker, status_code=400, text='{"error": "Max row limit exceeded (10000)"}')

    with pytest.raises(requests.exceptions.HTTPError, match="Max row limit exceeded"):
        quanthub.download_history_batch(
            ["SRAH24"], "HOURLY", "2020-01-01", "2026-09-22", use_date_range=True
        )


def test_row_limit_error_is_not_misclassified_as_another_quanthub_condition(mocker):
    """It is a plain provider HTTPError -- never the LSEG-specific
    MarketDataUnavailableError, never the rate-limit exception."""
    _mock_response(mocker, status_code=400, text='{"error": "Max row limit exceeded (10000)"}')

    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        quanthub.download_history_batch(
            ["SRAH24"], "HOURLY", "2020-01-01", "2026-09-22", use_date_range=True
        )

    assert not isinstance(excinfo.value, MarketDataUnavailableError)
    assert not isinstance(excinfo.value, quanthub.QuantHubRateLimitError)


def test_row_limit_400_on_a_single_day_is_attempted_exactly_once(mocker):
    """A row-limit 400 is never RETRIED -- the Task 3 guarantee.

    Automatic chunking later made a row-limit 400 over a MULTI-day range
    trigger a split into smaller ranges (different requests, not
    retries; see the chunking section below). A single day is the
    smallest range this client will request, so it is the case where
    chunking provably cannot intervene -- exactly one HTTP request, and
    a clear failure rather than a retry or an endless split.
    """
    mock_get = _mock_response(
        mocker, status_code=400, text='{"error": "Max row limit exceeded (10000)"}'
    )

    with pytest.raises(requests.exceptions.HTTPError, match="cannot be split further"):
        quanthub.download_history_batch(
            ["SRAH24"], "HOURLY", "2026-09-22", "2026-09-22", use_date_range=True
        )

    assert mock_get.call_count == 1


def test_date_range_rate_limit_error_still_raised(mocker):
    """The 429 contract is shape-independent. Retry COUNT changed with
    the rate-limit hardening (1 -> the shared 3-attempt budget); that
    the exception still reaches the caller did not."""
    mock_get = _mock_response(mocker, status_code=429, headers={"Retry-After": "1"})
    slept = _record_sleeps()
    mocker.patch.object(quanthub, "_fetch_quanthub_records", _fetch_no_sleep(slept))

    with pytest.raises(quanthub.QuantHubRateLimitError):
        quanthub.download_history_batch(
            ["SRAH24"], "HOURLY", "2026-03-23", "2026-09-22", use_date_range=True
        )

    assert mock_get.call_count == 3


def test_date_range_missing_credentials_raises_before_any_http_call(mocker, monkeypatch):
    monkeypatch.setattr(config, "QUANTHUB_TOKEN", "")
    mock_get = mocker.patch("core.quanthub.requests.get")

    with pytest.raises(quanthub.QuantHubCredentialsMissingError):
        quanthub.download_history_batch(
            ["SRAH24"], "DAILY", "2026-01-01", "2026-01-10", use_date_range=True
        )

    mock_get.assert_not_called()


# =====================================================================
# TASK 3: client-side rate limiting, 429 handling, deterministic 400s
#
# Timing is never real here. The rate limiter is driven by a fake
# monotonic clock, and tenacity's own backoff is redirected into a
# recorder via retry_with(sleep=...) -- tenacity exposes `sleep` as an
# injectable parameter precisely for this. No test in this section waits
# on the wall clock.
# =====================================================================

class _FakeClock:
    """A monotonic clock that only moves when a test says so, plus a
    sleep that advances it. Lets the limiter be asserted to the exact
    second without any real delay."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _record_sleeps() -> list[float]:
    """Collector for tenacity's inter-attempt backoff."""
    return []


def _fetch_no_sleep(recorder: list[float]):
    """core.quanthub._fetch_quanthub_records with tenacity's backoff
    redirected into `recorder` instead of the wall clock. The retry
    POLICY -- attempts, wait durations, which exceptions retry -- is
    entirely unchanged; only the sleeping is."""
    return quanthub._fetch_quanthub_records.retry_with(sleep=recorder.append)


# ---------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------

def test_configured_rate_is_below_the_observed_server_limit():
    """The whole point of the client limiter: stay under what QuantHub
    actually enforces, with headroom for jitter and retries."""
    assert quanthub.QUANTHUB_REQUESTS_PER_MINUTE == 25
    assert quanthub.OBSERVED_QUANTHUB_RATE_LIMIT_PER_MINUTE == 30
    assert quanthub.QUANTHUB_REQUESTS_PER_MINUTE < quanthub.OBSERVED_QUANTHUB_RATE_LIMIT_PER_MINUTE


def test_min_interval_matches_the_configured_rate():
    limiter = quanthub.QuantHubRateLimiter(25)
    assert limiter.min_interval_s == pytest.approx(2.4)

    limiter = quanthub.QuantHubRateLimiter(60)
    assert limiter.min_interval_s == pytest.approx(1.0)


def test_first_request_is_never_delayed():
    """A cold start must not pay for pacing it cannot have violated."""
    clock = _FakeClock()
    limiter = quanthub.QuantHubRateLimiter(25, monotonic=clock.monotonic, sleep=clock.sleep)

    assert limiter.acquire() == 0.0
    assert clock.sleeps == []


def test_second_immediate_request_waits_the_full_interval():
    clock = _FakeClock()
    limiter = quanthub.QuantHubRateLimiter(25, monotonic=clock.monotonic, sleep=clock.sleep)

    limiter.acquire()
    waited = limiter.acquire()

    assert waited == pytest.approx(2.4)
    assert clock.sleeps == [pytest.approx(2.4)]


def test_a_caller_that_was_already_slow_is_not_delayed_again():
    """Pacing must not punish a caller that already spent longer than
    the interval doing real work -- only genuine bursts are slowed."""
    clock = _FakeClock()
    limiter = quanthub.QuantHubRateLimiter(25, monotonic=clock.monotonic, sleep=clock.sleep)

    limiter.acquire()
    clock.advance(10.0)  # e.g. a slow response plus cache writes

    assert limiter.acquire() == 0.0
    assert clock.sleeps == []


def test_sustained_sequential_requests_hold_the_configured_rate():
    """25 requests must span at least a minute, i.e. the achieved rate
    never exceeds the configured one."""
    clock = _FakeClock()
    limiter = quanthub.QuantHubRateLimiter(25, monotonic=clock.monotonic, sleep=clock.sleep)

    started = clock.now
    for _ in range(25):
        limiter.acquire()
    elapsed = clock.now - started

    # 25 slots => 24 gaps of 2.4s once the first is free.
    assert elapsed == pytest.approx(57.6)
    achieved_per_minute = 25 / (elapsed / 60.0) if elapsed else float("inf")
    assert achieved_per_minute <= quanthub.OBSERVED_QUANTHUB_RATE_LIMIT_PER_MINUTE


def test_reset_lets_the_next_request_through_immediately():
    clock = _FakeClock()
    limiter = quanthub.QuantHubRateLimiter(25, monotonic=clock.monotonic, sleep=clock.sleep)

    limiter.acquire()
    limiter.reset()

    assert limiter.acquire() == 0.0


def test_invalid_rate_is_rejected():
    for bad in (0, -1):
        with pytest.raises(ValueError, match="must be positive"):
            quanthub.QuantHubRateLimiter(bad)


def test_concurrent_callers_cannot_collectively_bypass_the_limiter():
    """Streamlit runs each browser session's script in its own thread
    inside one process, so two sessions scanning at once reach the
    limiter in parallel. Each must take a DISTINCT, properly-spaced
    slot -- never all read the same "last request" timestamp and race
    through together.

    Uses a real lock with a real (but instant) sleep, and asserts on the
    reserved slots rather than on wall-clock timing, so the test is
    deterministic rather than flaky-by-construction.
    """
    import threading

    limiter = quanthub.QuantHubRateLimiter(25, sleep=lambda _s: None)
    waits: list[float] = []
    waits_lock = threading.Lock()
    start_together = threading.Barrier(8)

    def worker():
        start_together.wait()
        waited = limiter.acquire()
        with waits_lock:
            waits.append(waited)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(waits) == 8
    # One caller wins the first slot; every other caller is spaced behind
    # it by a distinct multiple of the interval. If the lock were missing,
    # several threads would compute a 0.0 wait from the same timestamp.
    assert sorted(waits)[0] == 0.0
    assert len([w for w in waits if w == 0.0]) == 1
    ordered = sorted(waits)
    for earlier, later in zip(ordered, ordered[1:]):
        assert later - earlier == pytest.approx(limiter.min_interval_s, abs=0.05)


def test_the_request_path_acquires_a_rate_limit_slot(mocker):
    """The limiter must sit on the ACTUAL HTTP path, not merely exist."""
    _mock_response(mocker, json_body=[])
    acquire = mocker.patch.object(quanthub._RATE_LIMITER, "acquire", return_value=0.0)

    quanthub._fetch_quanthub_records(["SONH26"], "1D", 5)

    assert acquire.call_count == 1


def test_each_batch_chunk_takes_its_own_rate_limit_slot(mocker):
    """Chunking is where burst risk actually comes from -- one logical
    download can be several HTTP requests."""
    _mock_response(mocker, json_body=[])
    acquire = mocker.patch.object(quanthub._RATE_LIMITER, "acquire", return_value=0.0)
    instruments = [f"SON{m}26" for m in "FGHJKMNQUVXZ"] + ["SONF27"]  # 13 -> 2 chunks

    quanthub.download_history_batch(instruments, "DAILY", "2026-01-01", "2026-01-10")

    assert acquire.call_count == 2


def test_a_request_that_is_never_sent_does_not_consume_a_slot(mocker):
    """Validation and credential failures happen before the wire, so
    they must not burn rate budget (nor make a failing test sleep)."""
    acquire = mocker.patch.object(quanthub._RATE_LIMITER, "acquire", return_value=0.0)
    mocker.patch("core.quanthub.requests.get")

    with pytest.raises(ValueError):
        quanthub._fetch_quanthub_records(
            ["SRAH24"], "1D", 5, start=datetime(2026, 9, 1), end=datetime(2026, 9, 22)
        )

    acquire.assert_not_called()


def test_missing_credentials_does_not_consume_a_slot(mocker, monkeypatch):
    monkeypatch.setattr(config, "QUANTHUB_TOKEN", "")
    acquire = mocker.patch.object(quanthub._RATE_LIMITER, "acquire", return_value=0.0)
    mocker.patch("core.quanthub.requests.get")

    with pytest.raises(quanthub.QuantHubCredentialsMissingError):
        quanthub._fetch_quanthub_records(["SONH26"], "1D", 5)

    acquire.assert_not_called()


def test_every_retry_attempt_takes_its_own_slot(mocker):
    """A retry is a real HTTP request and must count against the rate
    budget -- otherwise a retry storm would silently exceed it."""
    _mock_response(mocker, status_code=500, raise_exc=requests.exceptions.HTTPError("500"))
    acquire = mocker.patch.object(quanthub._RATE_LIMITER, "acquire", return_value=0.0)
    slept = _record_sleeps()

    with pytest.raises(requests.exceptions.HTTPError):
        _fetch_no_sleep(slept)(["SONH26"], "1D", 5)

    assert acquire.call_count == 3


# ---------------------------------------------------------------------
# HTTP 400: deterministic, never retried
# ---------------------------------------------------------------------

def test_http_400_is_attempted_exactly_once(mocker):
    mock_get = _mock_response(
        mocker, status_code=400, text='{"error": "Max row limit exceeded (10000)"}'
    )
    slept = _record_sleeps()

    with pytest.raises(quanthub.QuantHubRequestError):
        _fetch_no_sleep(slept)(["SRAH24"], "1H", 10_001)

    assert mock_get.call_count == 1
    assert slept == []  # no backoff spent on a guaranteed failure


def test_http_400_preserves_the_error_body(mocker):
    """The body is the only place QuantHub says WHICH validation failed,
    so it must reach the caller rather than being swallowed."""
    _mock_response(
        mocker, status_code=400, text='{"error": "Max row limit exceeded (10000)"}'
    )

    with pytest.raises(quanthub.QuantHubRequestError) as excinfo:
        quanthub._fetch_quanthub_records(["SRAH24"], "1H", 10_001)

    exc = excinfo.value
    assert "Max row limit exceeded (10000)" in str(exc)
    assert exc.response_body == '{"error": "Max row limit exceeded (10000)"}'
    assert exc.status_code == 400
    assert exc.response is not None


def test_http_400_stays_compatible_with_callers_catching_http_error(mocker):
    """Tightening retry behaviour must not change the exception CONTRACT
    -- QuantHubRequestError subclasses requests HTTPError so existing
    callers and tests keep working."""
    _mock_response(mocker, status_code=400, text='{"error": "bad request"}')

    assert issubclass(quanthub.QuantHubRequestError, requests.exceptions.HTTPError)
    with pytest.raises(requests.exceptions.HTTPError):
        quanthub._fetch_quanthub_records(["SRAH24"], "1D", 5)


def test_http_400_is_never_silently_turned_into_an_empty_frame(mocker):
    """A rejected request must fail loudly; an empty DataFrame would be
    indistinguishable from "this instrument genuinely has no data"."""
    _mock_response(mocker, status_code=400, text='{"error": "bad request"}')

    with pytest.raises(requests.exceptions.HTTPError):
        quanthub.download_history("SRAH24", "DAILY", "2026-01-01", "2026-01-10")


def test_http_400_is_not_confused_with_the_other_quanthub_conditions(mocker):
    _mock_response(mocker, status_code=400, text='{"error": "bad request"}')

    with pytest.raises(quanthub.QuantHubRequestError) as excinfo:
        quanthub._fetch_quanthub_records(["SRAH24"], "1D", 5)

    assert not isinstance(excinfo.value, quanthub.QuantHubRateLimitError)
    assert not isinstance(excinfo.value, quanthub.QuantHubCredentialsMissingError)
    assert not isinstance(excinfo.value, MarketDataUnavailableError)


# ---------------------------------------------------------------------
# HTTP 429: Retry-After honoured, bounded fallback, bounded attempts
# ---------------------------------------------------------------------

def test_429_with_retry_after_waits_for_exactly_that_long(mocker):
    _mock_response(mocker, status_code=429, headers={"Retry-After": "3"},
                   text='{"error": "Rate limit exceeded", "limit": 30, "window": "minute"}')
    slept = _record_sleeps()

    with pytest.raises(quanthub.QuantHubRateLimitError):
        _fetch_no_sleep(slept)(["YBAH28"], "1H", 4416)

    # Two waits for three attempts, both taken from the header.
    assert slept == [3.0, 3.0]


def test_429_retry_after_is_parsed_onto_the_exception(mocker):
    _mock_response(mocker, status_code=429, headers={"Retry-After": "7"},
                   text='{"error": "Rate limit exceeded"}')
    slept = _record_sleeps()

    with pytest.raises(quanthub.QuantHubRateLimitError) as excinfo:
        _fetch_no_sleep(slept)(["YBAH28"], "1H", 4416)

    exc = excinfo.value
    assert exc.retry_after == 7.0
    assert "Retry-After: 7.0s" in str(exc)
    assert "Rate limit exceeded" in str(exc)


def test_429_without_retry_after_uses_a_bounded_fallback(mocker):
    _mock_response(mocker, status_code=429, text='{"error": "Rate limit exceeded"}')
    slept = _record_sleeps()

    with pytest.raises(quanthub.QuantHubRateLimitError) as excinfo:
        _fetch_no_sleep(slept)(["YBAH28"], "1H", 4416)

    assert excinfo.value.retry_after is None
    assert len(slept) == 2
    # Bounded, and well clear of the sub-second retries that would be
    # useless against a per-minute limit.
    for wait in slept:
        assert 5.0 <= wait <= quanthub.QUANTHUB_MAX_RETRY_AFTER_WAIT_SECONDS


@pytest.mark.parametrize(
    "header_value",
    ["", "   ", "soon", "Wed, 21 Oct 2026 07:28:00 GMT", "-5", "NaN"],
)
def test_429_with_a_malformed_retry_after_falls_back_rather_than_guessing(mocker, header_value):
    """An HTTP-date, junk, or a negative value must not be coerced into
    a delay -- a misparsed date silently becomes an absurd wait."""
    _mock_response(mocker, status_code=429, headers={"Retry-After": header_value})
    slept = _record_sleeps()

    with pytest.raises(quanthub.QuantHubRateLimitError) as excinfo:
        _fetch_no_sleep(slept)(["YBAH28"], "1H", 4416)

    assert excinfo.value.retry_after is None
    for wait in slept:
        assert 5.0 <= wait <= quanthub.QUANTHUB_MAX_RETRY_AFTER_WAIT_SECONDS


def test_429_retry_after_is_clamped_to_the_maximum(mocker):
    """A provider bug or typo'd header must never hang a scan for hours."""
    _mock_response(mocker, status_code=429, headers={"Retry-After": "86400"})
    slept = _record_sleeps()

    with pytest.raises(quanthub.QuantHubRateLimitError):
        _fetch_no_sleep(slept)(["YBAH28"], "1H", 4416)

    assert slept == [
        quanthub.QUANTHUB_MAX_RETRY_AFTER_WAIT_SECONDS,
        quanthub.QUANTHUB_MAX_RETRY_AFTER_WAIT_SECONDS,
    ]


def test_429_followed_by_success_returns_the_data(mocker):
    """The point of retrying a rate limit: transient by nature, so the
    caller should get its data rather than an error."""
    mock_get = _mock_responses(
        mocker,
        [
            _response(status_code=429, headers={"Retry-After": "1"}),
            _response(json_body=_SAMPLE_RECORDS),
        ],
    )
    slept = _record_sleeps()

    grouped = _fetch_no_sleep(slept)(["SONH26"], "1D", 5)

    assert mock_get.call_count == 2
    assert slept == [1.0]
    assert grouped == {"SONH26": _SAMPLE_RECORDS}


def test_429_then_429_then_success_still_returns_the_data(mocker):
    """Recovery on the final attempt of the shared 3-attempt budget."""
    mock_get = _mock_responses(
        mocker,
        [
            _response(status_code=429, headers={"Retry-After": "1"}),
            _response(status_code=429, headers={"Retry-After": "2"}),
            _response(json_body=_SAMPLE_RECORDS),
        ],
    )
    slept = _record_sleeps()

    grouped = _fetch_no_sleep(slept)(["SONH26"], "1D", 5)

    assert mock_get.call_count == 3
    assert slept == [1.0, 2.0]
    assert grouped == {"SONH26": _SAMPLE_RECORDS}


def test_repeated_429_eventually_raises_after_the_attempt_budget(mocker):
    mock_get = _mock_response(mocker, status_code=429, headers={"Retry-After": "1"})
    slept = _record_sleeps()

    with pytest.raises(quanthub.QuantHubRateLimitError):
        _fetch_no_sleep(slept)(["YBAH28"], "1H", 4416)

    assert mock_get.call_count == 3  # the shared budget, not a new one
    assert len(slept) == 2


def test_429_does_not_get_its_own_extra_attempt_budget(mocker):
    """A 429 must consume the SAME 3 attempts a 5xx does -- otherwise a
    rate-limited scan would multiply the very requests that caused it."""
    mock_429 = _mock_response(mocker, status_code=429, headers={"Retry-After": "1"})
    slept = _record_sleeps()
    with pytest.raises(quanthub.QuantHubRateLimitError):
        _fetch_no_sleep(slept)(["YBAH28"], "1H", 4416)
    rate_limited_attempts = mock_429.call_count

    mocker.stopall()
    mock_500 = _mock_response(
        mocker, status_code=500, raise_exc=requests.exceptions.HTTPError("500")
    )
    slept2 = _record_sleeps()
    with pytest.raises(requests.exceptions.HTTPError):
        _fetch_no_sleep(slept2)(["SONH26"], "1D", 5)

    assert rate_limited_attempts == mock_500.call_count == 3


def test_rate_limit_error_still_propagates_through_the_public_entry_point(mocker):
    _mock_response(mocker, status_code=429, headers={"Retry-After": "1"})
    slept = _record_sleeps()
    mocker.patch.object(quanthub, "_fetch_quanthub_records", _fetch_no_sleep(slept))

    with pytest.raises(quanthub.QuantHubRateLimitError):
        quanthub.download_history_batch(
            ["SRAH24"], "HOURLY", "2026-03-23", "2026-09-22", use_date_range=True
        )


# ---------------------------------------------------------------------
# Transient failures: unchanged
# ---------------------------------------------------------------------

def test_transient_backoff_durations_are_unchanged(mocker):
    """The 5xx/network policy must be byte-identical to before rate-limit
    handling existed: wait_exponential(multiplier=1, min=2, max=10)."""
    _mock_response(mocker, status_code=500, raise_exc=requests.exceptions.HTTPError("500"))
    slept = _record_sleeps()

    with pytest.raises(requests.exceptions.HTTPError):
        _fetch_no_sleep(slept)(["SONH26"], "1D", 5)

    # wait_exponential(multiplier=1, min=2, max=10) clamps the first two
    # attempts up to its own floor, giving 2.0 then 2.0 -- verified
    # against a freshly-constructed copy of the original policy object.
    assert slept == [2.0, 2.0]


def test_connection_error_backoff_is_unchanged(mocker):
    mocker.patch(
        "core.quanthub.requests.get", side_effect=requests.exceptions.ConnectionError("boom")
    )
    slept = _record_sleeps()

    with pytest.raises(requests.exceptions.ConnectionError):
        _fetch_no_sleep(slept)(["SONH26"], "1D", 5)

    assert slept == [2.0, 2.0]


def test_timeout_still_retries(mocker):
    mock_get = mocker.patch(
        "core.quanthub.requests.get", side_effect=requests.exceptions.Timeout("slow")
    )
    slept = _record_sleeps()

    with pytest.raises(requests.exceptions.Timeout):
        _fetch_no_sleep(slept)(["SONH26"], "1D", 5)

    assert mock_get.call_count == 3


def test_a_500_is_still_not_classified_as_market_data_unavailable(mocker):
    """The 4xx classification work must not have leaked into 5xx."""
    _mock_response(mocker, status_code=500, raise_exc=requests.exceptions.HTTPError("500"))
    slept = _record_sleeps()

    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        _fetch_no_sleep(slept)(["SONH26"], "1D", 5)

    assert not isinstance(excinfo.value, MarketDataUnavailableError)
    assert not isinstance(excinfo.value, quanthub.QuantHubRequestError)
    assert not isinstance(excinfo.value, quanthub.QuantHubRateLimitError)


@pytest.mark.parametrize("status_code", [401, 403, 404, 502, 503])
def test_other_error_statuses_keep_the_generic_retrying_path(mocker, status_code):
    """Only 400 and 429 are specially classified; nothing else changed."""
    _mock_response(
        mocker, status_code=status_code,
        raise_exc=requests.exceptions.HTTPError(str(status_code)),
    )
    slept = _record_sleeps()

    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        _fetch_no_sleep(slept)(["SONH26"], "1D", 5)

    assert not isinstance(excinfo.value, quanthub.QuantHubRequestError)
    assert not isinstance(excinfo.value, quanthub.QuantHubRateLimitError)


# ---------------------------------------------------------------------
# Retry-After parsing, directly
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("3", 3.0),
        ("0", 0.0),
        ("2.5", 2.5),
        (" 4 ", 4.0),
        (7, 7.0),
    ],
)
def test_parse_retry_after_accepts_delay_seconds(raw, expected):
    assert quanthub._parse_retry_after(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "later", "Wed, 21 Oct 2026 07:28:00 GMT", "-1", "nan", "inf"],
)
def test_parse_retry_after_rejects_anything_it_cannot_trust(raw):
    assert quanthub._parse_retry_after(raw) is None


# =====================================================================
# TASK 4: automatic date-range chunking for the 10,000-row ceiling
#
# Two independent dimensions, never conflated here:
#   instruments per request  -- QUANTHUB_BATCH_SIZE, unchanged
#   days per request         -- everything in this section
#
# The strategy is proactive sizing (from _estimate_count, the module's
# one density model) plus a reactive safety net that splits only on
# QuantHub's specific row-limit 400.
# =====================================================================

def _row_limit_response():
    """QuantHub's live-confirmed row-ceiling rejection."""
    return _response(
        status_code=400, text='{"error": "Max row limit exceeded (10000)"}'
    )


def _records_for_range(product: str, start: str, end: str, freq: str = "D", seed=100.0):
    """One record per period across an inclusive date range, in the raw
    QuantHub wire shape (unix-ms `time`)."""
    idx = pd.date_range(start, end, freq=freq)
    return [
        {
            "product": product,
            "time": int(ts.value // 10**6),
            "open": seed + i, "high": seed + i + 1, "low": seed + i - 1,
            "close": seed + i + 0.5, "volume": 10 + i,
        }
        for i, ts in enumerate(idx)
    ]


def _requested_windows(mock_get):
    """The (start, end) datetimes of every request actually sent."""
    return [
        (
            pd.to_datetime(c.kwargs["params"]["start"], unit="s"),
            pd.to_datetime(c.kwargs["params"]["end"], unit="s"),
        )
        for c in mock_get.call_args_list
    ]


# ---------------------------------------------------------------------
# Proactive sizing
# ---------------------------------------------------------------------

def test_row_density_model_is_shared_with_the_count_shape():
    """Chunk sizing must not invent a second density model -- it reuses
    _estimate_count, which is documented as a generous UPPER bound and
    is therefore safe to size against."""
    anchor = datetime(2000, 1, 1)
    for native, days, instruments in (("1D", 100, 3), ("1H", 30, 2)):
        expected = quanthub._estimate_count(
            native, anchor, anchor + pd.Timedelta(days=days - 1).to_pytimedelta()
        ) * instruments
        assert quanthub._estimated_rows_for_day_span(native, days, instruments) == expected


def test_estimated_rows_scale_with_instrument_count():
    """The ceiling is on the TOTAL response, so instruments multiply."""
    one = quanthub._estimated_rows_for_day_span("1H", 30, 1)
    ten = quanthub._estimated_rows_for_day_span("1H", 30, 10)
    assert ten == one * 10


@pytest.mark.parametrize("native, instruments", [("1D", 1), ("1D", 10), ("1H", 1), ("1H", 10)])
def test_chunk_size_never_exceeds_the_row_ceiling(native, instruments):
    days = quanthub._max_days_per_chunk(native, instruments, 100_000)
    assert (
        quanthub._estimated_rows_for_day_span(native, days, instruments)
        <= quanthub.QUANTHUB_MAX_ROWS_PER_REQUEST
    )


@pytest.mark.parametrize("native, instruments", [("1D", 1), ("1D", 10), ("1H", 1), ("1H", 10)])
def test_chunk_size_is_the_largest_that_fits(native, instruments):
    """Not merely safe -- maximal, so no request is split more than the
    ceiling actually requires."""
    days = quanthub._max_days_per_chunk(native, instruments, 100_000)
    assert (
        quanthub._estimated_rows_for_day_span(native, days + 1, instruments)
        > quanthub.QUANTHUB_MAX_ROWS_PER_REQUEST
    )


def test_a_more_dense_interval_gets_smaller_chunks():
    """Interval-awareness falls out of the density model rather than
    being a hard-coded table of spans."""
    assert quanthub._max_days_per_chunk("1H", 1, 100_000) < quanthub._max_days_per_chunk(
        "1D", 1, 100_000
    )


def test_chunk_size_is_capped_by_the_requested_span():
    """A short request is never padded out to the maximum chunk."""
    assert quanthub._max_days_per_chunk("1D", 1, 7) == 7


def test_chunk_size_is_at_least_one_day():
    assert quanthub._max_days_per_chunk("1H", 10_000, 100_000) == 1


# ---------------------------------------------------------------------
# Day-range splitting: no gaps, no overlaps, no lost days
# ---------------------------------------------------------------------

def test_day_chunks_are_contiguous_non_overlapping_and_complete():
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    chunks = quanthub._day_chunks(start, end, 3)

    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    # Every day covered exactly once.
    covered = [d for lo, hi in chunks for d in pd.date_range(lo, hi, freq="D")]
    assert len(covered) == len(set(covered)) == 10
    # Each chunk begins the day after the previous ends.
    for (_lo, prev_hi), (next_lo, _hi) in zip(chunks, chunks[1:]):
        assert next_lo == prev_hi + timedelta(days=1)


def test_day_chunks_single_day_range():
    assert quanthub._day_chunks(date(2026, 1, 1), date(2026, 1, 1), 30) == [
        (date(2026, 1, 1), date(2026, 1, 1))
    ]


def test_day_chunks_span_smaller_than_chunk_size_is_one_chunk():
    assert quanthub._day_chunks(date(2026, 1, 1), date(2026, 1, 5), 30) == [
        (date(2026, 1, 1), date(2026, 1, 5))
    ]


def test_day_chunks_rejects_a_zero_or_negative_chunk_size():
    for bad in (0, -1):
        with pytest.raises(ValueError, match="must be >= 1"):
            quanthub._day_chunks(date(2026, 1, 1), date(2026, 1, 10), bad)


# ---------------------------------------------------------------------
# A request that fits is sent exactly once
# ---------------------------------------------------------------------

def test_six_month_single_instrument_hourly_is_still_one_request(mocker):
    """The benchmark measured ~2,989 rows for this -- comfortably under
    the ceiling. It must NOT be split; over-chunking a normal request
    would waste rate-limiter budget for nothing."""
    mock_get = _mock_response(mocker, json_body=[])

    quanthub.download_history_batch(
        ["SRAU26"], "HOURLY", "2026-03-23", "2026-09-22", use_date_range=True
    )

    assert mock_get.call_count == 1


def test_six_month_daily_batch_is_still_one_request_per_instrument_batch(mocker):
    mock_get = _mock_response(mocker, json_body=[])
    instruments = [f"SON{m}26" for m in "FGHJKMNQU"]  # 9 -> one instrument batch

    quanthub.download_history_batch(
        instruments, "DAILY", "2026-03-23", "2026-09-22", use_date_range=True
    )

    assert mock_get.call_count == 1


def test_short_range_is_never_split(mocker):
    mock_get = _mock_response(mocker, json_body=[])

    quanthub.download_history_batch(
        ["SRAU26"], "DAILY", "2026-09-01", "2026-09-05", use_date_range=True
    )

    assert mock_get.call_count == 1


# ---------------------------------------------------------------------
# Proactive splitting of an oversized range
# ---------------------------------------------------------------------

def test_multi_instrument_hourly_range_is_split_by_total_rows(mocker):
    """THE MULTI-INSTRUMENT REQUIREMENT. 10 instruments x ~2,000 hourly
    bars is ~20,000 rows -- over the ceiling even though no single
    instrument is. The ceiling applies to the whole response, so the
    range must be split for the batch, NOT assumed to be 10,000 per
    instrument."""
    mock_get = _mock_response(mocker, json_body=[])
    instruments = [f"SON{m}26" for m in "FGHJKMNQUV"]  # exactly 10 -> one batch

    quanthub.download_history_batch(
        instruments, "HOURLY", "2026-03-23", "2026-09-22", use_date_range=True
    )

    assert mock_get.call_count > 1
    # One instrument batch, so every request carries all 10 instruments
    # and differs only in its date window.
    for call in mock_get.call_args_list:
        assert call.kwargs["params"]["instruments"] == ",".join(instruments)

    windows = _requested_windows(mock_get)
    for _start, _end in windows:
        span_days = (_end - _start).days + 1
        assert (
            quanthub._estimated_rows_for_day_span("1H", span_days, 10)
            <= quanthub.QUANTHUB_MAX_ROWS_PER_REQUEST
        )


def test_split_windows_cover_the_request_without_gaps_or_overlap(mocker):
    """Boundary safety: no day skipped, no day fetched twice."""
    mock_get = _mock_response(mocker, json_body=[])
    instruments = [f"SON{m}26" for m in "FGHJKMNQUV"]

    quanthub.download_history_batch(
        instruments, "HOURLY", "2026-01-01", "2026-06-30", use_date_range=True
    )

    windows = sorted(_requested_windows(mock_get))
    assert windows[0][0] == pd.Timestamp("2026-01-01 00:00:00")
    assert windows[-1][1] == pd.Timestamp("2026-06-30 23:59:59")
    for (_lo, prev_hi), (next_lo, _hi) in zip(windows, windows[1:]):
        # Next window starts the instant after the previous day ends.
        assert next_lo == prev_hi.normalize() + pd.Timedelta(days=1)


def test_every_split_request_uses_whole_day_bounds(mocker):
    """Whole-day bounds are what keep 4H bucketing safe and match the
    convention established before chunking existed."""
    mock_get = _mock_response(mocker, json_body=[])
    instruments = [f"SON{m}26" for m in "FGHJKMNQUV"]

    quanthub.download_history_batch(
        instruments, "4H", "2026-01-01", "2026-06-30", use_date_range=True
    )

    for start, end in _requested_windows(mock_get):
        assert (start.hour, start.minute, start.second) == (0, 0, 0)
        assert (end.hour, end.minute, end.second) == (23, 59, 59)


# ---------------------------------------------------------------------
# Reactive splitting on the row-limit 400
# ---------------------------------------------------------------------

def test_row_limit_rejection_triggers_a_split_and_succeeds(mocker):
    """The safety net: a range the estimate thought would fit is
    rejected, so it is halved and the halves succeed."""
    mock_get = _mock_responses(
        mocker,
        [
            _row_limit_response(),                                        # 1-20 Jan: rejected
            _response(json_body=_records_for_range("SRAU26", "2026-01-01", "2026-01-10")),
            _response(json_body=_records_for_range("SRAU26", "2026-01-11", "2026-01-20")),
        ],
    )

    result = quanthub.download_history_batch(
        ["SRAU26"], "DAILY", "2026-01-01", "2026-01-20", use_date_range=True
    )

    assert mock_get.call_count == 3
    windows = _requested_windows(mock_get)
    assert windows[0] == (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-20 23:59:59"))
    assert windows[1] == (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-10 23:59:59"))
    assert windows[2] == (pd.Timestamp("2026-01-11"), pd.Timestamp("2026-01-20 23:59:59"))

    df = result["SRAU26"]
    assert len(df) == 20
    assert df["Date"].is_monotonic_increasing
    assert int(df["Date"].duplicated().sum()) == 0


def test_recursive_split_when_the_first_halving_is_still_too_large(mocker):
    """Splitting is recursive: the left half is still rejected and is
    halved again before succeeding."""
    mock_get = _mock_responses(
        mocker,
        [
            _row_limit_response(),   # 1-20 rejected
            _row_limit_response(),   # 1-10 still rejected
            _response(json_body=_records_for_range("SRAU26", "2026-01-01", "2026-01-05")),
            _response(json_body=_records_for_range("SRAU26", "2026-01-06", "2026-01-10")),
            _response(json_body=_records_for_range("SRAU26", "2026-01-11", "2026-01-20")),
        ],
    )

    result = quanthub.download_history_batch(
        ["SRAU26"], "DAILY", "2026-01-01", "2026-01-20", use_date_range=True
    )

    assert mock_get.call_count == 5
    df = result["SRAU26"]
    assert len(df) == 20
    assert df["Date"].min() == pd.Timestamp("2026-01-01")
    assert df["Date"].max() == pd.Timestamp("2026-01-20")
    assert int(df["Date"].duplicated().sum()) == 0


def test_recursive_split_is_depth_bounded_and_fails_clearly(mocker):
    """A provider that rejects everything must produce a finite, clear
    failure rather than an exponential request storm."""
    mock_get = _mock_response(
        mocker, status_code=400, text='{"error": "Max row limit exceeded (10000)"}'
    )

    with pytest.raises(quanthub.QuantHubRequestError) as excinfo:
        quanthub.download_history_batch(
            ["SRAU26"], "DAILY", "2026-01-01", "2026-03-31", use_date_range=True
        )

    message = str(excinfo.value)
    assert "cannot be split further" in message or "halvings" in message
    # Bounded: a single day is the floor, so at most ~2x the number of
    # days can ever be attempted -- never unbounded.
    assert mock_get.call_count < 400


def test_a_single_day_rejection_is_not_split_further(mocker):
    """The recursion floor. A day cannot be halved, so the client says
    so plainly instead of looping."""
    mock_get = _mock_response(
        mocker, status_code=400, text='{"error": "Max row limit exceeded (10000)"}'
    )

    with pytest.raises(quanthub.QuantHubRequestError, match="cannot be split further"):
        quanthub.download_history_batch(
            ["SRAU26"], "DAILY", "2026-01-01", "2026-01-01", use_date_range=True
        )

    assert mock_get.call_count == 1


def test_single_day_failure_message_suggests_the_real_remedy(mocker):
    """When a day is genuinely too dense, the only lever left is fewer
    instruments per request -- the message must say so."""
    _mock_response(mocker, status_code=400, text='{"error": "Max row limit exceeded (10000)"}')

    with pytest.raises(quanthub.QuantHubRequestError) as excinfo:
        quanthub.download_history_batch(
            ["SRAU26"], "HOURLY", "2026-01-01", "2026-01-01", use_date_range=True
        )

    message = str(excinfo.value)
    assert "fewer instruments" in message
    assert "Max row limit exceeded" in message  # original cause preserved


# ---------------------------------------------------------------------
# Only the row-limit condition triggers chunking
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        '{"error": "Only two of start or end or count should be provided"}',
        '{"error": "Invalid instrument"}',
        '{"error": "malformed parameter"}',
        "",
    ],
)
def test_a_non_row_limit_400_never_triggers_chunking(mocker, body):
    """Splitting a range cannot fix a malformed request, and splitting
    on it would turn one clear error into a burst of identical ones."""
    mock_get = _mock_response(mocker, status_code=400, text=body)

    with pytest.raises(quanthub.QuantHubRequestError):
        quanthub.download_history_batch(
            ["SRAU26"], "DAILY", "2026-01-01", "2026-03-31", use_date_range=True
        )

    assert mock_get.call_count == 1


def test_is_row_limit_error_matches_only_the_row_limit_body():
    matching = quanthub.QuantHubRequestError(
        "rejected", response_body='{"error": "Max row limit exceeded (10000)"}'
    )
    assert quanthub._is_row_limit_error(matching)

    for other in (
        '{"error": "Only two of start or end or count should be provided"}',
        '{"error": "Invalid instrument"}',
        None,
    ):
        assert not quanthub._is_row_limit_error(
            quanthub.QuantHubRequestError("rejected", response_body=other)
        )


def test_429_is_not_treated_as_a_row_limit_condition(mocker):
    """Task 3 owns 429; chunking must not intercept it."""
    mock_get = _mock_response(mocker, status_code=429, headers={"Retry-After": "1"})
    slept = _record_sleeps()
    mocker.patch.object(quanthub, "_fetch_quanthub_records", _fetch_no_sleep(slept))

    with pytest.raises(quanthub.QuantHubRateLimitError):
        quanthub.download_history_batch(
            ["SRAU26"], "DAILY", "2026-01-01", "2026-03-31", use_date_range=True
        )

    assert mock_get.call_count == 3  # Task 3's retry budget, not a split


def test_5xx_is_not_treated_as_a_row_limit_condition(mocker):
    mock_get = _mock_response(
        mocker, status_code=500, raise_exc=requests.exceptions.HTTPError("500")
    )
    slept = _record_sleeps()
    mocker.patch.object(quanthub, "_fetch_quanthub_records", _fetch_no_sleep(slept))

    with pytest.raises(requests.exceptions.HTTPError):
        quanthub.download_history_batch(
            ["SRAU26"], "DAILY", "2026-01-01", "2026-03-31", use_date_range=True
        )

    assert mock_get.call_count == 3  # transient retry, not a split


def test_timeout_is_not_treated_as_a_row_limit_condition(mocker):
    mock_get = mocker.patch(
        "core.quanthub.requests.get", side_effect=requests.exceptions.Timeout("slow")
    )
    slept = _record_sleeps()
    mocker.patch.object(quanthub, "_fetch_quanthub_records", _fetch_no_sleep(slept))

    with pytest.raises(requests.exceptions.Timeout):
        quanthub.download_history_batch(
            ["SRAU26"], "DAILY", "2026-01-01", "2026-03-31", use_date_range=True
        )

    assert mock_get.call_count == 3


def test_an_empty_response_never_causes_splitting(mocker):
    """An empty 200 means the instrument has no data there -- not that
    the request was too large."""
    mock_get = _mock_response(mocker, json_body=[])

    result = quanthub.download_history_batch(
        ["SRAU26"], "DAILY", "2026-01-01", "2026-01-20", use_date_range=True
    )

    assert mock_get.call_count == 1
    assert result["SRAU26"].empty


# ---------------------------------------------------------------------
# Result combination
# ---------------------------------------------------------------------

def test_merge_grouped_extends_rather_than_replaces():
    """The bug this guards is silent: dict.update() across date chunks
    would keep only the last chunk and still return a plausible frame."""
    dest = {"A": [{"time": 1}]}
    quanthub._merge_grouped(dest, {"A": [{"time": 2}], "B": [{"time": 3}]})

    assert dest["A"] == [{"time": 1}, {"time": 2}]
    assert dest["B"] == [{"time": 3}]


def test_dedupe_grouped_removes_repeated_timestamps_keeping_order():
    grouped = {"A": [{"time": 1}, {"time": 2}, {"time": 1}, {"time": 3}]}
    assert quanthub._dedupe_grouped(grouped) == {
        "A": [{"time": 1}, {"time": 2}, {"time": 3}]
    }


def test_chunked_result_is_indistinguishable_from_a_single_request(mocker):
    """The whole point: the caller cannot tell chunking happened."""
    all_records = _records_for_range("SRAU26", "2026-01-01", "2026-01-20")

    _mock_response(mocker, json_body=all_records)
    single = quanthub.download_history_batch(
        ["SRAU26"], "DAILY", "2026-01-01", "2026-01-20", use_date_range=True
    )

    mocker.stopall()
    _mock_responses(
        mocker,
        [
            _row_limit_response(),
            _response(json_body=all_records[:10]),
            _response(json_body=all_records[10:]),
        ],
    )
    chunked = quanthub.download_history_batch(
        ["SRAU26"], "DAILY", "2026-01-01", "2026-01-20", use_date_range=True
    )

    pd.testing.assert_frame_equal(single["SRAU26"], chunked["SRAU26"])


def test_overlapping_chunk_boundaries_are_deduplicated(mocker):
    """If a provider ever returned a boundary bar in both adjacent
    windows, the combined frame must still have one row per timestamp."""
    left = _records_for_range("SRAU26", "2026-01-01", "2026-01-10")
    right_overlapping = _records_for_range("SRAU26", "2026-01-10", "2026-01-20")

    _mock_responses(
        mocker,
        [_row_limit_response(), _response(json_body=left), _response(json_body=right_overlapping)],
    )

    df = quanthub.download_history_batch(
        ["SRAU26"], "DAILY", "2026-01-01", "2026-01-20", use_date_range=True
    )["SRAU26"]

    assert int(df["Date"].duplicated().sum()) == 0
    assert len(df) == 20


def test_chunked_result_keeps_schema_dtypes_and_sorting(mocker):
    """Out-of-order chunk responses must still produce a sorted frame
    with the canonical schema."""
    _mock_responses(
        mocker,
        [
            _row_limit_response(),
            # Deliberately returned newest-first, and the later window first.
            _response(json_body=list(reversed(_records_for_range("SRAU26", "2026-01-11", "2026-01-20")))),
            _response(json_body=_records_for_range("SRAU26", "2026-01-01", "2026-01-10")),
        ],
    )

    df = quanthub.download_history_batch(
        ["SRAU26"], "DAILY", "2026-01-01", "2026-01-20", use_date_range=True
    )["SRAU26"]

    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert str(df["Date"].dtype).startswith("datetime64")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        assert str(df[col].dtype) == "float64"
    assert df["Date"].is_monotonic_increasing
    assert len(df) == 20


def test_chunking_keeps_each_instruments_records_separate(mocker):
    """Instrument identity must survive recombination -- records are
    grouped by the response's own product field, per chunk."""
    _mock_responses(
        mocker,
        [
            _row_limit_response(),
            _response(json_body=(
                _records_for_range("SONH26", "2026-01-01", "2026-01-10", seed=1.0)
                + _records_for_range("ERH26", "2026-01-01", "2026-01-10", seed=500.0)
            )),
            _response(json_body=(
                _records_for_range("SONH26", "2026-01-11", "2026-01-20", seed=1.0)
                + _records_for_range("ERH26", "2026-01-11", "2026-01-20", seed=500.0)
            )),
        ],
    )

    result = quanthub.download_history_batch(
        ["SONH26", "ERH26"], "DAILY", "2026-01-01", "2026-01-20", use_date_range=True
    )

    assert len(result["SONH26"]) == 20
    assert len(result["ERH26"]) == 20
    assert result["SONH26"].iloc[0]["Close"] < 100.0
    assert result["ERH26"].iloc[0]["Close"] >= 500.0


# ---------------------------------------------------------------------
# Interval semantics across chunking
# ---------------------------------------------------------------------

def test_four_hour_buckets_are_built_after_chunks_are_recombined(mocker):
    """4H CORRECTNESS. 4H bars are resampled from native hourly bars.
    Resampling happens ONCE, above the chunker, on the combined record
    set -- so a chunk boundary can never produce a partial bucket.

    Two chunks of 12 hourly bars each must yield 6 complete 4H buckets,
    identical to what one 24-bar response produces.
    """
    hourly = _records_for_range("SRAU26", "2026-01-05 00:00", "2026-01-05 23:00", freq="1h")

    _mock_response(mocker, json_body=hourly)
    single = quanthub.download_history_batch(
        ["SRAU26"], "4H", "2026-01-05", "2026-01-05", use_date_range=True
    )["SRAU26"]

    mocker.stopall()
    _mock_responses(
        mocker,
        [_row_limit_response(), _response(json_body=hourly[:12]), _response(json_body=hourly[12:])],
    )
    chunked = quanthub.download_history_batch(
        ["SRAU26"], "4H", "2026-01-04", "2026-01-05", use_date_range=True
    )["SRAU26"]

    assert len(single) == 6
    pd.testing.assert_frame_equal(single, chunked)


def test_four_hour_requests_are_sized_like_hourly(mocker):
    """4H is fetched natively as 1H, so its chunk sizing must use the
    hourly density, not a daily one."""
    assert quanthub._max_days_per_chunk("1H", 10, 100_000) == quanthub._max_days_per_chunk(
        "1H", 10, 100_000
    )
    mock_get = _mock_response(mocker, json_body=[])
    instruments = [f"SON{m}26" for m in "FGHJKMNQUV"]

    quanthub.download_history_batch(
        instruments, "4H", "2026-01-01", "2026-06-30", use_date_range=True
    )

    for call in mock_get.call_args_list:
        assert call.kwargs["params"]["interval"] == "1H"
    assert mock_get.call_count > 1


@pytest.mark.parametrize("interval", ["DAILY", "HOURLY", "4H"])
def test_chunking_works_at_every_interval(mocker, interval):
    freq = "D" if interval == "DAILY" else "1h"
    left = _records_for_range("SRAU26", "2026-01-01", "2026-01-05", freq=freq)
    right = _records_for_range("SRAU26", "2026-01-06", "2026-01-10", freq=freq)
    _mock_responses(
        mocker,
        [_row_limit_response(), _response(json_body=left), _response(json_body=right)],
    )

    df = quanthub.download_history_batch(
        ["SRAU26"], interval, "2026-01-01", "2026-01-10", use_date_range=True
    )["SRAU26"]

    assert not df.empty
    assert df["Date"].is_monotonic_increasing
    assert int(df["Date"].duplicated().sum()) == 0


# ---------------------------------------------------------------------
# Interaction with the instrument-batch dimension and the rate limiter
# ---------------------------------------------------------------------

def test_instrument_batch_size_is_unchanged_by_chunking():
    assert quanthub.QUANTHUB_BATCH_SIZE == 10


def test_both_batching_dimensions_compose(mocker):
    """13 instruments (2 instrument batches) x an oversized hourly range
    (multiple date chunks each) -- the two dimensions multiply, and
    every request carries at most QUANTHUB_BATCH_SIZE instruments."""
    mock_get = _mock_response(mocker, json_body=[])
    instruments = [f"SON{m}26" for m in "FGHJKMNQUVXZ"] + ["SONF27"]  # 13

    quanthub.download_history_batch(
        instruments, "HOURLY", "2026-01-01", "2026-06-30", use_date_range=True
    )

    sizes = {len(c.kwargs["params"]["instruments"].split(",")) for c in mock_get.call_args_list}
    assert max(sizes) <= quanthub.QUANTHUB_BATCH_SIZE
    assert sizes == {10, 3}
    assert mock_get.call_count > 2  # both dimensions contributed


def test_every_chunk_request_passes_through_the_rate_limiter(mocker):
    """Task 3's limiter must not be bypassed by chunk requests -- N
    HTTP requests means N acquisitions."""
    mock_get = _mock_response(mocker, json_body=[])
    acquire = mocker.patch.object(quanthub._RATE_LIMITER, "acquire", return_value=0.0)
    instruments = [f"SON{m}26" for m in "FGHJKMNQUV"]

    quanthub.download_history_batch(
        instruments, "HOURLY", "2026-01-01", "2026-06-30", use_date_range=True
    )

    assert mock_get.call_count > 1
    assert acquire.call_count == mock_get.call_count


def test_reactive_split_requests_also_pass_through_the_rate_limiter(mocker):
    """Including the retries produced by a row-limit rejection."""
    _mock_responses(
        mocker,
        [
            _row_limit_response(),
            _response(json_body=_records_for_range("SRAU26", "2026-01-01", "2026-01-10")),
            _response(json_body=_records_for_range("SRAU26", "2026-01-11", "2026-01-20")),
        ],
    )
    acquire = mocker.patch.object(quanthub._RATE_LIMITER, "acquire", return_value=0.0)

    quanthub.download_history_batch(
        ["SRAU26"], "DAILY", "2026-01-01", "2026-01-20", use_date_range=True
    )

    assert acquire.call_count == 3  # the rejected request counted too


def test_the_count_request_shape_is_untouched_by_chunking(mocker):
    """Chunking is a date-range concern. The count shape already caps
    count x instruments against the same ceiling and must not change."""
    mock_get = _mock_response(mocker, json_body=[])

    quanthub.download_history_batch(
        ["SRAU26"], "HOURLY", "2020-01-01", "2026-09-22"  # count shape (default)
    )

    assert mock_get.call_count == 1
    params = mock_get.call_args.kwargs["params"]
    assert "count" in params
    assert "start" not in params and "end" not in params
