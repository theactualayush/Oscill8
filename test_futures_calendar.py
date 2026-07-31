"""
tests/test_futures_calendar.py

Unit tests for futures_calendar.py.
"""

from __future__ import annotations

import pytest

import futures_calendar as fc


# ---------------------------------------------------------------------
# generate_contracts
# ---------------------------------------------------------------------

def test_generate_contracts_quarterly_full_year():
    contracts = fc.generate_contracts("SOFR", "2026-01-01", "2026-12-31")
    assert contracts == ["SRAH26", "SRAM26", "SRAU26", "SRAZ26"]


def test_generate_contracts_monthly_full_year():
    contracts = fc.generate_contracts("FED_FUNDS", "2026-01-01", "2026-12-31")
    assert len(contracts) == 12
    assert contracts[0] == "FFF26"
    assert contracts[-1] == "FFZ26"


def test_generate_contracts_partial_range():
    contracts = fc.generate_contracts("FED_FUNDS", "2026-01-15", "2026-03-10")
    # Jan, Feb, Mar all "belong" to the range even though start/end land
    # mid-month -- a contract belongs to its month, not a specific day.
    assert contracts == ["FFF26", "FFG26", "FFH26"]


def test_generate_contracts_spans_year_boundary():
    contracts = fc.generate_contracts("SOFR", "2026-10-01", "2027-04-01")
    assert contracts == ["SRAZ26", "SRAH27"]


def test_generate_contracts_sorted_chronologically():
    contracts = fc.generate_contracts("FED_FUNDS", "2026-01-01", "2027-12-31")
    assert contracts == sorted(
        contracts, key=lambda r: (fc.ric_module.parse_ric(r).year, fc.ric_module.parse_ric(r).month)
    )


def test_generate_contracts_invalid_range_raises():
    with pytest.raises(ValueError, match="must be <="):
        fc.generate_contracts("SOFR", "2026-12-31", "2026-01-01")


# ---------------------------------------------------------------------
# next_contract / previous_contract
# ---------------------------------------------------------------------

def test_next_contract_within_year():
    assert fc.next_contract("SOFR", "SRAH26") == "SRAM26"


def test_next_contract_crosses_year_boundary():
    assert fc.next_contract("SOFR", "SRAZ26") == "SRAH27"


def test_next_contract_monthly_cycle():
    assert fc.next_contract("FED_FUNDS", "FFZ26") == "FFF27"


def test_previous_contract_within_year():
    assert fc.previous_contract("SOFR", "SRAM26") == "SRAH26"


def test_previous_contract_crosses_year_boundary():
    assert fc.previous_contract("SOFR", "SRAH27") == "SRAZ26"


def test_next_then_previous_is_identity():
    original = "SRAM26"
    assert fc.previous_contract("SOFR", fc.next_contract("SOFR", original)) == original


def test_next_contract_wrong_market_raises():
    with pytest.raises(ValueError, match="belongs to market"):
        fc.next_contract("FED_FUNDS", "SRAZ26")


def test_next_contract_month_not_in_cycle_raises():
    # SRAF26 (January) isn't a valid quarterly SOFR month
    with pytest.raises(ValueError, match="not part of"):
        fc.next_contract("SOFR", "SRAF26")


# ---------------------------------------------------------------------
# rolling_windows
# ---------------------------------------------------------------------

def test_rolling_windows_spread_consecutive():
    contracts = ["SRAH26", "SRAM26", "SRAU26", "SRAZ26"]
    windows = fc.rolling_windows(contracts, [0, 1])
    assert windows == [
        ("SRAH26", "SRAM26"),
        ("SRAM26", "SRAU26"),
        ("SRAU26", "SRAZ26"),
    ]


def test_rolling_windows_fly_three_legs():
    contracts = ["SRAH26", "SRAM26", "SRAU26", "SRAZ26"]
    windows = fc.rolling_windows(contracts, [0, 1, 2])
    assert windows == [
        ("SRAH26", "SRAM26", "SRAU26"),
        ("SRAM26", "SRAU26", "SRAZ26"),
    ]


def test_rolling_windows_monthly_spread_matches_spec_example():
    # Mirrors the spec's Sep-Dec / Oct-Jan / Nov-Feb / Dec-Mar example:
    # a 3-month-spaced spread sliding one month at a time over a
    # monthly contract list.
    contracts = fc.generate_contracts("FED_FUNDS", "2026-09-01", "2027-03-31")
    windows = fc.rolling_windows(contracts, [0, 3])
    assert windows == [
        ("FFU26", "FFZ26"),  # Sep - Dec
        ("FFV26", "FFF27"),  # Oct - Jan
        ("FFX26", "FFG27"),  # Nov - Feb
        ("FFZ26", "FFH27"),  # Dec - Mar
    ]


def test_rolling_windows_condor_four_legs():
    contracts = ["A", "B", "C", "D", "E"]
    windows = fc.rolling_windows(contracts, [0, 1, 2, 3])
    assert windows == [
        ("A", "B", "C", "D"),
        ("B", "C", "D", "E"),
    ]


def test_rolling_windows_too_few_contracts_returns_empty():
    windows = fc.rolling_windows(["A", "B"], [0, 1, 2])
    assert windows == []


def test_rolling_windows_empty_offsets_raises():
    with pytest.raises(ValueError, match="non-empty"):
        fc.rolling_windows(["A", "B"], [])


def test_rolling_windows_offsets_must_start_at_zero():
    with pytest.raises(ValueError, match="must start at 0"):
        fc.rolling_windows(["A", "B"], [1, 2])


def test_rolling_windows_offsets_must_be_increasing():
    with pytest.raises(ValueError, match="strictly increasing"):
        fc.rolling_windows(["A", "B", "C"], [0, 2, 1])
