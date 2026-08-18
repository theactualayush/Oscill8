"""
tests/test_strategy_import_market_mapping.py

strategy_import.market_mapping.resolve_market_code()'s three-way
classification: supported (translates to a real core.config.MARKETS
key), unavailable (recognized by name, not configured -- ER), and
unrecognized (not a known code at all).
"""

from __future__ import annotations

from core import config

from strategy_import.market_mapping import (
    SUPPORTED_MARKET_CODES,
    UNAVAILABLE_MARKET_CODES,
    resolve_market_code,
)


def test_supported_codes_translate_to_real_registry_keys():
    for code, market_key in SUPPORTED_MARKET_CODES.items():
        resolution = resolve_market_code(code)
        assert resolution.status == "supported"
        assert resolution.market_key == market_key
        # Every supported translation must land on a market Oscill8 can
        # actually construct a StrategyDefinition against today.
        assert market_key in config.MARKETS


def test_sra_son_cra_map_exactly_as_specified():
    assert resolve_market_code("SRA").market_key == "SOFR"
    assert resolve_market_code("SON").market_key == "SONIA"
    assert resolve_market_code("CRA").market_key == "CORRA"


def test_er_is_recognized_but_unavailable():
    resolution = resolve_market_code("ER")
    assert resolution.status == "unavailable"
    assert resolution.market_key is None
    assert resolution.reason == UNAVAILABLE_MARKET_CODES["ER"]
    assert "Euribor" in resolution.reason


def test_er_is_not_a_configured_market():
    # Locks in the CLAUDE.md-documented gap this whole distinction
    # exists to report accurately: EURIBOR must not silently appear in
    # the real registry as a side effect of the importer's own mapping.
    assert "EURIBOR" not in config.MARKETS


def test_unknown_code_is_unrecognized_not_unavailable():
    resolution = resolve_market_code("XYZ")
    assert resolution.status == "unrecognized"
    assert resolution.market_key is None
    assert resolution.reason is None


def test_resolution_is_case_insensitive_and_trims_whitespace():
    assert resolve_market_code("sra").status == "supported"
    assert resolve_market_code("  SRA  ").status == "supported"
    assert resolve_market_code(" er ").status == "unavailable"


def test_resolution_preserves_normalized_code():
    assert resolve_market_code("sra").code == "SRA"
    assert resolve_market_code(" er ").code == "ER"
    assert resolve_market_code("xyz").code == "XYZ"


def test_empty_code_is_unrecognized():
    resolution = resolve_market_code("")
    assert resolution.status == "unrecognized"
