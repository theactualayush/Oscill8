"""
tests/test_quanthub_live_smoke.py

Optional live smoke tests against the real QuantHub API, exercising
whatever endpoint core.config.QUANTHUB_BASE_URL currently resolves to --
since the backend migration, the new /apis/ohlc/ one.

Two live calls, both reproducing a request already verified by hand
against the real API:
    - SRAH24 / 1D / count=5 -- the ORIGINAL verified example, predating
      the migration.
    - SONU28 / 1H / count=5 -- the example verified against the MIGRATED
      /apis/ohlc/ endpoint.
Neither is routed through download_history()'s date-range filtering:
SRAH24 is a long-expired contract, so a recent [start, end] window would
legitimately filter its bars down to nothing, which would be
indistinguishable from a real failure.

Self-skips (never fails the suite) when:
    - RBS_QUANTHUB_TOKEN is not set,
    - the configured endpoint is still the RETIRED backend (that returns
      HTTP 403 for everything, which would look like a credential
      failure rather than the configuration problem it actually is), or
    - the QuantHub host is unreachable from this environment (e.g. no
      corp-network access, DNS failure, timeout).

AUTHENTICATION IS MANUAL. RBS_QUANTHUB_TOKEN holds an access_token the
operator obtained through QuantHub's own web auth page; Oscill8 does no
token acquisition, refresh, or Microsoft sign-in. These tests only check
whether the variable is SET -- its value is never read, logged, or
asserted on here.
"""

from __future__ import annotations

import os

import pytest
import requests

from core import config
from core.quanthub import RETIRED_QUANTHUB_OHLC_PATH, _fetch_quanthub_records

pytestmark = pytest.mark.skipif(
    not os.environ.get("RBS_QUANTHUB_TOKEN"),
    reason="RBS_QUANTHUB_TOKEN not set -- skipping live QuantHub smoke test",
)


def _skip_if_endpoint_not_migrated() -> None:
    """Skip rather than fail when RBS_QUANTHUB_BASE_URL still pins the
    retired backend -- that is an operator configuration problem with
    its own warning (core.quanthub), not a test failure."""
    if RETIRED_QUANTHUB_OHLC_PATH in (config.QUANTHUB_BASE_URL or ""):
        pytest.skip(
            "RBS_QUANTHUB_BASE_URL still points at the retired QuantHub "
            "backend; update it to the migrated /apis/ohlc/ endpoint."
        )


def test_live_smoke_targets_the_migrated_endpoint():
    """Guard for the two live calls below: prove the request they are
    about to make actually goes to the migrated endpoint."""
    _skip_if_endpoint_not_migrated()
    assert config.QUANTHUB_BASE_URL.endswith("/apis/ohlc/")


def test_live_sofr_verified_example_returns_real_data():
    _skip_if_endpoint_not_migrated()
    try:
        grouped = _fetch_quanthub_records(["SRAH24"], "1D", 5)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        pytest.skip(f"QuantHub host unreachable from this environment: {exc}")

    assert "SRAH24" in grouped
    records = grouped["SRAH24"]
    assert len(records) > 0
    for field in ("product", "time", "open", "high", "low", "close", "volume"):
        assert field in records[0]
    assert records[0]["product"] == "SRAH24"


def test_live_migrated_endpoint_verified_example_returns_real_data():
    """The request verified by hand against the NEW /apis/ohlc/ backend:
    instruments=SONU28, interval=1H, count=5 -> HTTP 200 with records
    shaped {product, time (unix ms), open, high, low, close, volume}.

    Asserting that exact shape here is what proves the migration needed
    no change to _normalize_quanthub_records: if the new backend had
    altered the record contract, this is where it would show up.
    """
    _skip_if_endpoint_not_migrated()
    try:
        grouped = _fetch_quanthub_records(["SONU28"], "1H", 5)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        pytest.skip(f"QuantHub host unreachable from this environment: {exc}")

    assert "SONU28" in grouped
    records = grouped["SONU28"]
    assert len(records) > 0
    for field in ("product", "time", "open", "high", "low", "close", "volume"):
        assert field in records[0]
    assert records[0]["product"] == "SONU28"
    assert isinstance(records[0]["time"], (int, float))  # unix milliseconds
