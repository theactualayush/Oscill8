"""
providers.py

Single, centralized source of truth for "which market-data provider
serves this Oscill8 market". Nothing else in the codebase should
hard-code a market->provider decision -- database.service is the only
caller of resolve_provider(), matching the existing rule that
database/service.py is the sole boundary between the cache-first
get_history() contract and the actual provider layer (core.downloader
for LSEG, core.quanthub for QuantHub).

Deliberately NOT a general "any market can go anywhere" registry: a
market can only be routed to QuantHub once BOTH of the following exist,
independently:
    1. a core.config.MARKETS entry (needed for LSEG-side RIC
       construction -- see core.futures_calendar/core.ric -- which is
       what strategy_engine uses to generate a StrategyInstance's
       .rics in the first place, regardless of which provider ends up
       serving the data), and
    2. an entry in _MARKET_KEY_TO_QH_PRODUCT below, pointing at the
       market's product name in config/market_instruments.json (the
       authoritative Reuters/QH mapping table).

A market with (1) but not (2) simply isn't in PROVIDER_ROUTING and
falls through to the LSEG default. A market with neither cannot be
scanned at all yet (see CLAUDE.md's per-market Deferred notes) --
that's an existing, pre-QuantHub limitation, not something this module
introduces or works around.
"""

from __future__ import annotations

from enum import Enum

from core.market_instruments import find_by_product, load_market_instruments
from core.utils import get_logger

logger = get_logger(__name__)


class Provider(str, Enum):
    LSEG = "LSEG"
    QUANTHUB = "QUANTHUB"


# Explicit market_key -> Provider routing. Absence means LSEG (the
# default, matching "for any market not explicitly configured for
# QuantHub, use LSEG"). Every key here MUST also have a core.config.
# MARKETS entry -- see the module docstring.
#
# EURIBOR / SARON / YBA / ESTR_ICE all now have core.config.MARKETS
# entries (trader-confirmed RIC root/year-digits/bp_per_point, not yet
# live-LSEG-verified in this environment -- see each MarketDefinition's
# own description) and are routed to QuantHub per the trader's explicit
# instruction. SOFR stays LSEG; the existing CME "ESTR" market key is
# untouched and stays LSEG -- it is a distinct product (RIC root "SRE")
# from "ESTR_ICE" (RIC root "EON3") and must never be conflated with it.
PROVIDER_ROUTING: dict[str, Provider] = {
    "CORRA": Provider.QUANTHUB,
    "SONIA": Provider.QUANTHUB,
    "EURIBOR": Provider.QUANTHUB,
    "SARON": Provider.QUANTHUB,
    "YBA": Provider.QUANTHUB,
    "ESTR_ICE": Provider.QUANTHUB,
}


def resolve_provider(market_key: str) -> Provider:
    """Return the provider that should serve `market_key`. Defaults to
    LSEG for any market not explicitly listed in PROVIDER_ROUTING.
    """
    return PROVIDER_ROUTING.get(market_key, Provider.LSEG)


# market_key (core.config.MARKETS) -> (product name, exchange-or-None)
# in config/market_instruments.json. Explicit and manually maintained --
# deliberately NOT derived by fuzzy-matching market_key against product
# names (e.g. "SONIA" vs "SONIA 3M"), since that kind of inference is
# exactly what CLAUDE.md's "do not infer" rule forbids. Only markets
# actually present in PROVIDER_ROUTING need an entry here.
#
# The exchange element disambiguates a product name that appears more
# than once in the mapping table -- "ESTR" has two distinct rows (CME:
# Reuters SRE/QH ESR, and ICE_EUROPE: Reuters EON3/QH FER). ESTR_ICE
# must resolve ONLY the ICE_EUROPE row; None means the product name is
# already unique and no further filter is needed.
_MARKET_KEY_TO_QH_PRODUCT: dict[str, tuple[str, str | None]] = {
    "CORRA": ("CORRA", None),
    "SONIA": ("SONIA 3M", None),
    "EURIBOR": ("Euribor", None),
    "SARON": ("SARON", None),
    "YBA": ("Australia 90 Day Bank Bill", None),
    "ESTR_ICE": ("ESTR", "ICE_EUROPE"),
}


def qh_root_for_market(market_key: str) -> str:
    """Resolve `market_key`'s QuantHub root code via config/
    market_instruments.json -- the ONLY path by which a QH root may be
    obtained. Never derives a QH root from the market's LSEG ric_root.

    Raises:
        ValueError: if `market_key` has no registered QH product
            mapping, the (product, exchange) combination isn't found
            (or is still ambiguous) in market_instruments.json, or its
            qh_code is null there.
    """
    entry = _MARKET_KEY_TO_QH_PRODUCT.get(market_key)
    if entry is None:
        raise ValueError(
            f"No QuantHub product mapping registered for market '{market_key}' "
            f"in core.providers._MARKET_KEY_TO_QH_PRODUCT."
        )
    product, exchange = entry

    mappings = load_market_instruments()
    matches = find_by_product(mappings, product)
    if exchange is not None:
        matches = [m for m in matches if m.exchange == exchange]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one config/market_instruments.json row for "
            f"product {product!r} exchange={exchange!r} (market '{market_key}'), "
            f"found {len(matches)}."
        )

    qh_root = matches[0].qh_code
    if not qh_root:
        raise ValueError(
            f"config/market_instruments.json row for product {product!r} "
            f"(market '{market_key}') has no qh_code."
        )
    return qh_root


__all__ = ["Provider", "PROVIDER_ROUTING", "resolve_provider", "qh_root_for_market"]
