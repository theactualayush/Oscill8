from __future__ import annotations

import pytest

from core import config
from core.config import BarInterval
from range_analytics.units import BpConversionUnavailable, price_to_bp, resolve_bp_per_point
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec


@pytest.mark.parametrize("market_key", list(config.MARKETS.keys()))
def test_price_to_bp_uses_configured_bp_per_point(market_key):
    market = config.get_market(market_key)
    assert price_to_bp(0.01, market_key) == pytest.approx(0.01 * market.bp_per_point)


def test_price_to_bp_sofr_convention_is_100_per_point():
    assert price_to_bp(-0.025, "SOFR") == pytest.approx(-2.5)


def test_price_to_bp_unknown_market_raises_keyerror():
    with pytest.raises(KeyError):
        price_to_bp(1.0, "NOT_A_MARKET")


# ---------------------------------------------------------------------
# resolve_bp_per_point -- generic across single-market and intermarket
# definitions, dispatched purely by TYPE.
# ---------------------------------------------------------------------

def test_resolve_bp_per_point_single_market_matches_registry():
    d = StrategyDefinition(
        market_key="CORRA", offsets=(0,), weights=(1,), interval=BarInterval.DAILY,
    )
    assert resolve_bp_per_point(d) == config.get_market("CORRA").bp_per_point


def test_resolve_bp_per_point_single_market_unknown_market_raises_keyerror():
    # Can't construct an invalid StrategyDefinition directly (its own
    # __post_init__ already rejects an unknown market_key), so this
    # exercises the same KeyError path resolve_bp_per_point would hit
    # for any object merely duck-typing `.market_key`.
    class _FakeDefinition:
        market_key = "NOT_A_MARKET"

    with pytest.raises(KeyError):
        resolve_bp_per_point(_FakeDefinition())


def test_resolve_bp_per_point_intermarket_uses_explicit_override():
    d = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("CORRA", 0, -1.0)),
        interval=BarInterval.DAILY,
        bp_per_point=42.0,
    )
    assert resolve_bp_per_point(d) == 42.0


def test_resolve_bp_per_point_intermarket_without_override_raises_typed_error():
    d = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("CORRA", 0, -1.0)), interval=BarInterval.DAILY,
    )
    with pytest.raises(BpConversionUnavailable):
        resolve_bp_per_point(d)


def test_resolve_bp_per_point_never_falls_back_to_any_leg_market():
    """The explicit non-guessing guarantee: even though both legs below
    belong to real, differently-converting markets, an intermarket
    definition with no override must raise -- never silently resolve to
    the first leg's, the anchor leg's, or any other leg's own
    bp_per_point."""
    d = IntermarketDefinition(
        legs=(LegSpec("SOFR", 0, 1.0), LegSpec("FED_FUNDS", 0, -1.0)), interval=BarInterval.DAILY,
    )
    assert d.bp_per_point is None
    with pytest.raises(BpConversionUnavailable):
        resolve_bp_per_point(d)
