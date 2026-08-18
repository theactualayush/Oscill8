"""
tests/test_strategy_import_market_mapping.py

strategy_import.market_mapping.resolve_market_code()'s three-way
classification: supported (translates to a real core.config.MARKETS
key), unavailable (recognized by name, not configured -- ER, YBA,
FSR), and unrecognized (not a known code at all).
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


def test_yba_is_recognized_but_unavailable():
    # Real-workbook finding: "Australian exchange market", RIC root
    # YBA -- confirmed by the trader, but no MarketDefinition exists
    # (and none is added here -- see the module's own "must not be
    # invented" note).
    resolution = resolve_market_code("YBA")
    assert resolution.status == "unavailable"
    assert resolution.market_key is None
    assert resolution.reason == UNAVAILABLE_MARKET_CODES["YBA"]
    assert "Australian" in resolution.reason


def test_fsr_is_recognized_but_unavailable():
    # Real-workbook finding: "SARON 3M futures", RIC root SARO3 --
    # confirmed by the trader, same treatment as ER/YBA.
    resolution = resolve_market_code("FSR")
    assert resolution.status == "unavailable"
    assert resolution.market_key is None
    assert resolution.reason == UNAVAILABLE_MARKET_CODES["FSR"]
    assert "SARON" in resolution.reason


def test_yba_and_fsr_are_not_configured_markets():
    # No MarketDefinition was added for either -- per the explicit
    # "do not add full configuration yet" instruction, only the
    # recognized-but-unavailable mapping exists.
    assert "AUSTRALIA" not in config.MARKETS
    assert "SARON" not in config.MARKETS
    assert all(m.ric_root not in ("YBA", "SARO3") for m in config.MARKETS.values())


def test_unavailable_reasons_are_distinct_per_market():
    # Each recognized-but-unavailable code gets its OWN accurate
    # reason, never a generic shared message -- a user reading the
    # preview must be able to tell ER/YBA/FSR apart.
    reasons = {UNAVAILABLE_MARKET_CODES[code] for code in ("ER", "YBA", "FSR")}
    assert len(reasons) == 3


def test_unknown_code_is_unrecognized_not_unavailable():
    resolution = resolve_market_code("XYZ")
    assert resolution.status == "unrecognized"
    assert resolution.market_key is None
    assert resolution.reason is None


def test_genuinely_unknown_typo_like_codes_remain_unrecognized():
    # A code that merely resembles a known one (typo, near-miss) must
    # never be silently coerced into "unavailable" -- only the exact,
    # confirmed codes in UNAVAILABLE_MARKET_CODES qualify.
    for typo in ("YB", "YBAA", "FSRR", "FS", "EURIBOR", "ERR", "AUD", "SARON"):
        resolution = resolve_market_code(typo)
        assert resolution.status == "unrecognized", f"{typo!r} should be unrecognized"


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
