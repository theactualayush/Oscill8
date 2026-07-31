"""
futures_calendar.py

Single responsibility: futures contract calendar mechanics. Knows how
to generate listed contracts for a market, step forward/backward
through them, and build rolling contract-tuples for strategy scanning.

Knows NOTHING about LSEG, prices, or the database -- only about which
months a market lists contracts for (config.py) and how to turn a
(market, month, year) into a RIC and back (ric.py).

NOTE ON NAMING: this module is NOT named `calendar.py`, even though
that's what the project's task list calls it. Python's stdlib module
`calendar` is imported internally by `datetime.strptime` and by
pandas -- a project-local `calendar.py` shadows it and breaks both
(confirmed: `datetime.strptime` raises `AttributeError: module
'calendar' has no attribute 'day_abbr'`, and importing pandas fails
the same way). `futures_calendar.py` avoids the collision.
"""

from __future__ import annotations

from datetime import date

import config
import ric as ric_module
from config import ListingCycle, QUARTERLY_MONTHS
from utils import get_logger, to_date, DateLike

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Month generation
# --------------------------------------------------------------------------

def _cycle_months(cycle: ListingCycle) -> tuple[int, ...]:
    """Return the sorted tuple of calendar months a cycle lists."""
    if cycle == ListingCycle.QUARTERLY:
        return QUARTERLY_MONTHS
    if cycle == ListingCycle.MONTHLY:
        return tuple(range(1, 13))
    raise ValueError(f"Unhandled ListingCycle: {cycle}")  # pragma: no cover


def _month_years_in_range(
    cycle: ListingCycle, start: date, end: date
) -> list[tuple[int, int]]:
    """Generate (month, year) tuples for a cycle, inclusive of start/end.

    Iterates year-by-year to stay simple and obviously correct rather
    than clever; contract calendars only span a handful of years at a
    time so this is not a performance concern.
    """
    if start > end:
        raise ValueError(f"start ({start}) must be <= end ({end})")

    months = _cycle_months(cycle)
    result: list[tuple[int, int]] = []
    for year in range(start.year, end.year + 1):
        for month in months:
            contract_date = date(year, month, 1)
            # Compare by (year, month) only -- a contract "belongs" to
            # its month regardless of which day within start/end falls.
            if (year, month) < (start.year, start.month):
                continue
            if (year, month) > (end.year, end.month):
                continue
            result.append((month, year))
    return result


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def generate_contracts(market_key: str, start: DateLike, end: DateLike) -> list[str]:
    """Generate all listed contract RICs for a market between start and end.

    Uses the market's configured listing_cycle (QUARTERLY or MONTHLY)
    to determine which months are listed. Returned RICs are sorted
    chronologically.

    Example:
        generate_contracts("SOFR", "2026-01-01", "2026-12-31")
        -> ["SRAH26", "SRAM26", "SRAU26", "SRAZ26"]   # Mar/Jun/Sep/Dec

        generate_contracts("FED_FUNDS", "2026-01-01", "2026-03-31")
        -> ["FFF26", "FFG26", "FFH26"]                # Jan/Feb/Mar
    """
    market = config.get_market(market_key)
    start_d, end_d = to_date(start), to_date(end)

    month_years = _month_years_in_range(market.listing_cycle, start_d, end_d)
    contracts = [ric_module.build_ric(market_key, m, y) for m, y in month_years]

    logger.debug(
        "Generated %d contracts for %s [%s -> %s] cycle=%s",
        len(contracts), market_key, start_d, end_d, market.listing_cycle.value,
    )
    return contracts


def next_contract(market_key: str, contract_ric: str) -> str:
    """Return the RIC of the next listed contract after the given one.

    Example:
        next_contract("SOFR", "SRAZ26") -> "SRAH27"   # Dec26 -> Mar27
        next_contract("FED_FUNDS", "FFZ26") -> "FFF27"  # Dec26 -> Jan27
    """
    market = config.get_market(market_key)
    parsed = ric_module.parse_ric(contract_ric)
    if parsed.market_key != market_key:
        raise ValueError(
            f"RIC '{contract_ric}' belongs to market '{parsed.market_key}', "
            f"not '{market_key}'"
        )

    months = _cycle_months(market.listing_cycle)
    idx = months.index(parsed.month) if parsed.month in months else None
    if idx is None:
        raise ValueError(
            f"Month {parsed.month} of '{contract_ric}' is not part of "
            f"{market_key}'s {market.listing_cycle.value} listing cycle"
        )

    if idx == len(months) - 1:
        next_month, next_year = months[0], parsed.year + 1
    else:
        next_month, next_year = months[idx + 1], parsed.year

    return ric_module.build_ric(market_key, next_month, next_year)


def previous_contract(market_key: str, contract_ric: str) -> str:
    """Return the RIC of the previous listed contract before the given one.

    Example:
        previous_contract("SOFR", "SRAH27") -> "SRAZ26"   # Mar27 -> Dec26
    """
    market = config.get_market(market_key)
    parsed = ric_module.parse_ric(contract_ric)
    if parsed.market_key != market_key:
        raise ValueError(
            f"RIC '{contract_ric}' belongs to market '{parsed.market_key}', "
            f"not '{market_key}'"
        )

    months = _cycle_months(market.listing_cycle)
    idx = months.index(parsed.month) if parsed.month in months else None
    if idx is None:
        raise ValueError(
            f"Month {parsed.month} of '{contract_ric}' is not part of "
            f"{market_key}'s {market.listing_cycle.value} listing cycle"
        )

    if idx == 0:
        prev_month, prev_year = months[-1], parsed.year - 1
    else:
        prev_month, prev_year = months[idx - 1], parsed.year

    return ric_module.build_ric(market_key, prev_month, prev_year)


def rolling_windows(contracts: list[str], leg_offsets: list[int]) -> list[tuple[str, ...]]:
    """Slide a set of leg offsets across a sorted contract list.

    This is the general primitive behind the rolling strategy scanner.
    `leg_offsets` describes the *shape* of a strategy as index gaps
    from a sliding starting point -- it says nothing about weights
    (that's strategy_engine's job later), only which contracts belong
    together.

    Args:
        contracts: Chronologically sorted RICs, e.g. from
            generate_contracts(). Must be sorted for results to make
            sense; this function does not sort them for you.
        leg_offsets: Index offsets from the sliding start position.
            Must start at 0 and be strictly increasing.
            [0, 1] over consecutive quarterly contracts -> calendar spreads
            [0, 1, 2] over consecutive quarterly contracts -> flies
            [0, 3] over a MONTHLY contract list -> 3-month-spaced spreads
                that slide by one month each step, e.g. Sep-Dec, Oct-Jan,
                Nov-Feb, Dec-Mar, ...

    Returns:
        List of tuples of RICs, one tuple per valid sliding position.

    Example:
        rolling_windows(["SRAH26","SRAM26","SRAU26","SRAZ26"], [0, 1])
        -> [("SRAH26","SRAM26"), ("SRAM26","SRAU26"), ("SRAU26","SRAZ26")]
    """
    if not leg_offsets:
        raise ValueError("leg_offsets must be non-empty")
    if leg_offsets[0] != 0:
        raise ValueError(f"leg_offsets must start at 0, got {leg_offsets}")
    if any(a >= b for a, b in zip(leg_offsets, leg_offsets[1:])):
        raise ValueError(f"leg_offsets must be strictly increasing, got {leg_offsets}")

    span = leg_offsets[-1]
    windows = []
    for start_idx in range(0, len(contracts) - span):
        windows.append(tuple(contracts[start_idx + offset] for offset in leg_offsets))
    return windows
