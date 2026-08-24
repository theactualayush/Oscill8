"""
tests/test_reuters_qh_namespace_independence.py

Part 9 (items 1-7) of the QuantHub architecture review, made explicit
and directly traceable: Reuters/LSEG identifiers and QuantHub
identifiers are two independent namespaces, resolved through two
independent sources (core.ric/core.config.MARKETS for LSEG,
config/market_instruments.json for QuantHub), and neither is ever
derived from the other.

These tests intentionally restate facts already covered individually
in test_market_instruments.py / test_quanthub.py / test_providers.py,
in the exact shape the architecture review asked for, so the namespace-
independence guarantee is auditable from one file rather than inferred
by reading three.
"""

from __future__ import annotations

from core.market_instruments import find_by_product, load_market_instruments
from core.providers import qh_root_for_market
from core.quanthub import build_instrument
from core.ric import build_ric


def _qh_code(product: str) -> str:
    mappings = load_market_instruments()
    matches = find_by_product(mappings, product)
    assert len(matches) == 1, f"expected exactly one row for {product!r}"
    return matches[0].qh_code


def _reuters_code(product: str) -> str:
    mappings = load_market_instruments()
    matches = find_by_product(mappings, product)
    assert len(matches) == 1, f"expected exactly one row for {product!r}"
    return matches[0].reuters_code


# 1. Reuters root and QH root remain independent, even where core.config.
#    MARKETS is not (yet) configured for the QH-side product.
def test_euribor_reuters_and_qh_roots_are_independent():
    assert _reuters_code("Euribor") == "FEI"
    assert _qh_code("Euribor") == "ER"


# 2. EURIBOR: FEI -> LSEG (would-be RIC root), ER -> QuantHub (verified root).
def test_euribor_fei_never_used_for_quanthub():
    qh_instrument = build_instrument(_qh_code("Euribor"), 3, 2026)
    assert qh_instrument == "ERH26"
    assert "FEI" not in qh_instrument


# 3. SARON: SARO3 -> LSEG (hypothesis, unverified per Stage 3), FSR -> QuantHub.
def test_saron_saro3_never_used_for_quanthub():
    assert _reuters_code("SARON") == "SARO3"
    qh_instrument = build_instrument(_qh_code("SARON"), 3, 2026)
    assert qh_instrument == "FSRH26"
    assert "SARO3" not in qh_instrument
    assert "SARO" not in qh_instrument


# 4. SONIA: SON3 -> mapping table's Reuters code (unresolved vs. the
#    live-confirmed LSEG root "SON" -- deliberately not reconciled
#    here), SON -> the verified QuantHub root.
def test_sonia_son3_reuters_code_vs_son_quanthub_root():
    assert _reuters_code("SONIA 3M") == "SON3"
    assert _qh_code("SONIA 3M") == "SON"
    # QuantHub routing resolves the QH root via the mapping table only --
    # never via core.config.MARKETS["SONIA"].ric_root, even though that
    # LSEG root ("SON") happens to be the identical string today.
    assert qh_root_for_market("SONIA") == "SON"


# 5. YBA: QuantHub root confirmed identical to its Reuters code here --
#    still resolved independently, not by assuming Reuters == QH.
def test_yba_quanthub_instrument():
    assert _reuters_code("Australia 90 Day Bank Bill") == "YBA"
    assert _qh_code("Australia 90 Day Bank Bill") == "YBA"
    assert build_instrument(_qh_code("Australia 90 Day Bank Bill"), 3, 2026) == "YBAH26"


# 6. ICE ESTR: EON3 -> LSEG (would-be root, no MarketDefinition yet),
#    FER -> QuantHub (verified root). Distinct from CME ESTR (SRE/ESR).
def test_ice_estr_eon3_never_used_for_quanthub_and_is_distinct_from_cme_estr():
    ice_estr = [
        m for m in load_market_instruments()
        if m.product == "ESTR" and m.exchange == "ICE_EUROPE"
    ]
    cme_estr = [
        m for m in load_market_instruments()
        if m.product == "ESTR" and m.exchange == "CME"
    ]
    assert len(ice_estr) == 1 and len(cme_estr) == 1
    assert (ice_estr[0].reuters_code, ice_estr[0].qh_code) == ("EON3", "FER")
    assert (cme_estr[0].reuters_code, cme_estr[0].qh_code) == ("SRE", "ESR")

    qh_instrument = build_instrument(ice_estr[0].qh_code, 3, 2026)
    assert qh_instrument == "FERH26"
    assert "EON3" not in qh_instrument
    assert "EON" not in qh_instrument


# 7. FEIH26 must never be used where ERH26 is required (the live-
#    confirmed case: FEIH26 -> HTTP 200 + empty data; ERH26 -> real data).
def test_feih26_is_not_a_valid_substitute_for_erh26():
    reuters_style_guess = "FEI" + "H26"  # what naive RIC-manipulation would produce
    verified_quanthub_instrument = build_instrument(_qh_code("Euribor"), 3, 2026)
    assert reuters_style_guess == "FEIH26"
    assert verified_quanthub_instrument == "ERH26"
    assert verified_quanthub_instrument != reuters_style_guess


# Sanity check: the CME ESTR market key already configured in
# core.config.MARKETS builds the expected LSEG RIC, confirming the LSEG
# side of the architecture is completely untouched by any of the above.
def test_cme_estr_lseg_ric_construction_unchanged():
    assert build_ric("ESTR", 3, 2026) == "SREH26"
