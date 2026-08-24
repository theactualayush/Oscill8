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

Live testing (see QUANTHUB_MAX_REQUEST_COUNT) also confirmed count=3000
is safe for at least 4 different instruments across 2 exchanges, while
count=4416 for one of them (YBAH28) returned HTTP 429 -- so this module
caps every single-request count at 3000 and NEVER makes multiple
requests or paginates to compensate (no such mechanism is known to
exist for this API): a request whose true required history exceeds the
cap simply retrieves a shorter window than asked for, exactly like an
instrument whose own real history is shorter than the requested count
(both are normal, expected outcomes here, never treated as errors and
never padded/fabricated). HTTP 429 itself is raised as a distinct,
non-retried QuantHubRateLimitError -- see that class and
_fetch_quanthub_records for the retry-policy detail.

Live testing also confirmed QuantHub accepts multiple instruments in
ONE request (10 instruments x count=48 -> 480 records, all HTTP 200) --
see QUANTHUB_BATCH_SIZE / download_history_batch(). Batching many
instruments into one HTTP request (instead of one request per
instrument) is the primary tool for staying under QuantHub's rate
limit during an intermarket scan that needs many contracts at once;
database.service.get_history_batch() is the caller-facing entry point
that uses this for QuantHub-routed RICs.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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


class QuantHubRateLimitError(Exception):
    """Raised when QuantHub returns HTTP 429 (rate limited) -- live-
    confirmed to occur (a count=4416 request for YBAH28 returned 429).
    Deliberately NOT retried by the generic tenacity policy on
    _fetch_quanthub_records: unlike a 5xx or network failure, we have no
    evidence of a Retry-After header or any other cooldown signal in the
    response, so blindly retrying on the same short exponential backoff
    used for transient errors risks compounding the rate-limit condition
    rather than resolving it. Whether 429 here is caused by request size
    or by a request-rate limit is NOT established -- this exception only
    makes the condition distinguishable to callers, it does not claim a
    cause. Distinct from QuantHubCredentialsMissingError (a configuration
    problem) and from core.downloader.MarketDataUnavailableError (LSEG's
    own narrow "no market data for this RIC" classification, unrelated).
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

# Conservative maximum single-request `count`, based on live testing
# (see the QuantHub history investigation): count=3000 returned HTTP 200
# with real data for FOUR different instruments across two exchanges
# (YBAH28, ERH26, FSRH26, SONH26); count=4416 for YBAH28 returned HTTP
# 429. This is NOT a claim that 3000 is QuantHub's actual limit, nor
# that every instrument has 3000 bars of real history (YBAH28 itself
# only ever returned 2995 regardless of whether 3000 or 4000 was
# requested -- available history is instrument-specific, never padded
# or fabricated here) -- it is simply the largest count value this
# integration has live evidence is safe to request in one call. Whether
# HTTP 429 is triggered by request size specifically, by a request-rate
# limit, or something else, is NOT established -- this cap addresses
# the one confirmed-safe data point without guessing at the cause.
QUANTHUB_MAX_REQUEST_COUNT = 3000


def _estimate_count(native_interval: str, start: datetime, end: datetime) -> int:
    """Estimate a QuantHub `count` generous enough to cover [start, end],
    capped at QUANTHUB_MAX_REQUEST_COUNT.

    KNOWN LIMITATION (see CLAUDE.md / the original QuantHub-integration
    task notes): whether `count` truly means "newest N observations" is
    unverified beyond the live tests QUANTHUB_MAX_REQUEST_COUNT is based
    on, and no pagination/start/end/offset/cursor mechanism is known to
    exist (confirmed absent from both this client and any documentation
    available to this repo) -- so a request whose true required count
    would exceed the cap simply retrieves a shorter history than the
    requested [start, end] window, never multiple requests, never
    fabricated bars. This heuristic is deliberately over-generous UNDER
    the cap and cannot be proven correct for a very old/wide date range
    without further live testing. _fetch_quanthub_records() logs a
    warning whenever the returned data does not reach back to `start`
    despite the full (possibly capped) count being consumed, so a
    too-small effective count fails loudly (a gap in cached history,
    visible in logs) rather than silently.
    """
    calendar_days = max((end.date() - start.date()).days + 1, 1)
    if native_interval == "1D":
        estimated = calendar_days + _DAILY_COUNT_BUFFER
    elif native_interval == "1H":
        estimated = calendar_days * _HOURLY_BARS_PER_DAY + _HOURLY_BARS_PER_DAY
    else:
        raise ValueError(f"No count-estimation rule for QuantHub native_interval={native_interval!r}")
    return min(estimated, QUANTHUB_MAX_REQUEST_COUNT)


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
    # A 429 (QuantHubRateLimitError) is deliberately excluded from retry
    # -- see that exception's own docstring for why blindly retrying a
    # rate-limit response on this short backoff is not safe to assume.
    # Every other exception (5xx, network/timeout failures, etc.) keeps
    # the existing retry behaviour unchanged -- mirrors core.downloader.
    # _fetch_chunk's own retry_if_exception_type & retry_if_not_
    # exception_type pattern for its own narrow exclusion.
    retry=retry_if_exception_type(Exception) & retry_if_not_exception_type(QuantHubRateLimitError),
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

    HTTP 429 (live-confirmed, see QuantHubRateLimitError) is raised as
    that specific exception BEFORE raise_for_status() and is excluded
    from this function's own retry policy above. Deliberately does NOT
    classify an HTTP 500 (observed live, as an HTML error page) as
    market-data-unavailable -- that would invent QuantHub error
    semantics we do not have evidence for (see CLAUDE.md Part 8).
    raise_for_status() lets a 500 (or any other non-429 error status)
    propagate as a plain requests.HTTPError, a real, unclassified
    provider failure that DOES still retry per the policy above -- no
    Retry-After or other cooldown header handling is implemented for
    429 or anything else, since none has been observed in this API's
    responses.
    """
    headers = _auth_headers()
    params = {
        "instruments": ",".join(instruments),
        "interval": native_interval,
        "count": count,
    }
    logger.debug("Fetching QuantHub %s interval=%s count=%d", instruments, native_interval, count)

    response = requests.get(config.QUANTHUB_BASE_URL, headers=headers, params=params, timeout=30)
    if response.status_code == 429:
        raise QuantHubRateLimitError(
            f"QuantHub rate-limited this request (HTTP 429) for {instruments} "
            f"interval={native_interval} count={count}."
        )
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
    """Fetch many QuantHub instruments in ONE HTTP call (no chunking --
    callers that may exceed QUANTHUB_BATCH_SIZE must chunk before calling
    this; see download_history_batch below, which does), each normalized
    to the canonical OHLCV schema.
    """
    if isinstance(interval, str):
        interval = BarInterval(interval)
    native_interval = config.QUANTHUB_NATIVE_INTERVAL[interval]

    grouped = _fetch_quanthub_records(instruments, native_interval, count)
    return {
        instrument: _normalize_quanthub_records(grouped.get(instrument, []))
        for instrument in instruments
    }


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

# Maximum instruments per QuantHub HTTP request. Live-verified: a single
# request for 10 distinct instruments (interval=1H, count=48) returned
# HTTP 200 with all 480 expected records (48 per instrument); a separate
# 6-instrument request behaved identically. NOT tested above 10 -- never
# assume a larger batch is safe without separately establishing it.
QUANTHUB_BATCH_SIZE = 10


def download_history_batch(
    instruments: list[str],
    interval: str | BarInterval,
    start: DateLike,
    end: DateLike,
) -> dict[str, pd.DataFrame]:
    """Download historical OHLCV bars for MANY QuantHub instruments,
    batching up to QUANTHUB_BATCH_SIZE instruments into each HTTP request
    (live-verified request shape -- see QUANTHUB_BATCH_SIZE) instead of
    one request per instrument. This is the one place QuantHub HTTP
    fetches happen; download_history() below is now a thin single-
    instrument wrapper around this function, so there is only one
    implementation of the count-estimation/resample/date-filter/
    truncation-warning logic, not two.

    Duplicate entries in `instruments` are fetched once; the returned
    dict has one entry per unique instrument (never per input-list
    position), regardless of how many times it appeared in `instruments`.

    Args:
        instruments: QuantHub instrument identifiers, e.g. ["ERH26",
            "FSRH26"] -- each must already be built via build_instrument();
            this function does no RIC/root resolution of its own.
        interval: "DAILY", "HOURLY", or "4H" (see core.config.BarInterval).
        start: Start date (inclusive), str "YYYY-MM-DD", date, or datetime.
        end: End date (inclusive), str "YYYY-MM-DD", date, or datetime.

    Returns:
        dict mapping each unique instrument to a DataFrame with columns
        Date, Open, High, Low, Close, Volume -- empty (correct columns)
        for an instrument QuantHub returned no data for.

    Note:
        FOUR_HOUR is fetched as native 1H and resampled via
        core.utils.resample_to_4h -- the same function core.downloader
        uses for LSEG, not a second implementation. `count` is estimated
        ONCE from [start, end] (capped at QUANTHUB_MAX_REQUEST_COUNT)
        and shared across every instrument in every chunk of this batch
        -- matching the live-verified request shape, where all
        instruments in one call shared one count= value.
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

    unique_instruments = list(dict.fromkeys(instruments))  # de-dupe, preserve order
    grouped: dict[str, list[dict]] = {}
    for chunk in _chunked(unique_instruments, QUANTHUB_BATCH_SIZE):
        grouped.update(_fetch_quanthub_records(chunk, native_interval, count))

    results: dict[str, pd.DataFrame] = {}
    for instrument in unique_instruments:
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

        results[instrument] = df

    logger.info(
        "Downloaded QuantHub batch: %d unique instrument(s) in %d request(s)",
        len(unique_instruments), len(_chunked(unique_instruments, QUANTHUB_BATCH_SIZE)),
    )
    return results


def download_history(
    instrument: str,
    interval: str | BarInterval,
    start: DateLike,
    end: DateLike,
) -> pd.DataFrame:
    """Download historical OHLCV bars for a single QuantHub instrument.
    Thin wrapper around download_history_batch([instrument], ...) -- see
    that function for the actual fetch/resample/filter logic.

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
    """
    results = download_history_batch([instrument], interval, start, end)
    df = results[instrument]
    logger.info("Downloaded %d bars for QuantHub instrument %s", len(df), instrument)
    return df
