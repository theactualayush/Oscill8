from __future__ import annotations

import pytest

from core import config
from range_analytics.units import price_to_bp


@pytest.mark.parametrize("market_key", list(config.MARKETS.keys()))
def test_price_to_bp_uses_configured_bp_per_point(market_key):
    market = config.get_market(market_key)
    assert price_to_bp(0.01, market_key) == pytest.approx(0.01 * market.bp_per_point)


def test_price_to_bp_sofr_convention_is_100_per_point():
    assert price_to_bp(-0.025, "SOFR") == pytest.approx(-2.5)


def test_price_to_bp_unknown_market_raises_keyerror():
    with pytest.raises(KeyError):
        price_to_bp(1.0, "NOT_A_MARKET")
