"""
tests/test_providers.py

Unit tests for core/providers.py: the market->provider routing table,
and QH-root resolution via config/market_instruments.json (never via
core.config.MARKETS/ric_root -- the two identifier namespaces must stay
independent).
"""

from __future__ import annotations

import pytest

from core.providers import PROVIDER_ROUTING, Provider, qh_root_for_market, resolve_provider


# ---------------------------------------------------------------------
# resolve_provider / PROVIDER_ROUTING
# ---------------------------------------------------------------------

def test_sofr_defaults_to_lseg():
    assert resolve_provider("SOFR") is Provider.LSEG


def test_unlisted_market_defaults_to_lseg():
    assert resolve_provider("SOME_MARKET_NOT_IN_ROUTING") is Provider.LSEG


def test_corra_routes_to_quanthub():
    assert resolve_provider("CORRA") is Provider.QUANTHUB


def test_sonia_routes_to_quanthub():
    assert resolve_provider("SONIA") is Provider.QUANTHUB


def test_fed_funds_defaults_to_lseg():
    # Existing LSEG-only market, unaffected by QuantHub routing.
    assert resolve_provider("FED_FUNDS") is Provider.LSEG


def test_estr_cme_market_key_not_routed_to_quanthub():
    # core.config.MARKETS["ESTR"] is the CME product (Reuters SRE) --
    # must stay on LSEG; only ICE-Europe ESTR was ever discussed for
    # QuantHub, and it has no market_key at all yet (see below).
    assert resolve_provider("ESTR") is Provider.LSEG


def test_bax_and_sterling_are_not_in_provider_routing():
    # Explicitly deferred -- must remain unconfigured (Part 4/9 item 13).
    assert "BAX" not in PROVIDER_ROUTING
    assert "Sterling" not in PROVIDER_ROUTING
    assert "STERLING" not in PROVIDER_ROUTING


def test_euribor_saron_yba_estr_ice_route_to_quanthub():
    # All four now have trader-confirmed core.config.MARKETS entries
    # and are explicitly routed to QuantHub for historical data.
    for market_key in ["EURIBOR", "SARON", "YBA", "ESTR_ICE"]:
        assert resolve_provider(market_key) is Provider.QUANTHUB
        assert market_key in PROVIDER_ROUTING


# ---------------------------------------------------------------------
# qh_root_for_market: independent of core.config.MARKETS/ric_root
# ---------------------------------------------------------------------

def test_qh_root_for_corra_matches_authoritative_mapping():
    assert qh_root_for_market("CORRA") == "CRA"


def test_qh_root_for_sonia_matches_authoritative_mapping_not_lseg_root():
    # market_instruments.json qh_code for "SONIA 3M" is "SON" -- this
    # happens to equal core.config.MARKETS["SONIA"].ric_root ("SON")
    # today, but qh_root_for_market must resolve it via the mapping
    # table, never by reading MarketDefinition.ric_root.
    assert qh_root_for_market("SONIA") == "SON"


def test_qh_root_for_market_raises_for_market_without_registered_mapping():
    # BAX has a market_instruments.json row but is explicitly deferred --
    # not yet added to core.config.MARKETS or QuantHub routing.
    with pytest.raises(ValueError, match="No QuantHub product mapping registered"):
        qh_root_for_market("BAX")


def test_qh_root_for_market_raises_for_completely_unknown_market():
    with pytest.raises(ValueError, match="No QuantHub product mapping registered"):
        qh_root_for_market("NOT_A_REAL_MARKET")


def test_qh_root_for_euribor_matches_authoritative_mapping():
    assert qh_root_for_market("EURIBOR") == "ER"


def test_qh_root_for_saron_matches_authoritative_mapping():
    assert qh_root_for_market("SARON") == "FSR"


def test_qh_root_for_yba_matches_authoritative_mapping():
    assert qh_root_for_market("YBA") == "YBA"


def test_qh_root_for_estr_ice_resolves_ice_europe_row_not_cme():
    # "ESTR" appears twice in market_instruments.json (CME and
    # ICE_EUROPE) -- ESTR_ICE must resolve ONLY the ICE_EUROPE row
    # (qh_code "FER"), never the CME row (qh_code "ESR").
    assert qh_root_for_market("ESTR_ICE") == "FER"
