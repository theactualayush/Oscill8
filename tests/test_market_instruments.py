"""
test_market_instruments.py

Proves the authoritative Product/TT/Reuters/QH reference mapping
(config/market_instruments.json, loaded via core.market_instruments)
loads correctly and preserves the trader-supplied mappings exactly --
this is a source-of-truth reference table, so a regression here means
silently corrupted identifiers for every future provider/market
integration.
"""

from __future__ import annotations

import json

import pytest

from core.market_instruments import (
    InstrumentMapping,
    find_by_product,
    load_market_instruments,
)


@pytest.fixture(scope="module")
def mappings() -> list[InstrumentMapping]:
    return load_market_instruments()


def test_loads_without_error(mappings):
    assert len(mappings) > 0


def test_every_entry_is_an_instrument_mapping(mappings):
    assert all(isinstance(m, InstrumentMapping) for m in mappings)


def test_no_duplicate_rows(mappings):
    # (product, exchange) uniquely identifies a row -- ESTR legitimately
    # appears twice (ICE_EUROPE and CME), so product alone is not unique.
    keys = [(m.product, m.exchange) for m in mappings]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize(
    "product, exchange, tt_code, reuters_code, qh_code",
    [
        ("Euribor", "ICE_EUROPE", "I", "FEI", "ER"),
        ("SARON", "ICE_EUROPE", "SA3", "SARO3", "FSR"),
        ("Sterling", "ICE_EUROPE", "L", "FSS", "LL"),
        ("SOFR 1M", "CME", "SR1", "S1R", "S1R"),
        ("SOFR 3M", "CME", "SR3", "SRA", "SRA"),
        ("CORRA", "MX", "CR", "CRA", "CRA"),
        ("Australia 90 Day Bank Bill", "ASX", "IR", "YBA", "YBA"),
        ("Australia 30 Day Interbank Rate", "ASX", "IB", "YIB", "YIB"),
        ("BAX", "MX", "BAX", "BAX", "BA"),
        ("3 month TONA Futures", "XOSE", "TOA3M", "JTOA", "JTOA"),
        ("New Zealand 90 Day Bank Bill", "ASX", None, "NBB", "NBB"),
        ("Brazil", "B3", "DI1", "DIJ", "DIJ"),
        ("SONIA 3M", "ICE_EUROPE", "SO3", "SON3", "SON"),
        ("SONIA 1M", "ICE_EUROPE", "SOA", None, "S1N"),
    ],
)
def test_key_mapping_preserved(mappings, product, exchange, tt_code, reuters_code, qh_code):
    matches = [m for m in mappings if m.product == product and m.exchange == exchange]
    assert len(matches) == 1, f"expected exactly one {product}/{exchange} row, got {matches}"
    m = matches[0]
    assert m.tt_code == tt_code
    assert m.reuters_code == reuters_code
    assert m.qh_code == qh_code


def test_estr_has_two_distinct_exchange_rows(mappings):
    """ESTR is deliberately NOT one row -- an ICE_EUROPE-listed product
    (Reuters EON3, QH FER) and a separate CME-listed product (Reuters
    SRE, QH ESR) both exist in the source table under the same product
    name. Collapsing them would lose a real, distinct instrument.
    """
    estr_rows = find_by_product(mappings, "ESTR")
    assert len(estr_rows) == 2

    by_exchange = {m.exchange: m for m in estr_rows}
    assert set(by_exchange) == {"ICE_EUROPE", "CME"}

    ice = by_exchange["ICE_EUROPE"]
    assert (ice.tt_code, ice.reuters_code, ice.qh_code) == ("ER3", "EON3", "FER")

    cme = by_exchange["CME"]
    assert (cme.tt_code, cme.reuters_code, cme.qh_code) == ("ESR", "SRE", "ESR")


def test_composite_intermarket_products_preserved(mappings):
    """Composite/spread products (e.g. the US 10Yr/30Yr T-Bond spread)
    carry '|'-joined TT codes and a null Reuters code in the source
    table -- both must be preserved verbatim, not split or fabricated.
    """
    us_10_30 = find_by_product(mappings, "US 10 Yr30 Yr T-Bond Spread")
    assert len(us_10_30) == 1
    m = us_10_30[0]
    assert m.tt_code == "ZN|ZB"
    assert m.reuters_code is None
    assert m.qh_code == "TYZB"

    lois = find_by_product(mappings, "ESTR - Euribor (LOIS)")
    assert len(lois) == 1
    assert lois[0].qh_code == "FERER"


def test_find_by_product_returns_empty_list_for_unknown_product(mappings):
    assert find_by_product(mappings, "Definitely Not A Real Product") == []


def test_missing_instruments_key_raises(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"schema_version": 1}))
    with pytest.raises(ValueError, match="instruments"):
        load_market_instruments(str(bad_file))


def test_entry_missing_required_field_raises(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps({"instruments": [{"product": "X", "asset_class": "FI"}]})
    )
    with pytest.raises(ValueError, match="missing required field"):
        load_market_instruments(str(bad_file))


def test_source_file_is_valid_json_with_expected_top_level_shape():
    from core.market_instruments import MARKET_INSTRUMENTS_PATH

    with open(MARKET_INSTRUMENTS_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    assert "instruments" in raw
    assert isinstance(raw["instruments"], list)
    assert len(raw["instruments"]) == 56
