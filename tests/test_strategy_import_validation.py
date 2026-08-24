"""
tests/test_strategy_import_validation.py

strategy_import.validation.validate_row(): the three-way ReadyRow /
UnavailableRow / InvalidRow classification, and that shape validation
(offsets/weights) is delegated to the real, unmodified
template_scanner.templates.template_from_dense_weights() /
strategy_engine.definitions.StrategyDefinition rather than
reimplemented.
"""

from __future__ import annotations

import pytest

from core.config import BarInterval

from strategy_import.market_mapping import UNAVAILABLE_MARKET_CODES
from strategy_import.validation import (
    DEFAULT_IMPORT_INTERVAL,
    InvalidRow,
    ReadyRow,
    UnavailableRow,
    validate_row,
)


@pytest.fixture
def synthetic_unavailable_markets(monkeypatch):
    """Registers clearly-synthetic, non-real market codes as
    recognized-but-unavailable, ONLY for the duration of a test that
    requests this fixture -- exercises the unavailable-row code path
    without asserting anything about a real market (ER/YBA/FSR are now
    genuinely SUPPORTED, per EURIBOR/SARON/YBA gaining core.config.
    MARKETS entries -- see strategy_import/market_mapping.py). Reverted
    automatically by monkeypatch; the real, committed
    UNAVAILABLE_MARKET_CODES (currently {}) is never actually modified.
    """
    codes = {
        "ZZZ": "ZZZ is not currently configured in Oscill8.",
        "ZZY": "ZZY market data is not currently configured in Oscill8.",
        "ZZX": "ZZX market data is not currently configured in Oscill8.",
    }
    for code, reason in codes.items():
        monkeypatch.setitem(UNAVAILABLE_MARKET_CODES, code, reason)
    return codes


def _row(market, label, *weights) -> dict:
    row = {"Market": market, "Label": label}
    row.update({str(i + 1): w for i, w in enumerate(weights)})
    return row


_COLUMNS = ("1", "2", "3")


# ---------------------------------------------------------------------
# Ready rows
# ---------------------------------------------------------------------

def test_supported_market_produces_ready_row():
    outcome = validate_row(_row("SRA", "3M Spread", 1, -1, 0), 2, _COLUMNS)
    assert isinstance(outcome, ReadyRow)
    assert outcome.row_number == 2
    assert outcome.entry.name == "3M Spread"
    assert outcome.entry.definition.market_key == "SOFR"
    assert outcome.entry.definition.offsets == (0, 1)
    assert outcome.entry.definition.weights == (1.0, -1.0)


def test_son_and_cra_also_resolve_to_ready_rows():
    son = validate_row(_row("SON", "SONIA Fly", 1, -2, 1), 2, _COLUMNS)
    cra = validate_row(_row("CRA", "CORRA Fly", 1, -2, 1), 3, _COLUMNS)
    assert son.entry.definition.market_key == "SONIA"
    assert cra.entry.definition.market_key == "CORRA"


def test_ready_row_defaults_to_default_import_interval():
    outcome = validate_row(_row("SRA", "X", 1, -1, 0), 2, _COLUMNS)
    assert outcome.entry.definition.interval == DEFAULT_IMPORT_INTERVAL
    assert DEFAULT_IMPORT_INTERVAL == BarInterval.DAILY


def test_explicit_interval_overrides_default():
    outcome = validate_row(_row("SRA", "X", 1, -1, 0), 2, _COLUMNS, interval=BarInterval.HOURLY)
    assert outcome.entry.definition.interval == BarInterval.HOURLY


def test_blank_label_falls_back_to_row_number_label():
    outcome = validate_row(_row("SRA", "", 1, -1, 0), 7, _COLUMNS)
    assert isinstance(outcome, ReadyRow)
    assert outcome.entry.name == "Row 7"


def test_leading_and_trailing_zeros_are_gaps_not_errors():
    outcome = validate_row(_row("SRA", "Gapped", 0, 1, -1), 2, ("1", "2", "3", "4"))
    assert isinstance(outcome, ReadyRow)
    assert outcome.entry.definition.offsets == (0, 1)


def test_blank_position_cell_produces_the_identical_definition_as_explicit_zero():
    # SRA | 1Yr Fly | 1 | -2 | blank | 1  ==  SRA | 1Yr Fly | 1 | -2 | 0 | 1
    columns = ("1", "2", "3", "4")
    blank_row = {"Market": "SRA", "Label": "1Yr Fly", "1": 1, "2": -2, "3": None, "4": 1}
    zero_row = {"Market": "SRA", "Label": "1Yr Fly", "1": 1, "2": -2, "3": 0, "4": 1}

    blank_outcome = validate_row(blank_row, 2, columns)
    zero_outcome = validate_row(zero_row, 3, columns)

    assert isinstance(blank_outcome, ReadyRow) and isinstance(zero_outcome, ReadyRow)
    # Byte-identical StrategyDefinition -- the same equality
    # strategy_import.preview._dedupe_ready() relies on directly.
    assert blank_outcome.entry.definition == zero_outcome.entry.definition
    assert blank_outcome.entry.definition.offsets == (0, 1, 3)
    assert blank_outcome.entry.definition.weights == (1.0, -2.0, 1.0)


def test_nan_position_cell_is_also_treated_as_zero():
    # pandas represents a blank Excel cell as float NaN, not None --
    # both must normalize identically (see strategy_import.parsing's
    # own _is_blank() using the same rule at the row-filter level).
    import math

    columns = ("1", "2", "3")
    nan_row = {"Market": "SRA", "Label": "X", "1": 1, "2": -1, "3": math.nan}
    zero_row = {"Market": "SRA", "Label": "X", "1": 1, "2": -1, "3": 0}

    nan_outcome = validate_row(nan_row, 2, columns)
    zero_outcome = validate_row(zero_row, 3, columns)

    assert nan_outcome.entry.definition == zero_outcome.entry.definition


# ---------------------------------------------------------------------
# Unavailable rows (synthetic "ZZZ"-family codes -- ER/YBA/FSR are now
# genuinely SUPPORTED, see the module docstring / synthetic_unavailable_
# markets fixture above)
# ---------------------------------------------------------------------

def test_synthetic_market_is_unavailable_not_invalid_and_not_ready(synthetic_unavailable_markets):
    outcome = validate_row(_row("ZZZ", "3M Synthetic Spread", 1, -1, 0), 5, _COLUMNS)
    assert isinstance(outcome, UnavailableRow)
    assert outcome.row_number == 5
    assert outcome.label == "3M Synthetic Spread"
    assert outcome.market_code == "ZZZ"
    assert outcome.reason == synthetic_unavailable_markets["ZZZ"]


def test_unavailable_row_is_reported_even_with_a_perfectly_valid_shape(synthetic_unavailable_markets):
    # Confirms shape correctness never rescues an unavailable market --
    # the row is reported purely because of the market, before shape is
    # even considered.
    outcome = validate_row(_row("ZZZ", "Valid Fly", 1, -2, 1), 2, _COLUMNS)
    assert isinstance(outcome, UnavailableRow)


def test_second_synthetic_market_is_unavailable_not_invalid_and_not_ready(synthetic_unavailable_markets):
    outcome = validate_row(_row("ZZY", "3M Spread", 1, -1, 0), 38, _COLUMNS)
    assert isinstance(outcome, UnavailableRow)
    assert outcome.row_number == 38
    assert outcome.label == "3M Spread"
    assert outcome.market_code == "ZZY"
    assert outcome.reason == synthetic_unavailable_markets["ZZY"]


def test_third_synthetic_market_is_unavailable_not_invalid_and_not_ready(synthetic_unavailable_markets):
    outcome = validate_row(_row("ZZX", "3M Spread", 1, -1, 0), 43, _COLUMNS)
    assert isinstance(outcome, UnavailableRow)
    assert outcome.row_number == 43
    assert outcome.label == "3M Spread"
    assert outcome.market_code == "ZZX"
    assert outcome.reason == synthetic_unavailable_markets["ZZX"]


def test_er_yba_fsr_now_resolve_as_ready_not_unavailable():
    # Direct regression lock for the production mapping change: these
    # three codes are now SUPPORTED (EURIBOR/YBA/SARON), not unavailable.
    for code, market_key in (("ER", "EURIBOR"), ("YBA", "YBA"), ("FSR", "SARON")):
        outcome = validate_row(_row(code, "X", 1, -1, 0), 2, _COLUMNS)
        assert isinstance(outcome, ReadyRow), f"{code!r} should now be ready, not unavailable"
        assert outcome.entry.definition.market_key == market_key


# ---------------------------------------------------------------------
# Invalid rows
# ---------------------------------------------------------------------

def test_unknown_market_code_is_invalid():
    outcome = validate_row(_row("XYZ", "Bad Market", 1, -1, 0), 9, _COLUMNS)
    assert isinstance(outcome, InvalidRow)
    assert outcome.row_number == 9
    assert outcome.label == "Bad Market"
    assert "XYZ" in outcome.message


def test_typo_like_near_miss_of_a_known_code_stays_invalid(synthetic_unavailable_markets):
    # "YBAA"/"FSRR" merely resemble the now-supported YBA/FSR codes, and
    # "ZZZZ" merely resembles the synthetic unavailable "ZZZ" code --
    # none must ever be silently treated as the same market. Only an
    # exact match counts.
    for typo in ("YBAA", "FSRR", "ZZZZ"):
        outcome = validate_row(_row(typo, "X", 1, -1, 0), 2, _COLUMNS)
        assert isinstance(outcome, InvalidRow), f"{typo!r} should be invalid, not unavailable"
        assert typo in outcome.message


def test_missing_market_value_is_invalid():
    outcome = validate_row(_row(None, "No Market", 1, -1, 0), 4, _COLUMNS)
    assert isinstance(outcome, InvalidRow)
    assert "Market" in outcome.message


def test_non_numeric_position_value_is_invalid_never_coerced_to_zero():
    row = {"Market": "SRA", "Label": "Bad Weight", "1": "abc", "2": -1, "3": 0}
    outcome = validate_row(row, 42, _COLUMNS)
    assert isinstance(outcome, InvalidRow)
    assert outcome.row_number == 42
    assert "non-numeric" in outcome.message.lower()


def test_all_zero_weights_is_invalid_not_silently_skipped():
    # A row with a Market/Label but literally all zero weights was not
    # filtered as "blank" by parsing.py (parsing only drops rows whose
    # cells are genuinely empty) -- validate_row must still reject it
    # via StrategyDefinition's own all-zero-weights rule, not import it
    # as a strategy with no legs.
    outcome = validate_row(_row("SRA", "All Zero", 0, 0, 0), 6, _COLUMNS)
    assert isinstance(outcome, InvalidRow)


def test_invalid_row_never_silently_drops_the_label_or_row_number():
    outcome = validate_row(_row("XYZ", "Row 17 Strategy", 1, -1, 0), 17, _COLUMNS)
    assert outcome.row_number == 17
    assert outcome.label == "Row 17 Strategy"
    assert outcome.message  # never empty -- the user always sees a reason
