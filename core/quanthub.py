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

Live testing (see QUANTHUB_MAX_ROWS_PER_REQUEST) established QuantHub's
actual limit is on TOTAL ROWS returned per request, not on `count`
directly: 8 EURIBOR instruments x count=500 (4000 total rows) returned
HTTP 200; the same 8 instruments x count=1000 (8000 total rows) also
returned HTTP 200; x count=2000 (16000 total rows) returned HTTP 400
with body {"error": "Max row limit exceeded (10000)"}. This supersedes
an earlier, incorrect assumption (a flat count=3000 per-request cap,
based on a smaller single/few-instrument live test) that did not hold
once batched multi-instrument requests were tested -- 8 instruments x
3000 = 24,000 rows would itself now exceed the limit. The module caps
every request's `count` so that instruments_in_request x count never
exceeds QUANTHUB_MAX_ROWS_PER_REQUEST, computed freshly per request
since a batch's instrument count can vary (see download_history_batch's
per-chunk count calculation), and NEVER makes multiple requests or
paginates to compensate (no such mechanism is known to exist for this
API): a request whose true required history exceeds the cap simply
retrieves a shorter window than asked for, exactly like an instrument
whose own real history is shorter than the requested count (both are
normal, expected outcomes here, never treated as errors and never
padded/fabricated). HTTP 429 itself is raised as a distinct,
non-retried QuantHubRateLimitError -- see that class and
_fetch_quanthub_records for the retry-policy detail; the 429 finding is
independent of the 400 row-limit finding above and its handling is
unchanged by this row-limit model.

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

# QuantHub's actual per-request limit, live-verified as a TOTAL-ROW cap,
# not a per-instrument `count` cap: 8 EURIBOR instruments (ERZ26, ERU27,
# ERH27, ERU26, ERM27, ERM28, ERH28, ERZ27) x count=500 = 4000 rows ->
# HTTP 200; the same 8 x count=1000 = 8000 rows -> HTTP 200; the same 8
# x count=2000 = 16000 rows -> HTTP 400 {"error": "Max row limit
# exceeded (10000)"}. So total_rows = instruments_in_request x count
# must stay <= 10,000. This replaces an earlier, now-disproven
# assumption that `count` alone had a flat single-request cap (3000)
# independent of how many instruments were in that request -- seeing
# the actual limiting quantity is instruments x count, a flat per-
# instrument count cap would let a batched request silently exceed it
# (e.g. 8 instruments x 3000 = 24,000 rows -> HTTP 400). See
# _max_count_for_batch()/download_history_batch() for where this is
# actually applied -- always computed fresh per request, since the
# permissible `count` depends on how many instruments that specific
# request covers.
QUANTHUB_MAX_ROWS_PER_REQUEST = 10_000


def _max_count_for_batch(batch_size: int) -> int:
    """Maximum permitted `count` for a single QuantHub request covering
    `batch_size` instruments, so that instruments_in_request x count
    never exceeds the live-verified QUANTHUB_MAX_ROWS_PER_REQUEST total-
    row limit (see that constant's own docstring). Integer floor
    division is intentional -- rounding up would risk exceeding the
    limit; a `count` slightly below what free row budget allows is
    always safe, never an error.
    """
    return QUANTHUB_MAX_ROWS_PER_REQUEST // batch_size


def _estimate_count(native_interval: str, start: datetime, end: datetime) -> int:
    """Estimate a QuantHub `count` generous enough to cover [start, end].

    Deliberately NOT capped here -- the permissible per-request `count`
    depends on how many instruments share that specific request (see
    QUANTHUB_MAX_ROWS_PER_REQUEST / _max_count_for_batch()), which this
    function has no visibility into; callers (download_history_batch)
    apply min(this estimate, _max_count_for_batch(batch_size)) once the
    actual batch is known.

    CONFIRMED LIMITATION (live-tested against the real API, a controlled
    parameter-by-parameter investigation -- no longer an open question):
    `instruments=`, `interval=`, and `count=` are the only request
    parameters that have any effect. `start=`/`end=` returns HTTP 500.
    `from=`/`to=` returns HTTP 200 but is silently ignored -- the
    response is byte-identical to the same request without it.
    `offset=`, `page=`, `cursor=`, and `before=` were each tested in
    isolation against a fixed baseline and every one returned HTTP 200
    with the exact same window as the baseline -- silently ignored, not
    applied. No pagination, cursor, offset, or timestamp/date-range
    mechanism is available through /api/v2/ohlc/ at all. `count=` means
    "the most recent N observations as of when the request is made" --
    there is no way to anchor a request to an earlier reference point,
    so a request whose true required count would exceed the effective
    per-batch cap simply retrieves a shorter history than the requested
    [start, end] window, never multiple requests, never fabricated bars,
    and this cannot be worked around client-side: older history is
    genuinely unreachable in a single request beyond that cap (see
    download_history_batch()'s own docstring for what this means for a
    cold-started instrument). This heuristic is deliberately over-
    generous under whatever cap ends up applying. _fetch_quanthub_
    records() logs a warning whenever the returned data does not reach
    back to `start` despite the full (possibly capped) count being
    consumed, so a too-small effective count fails loudly (a gap in
    cached history, visible in logs) rather than silently.
    """
    calendar_days = max((end.date() - start.date()).days + 1, 1)
    if native_interval == "1D":
        return calendar_days + _DAILY_COUNT_BUFFER
    elif native_interval == "1H":
        return calendar_days * _HOURLY_BARS_PER_DAY + _HOURLY_BARS_PER_DAY
    else:
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
        ONCE from [start, end] (see _estimate_count), then capped PER
        CHUNK via _max_count_for_batch(len(chunk)) -- QuantHub's limit is
        on TOTAL ROWS per request (instruments_in_request x count <=
        QUANTHUB_MAX_ROWS_PER_REQUEST, live-verified), not a flat count
        cap independent of batch size, so a smaller trailing chunk (e.g.
        the 1-instrument remainder of a 21-instrument batch) legitimately
        gets a HIGHER count than a full QUANTHUB_BATCH_SIZE-sized chunk.

    CONFIRMED PERMANENT API CONSTRAINT (live-tested, a controlled
    parameter-by-parameter investigation -- see _estimate_count()'s own
    docstring for the full evidence): /api/v2/ohlc/ has no pagination,
    cursor, offset, or date-range mechanism of any kind, and the total
    row cap is a HARD, exactly-enforced 10,000 rows per HTTP request
    (10,000 succeeds, 10,001 returns HTTP 400 "Max row limit exceeded
    (10000)", live-confirmed at both a 1-instrument and an 8-instrument
    batch size). Because that cap is shared across every instrument in
    one request, the effective per-instrument count this function can
    ever request is QUANTHUB_MAX_ROWS_PER_REQUEST // len(chunk) --
    batching more instruments into one request (see QUANTHUB_BATCH_SIZE)
    directly shrinks how far back each individual instrument can reach.
    This is a genuine, permanent tradeoff between fewer HTTP requests
    (larger batches) and deeper reach for an instrument queried for the
    first time (smaller batches) -- there is no client-side workaround,
    since no parameter exists to request a window anchored anywhere
    other than "now".

    A COLD-STARTED instrument (never previously cached) can therefore
    only ever receive, on its very first fetch, the most recent history
    reachable within that request's effective count cap -- nothing
    older is retrievable through this endpoint, ever, no matter how the
    request is shaped. This does NOT mean deep history is permanently
    unreachable for an actively-scanned instrument, though: database.
    service's SQLite cache (Module 2) persists every completed bar this
    function returns and never re-fetches what it already has, so an
    instrument that gets scanned repeatedly over time accumulates
    history day by day as "now" (and therefore QuantHub's own reachable
    window) advances -- this is the existing, unmodified caching
    behavior already doing the only thing that can compensate for this
    API-side ceiling; it does not change the ceiling itself for a
    genuinely new instrument's first request.
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
    estimated_count = _estimate_count(native_interval, start_dt, end_dt)

    unique_instruments = list(dict.fromkeys(instruments))  # de-dupe, preserve order
    grouped: dict[str, list[dict]] = {}
    count_by_instrument: dict[str, int] = {}
    for chunk in _chunked(unique_instruments, QUANTHUB_BATCH_SIZE):
        count = min(estimated_count, _max_count_for_batch(len(chunk)))
        grouped.update(_fetch_quanthub_records(chunk, native_interval, count))
        for instrument in chunk:
            count_by_instrument[instrument] = count

    results: dict[str, pd.DataFrame] = {}
    for instrument in unique_instruments:
        raw_records = grouped.get(instrument, [])
        df = _normalize_quanthub_records(raw_records)
        count = count_by_instrument[instrument]

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
