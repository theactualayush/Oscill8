"""
market_instruments.py

Loads the authoritative Product -> Asset Class -> Sub Asset Class ->
Exchange -> TT Code -> Reuters/LSEG Code -> QuantHub Code reference
mapping from config/market_instruments.json (see that file's own
"description" field, and CLAUDE.md's "AUTHORITATIVE PRODUCT /
IDENTIFIER MAPPING" section).

This is deliberately NOT the same thing as core.config.MARKETS:

- core.config.MARKETS is Oscill8's own operational registry (RIC
  construction rules, bp_per_point, tick_value, listing_cycle, ...) for
  markets Oscill8 can actually price today.
- This module is a read-only reference table of trader-supplied
  identifiers across three separate, non-interchangeable systems (TT,
  Reuters/LSEG, QuantHub) for a much broader instrument universe,
  including markets/products Oscill8 does not configure at all.

Never derive a Reuters code from a QH code, or a QH code from a Reuters
code, or invent a missing mapping -- a lookup that finds nothing must
return None/raise, never guess. This module performs no such inference;
it only loads and looks up the supplied table verbatim.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from core.config import REPO_ROOT_DIR

MARKET_INSTRUMENTS_PATH = os.path.join(REPO_ROOT_DIR, "config", "market_instruments.json")

_REQUIRED_FIELDS = (
    "product",
    "asset_class",
    "sub_asset_class",
    "exchange",
    "tt_code",
    "reuters_code",
    "qh_code",
)


@dataclass(frozen=True)
class InstrumentMapping:
    """One row of the authoritative reference table, preserved verbatim.

    `tt_code`/`reuters_code`/`qh_code` are None where the source table
    left that cell blank (e.g. SONIA 1M has no reuters_code; several
    composite/intermarket products have no reuters_code) -- never
    guessed or backfilled.
    """

    product: str
    asset_class: str
    sub_asset_class: str
    exchange: str
    tt_code: str | None
    reuters_code: str | None
    qh_code: str | None


def load_market_instruments(path: str | None = None) -> list[InstrumentMapping]:
    """Load and parse config/market_instruments.json.

    Args:
        path: override the default MARKET_INSTRUMENTS_PATH (mainly for
            tests exercising a fixture file).

    Raises:
        FileNotFoundError: if the reference file is missing.
        ValueError: if the file's JSON is malformed, or an entry is
            missing one of the required keys.
    """
    load_path = path or MARKET_INSTRUMENTS_PATH
    with open(load_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("instruments")
    if entries is None:
        raise ValueError(f"{load_path}: missing top-level 'instruments' key")

    mappings = []
    for i, entry in enumerate(entries):
        missing = [field for field in _REQUIRED_FIELDS if field not in entry]
        if missing:
            raise ValueError(
                f"{load_path}: instrument entry {i} ({entry.get('product', '?')!r}) "
                f"is missing required field(s): {missing}"
            )
        mappings.append(InstrumentMapping(**{field: entry[field] for field in _REQUIRED_FIELDS}))

    return mappings


def find_by_product(mappings: list[InstrumentMapping], product: str) -> list[InstrumentMapping]:
    """Return every mapping whose product name matches exactly.

    A list, not a single result -- some products (e.g. "ESTR") appear
    more than once for distinct exchanges/instruments in the source
    table, and collapsing that to one result would silently discard a
    real, distinct row.
    """
    return [m for m in mappings if m.product == product]


__all__ = [
    "InstrumentMapping",
    "MARKET_INSTRUMENTS_PATH",
    "load_market_instruments",
    "find_by_product",
]
