"""
tests/test_ric.py

Unit tests for ric.py -- RIC construction and parsing.
"""

from __future__ import annotations

from datetime import date

import pytest

import ric


def test_build_ric_sofr():
    assert ric.build_ric("SOFR", 12, 2026) == "SRAZ26"


def test_build_ric_fed_funds():
    assert ric.build_ric("FED_FUNDS", 1, 2027) == "FFF27"


def test_build_ric_one_digit_year_market():
    # SONIA uses ric_year_digits=1
    assert ric.build_ric("SONIA", 3, 2027) == "SFIH7"


def test_build_ric_invalid_month_raises():
    with pytest.raises(ValueError, match="month must be 1-12"):
        ric.build_ric("SOFR", 13, 2026)


def test_build_ric_unknown_market_raises():
    with pytest.raises(KeyError):
        ric.build_ric("NOT_A_MARKET", 1, 2026)


def test_parse_ric_two_digit_year():
    parsed = ric.parse_ric("SRAZ26")
    assert parsed.market_key == "SOFR"
    assert parsed.month == 12
    assert parsed.year == 2026


def test_parse_ric_fed_funds():
    parsed = ric.parse_ric("FFF27")
    assert parsed.market_key == "FED_FUNDS"
    assert parsed.month == 1
    assert parsed.year == 2027


def test_parse_ric_one_digit_year_near_term():
    # reference date in 2026 -> "7" should resolve to 2027, not 2037
    parsed = ric.parse_ric("SFIH7", reference_date=date(2026, 1, 1))
    assert parsed.market_key == "SONIA"
    assert parsed.month == 3
    assert parsed.year == 2027


def test_parse_ric_one_digit_year_wraps_to_next_decade():
    # reference date late in a decade -> should still resolve forward,
    # not accidentally pick a year in the past.
    parsed = ric.parse_ric("SFIH1", reference_date=date(2029, 6, 1))
    assert parsed.year == 2031


def test_parse_ric_unknown_root_raises():
    with pytest.raises(ValueError, match="Could not parse RIC"):
        ric.parse_ric("ZZZQ99")


def test_parse_ric_wrong_length_for_market_raises():
    # "SRA" root but too many digits for a 2-digit-year market
    with pytest.raises(ValueError, match="Could not parse RIC"):
        ric.parse_ric("SRAZ2026")


@pytest.mark.parametrize(
    "market_key,month,year",
    [
        ("SOFR", 3, 2026),
        ("SOFR", 6, 2027),
        ("SOFR", 9, 2028),
        ("SOFR", 12, 2030),
        ("FED_FUNDS", 1, 2026),
        ("FED_FUNDS", 7, 2029),
    ],
)
def test_build_then_parse_round_trip(market_key, month, year):
    generated = ric.build_ric(market_key, month, year)
    parsed = ric.parse_ric(generated, reference_date=date(year, 1, 1))
    assert parsed.market_key == market_key
    assert parsed.month == month
    assert parsed.year == year
