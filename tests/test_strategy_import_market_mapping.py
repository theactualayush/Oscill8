"""
tests/test_strategy_import_market_mapping.py

strategy_import.market_mapping.resolve_market_code()'s three-way
classification: supported (translates to a real core.config.MARKETS
key -- includes ER/FSR/YBA as of EURIBOR/SARON/YBA gaining
MarketDefinition entries), unavailable (recognized by name, not
configured -- currently no codes in this state), and unrecognized (not
a known code at all).
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


def test_er_is_now_supported_and_maps_to_euribor():
    # EURIBOR gained a real core.config.MARKETS entry (trader-confirmed
    # ric_root/ric_year_digits/bp_per_point) and QuantHub routing --
    # the workbook code "ER" now resolves as supported, not unavailable.
    resolution = resolve_market_code("ER")
    assert resolution.status == "supported"
    assert resolution.market_key == "EURIBOR"
    assert resolution.reason is None
    assert "EURIBOR" in config.MARKETS


def test_yba_is_now_supported_and_maps_to_yba():
    resolution = resolve_market_code("YBA")
    assert resolution.status == "supported"
    assert resolution.market_key == "YBA"
    assert resolution.reason is None
    assert "YBA" in config.MARKETS


def test_fsr_is_now_supported_and_maps_to_saron():
    resolution = resolve_market_code("FSR")
    assert resolution.status == "supported"
    assert resolution.market_key == "SARON"
    assert resolution.reason is None
    assert "SARON" in config.MARKETS


def test_unavailable_market_codes_is_currently_empty():
    # ER/YBA/FSR were the only entries; both moved to
    # SUPPORTED_MARKET_CODES once their markets were configured. The
    # dict itself stays a first-class, non-removed status for the next
    # recognized-but-not-yet-configured market (e.g. BAX/Sterling).
    assert UNAVAILABLE_MARKET_CODES == {}


def test_unknown_code_is_unrecognized_not_unavailable():
    resolution = resolve_market_code("XYZ")
    assert resolution.status == "unrecognized"
    assert resolution.market_key is None
    assert resolution.reason is None


def test_genuinely_unknown_typo_like_codes_remain_unrecognized():
    # A code that merely resembles a known one (typo, near-miss) must
    # never be silently coerced into "supported" -- only the exact
    # codes in SUPPORTED_MARKET_CODES qualify.
    for typo in ("YB", "YBAA", "FSRR", "FS", "EURIBOR", "ERR", "AUD", "SARON"):
        resolution = resolve_market_code(typo)
        assert resolution.status == "unrecognized", f"{typo!r} should be unrecognized"


def test_resolution_is_case_insensitive_and_trims_whitespace():
    assert resolve_market_code("sra").status == "supported"
    assert resolve_market_code("  SRA  ").status == "supported"
    assert resolve_market_code(" er ").status == "supported"


def test_resolution_preserves_normalized_code():
    assert resolve_market_code("sra").code == "SRA"
    assert resolve_market_code(" er ").code == "ER"
    assert resolve_market_code("xyz").code == "XYZ"


def test_empty_code_is_unrecognized():
    resolution = resolve_market_code("")
    assert resolution.status == "unrecognized"
