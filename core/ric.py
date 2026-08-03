"""
ric.py

Single responsibility: turn (market, month, year) into a RIC, and turn
a RIC back into (market, month, year).

This is pulled out of config.py deliberately -- config.py should stay
pure data (the market registry, settings), while this module owns the
string-manipulation logic that depends on that data. Downloader,
strategy_engine, and the future rolling-contract scanner all import
from here rather than duplicating RIC-building logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core import config

# Reverse lookup: month letter code -> month number (built once from config)
_CODE_TO_MONTH: dict[str, int] = {v: k for k, v in config.FUTURES_MONTH_CODES.items()}


@dataclass(frozen=True)
class ParsedRic:
    """Result of parsing a RIC back into its components."""

    ric: str
    market_key: str
    month: int
    year: int

    @property
    def market(self) -> config.MarketDefinition:
        return config.get_market(self.market_key)


def build_ric(market_key: str, month: int, year: int) -> str:
    """Build a futures RIC for a given market, contract month, and year.

    Example:
        build_ric("SOFR", 12, 2026) -> "SRAZ26"

    Raises:
        KeyError: if market_key is not in config.MARKETS.
        ValueError: if month is not 1-12.
    """
    if month not in config.FUTURES_MONTH_CODES:
        raise ValueError(f"month must be 1-12, got {month}")

    market = config.get_market(market_key)
    month_code = config.FUTURES_MONTH_CODES[month]
    year_str = str(year)[-market.ric_year_digits:]
    return f"{market.ric_root}{month_code}{year_str}"


def parse_ric(ric: str, reference_date: date | None = None) -> ParsedRic:
    """Parse a RIC back into (market_key, month, year).

    Args:
        ric: e.g. "SRAZ26" or "SFIH7".
        reference_date: used only to disambiguate 1-digit-year roots
            (e.g. "SFIH7" -> year could be 2027 or 2037). Defaults to
            today. The nearest year to reference_date whose last digit
            matches is chosen, preferring a year within the next 9
            years over one in the past.

    Returns:
        ParsedRic

    Raises:
        ValueError: if the RIC doesn't match any known market's root,
            or the remaining characters don't decode to a valid
            month/year for that market.
    """
    reference_date = reference_date or date.today()

    # Try longest root match first, in case of overlapping prefixes
    # (e.g. a hypothetical "SR" root vs "SRA").
    candidate_markets = sorted(
        config.MARKETS.items(), key=lambda kv: len(kv[1].ric_root), reverse=True
    )

    for market_key, market in candidate_markets:
        if not ric.startswith(market.ric_root):
            continue

        remainder = ric[len(market.ric_root):]
        expected_len = 1 + market.ric_year_digits
        if len(remainder) != expected_len:
            continue

        month_char, year_digits = remainder[0], remainder[1:]
        if month_char not in _CODE_TO_MONTH:
            continue
        if not year_digits.isdigit():
            continue

        month = _CODE_TO_MONTH[month_char]
        year = _resolve_year(year_digits, market.ric_year_digits, reference_date)
        return ParsedRic(ric=ric, market_key=market_key, month=month, year=year)

    raise ValueError(
        f"Could not parse RIC '{ric}' against any known market root "
        f"({', '.join(m.ric_root for m in config.MARKETS.values())})"
    )


def _resolve_year(year_digits: str, digit_count: int, reference_date: date) -> int:
    """Resolve a 1- or 2-digit RIC year fragment into a full 4-digit year."""
    if digit_count == 2:
        # Standard convention: 2-digit RIC years are always 2000s for
        # any market this scanner covers (no STIR futures RIC data from
        # the 1900s is relevant here).
        return 2000 + int(year_digits)

    # 1-digit year: pick the closest year (today's decade or the next)
    # whose last digit matches, biased toward the future since futures
    # RICs referenced here are for live/upcoming contracts.
    digit = int(year_digits)
    ref_year = reference_date.year
    candidates = [
        y for y in range(ref_year - 1, ref_year + 11)
        if y % 10 == digit
    ]
    return min(candidates, key=lambda y: abs(y - ref_year))
