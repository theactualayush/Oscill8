"""
tests/test_quanthub_live_smoke.py

Optional live smoke test against the real QuantHub API. Reproduces
exactly the originally-verified live call (instrument="SRAH24",
interval=1D, count=5) -- deliberately NOT routed through download_
history()'s date-range filtering, since SRAH24 is a long-expired
contract and a recent [start, end] window would legitimately filter
its bars down to nothing, which would be indistinguishable from a
real failure.

Self-skips (never fails the suite) when:
    - RBS_QUANTHUB_TOKEN is not set, or
    - the QuantHub host is unreachable from this environment (e.g. no
      corp-network access, DNS failure, timeout).

No credentials are hardcoded here; RBS_QUANTHUB_TOKEN must already be
set in the environment running this test.
"""

from __future__ import annotations

import os

import pytest
import requests

from core.quanthub import _fetch_quanthub_records

pytestmark = pytest.mark.skipif(
    not os.environ.get("RBS_QUANTHUB_TOKEN"),
    reason="RBS_QUANTHUB_TOKEN not set -- skipping live QuantHub smoke test",
)


def test_live_sofr_verified_example_returns_real_data():
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
