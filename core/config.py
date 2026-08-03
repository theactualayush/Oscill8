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
    """

    name: str
    ric_root: str
    exchange: str
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
        ric_root="SFI",
        exchange="ICE",
        ric_year_digits=1,
        verified=False,
        tick_value=12.50,
        currency="GBP",
        description="ICE 3-Month SONIA futures (VERIFY RIC ROOT)",
    ),
    "CORRA": MarketDefinition(
        name="CORRA (3M)",
        ric_root="CRA",
        exchange="MX",
        ric_year_digits=1,
        verified=False,
        tick_value=12.50,
        currency="CAD",
        description="Montreal Exchange 3-Month CORRA futures (VERIFY RIC ROOT)",
    ),
    "ESTR": MarketDefinition(
        name="Euro Short-Term Rate (€STR)",
        ric_root="ESR",
        exchange="ICE",
        ric_year_digits=1,
        verified=False,
        tick_value=12.50,
        currency="EUR",
        description="3-Month €STR futures (VERIFY RIC ROOT)",
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
