"""
quanthub.py

Responsible for ONE thing: getting clean OHLCV bars out of QuantHub
(the in-house secondary market-data provider, GET
https://qh-api.corp.hertshtengroup.com/api/v2/ohlc/) for a given QH
instrument identifier / interval / date range.

Mirrors core.downloader's structure and public-API shape deliberately,
so database/service.py's provider dispatch can treat both providers
uniformly: open questions LSEG already answered (retry policy, column
normalization, 4H-via-resample) are answered the same way here rather
than reinvented.

Public API:
    build_instrument(qh_root, month, year) -> str
    download_history(instrument, interval, start, end) -> pd.DataFrame

CRITICAL NAMESPACE RULE (see CLAUDE.md / the QuantHub architecture
review this module implements): a QH instrument identifier is built
from a QH root (core.market_instruments / config/market_instruments.json)
plus a month/year suffix -- NEVER from an LSEG/Reuters RIC string. This
module has no function that takes a RIC and "converts" it; callers
(core.providers / database.service) are responsible for resolving the
QH root independently, via core.market_instruments, before calling
build_instrument().

QuantHub contract-suffix convention (VERIFIED against 6 live examples
spanning 4 different exchanges -- SRAH24, SONH26, ERH26, FSRH26,
YBAH26, FERH26): <qh_root><month_code><2-digit-year>, using the same
FUTURES_MONTH_CODES letters as the wider futures industry (F/G/H/J/K/M/
N/Q/U/V/X/Z). The month-code table itself is a universal futures-
industry convention (also published by TT/CME et al.), not something
inferred from LSEG's RIC construction -- but only the "H" (March) code
has actually been exercised against the live API; the other eleven are
carried over on that basis, not independently confirmed. The 2-digit
year is directly evidenced across all 6 examples (notably including
SONIA, whose LSEG ric_year_digits is 1 -- proving QuantHub's year-digit
convention is independent of, and must never be copied from, the
market's MarketDefinition.ric_year_digits).

QuantHub does NOT support a start/end date-range request shape
(confirmed via live testing -- a start=/end= request returned HTTP 500;
count= works). This module therefore estimates a `count` generous
enough to cover [start, end] and filters client-side -- see
_estimate_count()'s docstring for the known limitation this carries.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from core import config
from core.config import BarInterval, FUTURES_MONTH_CODES
from core.utils import CANONICAL_OHLCV_COLUMNS, DateLike, get_logger, resample_to_4h, to_date

logger = get_logger(__name__)


class QuantHubCredentialsMissingError(Exception):
    """Raised when a QuantHub call is attempted with no RBS_QUANTHUB_TOKEN
    configured. Deliberately distinct from core.downloader.
    MarketDataUnavailableError -- this is a configuration problem, not a
    market-data-availability finding, and must never be caught/skipped
    the way that narrow, LSEG-specific exception is. A market routed to
    LSEG must remain fully usable with no QuantHub credentials present
    at all -- this exception only ever fires on the QuantHub call path.
    """


# --------------------------------------------------------------------------
# Instrument construction (QH namespace -- never derived from a RIC)
# --------------------------------------------------------------------------

# Month-code letters actually exercised against the live QuantHub API.
# Every one of the 6 directly-verified examples (SRAH24, SONH26, ERH26,
# FSRH26, YBAH26, FERH26) is a March/"H" contract -- that is the ONLY
# letter with live evidence behind it. build_instrument() below still
# uses the full FUTURES_MONTH_CODES table for the other 11 months (do
# not remove that -- a market can't be scanned across a rolling curve
# with March-only contracts), but that is a carried-over assumption
# from the universal futures-industry month-code convention (the same
# letters TT/CME/ICE/ASX/MX all publish), NOT an independent QuantHub
# confirmation. Treat this constant as documenting the boundary of what
# has actually been tested, never as a claim that only "H" works.
LIVE_VERIFIED_QUANTHUB_MONTH_CODES = frozenset({"H"})


def build_instrument(qh_root: str, month: int, year: int) -> str:
    """Build a QuantHub instrument identifier from a QH root + contract
    month/year -- the QuantHub-side analogue of core.ric.build_ric(), but
    an entirely independent function/namespace. `qh_root` must come from
    core.market_instruments (config/market_instruments.json); this
    function does no lookup of its own and has no notion of an LSEG RIC.

    Example:
        build_instrument("ER", 3, 2026) -> "ERH26"   # EURIBOR, not FEIH26

    KNOWN LIMITATION: the month code beyond "H" (see
    LIVE_VERIFIED_QUANTHUB_MONTH_CODES above) is an assumed, not
    independently live-verified, transformation -- this function does
    not restrict which months may be requested (a market needs its full
    listing cycle to be scannable), it only documents/tests the
    assumption rather than hiding it.

    Raises:
        ValueError: if month is not 1-12, or qh_root is empty.
    """
    if not qh_root:
        raise ValueError("qh_root must be a non-empty QuantHub root code")
    if month not in FUTURES_MONTH_CODES:
        raise ValueError(f"month must be 1-12, got {month}")

    month_code = FUTURES_MONTH_CODES[month]
    if month_code not in LIVE_VERIFIED_QUANTHUB_MONTH_CODES:
        logger.debug(
            "build_instrument: month code '%s' is not in "
            "LIVE_VERIFIED_QUANTHUB_MONTH_CODES -- assumed via the standard "
            "futures month-code convention, not independently confirmed "
            "against the live QuantHub API.",
            month_code,
        )
    year_str = str(year)[-2:]
    return f"{qh_root}{month_code}{year_str}"


# --------------------------------------------------------------------------
# Count estimation (QuantHub has no start/end -- count + client filter)
# --------------------------------------------------------------------------

# Small safety margin on top of the calendar-day span for DAILY requests.
# Calendar days is already an upper bound on business days (weekends/
# holidays only ever reduce the true bar count), so this only guards
# against off-by-one edges, not a real historical-coverage assumption.
_DAILY_COUNT_BUFFER = 5

# Hourly: 24 native bars/calendar-day is a deliberately generous upper
# bound (no market trades 24 genuinely distinct hourly bars/day) --
# safer to over-ask than to silently under-cover a requested range,
# since QuantHub returns the most recent N bars, not a specific range.
_HOURLY_BARS_PER_DAY = 24


def _estimate_count(native_interval: str, start: datetime, end: datetime) -> int:
    """Estimate a QuantHub `count` generous enough to cover [start, end].

    KNOWN LIMITATION (see CLAUDE.md / the original QuantHub-integration
    task notes): whether `count` truly means "newest N observations",
    what QuantHub's maximum allowed count actually is, and whether
    pagination exists, are all UNVERIFIED as of this implementation --
    this heuristic is deliberately over-generous and cannot be proven
    correct for a very old/wide date range without further live testing
    against the real API. _fetch_quanthub_records() logs a warning
    whenever the returned data does not reach back to `start`, so a
    too-small count fails loudly (a gap in cached history, visible in
    logs) rather than silently.
    """
    calendar_days = max((end.date() - start.date()).days + 1, 1)
    if native_interval == "1D":
        return calendar_days + _DAILY_COUNT_BUFFER
    if native_interval == "1H":
        return calendar_days * _HOURLY_BARS_PER_DAY + _HOURLY_BARS_PER_DAY
    raise ValueError(f"No count-estimation rule for QuantHub native_interval={native_interval!r}")


# --------------------------------------------------------------------------
# HTTP fetch + response normalization
# --------------------------------------------------------------------------

def _auth_headers() -> dict:
    if not config.QUANTHUB_TOKEN:
        raise QuantHubCredentialsMissingError(
            "RBS_QUANTHUB_TOKEN is not set -- cannot call QuantHub. Markets "
            "routed to LSEG are unaffected; this only blocks QuantHub-routed "
            "markets (see core.providers.PROVIDER_ROUTING)."
        )
    return {"Authorization": f"Bearer {config.QUANTHUB_TOKEN}"}


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def _fetch_quanthub_records(
    instruments: list[str], native_interval: str, count: int
) -> dict[str, list[dict]]:
    """One HTTP call, potentially covering many instruments (QuantHub's
    `instruments=` parameter accepts a comma-separated list -- confirmed
    live: a 5-instrument batched request returned 5 records per
    instrument in one response).

    Returns raw records grouped by the response's own "product" field --
    NOT necessarily grouped by the order of `instruments`, since that's
    simply how the API tags each record.

    Response shape handling (both observed live, never guessed): a bare
    JSON list of records (the documented success shape), or a
    {"status": ..., "data": [...]} wrapper (observed for an empty
    result, e.g. {"status": "SUCCESS", "data": []}). Both are handled;
    no other wrapper shape has been observed or is assumed.

    Deliberately does NOT classify an HTTP 500 (observed live, as an
    HTML error page) as market-data-unavailable -- that would invent
    QuantHub error semantics we do not have evidence for (see CLAUDE.md
    Part 8). raise_for_status() lets it propagate as a plain
    requests.HTTPError, a real, unclassified provider failure.
    """
    headers = _auth_headers()
    params = {
        "instruments": ",".join(instruments),
        "interval": native_interval,
        "count": count,
    }
    logger.debug("Fetching QuantHub %s interval=%s count=%d", instruments, native_interval, count)

    response = requests.get(config.QUANTHUB_BASE_URL, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    payload = response.json()
    records = payload if isinstance(payload, list) else payload.get("data", [])

    grouped: dict[str, list[dict]] = {}
    for rec in records:
        grouped.setdefault(rec["product"], []).append(rec)
    return grouped


def _normalize_quanthub_records(records: list[dict]) -> pd.DataFrame:
    """Normalize QuantHub's native record shape
    ({"product","time","open","high","low","close","volume"}, time in
    Unix milliseconds) into the canonical OHLCV schema.
    """
    if not records:
        return pd.DataFrame(columns=CANONICAL_OHLCV_COLUMNS)

    df = pd.DataFrame.from_records(records)
    out = pd.DataFrame(
        {
            "Date": pd.to_datetime(df["time"], unit="ms"),
            "Open": pd.to_numeric(df["open"], errors="coerce").astype("float64"),
            "High": pd.to_numeric(df["high"], errors="coerce").astype("float64"),
            "Low": pd.to_numeric(df["low"], errors="coerce").astype("float64"),
            "Close": pd.to_numeric(df["close"], errors="coerce").astype("float64"),
            "Volume": pd.to_numeric(df["volume"], errors="coerce").astype("float64"),
        }
    )
    return out.sort_values("Date").reset_index(drop=True)


def fetch_batch(
    instruments: list[str], interval: str | BarInterval, count: int
) -> dict[str, pd.DataFrame]:
    """Fetch many QuantHub instruments in one HTTP call, each normalized
    to the canonical OHLCV schema. Exercises the same batching QuantHub
    itself supports -- not currently wired into database.get_history()
    (which is single-RIC), but available for a future multi-leg
    optimization without needing a second implementation later.
    """
    if isinstance(interval, str):
        interval = BarInterval(interval)
    native_interval = config.QUANTHUB_NATIVE_INTERVAL[interval]

    grouped = _fetch_quanthub_records(instruments, native_interval, count)
    return {
        instrument: _normalize_quanthub_records(grouped.get(instrument, []))
        for instrument in instruments
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def download_history(
    instrument: str,
    interval: str | BarInterval,
    start: DateLike,
    end: DateLike,
) -> pd.DataFrame:
    """Download historical OHLCV bars for a single QuantHub instrument.

    Args:
        instrument: QuantHub instrument identifier, e.g. "ERH26" -- must
            already be built via build_instrument(); this function does
            no RIC/root resolution of its own.
        interval: "DAILY", "HOURLY", or "4H" (see core.config.BarInterval).
        start: Start date (inclusive), str "YYYY-MM-DD", date, or datetime.
        end: End date (inclusive), str "YYYY-MM-DD", date, or datetime.

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
        Empty DataFrame (correct columns) if QuantHub returned no data
        for the requested instrument.

    Note:
        FOUR_HOUR is fetched as native 1H and resampled via
        core.utils.resample_to_4h -- the same function core.downloader
        uses for LSEG, not a second implementation.
    """
    if isinstance(interval, str):
        interval = BarInterval(interval)

    start_d = to_date(start)
    end_d = to_date(end)
    if start_d > end_d:
        raise ValueError(f"start ({start_d}) must be <= end ({end_d})")

    start_dt = datetime.combine(start_d, datetime.min.time())
    end_dt = datetime.combine(end_d, datetime.min.time())

    native_interval = config.QUANTHUB_NATIVE_INTERVAL[interval]
    count = _estimate_count(native_interval, start_dt, end_dt)

    grouped = _fetch_quanthub_records([instrument], native_interval, count)
    raw_records = grouped.get(instrument, [])
    df = _normalize_quanthub_records(raw_records)

    if not df.empty and len(df) >= count and df["Date"].min() > pd.Timestamp(start_d):
        logger.warning(
            "QuantHub %s: fetched count=%d bars but earliest returned Date (%s) "
            "is after the requested start (%s) -- history before that point may "
            "be missing because count was insufficient, not because it doesn't "
            "exist. See core.quanthub._estimate_count's documented limitation.",
            instrument, count, df["Date"].min(), start_d,
        )

    if interval == BarInterval.FOUR_HOUR:
        df = resample_to_4h(df, config.RESAMPLE_RULE[BarInterval.FOUR_HOUR])

    if not df.empty:
        df = df[(df["Date"] >= pd.Timestamp(start_d)) & (df["Date"] < pd.Timestamp(end_d) + pd.Timedelta(days=1))]
        df = df.reset_index(drop=True)

    logger.info("Downloaded %d bars for QuantHub instrument %s", len(df), instrument)
    return df
