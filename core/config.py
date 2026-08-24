"""
config.py

Central configuration for the Range Bound Strategy Scanner.

Holds:
    - Market / RIC registry (contract root codes, month codes, tick sizes)
    - Interval definitions
    - Database connection settings
    - Application-wide constants

No business logic lives here. This module is imported by nearly every
other module in the project, so it must have zero dependencies on them
(avoid circular imports).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os


# --------------------------------------------------------------------------
# Environment / paths
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# Repo root (parent of core/) and the SQLite market-data cache path (Module 2:
# database/). Directory creation for the DB file is database/connection.py's
# responsibility at call time, not a side effect of importing this module.
REPO_ROOT_DIR = os.path.dirname(BASE_DIR)
SQLITE_DB_PATH = os.environ.get(
    "RBS_SQLITE_PATH",
    os.path.join(REPO_ROOT_DIR, "data", "oscill8.db"),
)

# Directory Module 7A's StrategySetRepository persists one JSON file per
# saved StrategySet into. Directory creation is that repository's own
# responsibility at save() time, not a side effect of importing this
# module -- same convention as SQLITE_DB_PATH above.
STRATEGY_SETS_DIR = os.environ.get(
    "RBS_STRATEGY_SETS_DIR",
    os.path.join(REPO_ROOT_DIR, "data", "strategy_sets"),
)


# --------------------------------------------------------------------------
# Database settings (PostgreSQL)
# --------------------------------------------------------------------------

DB_CONFIG = {
    "host": os.environ.get("RBS_DB_HOST", "localhost"),
    "port": int(os.environ.get("RBS_DB_PORT", 5432)),
    "dbname": os.environ.get("RBS_DB_NAME", "range_bound_scanner"),
    "user": os.environ.get("RBS_DB_USER", "postgres"),
    "password": os.environ.get("RBS_DB_PASSWORD", ""),
}


# --------------------------------------------------------------------------
# LSEG session settings
# --------------------------------------------------------------------------

# "desktop.workspace" uses the locally running Workspace/Eikon session.
# app_key is only required the first time; after `lseg-data.config.json`
# (or Workspace API Proxy) is set up, name="desktop.workspace" is enough.
LSEG_SESSION_TYPE = os.environ.get("RBS_LSEG_SESSION", "desktop.workspace")
LSEG_APP_KEY = os.environ.get("RBS_LSEG_APP_KEY", "")


# --------------------------------------------------------------------------
# QuantHub session settings
# --------------------------------------------------------------------------

# In-house secondary market-data provider (core.quanthub). Never hardcoded --
# read from the environment only, consistent with LSEG_APP_KEY above. The
# app must remain usable with no QuantHub credentials configured for any
# market still routed to LSEG (core.providers.PROVIDER_ROUTING) -- these
# settings are only consulted when core.quanthub.download_history() is
# actually called.
QUANTHUB_BASE_URL = os.environ.get(
    "RBS_QUANTHUB_BASE_URL", "https://qh-api.corp.hertshtengroup.com/api/v2/ohlc/"
)
QUANTHUB_TOKEN = os.environ.get("RBS_QUANTHUB_TOKEN", "")


# --------------------------------------------------------------------------
# Interval definitions
# --------------------------------------------------------------------------

class BarInterval(str, Enum):
    """Supported bar intervals across the application.

    FOUR_HOUR is NOT a native LSEG historical-pricing interval. It is
    synthesized by downloading HOURLY bars and resampling them
    (see downloader.py:_resample_to_4h). Every other module should treat
    it as a first-class interval; the resampling detail is contained
    entirely inside the downloader.
    """

    DAILY = "DAILY"
    HOURLY = "HOURLY"
    FOUR_HOUR = "4H"


# Maps our BarInterval -> the native lseg.data.content.historical_pricing
# Intervals value that should actually be requested from the API.
LSEG_NATIVE_INTERVAL = {
    BarInterval.DAILY: "daily",
    BarInterval.HOURLY: "hourly",
    BarInterval.FOUR_HOUR: "hourly",  # fetched hourly, then resampled to 4H
}

# Pandas resample rule used when synthesizing bars from native ones.
RESAMPLE_RULE = {
    BarInterval.FOUR_HOUR: "4h",
}

# Maps our BarInterval -> the native QuantHub /api/v2/ohlc/ `interval`
# value. QuantHub natively supports 1M/5M/1H/1D only (confirmed) -- there
# is no native 4H, so FOUR_HOUR is fetched as 1H and resampled, exactly
# mirroring LSEG_NATIVE_INTERVAL's FOUR_HOUR -> hourly treatment above
# (same core.utils.resample_to_4h, not a second implementation).
QUANTHUB_NATIVE_INTERVAL = {
    BarInterval.DAILY: "1D",
    BarInterval.HOURLY: "1H",
    BarInterval.FOUR_HOUR: "1H",
}

# Maximum practical lookback the API will comfortably serve per interval
# without needing chunked requests. Used by downloader.py to decide
# whether a request needs to be split into multiple calls.
MAX_LOOKBACK_DAYS = {
    BarInterval.DAILY: 3650,   # ~10 years
    BarInterval.HOURLY: 180,   # ~6 months per call is a safe chunk size
    BarInterval.FOUR_HOUR: 180,
}


# --------------------------------------------------------------------------
# Futures month codes (standard futures industry codes)
# --------------------------------------------------------------------------

FUTURES_MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}


# --------------------------------------------------------------------------
# Contract listing cycles
# --------------------------------------------------------------------------

class ListingCycle(str, Enum):
    """Which calendar months a market lists contracts for.

    QUARTERLY: Mar/Jun/Sep/Dec only (the IMM months) -- the standard,
        most-liquid convention for SOFR/SONIA/CORRA/ESTR quarterlies.
    MONTHLY: every calendar month -- e.g. Fed Funds futures.

    Note: some markets (e.g. CME SOFR) also list near-dated *serial*
    (monthly) contracts alongside quarterlies. This registry defaults
    such markets to QUARTERLY since that's the liquid convention used
    for range-bound scanning; switch a market to MONTHLY (or extend
    this enum with a MONTHLY_PLUS_QUARTERLY hybrid later) if serials
    are needed.
    """

    QUARTERLY = "QUARTERLY"
    MONTHLY = "MONTHLY"


QUARTERLY_MONTHS = (3, 6, 9, 12)  # Mar, Jun, Sep, Dec -- universal IMM months


# --------------------------------------------------------------------------
# Market registry
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketDefinition:
    """Defines a STIR futures market and how to build its RICs.

    ric_root: The RIC root/prefix as it appears on LSEG (e.g. "SR3" for
        CME 3M SOFR futures -> contracts look like "SR3Z26").
    ric_year_digits: 1 or 2. Most LSEG STIR futures use a 2-digit year
        (e.g. "SR3Z26"); some legacy chains use 1 digit ("SR3Z6").
        Known limitation (documented, not solved): a 1-digit-year
        market's RIC string collides across decades -- e.g. Sep-2026
        and Sep-2036 both encode to the same "...U6" suffix; build_ric()
        does not disambiguate this at construction time (parse_ric()'s
        reference_date-based disambiguation only helps when reading an
        already-received RIC back, not when generating two of them).
        Not a live issue today: no market in this registry lists
        contracts anywhere near a decade out, so generate_contracts()/
        generate_instances() never actually reach the collision in
        practice. Revisit only if a market's practical
        contract-generation horizon ever approaches ~10 years.
    exchange: Informational only (used in UI labels).
    verified: Whether the ric_root has been confirmed against a live
        LSEG chain/search on a Workspace terminal. Markets with
        verified=False are included so the architecture supports them,
        but the ric_root is a best-effort placeholder and MUST be
        checked (e.g. via `ld.discovery.search`) before relying on it
        for real data.
    listing_cycle: Which months this market lists contracts for. See
        ListingCycle above.
    tick_value: Value of one tick, in the contract's local currency.
        Used later by analytics/risk modules; not needed by the
        downloader itself.
    bp_per_point: Price-point -> basis-point multiplier for this
        market's quoting convention (e.g. 100.0 for a standard STIR
        "100 - rate" quote, where 0.01 price points = 1bp). Required,
        deliberately with no default -- range_analytics (Module 4)
        converts price-unit diagnostics to bp via this field rather
        than a hard-coded *100, and a market that forgets to state its
        own convention should fail loudly at registration time rather
        than silently inherit an assumption that may not hold for it.
    """

    name: str
    ric_root: str
    exchange: str
    bp_per_point: float
    ric_year_digits: int = 2
    verified: bool = True
    listing_cycle: ListingCycle = ListingCycle.QUARTERLY
    tick_value: float = 0.0
    currency: str = "USD"
    description: str = ""


MARKETS: dict[str, MarketDefinition] = {
    "SOFR": MarketDefinition(
        name="SOFR (3M)",
        ric_root="SRA",
        exchange="CME",
        bp_per_point=100.0,
        ric_year_digits=2,
        verified=True,
        listing_cycle=ListingCycle.QUARTERLY,
        tick_value=12.50,
        currency="USD",
        description="CME 3-Month SOFR futures",
    ),
    "FED_FUNDS": MarketDefinition(
        name="Fed Funds (30-Day)",
        ric_root="FF",
        exchange="CME",
        bp_per_point=100.0,
        ric_year_digits=2,
        verified=False,
        listing_cycle=ListingCycle.MONTHLY,
        tick_value=41.67,
        currency="USD",
        description="CME 30-Day Fed Funds futures (RE-VERIFY RIC ROOT — "
                     "SOFR's assumed root 'SR3' turned out to be wrong, "
                     "confirmed 'SRA' via Workspace; FF has not yet been "
                     "checked the same way)",
    ),
    # --- The following roots are best-effort placeholders. -------------
    # Confirm the exact RIC root against a live LSEG chain/search
    # (e.g. ld.discovery.search(query="SONIA futures")) before trading
    # or relying on this data. Only the ric_root field needs to change
    # once verified -- no other code depends on it.
    "SONIA": MarketDefinition(
        name="SONIA (3M)",
        ric_root="SON",
        exchange="ICE",
        bp_per_point=100.0,
        ric_year_digits=1,
        verified=False,
        tick_value=12.50,
        currency="GBP",
        description="ICE 3-Month SONIA futures (RIC root 'SON' confirmed "
                     "by trader spec; not yet verified via live LSEG "
                     "Workspace chain/search)",
    ),
    "CORRA": MarketDefinition(
        name="CORRA (3M)",
        ric_root="CRA",
        exchange="MX",
        bp_per_point=100.0,
        ric_year_digits=1,
        verified=False,
        tick_value=12.50,
        currency="CAD",
        description="Montreal Exchange 3-Month CORRA futures (VERIFY RIC ROOT)",
    ),
    "ESTR": MarketDefinition(
        name="Euro Short-Term Rate (€STR)",
        ric_root="SRE",
        exchange="ICE",
        bp_per_point=100.0,
        ric_year_digits=2,
        verified=False,
        tick_value=12.50,
        currency="EUR",
        description="3-Month €STR futures (RIC root 'SRE', 2-digit year, "
                     "confirmed via live LSEG pull -- SREU26 returned full "
                     "daily OHLC/SETTLE/BID/ASK; not yet re-verified via a "
                     "live chain/search)",
    ),
    # --- Trader-confirmed (not yet live-LSEG-tested in this environment) ---
    # RIC root/year-digit convention confirmed directly by the trader
    # against real LSEG-style contract codes (see each description below).
    # `verified=False` because no live LSEG Workspace chain/search or data
    # pull has been performed in this development environment (no
    # `lseg.data` package/session available here) -- ready for live
    # verification on a machine with LSEG Workspace, per the trader's own
    # instruction not to fabricate a successful live test.
    "EURIBOR": MarketDefinition(
        name="Euribor (3M)",
        ric_root="FEI",
        exchange="ICE_EUROPE",
        bp_per_point=100.0,
        ric_year_digits=1,
        verified=False,
        currency="EUR",
        description="ICE 3-Month Euribor futures (RIC root 'FEI', 1-digit "
                     "year -- trader-confirmed against real contract codes "
                     "FEIM6/FEIU7; not yet live-LSEG-verified in this "
                     "environment). Routed to QuantHub for historical data "
                     "(core.providers) -- QH root 'ER', independent of this "
                     "LSEG root.",
    ),
    "SARON": MarketDefinition(
        name="SARON (3M)",
        ric_root="SARO3",
        exchange="ICE_EUROPE",
        bp_per_point=100.0,
        ric_year_digits=1,
        verified=False,
        currency="CHF",
        description="ICE 3-Month SARON futures (RIC root 'SARO3', 1-digit "
                     "year -- trader-confirmed against real contract codes "
                     "SARO3U6/SARO3M7; not yet live-LSEG-verified in this "
                     "environment). Routed to QuantHub for historical data "
                     "(core.providers) -- QH root 'FSR', independent of "
                     "this LSEG root.",
    ),
    "YBA": MarketDefinition(
        name="Australia 90 Day Bank Bill",
        ric_root="YBA",
        exchange="ASX",
        bp_per_point=100.0,
        ric_year_digits=1,
        verified=False,
        currency="AUD",
        description="ASX 90-Day Bank Accepted Bill futures (RIC root "
                     "'YBA', 1-digit year -- trader-confirmed against real "
                     "contract codes YBAU6/YBAZ7; not yet live-LSEG-"
                     "verified in this environment). Routed to QuantHub "
                     "for historical data (core.providers) -- QH root "
                     "'YBA', independent of this LSEG root even though "
                     "both happen to be the same string.",
    ),
    "ESTR_ICE": MarketDefinition(
        name="ICE Europe Euro Short-Term Rate (€STR)",
        ric_root="EON3",
        exchange="ICE_EUROPE",
        bp_per_point=100.0,
        ric_year_digits=1,
        verified=False,
        currency="EUR",
        description="ICE Europe 3-Month €STR futures (RIC root 'EON3', "
                     "1-digit year -- trader-confirmed against real "
                     "contract codes EON3U6/EON3Z7; not yet live-LSEG-"
                     "verified in this environment). Deliberately a "
                     "SEPARATE market key from the existing 'ESTR' entry "
                     "above, which is the distinct CME product (RIC root "
                     "'SRE') -- never collapse the two. Routed to "
                     "QuantHub for historical data (core.providers) -- QH "
                     "root 'FER', independent of this LSEG root.",
    ),
}


def get_market(key: str) -> MarketDefinition:
    """Look up a market definition by its registry key (e.g. 'SOFR')."""
    try:
        return MARKETS[key]
    except KeyError as exc:
        valid = ", ".join(MARKETS.keys())
        raise KeyError(f"Unknown market '{key}'. Valid options: {valid}") from exc


# NOTE: RIC construction/parsing lives in ric.py, not here. config.py stays
# pure data (registry + settings) so it has zero business logic and can be
# safely imported from anywhere without side effects.


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("RBS_LOG_LEVEL", "INFO")
LOG_FILE = os.path.join(LOG_DIR, "range_bound_scanner.log")
