"""
quanthub.py

Responsible for ONE thing: getting clean OHLCV bars out of QuantHub
(the in-house secondary market-data provider, GET
https://qh-api.corp.hertshtengroup.com/apis/ohlc/) for a given QH
instrument identifier / interval / date range.

BACKEND MIGRATION (/api/v2/ohlc/ -> /apis/ohlc/): QuantHub retired its
old /api/ backend. The old OHLC path now returns HTTP 403 Forbidden for
every instrument, at every interval and count -- reproduced against
CRAM28, CRAU28 and SONU28, all of which had previously returned real
data through this same code path, which is what identified the cause as
a retired endpoint rather than an instrument entitlement, a rate limit,
or an Oscill8 defect. Only the URL changed here: the request shape
(instruments=/interval=/count=), the Bearer header, the response record
shape, batching, count estimation, interval mapping, normalization and
the retry policy are all unchanged and were re-verified against the new
endpoint (see the response-shape note on _fetch_quanthub_records).

AUTHENTICATION IS MANUAL AND STAYS THAT WAY. The new backend documents
a /apis/auth/ endpoint, but Oscill8 NEVER calls it and has no auth
client, no Microsoft sign-in, no token acquisition, and no refresh. The
operator signs in through QuantHub's own web auth page, copies the
resulting access_token into RBS_QUANTHUB_TOKEN, and this module sends
it verbatim as `Authorization: Bearer <token>` (see _auth_headers) --
exactly as it did before the migration. An expired token surfaces as
whatever HTTP status QuantHub returns for one; nothing here detects,
classifies, or renews it.

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

REQUEST SHAPES: `count`, or `start`+`end`. Two mutually exclusive ways
to ask for history, both live-verified against the MIGRATED /apis/ohlc/
backend:

    count=N                 the most recent N observations AS OF NOW.
                            No way to anchor to an earlier reference
                            point. The original, long-standing shape.

    start=S&end=E           a genuine date range, both as UNIX SECONDS
                            (see _to_unix_seconds). `count` MUST be
                            omitted: QuantHub rejects all three together
                            with {"error": "Only two of start or end or
                            count should be provided"}.

The start/end shape SUPERSEDES an earlier, now-disproven statement in
this docstring that QuantHub does not support date ranges at all ("a
start=/end= request returned HTTP 500"). That finding was established
against the RETIRED /api/v2/ohlc/ backend and carried over unverified
through the migration. Re-tested against the live migrated endpoint by
the standalone benchmark in tools/qh_stress_test.py, which established
all three of the conditions above -- in particular that the HTTP 500 is
a TIMESTAMP FORMAT failure, not a missing capability: unix MILLIseconds
silently return zero rows, while "YYYY-MM-DD" and ISO-8601 strings both
return HTTP 500. Only unix SECONDS work. Do not change the encoding in
_to_unix_seconds() without re-establishing this against the live API.

Which shape a caller gets is an explicit choice, never inferred:
download_history()/download_history_batch() default to the count shape
(unchanged behaviour for every existing caller) and switch to the date
range only when passed use_date_range=True. Wiring that flag into the
cache's own missing-range logic (database.service._missing_ranges) is
deliberately NOT done here -- it is the next task in a staged migration.

Why the date range matters: `count` can only ever mean "N bars ending
now", so topping up a cache that is one day stale means re-downloading
the entire window. Measured on SR3 hourly with a one-day gap: the count
shape downloaded 2,989 rows to keep 34 new ones; the date-range shape
downloaded 36 to keep the same 34 -- a 98.8% reduction. See
_estimate_count()'s docstring for the limitation the count shape
carries, which the date-range shape removes.

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
per-chunk count calculation). In the COUNT shape that cap is the whole
story: a request whose true required history exceeds it simply
retrieves a shorter window than asked for, exactly like an instrument
whose own real history is shorter than the requested count (both are
normal, expected outcomes here, never treated as errors and never
padded/fabricated) -- the count shape never issues multiple requests to
compensate, because `count=` cannot be anchored anywhere but "now".

The DATE-RANGE shape does split, precisely because it can: an
oversized [start, end] is chunked by day across several requests and
recombined (see the automatic-chunking section further down this
module). That is a property of the date-range shape only; nothing about
the count shape changed.

HTTP 429 is raised as a distinct QuantHubRateLimitError and IS retried,
honouring the response's own Retry-After -- see that class and
_quanthub_wait for the retry-policy detail. (An earlier version of this
docstring described 429 as non-retried, which was accurate before the
rate-limit hardening.) The 429 finding is independent of the 400
row-limit finding above.

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

import math
import threading
# Aliased because `time` in this module already means datetime.time (see
# the datetime import below, used for whole-day range bounds). Only the
# monotonic clock and sleep are needed from the stdlib module.
import time as _time
from datetime import date, datetime, time, timedelta

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


# The retired QuantHub OHLC path (see the backend-migration note in this
# module's docstring). Kept ONLY to recognise a stale configured URL --
# never used to build a request, and never rewritten on the operator's
# behalf: RBS_QUANTHUB_BASE_URL is their setting, and silently
# "correcting" it would hide a real configuration problem rather than
# surface it.
RETIRED_QUANTHUB_OHLC_PATH = "/api/v2/ohlc/"


def _warn_if_retired_endpoint_configured() -> None:
    """Log ONCE at import if RBS_QUANTHUB_BASE_URL still points at the
    retired /api/v2/ohlc/ backend.

    Why this exists: the migrated default in core.config only applies
    when RBS_QUANTHUB_BASE_URL is UNSET. An existing .env that pins the
    old URL shadows it completely, so the app would keep calling the
    retired endpoint and keep getting HTTP 403 -- with no signal
    distinguishing that from a genuine provider problem. This turns
    that silent, easily-misdiagnosed configuration state into one
    obvious log line. It changes no request and blocks nothing.
    """
    if RETIRED_QUANTHUB_OHLC_PATH in (config.QUANTHUB_BASE_URL or ""):
        logger.warning(
            "RBS_QUANTHUB_BASE_URL points at the RETIRED QuantHub backend "
            "(%s). That endpoint returns HTTP 403 for every request. Update "
            "the variable in your .env to the migrated endpoint (.../apis/ohlc/), "
            "or remove it entirely to use core.config's own migrated default.",
            RETIRED_QUANTHUB_OHLC_PATH,
        )


_warn_if_retired_endpoint_configured()


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
    """Raised when QuantHub returns HTTP 429 (rate limited).

    NOW RETRIED, with a cooldown honoured from the response. An earlier
    version of this class documented 429 as deliberately NOT retried,
    on the grounds that no Retry-After or other cooldown signal had been
    observed and that blind exponential retry could compound the
    condition. The cause is now established: QuantHub enforces roughly
    30 requests per minute and says so in the 429 body --

        {"error": "Rate limit exceeded",
         "detail": "Too many requests. Retry after 3 second(s)",
         "limit": 30, "window": "minute"}

    -- so the retry is no longer blind. _quanthub_wait() waits for the
    response's own Retry-After when it carries one, and a bounded
    fallback otherwise (see _RATE_LIMIT_FALLBACK_WAIT), within the same
    3-attempt budget every other retryable failure already uses. The
    primary defence is still not retrying at all: _RATE_LIMITER paces
    outbound requests below the server's limit, and the SQLite cache
    (database.service) keeps most requests from being made in the first
    place.

    Carries what a caller or a log needs to understand the failure:
        retry_after     seconds from the Retry-After header, or None
        response_body   short excerpt of the 429 body, or None

    Distinct from QuantHubRequestError (a deterministic 400, never
    retried), from QuantHubCredentialsMissingError (a configuration
    problem), and from core.downloader.MarketDataUnavailableError
    (LSEG's own narrow "no market data for this RIC" classification,
    unrelated).
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.response_body = response_body


class QuantHubRequestError(requests.exceptions.HTTPError):
    """Raised for a DETERMINISTIC QuantHub HTTP 400 -- never retried.

    A 400 means QuantHub rejected the request as malformed or
    out-of-bounds: an unparseable parameter, an invalid request shape,
    or the hard row ceiling ({"error": "Max row limit exceeded
    (10000)"}). Re-sending the identical request cannot change the
    answer, so the previous behaviour -- falling through
    raise_for_status() into the generic retry policy and spending three
    attempts plus ~6 seconds of backoff on a guaranteed failure -- was
    pure waste, and against a rate-limited API it also burned three of
    the minute's request budget.

    SUBCLASSES requests.exceptions.HTTPError deliberately, so this is a
    tightening of behaviour rather than a change of contract: existing
    callers and tests that catch, or assert on, requests HTTPError keep
    working unchanged, and `.response` is populated as usual. Only the
    number of attempts changes.

    Carries:
        status_code     always 400
        response_body   short excerpt of the body, included in str()
    """

    def __init__(
        self,
        message: str,
        *,
        response=None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message, response=response)
        self.status_code = 400
        self.response_body = response_body


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
# Count estimation: sizes the COUNT request shape, and doubles as the
# row-density model used to size DATE-RANGE chunks (see
# _estimated_rows_for_day_span / _max_days_per_chunk below).
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

    LIMITATION OF THE COUNT SHAPE -- and what now lifts it.

    `count=` means "the most recent N observations as of when the
    request is made". There is no way to anchor a COUNT request to an
    earlier reference point, so a request whose true required count
    would exceed the effective per-batch cap simply retrieves a shorter
    history than the requested [start, end] window -- never multiple
    requests, never fabricated bars. download_history_batch() logs a
    warning whenever the returned data does not reach back to `start`
    despite the full (possibly capped) count being consumed, so a
    too-small effective count fails loudly rather than silently. This
    heuristic is deliberately over-generous under whatever cap applies.

    That limitation is INHERENT TO THE COUNT SHAPE, not to the endpoint.
    It no longer applies to a request sent with use_date_range=True: an
    explicit start/end window reaches directly into the past, so the
    cold-start ceiling and the "count was insufficient" warning are both
    count-shape concerns only. See this module's docstring.

    SUPERSEDED FINDING, kept because it explains why this function
    exists at all. A parameter-by-parameter investigation against the
    RETIRED /api/v2/ohlc/ backend concluded that `instruments=`,
    `interval=` and `count=` were the only parameters with any effect:
    `start=`/`end=` returned HTTP 500; `from=`/`to=` returned HTTP 200
    but was silently ignored; `offset=`, `page=`, `cursor=` and
    `before=` were each tested in isolation and every one returned the
    same window as a baseline request. Those results were carried across
    the backend migration without re-testing. Re-testing against the
    live MIGRATED endpoint (tools/qh_stress_test.py) found `start`/`end`
    DO work there, in unix seconds and without `count` -- the HTTP 500
    was a timestamp-format rejection, not an unsupported parameter. The
    remaining parameters above have not been re-tested and no claim is
    made about them; pagination/cursor/offset are still not used by
    anything in this module.
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

# --------------------------------------------------------------------------
# Client-side request rate limiting
# --------------------------------------------------------------------------

# QuantHub's own request-rate limit, live-measured. A benchmark run that
# paced itself too fast was rejected with HTTP 429 and this body:
#   {"error": "Rate limit exceeded",
#    "detail": "Too many requests. Retry after 3 second(s)",
#    "limit": 30, "window": "minute"}
# Recorded here for provenance only -- never used to build a request,
# and never used as the client's own target (see below).
OBSERVED_QUANTHUB_RATE_LIMIT_PER_MINUTE = 30

# What Oscill8 holds ITSELF to: deliberately under the observed server
# limit, so ordinary jitter, a retry, or a second Streamlit session
# cannot tip a normal scan over the edge. 25/minute == one request every
# 2.4 seconds. The same benchmark sustained ~26/minute across 66
# requests with zero 429s.
#
# A plain module constant, matching QUANTHUB_BATCH_SIZE and
# QUANTHUB_MAX_ROWS_PER_REQUEST, which are the other live-measured
# QuantHub limits and also live here rather than in core.config. No
# environment variable: this is a property of the provider, not of a
# deployment, and nothing has asked to vary it per machine.
QUANTHUB_REQUESTS_PER_MINUTE = 25

# Upper bound on how long a single 429 cooldown may block for, whether
# that number came from a Retry-After header or the fallback below. A
# provider bug, a typo'd header, or an HTTP-date misread as a huge
# number must never be able to hang a scan for hours.
QUANTHUB_MAX_RETRY_AFTER_WAIT_SECONDS = 60.0


class QuantHubRateLimiter:
    """Minimum-interval limiter for outbound QuantHub HTTP requests.

    Deliberately the simplest thing that satisfies the requirement: keep
    consecutive requests at least 60/requests_per_minute seconds apart.
    No token bucket, no burst allowance, no distributed coordination --
    QuantHub's limit is a simple per-minute count against a single
    process, and a burst allowance would only make it easier to trip.

    THREAD SAFETY. No production code path issues QuantHub requests
    concurrently today (nothing under core/, database/, strategy_engine/,
    template_scanner/ or ui/ uses threads, asyncio or a pool, and this
    task deliberately did not add any). But Streamlit runs each browser
    session's script in its own thread inside ONE process, so two
    sessions scanning at once would call straight through here in
    parallel. The lock makes that safe, and -- more importantly -- makes
    it impossible for concurrent callers to COLLECTIVELY exceed the rate:
    each caller reserves the next slot under the lock and only then
    sleeps, so N threads take N distinct, properly-spaced slots rather
    than all reading the same "last request" timestamp and racing.

    The sleep happens OUTSIDE the lock on purpose. Holding it while
    sleeping would serialise slot reservation behind the sleep, which
    changes nothing about the achieved rate but needlessly blocks other
    threads from computing their own slot.

    `monotonic`/`sleep` are injectable so tests can drive the limiter
    with a fake clock and assert exact waits without real delays. The
    module-level singleton uses the real ones.
    """

    def __init__(
        self,
        requests_per_minute: int = QUANTHUB_REQUESTS_PER_MINUTE,
        *,
        monotonic=None,
        sleep=None,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError(
                f"requests_per_minute must be positive, got {requests_per_minute!r}"
            )
        self.requests_per_minute = requests_per_minute
        self.min_interval_s = 60.0 / requests_per_minute
        self._monotonic = monotonic or _time.monotonic
        self._sleep = sleep or _time.sleep
        self._lock = threading.Lock()
        self._next_allowed_at: float | None = None

    def acquire(self) -> float:
        """Block until this caller may send a request. Returns the
        seconds actually waited (0.0 when no wait was needed).

        The FIRST request through a fresh limiter never waits -- there is
        no previous request to be spaced from, and delaying it would add
        latency to every cold start for no benefit.
        """
        with self._lock:
            now = self._monotonic()
            if self._next_allowed_at is None or now >= self._next_allowed_at:
                wait = 0.0
                slot = now
            else:
                wait = self._next_allowed_at - now
                slot = self._next_allowed_at
            self._next_allowed_at = slot + self.min_interval_s

        if wait > 0:
            self._sleep(wait)
        return wait

    def reset(self) -> None:
        """Forget the last slot, so the next acquire() proceeds without
        waiting. Exists for tests and for an operator-driven restart of
        pacing; nothing in the request path calls it."""
        with self._lock:
            self._next_allowed_at = None


# The one limiter every QuantHub request in this process passes through.
# Module-level rather than per-call so that pacing spans a whole scan,
# not a single batch.
_RATE_LIMITER = QuantHubRateLimiter()


def _parse_retry_after(value) -> float | None:
    """Parse a Retry-After header into seconds, or None if unusable.

    RFC 9110 permits either delay-seconds or an HTTP-date. Only the
    delay-seconds form is parsed here, because that is the form QuantHub
    has actually been observed to communicate ("Retry after 3
    second(s)"); an HTTP-date, an empty value, or anything else returns
    None and the caller falls back to its own bounded backoff. Returning
    None rather than guessing is the point: a misparsed date silently
    becomes an absurd delay, which is exactly the failure mode
    QUANTHUB_MAX_RETRY_AFTER_WAIT_SECONDS also guards.

    Negative and non-finite values are rejected for the same reason.
    """
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _response_body_excerpt(response, limit: int = 300) -> str | None:
    """A short, whitespace-collapsed excerpt of a response body, for
    error messages and logs.

    Reads only the RESPONSE -- request headers, which carry the Bearer
    token, are never touched, so an error message can never leak a
    credential. Any failure to read the body returns None rather than
    masking the original error with a new one.
    """
    try:
        body = response.text
    except Exception:  # noqa: BLE001 -- diagnostics must never raise
        return None
    if not isinstance(body, str) or not body:
        return None
    return " ".join(body.split())[:limit]


def _to_unix_seconds(value: DateLike) -> int:
    """Encode a timestamp the way QuantHub's start/end parameters need
    it: UNIX SECONDS. THE ONE PLACE this conversion happens.

    Format matters and is not negotiable (live-verified against the
    migrated /apis/ohlc/ backend -- see this module's docstring):
    milliseconds silently return zero rows, "YYYY-MM-DD" and ISO-8601
    strings both return HTTP 500. A silent-zero-rows failure mode is
    exactly why this is centralized rather than formatted at each call
    site.

    TIMEZONE CONVENTION -- follows the one already established across
    Oscill8, rather than introducing a new one. Every timestamp in this
    pipeline is naive UTC: _normalize_quanthub_records() decodes
    QuantHub's own `time` field with pd.to_datetime(..., unit="ms"),
    which yields naive UTC, and database.service works in
    datetime.utcnow() throughout. So:

        naive value      -> interpreted as UTC (never as local time)
        tz-aware value   -> converted to UTC

    Reading a naive value as local time would make the outgoing request
    depend on the machine's timezone, which is precisely the bug
    tests/test_quanthub.py's own _ms() helper documents avoiding.

    Accepts anything pandas can turn into a Timestamp -- str, date,
    datetime, pd.Timestamp -- which covers core.utils.DateLike and the
    date objects download_history_batch() already works in.

    Sub-second precision floors to the whole second, the finest
    granularity the parameter carries. Irrelevant for the DAILY/HOURLY/
    4H bars this module fetches, all of which land on minute
    boundaries at worst.
    """
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    return int(ts.value // 10**9)


def _build_request_params(
    instruments: list[str],
    native_interval: str,
    count: int | None,
    start: DateLike | None,
    end: DateLike | None,
) -> dict:
    """Build the outgoing query parameters for one QuantHub request, and
    validate the shape BEFORE any HTTP call is made.

    Exactly one of the two supported shapes is produced (see this
    module's docstring):

        count only      -> {instruments, interval, count}
        start + end     -> {instruments, interval, start, end}

    `count` is omitted from a date-range request deliberately, not
    incidentally: QuantHub rejects a request carrying all three with
    {"error": "Only two of start or end or count should be provided"}.
    Catching that here turns a wasted round trip into an immediate,
    explanatory ValueError.

    Deliberately rejected rather than guessed at:

      - start WITHOUT end, or end WITHOUT start. QuantHub does accept
        `end`+`count` (live-verified: N bars anchored backwards from a
        past point), but Oscill8 has no use for it today and inventing
        semantics for a shape nothing calls would mean shipping
        untested behaviour. A future task that needs it should add it
        with its own evidence.
      - neither count nor a date range, which has no meaning at all.

    Raises:
        ValueError: for any unsupported combination, always before the
            request is sent.
    """
    if (start is None) != (end is None):
        raise ValueError(
            "QuantHub date-range requests need BOTH start and end "
            f"(got start={start!r}, end={end!r}). A start-only or end-only "
            "request is not a shape Oscill8 uses; pass count= instead, or "
            "supply both bounds."
        )

    has_range = start is not None and end is not None
    if has_range and count is not None:
        raise ValueError(
            "QuantHub rejects a request carrying start, end AND count "
            '("Only two of start or end or count should be provided"). '
            f"Got start={start!r}, end={end!r}, count={count!r} -- pass a "
            "date range OR a count, never both."
        )
    if not has_range and count is None:
        raise ValueError(
            "A QuantHub request needs either count= or both start= and end=; "
            "neither was supplied."
        )

    params: dict = {
        "instruments": ",".join(instruments),
        "interval": native_interval,
    }
    if has_range:
        params["start"] = _to_unix_seconds(start)
        params["end"] = _to_unix_seconds(end)
    else:
        params["count"] = count
    return params


def _auth_headers() -> dict:
    if not config.QUANTHUB_TOKEN:
        raise QuantHubCredentialsMissingError(
            "RBS_QUANTHUB_TOKEN is not set -- cannot call QuantHub. Markets "
            "routed to LSEG are unaffected; this only blocks QuantHub-routed "
            "markets (see core.providers.PROVIDER_ROUTING)."
        )
    return {"Authorization": f"Bearer {config.QUANTHUB_TOKEN}"}


# The transient-failure backoff, UNCHANGED from before rate-limit
# handling existed: 5xx, connection errors and timeouts still wait
# 2s, 4s, ... capped at 10s, across the same 3 attempts. Kept as its own
# named object so _quanthub_wait can delegate to the identical policy
# rather than restate it.
_TRANSIENT_WAIT = wait_exponential(multiplier=1, min=2, max=10)

# Backoff for a 429 whose response carried no usable Retry-After. Starts
# well above the transient backoff and is bounded: QuantHub's limit is
# measured per MINUTE, so retrying a rate-limited request 2 seconds
# later is very likely to be rejected again, while waiting minutes would
# be worse than failing. 5s then 10s within the 3-attempt budget.
_RATE_LIMIT_FALLBACK_WAIT = wait_exponential(multiplier=5, min=5, max=30)


def _quanthub_wait(retry_state) -> float:
    """How long to wait before the next attempt.

    Routes by the failure that actually occurred, so that adding
    rate-limit handling changed nothing about transient failures:

        429 with a usable Retry-After -> that value, clamped to
            QUANTHUB_MAX_RETRY_AFTER_WAIT_SECONDS
        429 without one              -> _RATE_LIMIT_FALLBACK_WAIT
        anything else                -> _TRANSIENT_WAIT, the original
                                        policy, byte for byte

    A separate tenacity `retry` policy per exception type is not
    available in one decorator, so the branch lives here; the attempt
    budget (3) stays single and shared, which is what keeps a 429 from
    multiplying the total request count.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None

    if isinstance(exc, QuantHubRateLimitError):
        if exc.retry_after is not None:
            wait = min(exc.retry_after, QUANTHUB_MAX_RETRY_AFTER_WAIT_SECONDS)
            logger.warning(
                "QuantHub rate-limited (HTTP 429); honouring Retry-After: waiting %.1fs "
                "before attempt %d of %d.",
                wait, retry_state.attempt_number + 1, _MAX_ATTEMPTS,
            )
            return wait
        wait = min(
            _RATE_LIMIT_FALLBACK_WAIT(retry_state), QUANTHUB_MAX_RETRY_AFTER_WAIT_SECONDS
        )
        logger.warning(
            "QuantHub rate-limited (HTTP 429) with no usable Retry-After; backing off "
            "%.1fs before attempt %d of %d.",
            wait, retry_state.attempt_number + 1, _MAX_ATTEMPTS,
        )
        return wait

    return _TRANSIENT_WAIT(retry_state)


# Total attempts per logical request, shared by every retryable failure.
# Unchanged from the original policy.
_MAX_ATTEMPTS = 3


@retry(
    reraise=True,
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    wait=_quanthub_wait,
    # Two exception types are excluded from retry, both because retrying
    # them CANNOT change the outcome:
    #
    #   QuantHubRequestError -- a deterministic HTTP 400 (malformed
    #     parameters, invalid shape, or the 10,000-row ceiling). Re-
    #     sending the identical request gets the identical rejection.
    #     Previously these fell through raise_for_status() as a plain
    #     HTTPError and consumed all 3 attempts plus ~6s of backoff for
    #     nothing -- and, against a rate-limited API, 3 of the minute's
    #     request budget. It subclasses requests HTTPError, so callers
    #     catching that keep working; only the attempt count changed.
    #
    #   ValueError -- _build_request_params()'s request-shape validation,
    #     which raises before any HTTP call is made.
    #
    # QuantHubRateLimitError (429) IS retried, unlike before: the wait is
    # now driven by the response's own Retry-After (see _quanthub_wait),
    # so it is a directed cooldown rather than the blind exponential
    # backoff that exclusion originally guarded against.
    #
    # EVERYTHING ELSE keeps the existing behaviour exactly: 5xx,
    # connection errors, timeouts and any unrecognised exception still
    # retry up to 3 attempts on _TRANSIENT_WAIT -- mirrors
    # core.downloader._fetch_chunk's own narrow-exclusion pattern.
    retry=(
        retry_if_exception_type(Exception)
        & retry_if_not_exception_type((QuantHubRequestError, ValueError))
    ),
)
def _fetch_quanthub_records(
    instruments: list[str],
    native_interval: str,
    count: int | None = None,
    *,
    start: DateLike | None = None,
    end: DateLike | None = None,
) -> dict[str, list[dict]]:
    """One HTTP call, potentially covering many instruments (QuantHub's
    `instruments=` parameter accepts a comma-separated list -- confirmed
    live: a 5-instrument batched request returned 5 records per
    instrument in one response).

    Supports both request shapes (see this module's docstring). `count`
    stays the third POSITIONAL parameter so every existing call site --
    _fetch_quanthub_records(chunk, "1D", 5) -- is untouched; start/end
    are keyword-only, so a date-range request always reads explicitly at
    the call site. _build_request_params() decides and validates which
    shape is being sent, before the request goes out.

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
    params = _build_request_params(instruments, native_interval, count, start, end)
    headers = _auth_headers()
    logger.debug(
        "Fetching QuantHub %s interval=%s %s",
        instruments,
        native_interval,
        f"start={params['start']} end={params['end']} (unix seconds)"
        if "start" in params
        else f"count={params['count']}",
    )

    # Pace AFTER parameter validation and credential lookup, so a request
    # that was never going to be sent does not consume a rate-limit slot
    # (or make a failing unit test sleep). Every retry attempt re-enters
    # this function and therefore takes its own slot -- a retry is a real
    # HTTP request and must count against the budget.
    _RATE_LIMITER.acquire()

    response = requests.get(config.QUANTHUB_BASE_URL, headers=headers, params=params, timeout=30)

    if response.status_code == 429:
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        body = _response_body_excerpt(response)
        raise QuantHubRateLimitError(
            f"QuantHub rate-limited this request (HTTP 429) for {instruments} "
            f"interval={native_interval} count={count}."
            + (f" Retry-After: {retry_after}s." if retry_after is not None else "")
            + (f" Response: {body}" if body else ""),
            retry_after=retry_after,
            response_body=body,
        )

    if response.status_code == 400:
        # Deterministic request rejection -- see QuantHubRequestError.
        # Classified BEFORE raise_for_status() so it becomes the
        # non-retryable subclass rather than a generic, retried HTTPError.
        # The body is preserved because it is the only place QuantHub
        # says WHICH validation failed (e.g. "Max row limit exceeded
        # (10000)" vs "Only two of start or end or count should be
        # provided") -- never swallowed, never turned into an empty frame.
        body = _response_body_excerpt(response)
        raise QuantHubRequestError(
            f"QuantHub rejected this request (HTTP 400) for {instruments} "
            f"interval={native_interval} params={sorted(params)}."
            + (f" Response: {body}" if body else ""),
            response=response,
            response_body=body,
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


# --------------------------------------------------------------------------
# Automatic date-range chunking for the 10,000-row response ceiling
#
# TWO INDEPENDENT BATCHING DIMENSIONS, never to be confused:
#
#   instruments per request  -- QUANTHUB_BATCH_SIZE above, unchanged.
#   days per request         -- everything below.
#
# The row ceiling is on the TOTAL response, shared across every
# instrument in it (see QUANTHUB_MAX_ROWS_PER_REQUEST), so the two
# dimensions multiply: 10 instruments x 2,000 bars is 20,000 rows and is
# rejected, even though 2,000 bars for one instrument is fine. Day
# chunking therefore always reasons about instruments x bars, never
# about one instrument in isolation.
#
# STRATEGY: proactive sizing, with a reactive safety net.
#
#   1. Size chunks up front from _estimate_count -- the SAME estimator
#      the count request shape already uses, so there is one density
#      model in this module, not two. It is documented there as a
#      deliberately generous UPPER bound (24 bars/calendar-day for
#      hourly; calendar days for daily), which is exactly the property
#      proactive sizing needs: erring high produces slightly smaller
#      chunks, never an oversized request.
#
#   2. If a request is rejected anyway with the specific row-limit 400,
#      halve that range and retry the halves, recursively.
#
# Why both. Pure reaction (send it, split on rejection) needs no density
# model, but every rejection is a wasted request AND a wasted
# rate-limiter slot -- scarce at 25/minute. Halving a 6-month 10-
# instrument hourly range reactively costs 1 + 2 + 4 = 7 requests where
# proactive sizing costs 5. Pure proaction is cheap but trusts the
# estimate absolutely, and a future instrument or interval with
# unexpected density would simply fail. Together: the estimate avoids
# essentially all rejections, and the reactive path means an estimate
# that is ever wrong degrades into extra requests rather than an error.
#
# Deliberately NOT a hard-coded "30-day chunk": chunk size is derived
# from the real constraint (rows) and the real request shape (how many
# instruments are in THIS request), so it adapts to interval and batch
# size without anyone maintaining a table of magic spans.

# The live-confirmed marker in QuantHub's row-limit rejection body:
#   {"error": "Max row limit exceeded (10000)"}
# Matched case-insensitively as a substring. Deliberately narrow -- this
# is the ONLY 400 that may trigger splitting. Every other 400 (malformed
# parameters, an invalid request shape, an unknown instrument) is
# deterministic in a way that a smaller date range cannot fix, and
# splitting on it would turn one clear error into a burst of identical
# failures.
QUANTHUB_ROW_LIMIT_ERROR_MARKER = "max row limit exceeded"

# Maximum recursive halvings of a single proactively-sized chunk.
#
# Chunks handed to the reactive path have already been sized to fit the
# estimate, so a rejection means the estimate was wrong for that data --
# a correction of at most a few halvings in any realistic case. 10
# allows a 1,024-fold correction (e.g. a 365-day chunk down to a single
# day) before giving up, which is far past the point where the real
# explanation is a changed API rather than dense data. Bounded so that a
# provider that rejects everything produces a clear, finite failure
# instead of an exponential request storm.
_MAX_CHUNK_SPLIT_DEPTH = 10


def _is_row_limit_error(exc: QuantHubRequestError) -> bool:
    """True only for QuantHub's row-ceiling rejection.

    Checks the response body first (where QuantHub actually states the
    reason) and falls back to the exception message, which embeds that
    same body. Never matches on the status code alone: a 400 is not by
    itself a row-limit condition.
    """
    haystack = f"{exc.response_body or ''} {exc}".lower()
    return QUANTHUB_ROW_LIMIT_ERROR_MARKER in haystack


def _estimated_rows_for_day_span(
    native_interval: str, days: int, instrument_count: int
) -> int:
    """Estimated TOTAL response rows for `days` calendar days across
    `instrument_count` instruments in one request.

    Delegates the per-instrument density to _estimate_count rather than
    restating its formula, so the count and date-range shapes can never
    drift apart. The anchor date is arbitrary -- _estimate_count depends
    only on the span's length, not on where it falls.
    """
    anchor = datetime(2000, 1, 1)
    per_instrument = _estimate_count(
        native_interval, anchor, anchor + timedelta(days=max(days, 1) - 1)
    )
    return per_instrument * max(instrument_count, 1)


def _max_days_per_chunk(
    native_interval: str, instrument_count: int, span_days: int
) -> int:
    """Largest number of calendar days whose estimated rows stay within
    QUANTHUB_MAX_ROWS_PER_REQUEST, capped at `span_days`.

    Binary search rather than algebraic inversion of _estimate_count:
    the estimator's shape (a per-day rate plus a constant buffer) is its
    own business, and inverting it here would duplicate -- and could
    silently diverge from -- that formula. It is monotonic in days,
    which is all a search needs.

    Returns at least 1. A single day that still exceeds the ceiling is
    not treated as an error here: the request is attempted, and the
    reactive path reports the real provider rejection rather than this
    function guessing that one would occur.
    """
    if span_days <= 1:
        return 1
    if _estimated_rows_for_day_span(native_interval, span_days, instrument_count) <= (
        QUANTHUB_MAX_ROWS_PER_REQUEST
    ):
        return span_days

    low, high = 1, span_days
    while low < high:
        mid = (low + high + 1) // 2
        if _estimated_rows_for_day_span(native_interval, mid, instrument_count) <= (
            QUANTHUB_MAX_ROWS_PER_REQUEST
        ):
            low = mid
        else:
            high = mid - 1
    return low


def _day_chunks(start_d: date, end_d: date, days_per_chunk: int) -> list[tuple[date, date]]:
    """Split [start_d, end_d] into consecutive, NON-OVERLAPPING day
    ranges of at most `days_per_chunk` days each, both bounds inclusive.

    Every day in the requested span appears in exactly one chunk: each
    chunk ends on a whole day and the next begins on the following day,
    so no day is skipped and none is fetched twice. Whole-day bounds also
    keep 4H bucketing safe -- see download_history_batch.
    """
    if days_per_chunk < 1:
        raise ValueError(f"days_per_chunk must be >= 1, got {days_per_chunk}")

    chunks: list[tuple[date, date]] = []
    cursor = start_d
    while cursor <= end_d:
        chunk_end = min(cursor + timedelta(days=days_per_chunk - 1), end_d)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _merge_grouped(dest: dict[str, list[dict]], src: dict[str, list[dict]]) -> None:
    """Accumulate one response's grouped records into `dest`.

    EXTENDS per product rather than replacing. dict.update() would be
    wrong here and silently so: across date chunks the same instrument
    appears in every response, and update() would keep only the last
    chunk's records -- losing every earlier period while still returning
    a plausible-looking frame.
    """
    for product, records in src.items():
        dest.setdefault(product, []).extend(records)


def _dedupe_grouped(grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Drop records sharing a timestamp within one product, keeping the
    first occurrence and the original order.

    Applied ONLY when more than one HTTP response contributed, so a
    single-request result is passed through untouched and a genuine
    provider-side duplicate in one response stays visible rather than
    being quietly masked. Chunks are non-overlapping by construction, so
    this is a guard against boundary-inclusivity surprises, not an
    expected step -- it logs when it actually removes anything.
    """
    deduped: dict[str, list[dict]] = {}
    removed = 0
    for product, records in grouped.items():
        seen: set = set()
        kept: list[dict] = []
        for record in records:
            stamp = record.get("time")
            if stamp in seen:
                removed += 1
                continue
            seen.add(stamp)
            kept.append(record)
        deduped[product] = kept
    if removed:
        logger.debug(
            "QuantHub date chunking: removed %d duplicate record(s) at chunk boundaries",
            removed,
        )
    return deduped


def _fetch_day_range_or_split(
    instruments: list[str],
    native_interval: str,
    start_d: date,
    end_d: date,
    depth: int = 0,
) -> tuple[dict[str, list[dict]], int]:
    """Fetch one whole-day range, halving it if QuantHub rejects it for
    exceeding the row ceiling.

    Returns (grouped_records, successful_response_count). The count is
    reported back rather than inferred, because the caller needs to know
    whether MORE THAN ONE response contributed before deciding to
    de-duplicate -- and a reactive split makes that true even when the
    proactive plan was a single chunk.

    This is the REACTIVE half of the strategy -- the correction for a
    proactive estimate that turned out to be too generous for this
    instrument/interval/period. It splits on nothing else: any other
    QuantHubRequestError, and every QuantHubRateLimitError, transient
    failure and unrecognised exception, propagates untouched so that
    Task 3's retry, rate-limit and 400 semantics stay exactly as they
    are. Each attempt goes through _fetch_quanthub_records, so every
    request -- including every split retry -- takes its own rate-limiter
    slot and its own retry policy.
    """
    try:
        return (
            _fetch_quanthub_records(
                instruments,
                native_interval,
                start=datetime.combine(start_d, time.min),
                end=datetime.combine(end_d, time.max),
            ),
            1,
        )
    except QuantHubRequestError as exc:
        if not _is_row_limit_error(exc):
            raise  # a different deterministic rejection -- splitting cannot help

        span_days = (end_d - start_d).days + 1
        if span_days <= 1:
            raise QuantHubRequestError(
                f"QuantHub rejected a SINGLE-DAY request for {len(instruments)} "
                f"instrument(s) on {start_d} as exceeding the "
                f"{QUANTHUB_MAX_ROWS_PER_REQUEST:,}-row response limit. A day is the "
                f"smallest range this client will request, so it cannot be split "
                f"further -- fetch fewer instruments per request instead. "
                f"Original error: {exc}",
                response=exc.response,
                response_body=exc.response_body,
            ) from exc

        if depth >= _MAX_CHUNK_SPLIT_DEPTH:
            raise QuantHubRequestError(
                f"QuantHub still rejected {start_d} -> {end_d} as exceeding the "
                f"{QUANTHUB_MAX_ROWS_PER_REQUEST:,}-row response limit after "
                f"{_MAX_CHUNK_SPLIT_DEPTH} successive range halvings. Refusing to "
                f"split further. Original error: {exc}",
                response=exc.response,
                response_body=exc.response_body,
            ) from exc

        midpoint = start_d + timedelta(days=span_days // 2)
        logger.info(
            "QuantHub rejected %s -> %s for %d instrument(s) as over the row limit; "
            "splitting into %s -> %s and %s -> %s (depth %d)",
            start_d, end_d, len(instruments),
            start_d, midpoint - timedelta(days=1), midpoint, end_d, depth + 1,
        )

        merged: dict[str, list[dict]] = {}
        responses = 0
        for sub_start, sub_end in (
            (start_d, midpoint - timedelta(days=1)),
            (midpoint, end_d),
        ):
            sub_grouped, sub_responses = _fetch_day_range_or_split(
                instruments, native_interval, sub_start, sub_end, depth + 1
            )
            _merge_grouped(merged, sub_grouped)
            responses += sub_responses
        return merged, responses


def _fetch_date_range_chunked(
    instruments: list[str], native_interval: str, start_d: date, end_d: date
) -> dict[str, list[dict]]:
    """Fetch [start_d, end_d] for `instruments`, transparently using as
    many HTTP requests as the row ceiling requires.

    Returns records grouped by product exactly as a single
    _fetch_quanthub_records call would, so callers cannot tell how many
    requests produced them. Normalization, 4H resampling and date
    filtering all happen ABOVE this, once, on the combined records --
    which is what keeps chunk boundaries from ever producing a partial
    4H bucket.
    """
    span_days = (end_d - start_d).days + 1
    days_per_chunk = _max_days_per_chunk(native_interval, len(instruments), span_days)
    chunks = _day_chunks(start_d, end_d, days_per_chunk)

    if len(chunks) > 1:
        logger.info(
            "QuantHub date chunking: %d-day range for %d instrument(s) at %s estimated "
            "at ~%d rows, over the %d-row ceiling -- splitting into %d requests of up "
            "to %d day(s)",
            span_days, len(instruments), native_interval,
            _estimated_rows_for_day_span(native_interval, span_days, len(instruments)),
            QUANTHUB_MAX_ROWS_PER_REQUEST, len(chunks), days_per_chunk,
        )

    merged: dict[str, list[dict]] = {}
    responses = 0
    for chunk_start, chunk_end in chunks:
        chunk_grouped, chunk_responses = _fetch_day_range_or_split(
            instruments, native_interval, chunk_start, chunk_end
        )
        _merge_grouped(merged, chunk_grouped)
        responses += chunk_responses

    # De-duplicate whenever more than one RESPONSE contributed, not
    # merely when more than one chunk was planned: a reactive split
    # makes that true even for a single proactively-sized chunk. A
    # genuine single-response result is still passed through untouched.
    return _dedupe_grouped(merged) if responses > 1 else merged


def download_history_batch(
    instruments: list[str],
    interval: str | BarInterval,
    start: DateLike,
    end: DateLike,
    *,
    use_date_range: bool = False,
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
        use_date_range: Which QuantHub request shape to send.

            False (default) -- the COUNT shape, unchanged from before
                this parameter existed: estimate a count from
                [start, end], ask for that many most-recent bars, and
                filter client-side. Every existing caller keeps exactly
                its previous behaviour without passing anything.

            True -- the DATE-RANGE shape: send start/end as unix
                seconds and no count at all. Returns the same normalized
                schema; what changes is how much QuantHub has to send to
                produce it, which matters enormously when topping up an
                almost-current cache (see this module's docstring for
                the measured 98.8% reduction).

            Deliberately explicit rather than inferred from the window's
            width: a request shape a caller did not ask for should never
            be chosen for it, and the cache layer that will actually
            want the date range (database.service) is the next task in
            a staged migration, not this one.

    Returns:
        dict mapping each unique instrument to a DataFrame with columns
        Date, Open, High, Low, Close, Volume -- empty (correct columns)
        for an instrument QuantHub returned no data for. IDENTICAL in
        type, schema and dtypes for both request shapes.

    Note:
        FOUR_HOUR is fetched as native 1H and resampled via
        core.utils.resample_to_4h -- the same function core.downloader
        uses for LSEG, not a second implementation; this applies to both
        request shapes. In the COUNT shape, `count` is estimated ONCE
        from [start, end] (see _estimate_count), then capped PER CHUNK
        via _max_count_for_batch(len(chunk)) -- QuantHub's limit is on
        TOTAL ROWS per request (instruments_in_request x count <=
        QUANTHUB_MAX_ROWS_PER_REQUEST, live-verified), not a flat count
        cap independent of batch size, so a smaller trailing chunk (e.g.
        the 1-instrument remainder of a 21-instrument batch) legitimately
        gets a HIGHER count than a full QUANTHUB_BATCH_SIZE-sized chunk.

    ROW CEILING (applies to BOTH shapes): the total row cap is a HARD,
    exactly-enforced 10,000 rows per HTTP request (10,000 succeeds,
    10,001 returns HTTP 400 "Max row limit exceeded (10000)"; re-
    confirmed live at 1x10,000, 1x10,001, 2x5,000 and 2x5,001). It is
    shared across every instrument in the request, so in the COUNT shape
    the effective per-instrument count is QUANTHUB_MAX_ROWS_PER_REQUEST
    // len(chunk) -- batching more instruments directly shrinks how far
    back each one can reach.

    In the DATE-RANGE shape there is no count to cap, so a range whose
    rows exceed the ceiling is NOT trimmed client-side: QuantHub's own
    HTTP 400 propagates to the caller unchanged. That is deliberate at
    this stage -- automatic multi-request chunking is a later task, and
    silently returning a truncated window would hide the very condition
    that chunking needs to detect. For scale: six months of hourly data
    measured 1,378-2,989 rows per instrument across eight STIR markets,
    comfortably inside the ceiling, so a realistic single-instrument
    Oscill8 request does not approach it.

    COLD START -- and how the date-range shape changes it. Under the
    COUNT shape, a never-cached instrument can only ever receive, on its
    first fetch, the most recent history within that request's effective
    count cap, because `count` always means "as of now" and no parameter
    anchors a window earlier. database.service's SQLite cache (Module 2)
    compensates over time by accumulating bars as "now" advances. The
    DATE-RANGE shape removes that ceiling at the source: an explicit
    [start, end] reaches directly into the past in one request, which is
    what makes the staged migration worth doing. Nothing in THIS module
    depends on that cache behaviour either way.
    """
    if isinstance(interval, str):
        interval = BarInterval(interval)

    start_d = to_date(start)
    end_d = to_date(end)
    if start_d > end_d:
        raise ValueError(f"start ({start_d}) must be <= end ({end_d})")

    native_interval = config.QUANTHUB_NATIVE_INTERVAL[interval]

    unique_instruments = list(dict.fromkeys(instruments))  # de-dupe, preserve order
    grouped: dict[str, list[dict]] = {}
    count_by_instrument: dict[str, int | None] = {}

    if use_date_range:
        # Per instrument-batch, the whole requested day span is fetched
        # in as many DATE chunks as the row ceiling requires -- see
        # _fetch_date_range_chunked. Bounds stay whole days (00:00:00
        # through 23:59:59.999999), exactly as before chunking existed:
        # that is what the client-side filter below keeps, and what
        # keeps 4H buckets whole.
        #
        # _merge_grouped, not grouped.update(): a date-chunked fetch
        # returns the same instrument in several responses, and update()
        # would keep only the last one.
        for chunk in _chunked(unique_instruments, QUANTHUB_BATCH_SIZE):
            _merge_grouped(
                grouped,
                _fetch_date_range_chunked(chunk, native_interval, start_d, end_d),
            )
            for instrument in chunk:
                count_by_instrument[instrument] = None
    else:
        start_dt = datetime.combine(start_d, datetime.min.time())
        end_dt = datetime.combine(end_d, datetime.min.time())
        estimated_count = _estimate_count(native_interval, start_dt, end_dt)
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

        # Count-shape-only diagnostic: a date-range request has no
        # `count` that could have been insufficient, so a short history
        # there means the instrument genuinely has no older data, not
        # that the request under-asked.
        if count is not None and not df.empty and len(df) >= count and df["Date"].min() > pd.Timestamp(start_d):
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
        "Downloaded QuantHub batch: %d unique instrument(s) in %d request(s) using the %s shape",
        len(unique_instruments),
        len(_chunked(unique_instruments, QUANTHUB_BATCH_SIZE)),
        "start/end date-range" if use_date_range else "count",
    )
    return results


def download_history(
    instrument: str,
    interval: str | BarInterval,
    start: DateLike,
    end: DateLike,
    *,
    use_date_range: bool = False,
) -> pd.DataFrame:
    """Download historical OHLCV bars for a single QuantHub instrument.
    Thin wrapper around download_history_batch([instrument], ...) -- see
    that function for the actual fetch/resample/filter logic, and for
    what use_date_range selects.

    Args:
        instrument: QuantHub instrument identifier, e.g. "ERH26" -- must
            already be built via build_instrument(); this function does
            no RIC/root resolution of its own.
        interval: "DAILY", "HOURLY", or "4H" (see core.config.BarInterval).
        start: Start date (inclusive), str "YYYY-MM-DD", date, or datetime.
        end: End date (inclusive), str "YYYY-MM-DD", date, or datetime.
        use_date_range: False (default) sends the count shape, exactly as
            before this parameter existed; True sends start/end as unix
            seconds with no count. Forwarded verbatim to
            download_history_batch().

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
        Empty DataFrame (correct columns) if QuantHub returned no data
        for the requested instrument. The schema is identical for both
        request shapes.
    """
    results = download_history_batch(
        [instrument], interval, start, end, use_date_range=use_date_range
    )
    df = results[instrument]
    logger.info("Downloaded %d bars for QuantHub instrument %s", len(df), instrument)
    return df
