"""
service.py

Cache-first market-data access: the single public entry point for
Module 2. This is the only module in database/ that imports the
provider layer (core.downloader for LSEG, core.quanthub for QuantHub,
core.providers for the market->provider routing decision) -- everything
else in database/ operates purely on SQLAlchemy models and Pandas
DataFrames.

    get_history(ric, interval, start, end) -> pd.DataFrame
    get_history_batch(rics, interval, start, end) -> dict[str, pd.DataFrame]

ONLY FULLY CLOSED BARS ARE EVER FETCHED, CACHED, OR RETURNED, for every
interval (DAILY/HOURLY/4H) and both providers. Before anything else
below runs, the caller's requested end is capped to the last bar that
has actually closed as of "now" (_effective_request_end, applied in
get_history()/_get_history_batch_with_provenance()/
_get_history_batch_quanthub() -- each independently, from their own
already-computed `boundary`/`now`, exactly like each already
independently computed those before this existed). This is what fixes
a real, live-observed issue: a plain-date request used to stay
uncapped all the way to day-end, so a scan for a currently-forming
interval (e.g. a 4H window not yet closed) kept re-requesting that same
still-forming bar from the provider on every identical re-scan, since
nothing in that wide, always-in-the-future tail could ever be marked
synced. A second identical scan during the same still-forming period
now makes zero further provider requests; the next request after the
period actually closes fetches exactly that newly-closed bar. See
_effective_request_end and _persist_downloaded's own docstrings for the
full mechanism, including why the Date < boundary filter inside
_persist_downloaded remains the actual enforcement point (QuantHub's
own API cannot be asked to exclude a same-day in-progress bar at all).

Request
   |
Check SQLite (sync_ranges)
   |
Is requested history fully covered?
   |-- Yes -> return cached DataFrame
   |
   \\-- No
        |
   core.ric.parse_ric(ric) -> market_key
        |
   core.providers.resolve_provider(market_key)
        |
        |-- LSEG only (no QuantHub mapping) --------------------------.
        |     core.downloader.download_history(ric, ...)              |
        |     (unchanged -- fetches only the missing sub-range(s))    |
        |                                                              |
        \\-- QUANTHUB-mapped market -- PER-(RIC, INTERVAL) PROVIDER     |
            PROVENANCE (see database/models.py's SyncRange.provider,   |
            database.cache.get_established_provider/record_sync_range, |
            and _establish_provider_and_fetch/_fetch_established_      |
            quanthub/_get_history_batch_with_provenance below):        |
                |                                                      |
           cache.get_established_provider(ric, interval)               |
           -- returns None ambiguously: EITHER "genuinely never        |
           touched" OR "touched, but before provider provenance        |
           existed" (a LEGACY row). Disambiguated below by ALSO         |
           checking whether sync_ranges coverage already exists for    |
           this (ric, interval) -- see database.cache.                 |
           get_established_provider's own docstring.                   |
                |                                                      |
                |-- None, AND no sync_ranges coverage exists at all    |
                |   (genuinely new (ric, interval)) ----------------.   |
                |     Try LSEG for the FULL requested window       |   |
                |     (by construction this equals the missing     |   |
                |     range: nothing has ever been cached yet).    |   |
                |          |                                       |   |
                |     Is the returned frame COMPLETE?               |   |
                |     (_is_complete_history -- non-empty, no        |   |
                |     interior gap wider than a generous business-  |   |
                |     day threshold; a contract's own real start/   |   |
                |     end shortfall does NOT count as incomplete)   |   |
                |          |-- Yes -> ESTABLISH LSEG, persist LSEG   |   |
                |          |-- No  -> discard the LSEG attempt      |   |
                |                    entirely (never persisted),   |   |
                |                    ESTABLISH QUANTHUB, fetch/    |   |
                |                    persist QuantHub for the      |   |
                |                    full window instead           |   |
                |     This completeness test runs EXACTLY ONCE per |   |
                |     (ric, interval) -- never repeated again.     |   |
                |                                                   |   |
                |-- None, BUT sync_ranges coverage already exists   |   |
                |   (a LEGACY/UNKNOWN row -- e.g. cached before     |   |
                |   provider provenance existed, migrated to        |   |
                |   provider=NULL) -----------------------------.   |   |
                |     _fetch_legacy_unknown_provider: per missing    |  |   |
                |     sub-range, LSEG is tried FIRST; if it returns  |  |   |
                |     usable/complete data (_is_complete_history --  |  |   |
                |     the SAME completeness test used below), that   |  |   |
                |     sub-range is persisted with provider=None. If  |  |   |
                |     LSEG is unavailable (a confirmed              |  |   |
                |     MarketDataUnavailableError, e.g. an Interday   |  |   |
                |     70112 or Intraday 92000 entitlement gap -- see |  |   |
                |     core.downloader's classifiers), incomplete, or |  |   |
                |     empty, that sub-range falls back to QuantHub   |  |   |
                |     (_download_quanthub_full_window) for JUST that |  |   |
                |     sub-range -- and is STILL persisted with       |  |   |
                |     provider=None regardless of which provider     |  |   |
                |     actually served it. NEVER runs the ONE-TIME     |  |   |
                |     establishment decision, NEVER records          |  |   |
                |     provider="LSEG" or "QUANTHUB" here -- the       |  |   |
                |     pre-existing history's true origin is unknown  |  |   |
                |     and must never be fabricated as either         |  |   |
                |     provider, no matter which one served a given   |  |   |
                |     sub-range this call. This is a permanent       |  |   |
                |     state -- only cache.delete_bars_and_           |  |   |
                |     sync_ranges() (the administrative/reset        |  |   |
                |     utility) can clear it.                         |  |   |
                |                                                     |   |
                |-- Established LSEG --------------------------.   |   |
                |     Incremental: _missing_ranges() as always,  |  |   |
                |     fetch ONLY the missing sub-range(s) from    |  |   |
                |     LSEG (core.downloader.download_history) --  |  |   |
                |     byte-identical to the original, pre-        |  |   |
                |     fallback incremental behavior. QuantHub is  |  |   |
                |     never consulted again for this (ric,        |  |   |
                |     interval).                                  |  |   |
                |                                                  |  |   |
                \\-- Established QUANTHUB -----------------------.  |  |   |
                      QuantHub has no start/end/offset/pagination |  |   |
                      mechanism (live-verified -- see core.        |  |   |
                      quanthub's own module docstring), so "only   |  |   |
                      the missing range" cannot be requested        |  |   |
                      literally: whenever ANYTHING is missing,      |  |   |
                      the FULL requested window is re-requested     |  |   |
                      from QuantHub (existing, unmodified           |  |   |
                      core.quanthub.download_history_batch, still   |  |   |
                      capped at QUANTHUB_MAX_ROWS_PER_REQUEST/      |  |   |
                      QUANTHUB_BATCH_SIZE) -- this is a QuantHub    |  |   |
                      API limitation, not a design choice. LSEG is  |  |   |
                      never consulted again for this (ric,          |  |   |
                      interval) either way.                         |  |   |
                |                                                              |
        ONE (RIC, INTERVAL)'S HISTORY IS NEVER A MIX OF LSEG AND QUANTHUB BARS  |
        `-----------------------------------------------------------------'
        |
   Store in SQLite (same canonical DataFrame shape either way)
        |
   Return complete DataFrame

Callers cannot tell whether any given bar came from the cache, a fresh
LSEG call, or a fresh QuantHub call -- get_history's own signature is
unchanged; provider selection is entirely internal, keyed off the RIC's
own market (via core.ric.parse_ric) plus a persisted per-(ric, interval)
decision, never a new parameter callers must supply. The SAME contract
can legitimately have different established providers at different
intervals (e.g. SONH26 DAILY established LSEG, SONH26 HOURLY established
QUANTHUB) -- provenance is always keyed on (ric, interval) together,
never on the ric alone.

cache.delete_bars_and_sync_ranges() is an ADMINISTRATIVE/RESET utility,
not part of this normal retrieval flow -- see its own docstring. Once a
provider is established for a (ric, interval) it is never automatically
revisited; an operator can force fresh LSEG-first discovery by manually
clearing that (ric, interval)'s cache/provenance with that function.

get_history_batch() applies this exact same per-(ric, interval) decision
to many RICs at once. Every ric needing a QuantHub fetch THIS call --
whether newly establishing QuantHub or already established as QuantHub
from an earlier call -- is collected into ONE batched fetch through the
existing, unmodified core.quanthub.download_history_batch()
(QUANTHUB_BATCH_SIZE chunking, QUANTHUB_MAX_ROWS_PER_REQUEST cap, both
completely untouched by this design); QuantHub is never called
speculatively for a ric already resolved via LSEG.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd

from core.config import BarInterval
from core.downloader import MarketDataUnavailableError, download_history
from core.providers import Provider, qh_root_for_market, resolve_provider
from core.quanthub import download_history as download_history_quanthub
from core.quanthub import download_history_batch as download_history_quanthub_batch
from core.quanthub import build_instrument
from core.ric import parse_ric
from core.utils import DateLike, get_logger, longest_missing_business_day_run, to_date

from database import cache
from database.connection import get_session

logger = get_logger(__name__)


def _download_from_provider(
    ric: str, interval: BarInterval, start: datetime, end: datetime
) -> pd.DataFrame:
    """Resolve which provider owns `ric`'s market and download from it.

    market_key/month/year are recovered from the RIC via core.ric.
    parse_ric() -- semantic decoding of the (market, contract) identity
    the RIC already encodes, not string manipulation of the RIC itself.
    A QuantHub instrument is then built from scratch from an
    independently-looked-up QH root (core.providers.qh_root_for_market)
    plus that same month/year -- the LSEG RIC string itself is never
    touched or reused as part of the QuantHub identifier.
    """
    parsed = parse_ric(ric)
    provider = resolve_provider(parsed.market_key)

    if provider is Provider.LSEG:
        return download_history(ric, interval.value, start, end)

    qh_root = qh_root_for_market(parsed.market_key)
    instrument = build_instrument(qh_root, parsed.month, parsed.year)
    return download_history_quanthub(instrument, interval.value, start, end)


_EPSILON = timedelta(microseconds=1)


def _coerce_start(value: DateLike) -> datetime:
    """Coerce a DateLike into a datetime at the start of its day.

    A bare datetime is used as-is, so intraday callers can request
    sub-day boundaries; a str/date is treated as the start of that day.
    """
    if isinstance(value, datetime):
        return value
    return datetime.combine(to_date(value), time.min)


def _coerce_end(value: DateLike) -> datetime:
    """Coerce a DateLike into a datetime at the end of its day (inclusive)."""
    if isinstance(value, datetime):
        return value
    return datetime.combine(to_date(value), time.max)


def _last_completed_boundary(interval: BarInterval, now: datetime) -> datetime:
    """Return the start timestamp of the bar currently in progress at `now`.

    Bars with Date >= this boundary have not fully closed yet (e.g.
    today's DAILY bar before day-end, or the current HOURLY bar) and
    must never be cached or marked as synced -- their values can still
    change. Historical sub-ranges (well before `now`) are unaffected;
    this boundary only ever trims the trailing, still-forming edge of a
    request.

    Bar timestamps are OPEN-labeled (a bar's Date is the START of its
    period, e.g. a 4H bar dated 12:00 spans [12:00, 16:00) and closes at
    16:00) -- confirmed both by this function's own floor-division logic
    and empirically by core.utils.resample_to_4h's pandas default
    (label='left' for a fixed-frequency rule). "The latest completed
    bar" therefore always means the bar immediately BEFORE this
    boundary, not the one dated at/after it -- see
    _effective_request_end, which is where that distinction actually
    gets enforced against a caller's requested end.
    """
    delta = cache.bar_delta(interval.value)
    epoch = datetime(1970, 1, 1)
    periods_elapsed = (now - epoch) // delta
    return epoch + periods_elapsed * delta


def _effective_request_end(end: datetime, boundary: datetime) -> datetime:
    """Cap a requested/coverage end to the last FULLY CLOSED bar, per
    (interval, now) -- never treat coverage as missing, or request/
    persist anything, that reaches into the currently-forming period
    (see _last_completed_boundary). Returns `end` unchanged if it
    already falls before the current bar's open; otherwise returns the
    instant just before that open (boundary - one microsecond), the
    true upper bound of what could possibly be complete right now.

    This is the central fix for a real, live-observed issue: a plain-
    date request (_coerce_end() -> day-end 23:59:59.999999) previously
    stayed uncapped all the way through _missing_ranges() and into the
    provider request itself, so e.g. a 4H scan at 15:47 requested
    "12:00:00.000001 -> 23:59:59.999999" from LSEG on every identical
    re-scan -- the same still-forming 12:00 bar (spanning [12:00,16:00),
    not yet closed), re-requested every time, since nothing in that wide
    tail could ever be marked synced (see _persist_downloaded). Capping
    the effective end HERE, before _missing_ranges() ever runs, means
    the gap closes to nothing once the last closed bar is already
    cached, so a second identical scan makes zero further provider
    requests until the next bar actually closes.

    Called independently by every function that computes its own
    `boundary` from (interval, now) -- get_history(),
    _get_history_batch_with_provenance(), _get_history_batch_quanthub()
    -- exactly mirroring how each already independently computes `now`/
    `boundary` today; this introduces no new cross-function coupling.
    """
    return min(end, boundary - _EPSILON)


def _missing_ranges(
    sync_ranges: list[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Compute the gap(s) in [start, end] not covered by sync_ranges.

    Pure function, no DB access -- independently testable. Handles every
    boundary case (no coverage, full coverage, leading/trailing/interior
    gaps, or a request wider than all known coverage) via a single
    left-to-right sweep, rather than distinct branches per case.
    """
    overlapping = sorted(
        (r for r in sync_ranges if r[1] >= start and r[0] <= end),
        key=lambda r: r[0],
    )
    if not overlapping:
        return [(start, end)]

    gaps: list[tuple[datetime, datetime]] = []
    cursor = start
    for range_start, range_end in overlapping:
        if range_start > cursor:
            gaps.append((cursor, min(range_start, end)))
        if range_end + _EPSILON > cursor:
            cursor = range_end + _EPSILON
        if cursor > end:
            return gaps
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


# Maximum consecutive missing business days a freshly-fetched LSEG frame
# may have before it's rejected as incomplete for a QuantHub-mapped
# contract (see _is_complete_history). Deliberately generous: comfortably
# covers any single real holiday cluster for the markets this currently
# applies to (developed-market STIR futures -- CORRA/SONIA/EURIBOR/
# SARON/YBA/ESTR_ICE, see core.providers.PROVIDER_ROUTING), while being
# unambiguously smaller than a genuine multi-week vendor data gap. Not a
# claim about any specific exchange's real holiday calendar -- Oscill8
# has none (see core.utils.missing_business_days' own docstring) -- this
# is a deliberately wide safety margin, not a precise calendar model.
_MAX_INCOMPLETE_BUSINESS_DAY_GAP = 10


def _is_complete_history(df: pd.DataFrame) -> bool:
    """True iff `df` (a freshly-fetched LSEG frame covering one
    contract's FULL requested [start, end] window) is usable as that
    contract's SOLE history, per the cache -> LSEG -> QuantHub fallback
    design (see the module docstring).

    An empty frame is never complete -- LSEG returning nothing at all
    for the requested window (the likely outcome for a market whose
    LSEG root has never been live-verified, e.g. EURIBOR/SARON/YBA/
    ESTR_ICE today) must trigger the QuantHub fallback, not be silently
    accepted as "this contract simply has no history".

    A non-empty frame is complete unless it has an INTERIOR gap: a run
    of more than _MAX_INCOMPLETE_BUSINESS_DAY_GAP consecutive missing
    business days anywhere in the frame (core.utils.
    longest_missing_business_day_run) -- the same 'valid observation'
    concept already established for chart rendering (ui.chart_view.
    _missing_weekdays), generalized here from DAILY-only to DAILY/
    HOURLY/4H by checking for any bar on a date rather than a bar at
    every expected intraday timestamp (Oscill8 has no per-market
    trading-session calendar to check the latter against, and none is
    invented here).

    Deliberately does NOT check whether the frame reaches all the way
    to the requested start/end. A contract that stopped trading before
    the requested end (expired) or only started trading after the
    requested start (newly listed) is still complete: that shortfall
    reflects the contract's own real lifecycle, not a provider gap --
    QuantHub could not fill it in either, and treating it as
    "incomplete" would just discard LSEG's real data for nothing.
    """
    if df.empty:
        return False
    return longest_missing_business_day_run(pd.to_datetime(df["Date"])) <= _MAX_INCOMPLETE_BUSINESS_DAY_GAP


def _persist_downloaded(
    session,
    ric: str,
    interval: BarInterval,
    downloaded: pd.DataFrame,
    sub_start: datetime,
    sub_end: datetime,
    boundary: datetime,
    provider: str | None = None,
) -> None:
    """Persist one already-downloaded frame for (ric, interval) covering
    [sub_start, sub_end] -- shared by get_history() and get_history_batch()
    so there is exactly one implementation of the completed-only insert +
    sync-range recording, not two.

    Only FULLY CLOSED bars (Date < boundary) are ever inserted, and only
    [sub_start, min(sub_end, boundary)] is ever recorded as synced
    (carrying `provider`, see database.cache.record_sync_range/
    get_established_provider -- optional, defaults to None so a market
    with no QuantHub mapping is unaffected). Any still-forming bar in
    `downloaded` (Date >= boundary) is silently dropped: never cached,
    never returned to any caller -- the scanner only ever considers
    completed bars, for every interval (see the module docstring and
    _effective_request_end).

    This split is the ACTUAL, load-bearing enforcement point for both
    providers, not a redundant safety net -- callers now cap their
    OUTBOUND request end below `boundary` via _effective_request_end,
    but QuantHub in particular has no way to be asked, server-side, to
    exclude a same-day in-progress bar: its `count=`-only API always
    means "the most recent N observations as of now" (core.quanthub's
    own module docstring), and core.quanthub.download_history_batch's
    local response filter truncates `end` to a bare date before
    filtering, so it cannot exclude a same-day in-progress bar either.
    This function's Date < boundary check is what actually keeps such a
    bar out of the cache regardless of what either provider returns.
    """
    if not downloaded.empty:
        completed_df = downloaded[downloaded["Date"] < boundary]
        if not completed_df.empty:
            cache.insert_bars(session, ric, interval.value, completed_df)
        dropped = len(downloaded) - len(completed_df)
        if dropped:
            logger.debug(
                "%s [%s]: dropped %d still-forming bar(s) (Date >= %s) -- "
                "never cached or returned",
                ric, interval.value, dropped, boundary,
            )

    synced_end = min(sub_end, boundary)
    if synced_end > sub_start:
        cache.record_sync_range(session, ric, interval.value, sub_start, synced_end, provider=provider)
    else:
        logger.debug(
            "%s [%s] sub-range %s -> %s is entirely in-progress/future; "
            "not marking as synced",
            ric, interval.value, sub_start, sub_end,
        )


def _fetch_legacy_unknown_provider(
    session,
    ric: str,
    interval: BarInterval,
    missing: list[tuple[datetime, datetime]],
    boundary: datetime,
) -> None:
    """Incremental LSEG-only fetch for a QuantHub-mapped (ric, interval)
    that has EXISTING sync_ranges coverage but NO recorded provider --
    a LEGACY row, most commonly one cached before database.models.
    SyncRange.provider existed (migrated to provider=NULL by database.
    connection._ensure_sync_ranges_provider_column, which cannot know
    -- and must not guess -- what actually produced that older data).

    cache.get_established_provider() returning None is ambiguous by
    itself (see its own docstring) -- it means EITHER "never touched"
    OR "touched, but before provider provenance existed". The caller
    (get_history()/_get_history_batch_with_provenance()) disambiguates
    by also checking whether sync_ranges is non-empty: non-empty means
    this function applies; empty means a genuinely new (ric, interval)
    and _establish_provider_and_fetch() applies instead. Getting this
    wrong previously caused a real bug: treating a legacy NULL row as
    "nothing established yet" ran the full-window LSEG-first
    establishment test against the ENTIRE historical span, and win-or-
    lose, database.cache.record_sync_range() then MERGED the new
    result's provider claim across the old, never-actually-verified
    coverage -- silently fabricating "LSEG" (or "QuantHub") provenance
    for months of data nobody had re-examined.

    For each missing sub-range, LSEG is tried FIRST. If it returns
    usable, complete data (_is_complete_history -- the SAME completeness
    test _establish_provider_and_fetch uses), that sub-range is
    persisted with provider=None explicitly. If LSEG cannot provide
    usable data for that sub-range -- a confirmed MarketDataUnavailableError
    (e.g. CORRA's Interday 70112, or CRAU7's live-confirmed Intraday
    92000 entitlement gap -- see core.downloader's own classifiers), a
    returned-but-incomplete frame, or a returned-but-EMPTY frame (LSEG
    can legitimately return 0 bars with no exception at all, e.g.
    SONM8/SONZ7 in production -- _is_complete_history treats an empty
    frame as incomplete too, so this is caught by the same check, not a
    separate branch) -- that sub-range is instead fetched from QuantHub
    (_download_quanthub_full_window, the SAME QuantHub-fetch mechanism
    establishment/established-QuantHub already use) and STILL persisted
    with provider=None.

    Because record_sync_range() merges an incoming range with any
    existing overlapping row and the merged row always takes the
    INCOMING call's provider (see its own docstring), passing
    provider=None on EVERY branch here -- whether the bars actually came
    from LSEG or from this QuantHub fallback -- is what keeps the merged
    row's provider permanently NULL: this function NEVER establishes
    "LSEG" or "QUANTHUB" as this (ric, interval)'s provider, no matter
    which one actually served a given sub-range. The pre-existing
    portion's true origin remains genuinely unknown regardless, and nothing
    here ever "graduates" a legacy row into an established one; only
    cache.delete_bars_and_sync_ranges() (the administrative/reset
    utility) can clear it so a future request performs fresh, explicit
    establishment.

    This is a deliberate correction from an earlier version of this
    function, which left MarketDataUnavailableError uncaught here on the
    theory that falling back to QuantHub would risk fabricating
    QuantHub provenance -- that concern doesn't apply once provider is
    ALWAYS recorded as None regardless of which provider actually
    served the data, and leaving it uncaught instead meant a single
    confirmed-unavailable or empty LSEG response for ANY missing
    sub-range aborted the entire scan (live-observed for CRAU7 in
    production) instead of degrading gracefully to QuantHub, exactly as
    every other QuantHub-mapped state already does. Any OTHER exception
    (network/auth/an unrecognized LDError, a programming bug) is still
    NOT caught and still propagates, aborting the caller -- this
    deliberately does not broaden the exception policy into a general
    "any LSEG failure means try QuantHub" bucket (see
    _establish_provider_and_fetch's own docstring for the same
    principle). The QuantHub fallback call itself is not wrapped in any
    additional exception handling either -- a QuantHub failure here
    propagates exactly as it already does from establishment/established-
    QuantHub, unchanged.
    """
    for sub_start, sub_end in missing:
        downloaded_lseg = None
        try:
            downloaded_lseg = download_history(ric, interval.value, sub_start, sub_end)
        except MarketDataUnavailableError:
            downloaded_lseg = None

        if downloaded_lseg is not None and _is_complete_history(downloaded_lseg):
            logger.info(
                "%s [%s]: legacy/unknown-provenance cache -- LSEG provided usable "
                "data for missing %s -> %s (provider stays unrecorded)",
                ric, interval.value, sub_start, sub_end,
            )
            _persist_downloaded(
                session, ric, interval, downloaded_lseg, sub_start, sub_end, boundary, provider=None
            )
        else:
            logger.info(
                "%s [%s]: legacy/unknown-provenance cache -- LSEG could not provide "
                "usable data for missing %s -> %s -- falling back to QuantHub for "
                "this sub-range (provider stays unrecorded)",
                ric, interval.value, sub_start, sub_end,
            )
            downloaded_qh = _download_quanthub_full_window(ric, interval, sub_start, sub_end)
            _persist_downloaded(
                session, ric, interval, downloaded_qh, sub_start, sub_end, boundary, provider=None
            )


def _establish_provider_and_fetch(
    session,
    ric: str,
    interval: BarInterval,
    start_dt: datetime,
    end_dt: datetime,
    boundary: datetime,
) -> None:
    """ONE-TIME provider establishment for a QuantHub-mapped market's
    (ric, interval) that has no established provider yet (cache.
    get_established_provider returned None) -- the cache -> LSEG ->
    QuantHub fallback's only completeness decision, made exactly once
    per (ric, interval) and never repeated (see the module docstring).

    By construction, no established provider means no cached bars for
    this (ric, interval) either (a provider is always established in
    the SAME persist step as its first bars -- see below), so
    [start_dt, end_dt] IS the full missing range on this call; there is
    no narrower sub-range to test against. `end_dt` here is already the
    CALLER's effective end (see _effective_request_end) -- capped below
    the currently-forming bar's boundary -- so the completeness test
    below never sees a partial/in-progress bar in the first place.

    LSEG is tried FIRST, for the full window. If its result is complete
    (_is_complete_history), LSEG is ESTABLISHED as the provider and its
    result is persisted -- QuantHub is never called. If LSEG cannot
    provide complete history -- a confirmed MarketDataUnavailableError,
    or a returned-but-incomplete frame -- LSEG's result is discarded
    entirely (never persisted, never mixed with QuantHub's), and
    QUANTHUB is ESTABLISHED as the provider instead, fetched for the
    full window, and persisted as this (ric, interval)'s sole history.

    Every SUBSEQUENT call for this (ric, interval) finds an established
    provider via cache.get_established_provider() and skips straight to
    incremental fetching from that one provider -- see get_history()/
    get_history_batch(), which call this function ONLY when no
    provider is established yet.

    Any exception from the LSEG call OTHER than MarketDataUnavailableError
    (network/auth/an unrecognized LSEG error, a programming bug) is NOT
    caught here and propagates unchanged, aborting the caller -- this
    deliberately does not broaden the existing narrow exception policy
    (see template_scanner.scanner's own module docstring) into a general
    "any LSEG failure means try QuantHub" bucket, which would silently
    mask real failures instead of surfacing them.
    """
    downloaded_lseg = None
    try:
        downloaded_lseg = download_history(ric, interval.value, start_dt, end_dt)
    except MarketDataUnavailableError:
        downloaded_lseg = None

    if downloaded_lseg is not None and _is_complete_history(downloaded_lseg):
        logger.info(
            "%s [%s]: LSEG provided complete history for %s -> %s -- "
            "establishing LSEG as the provider",
            ric, interval.value, start_dt, end_dt,
        )
        _persist_downloaded(
            session, ric, interval, downloaded_lseg, start_dt, end_dt, boundary,
            provider=Provider.LSEG.value,
        )
        return

    logger.info(
        "%s [%s]: LSEG could not provide complete history for %s -> %s -- "
        "establishing QuantHub as the provider instead",
        ric, interval.value, start_dt, end_dt,
    )
    downloaded_qh = _download_quanthub_full_window(ric, interval, start_dt, end_dt)
    _persist_downloaded(
        session, ric, interval, downloaded_qh, start_dt, end_dt, boundary,
        provider=Provider.QUANTHUB.value,
    )


def _download_quanthub_full_window(
    ric: str, interval: BarInterval, start_dt: datetime, end_dt: datetime
) -> pd.DataFrame:
    """Fetch [start_dt, end_dt] for one ric from QuantHub -- always the
    FULL window, never a sub-range, since QuantHub has no start/end/
    offset/pagination mechanism (live-verified -- see core.quanthub's
    own module docstring: only instruments=/interval=/count= exist).
    Used both when establishing QuantHub for the first time and on
    every subsequent call for a (ric, interval) already established as
    QuantHub -- the same unavoidable API limitation applies either way.
    """
    parsed = parse_ric(ric)
    qh_root = qh_root_for_market(parsed.market_key)
    instrument = build_instrument(qh_root, parsed.month, parsed.year)
    return download_history_quanthub(instrument, interval.value, start_dt, end_dt)


def _fetch_established_quanthub(
    session,
    ric: str,
    interval: BarInterval,
    start_dt: datetime,
    end_dt: datetime,
    boundary: datetime,
) -> None:
    """Fetch from QuantHub for a (ric, interval) already ESTABLISHED as
    QuantHub's responsibility (cache.get_established_provider returned
    "QUANTHUB") -- LSEG is never consulted again once established. See
    _download_quanthub_full_window for why the full window is requested
    rather than just the missing portion (a QuantHub API limitation,
    not a design choice). `end_dt` here is already the caller's
    effective end (see _effective_request_end).
    """
    downloaded_qh = _download_quanthub_full_window(ric, interval, start_dt, end_dt)
    _persist_downloaded(
        session, ric, interval, downloaded_qh, start_dt, end_dt, boundary,
        provider=Provider.QUANTHUB.value,
    )


def get_history(
    ric: str,
    interval: str | BarInterval,
    start: DateLike,
    end: DateLike,
) -> pd.DataFrame:
    """Cache-first historical OHLCV bars for a single RIC.

    Checks the local SQLite cache first. If the requested range is
    fully covered, no provider is contacted at all.

    Otherwise: for a market with NO QuantHub fallback target (core.
    providers.resolve_provider returns Provider.LSEG -- every market
    except CORRA/SONIA/EURIBOR/SARON/YBA/ESTR_ICE today), behavior is
    completely unchanged from before this fallback design existed --
    only the missing sub-range(s) are fetched from LSEG via
    _download_from_provider.

    For a QuantHub-mapped market: cache.get_established_provider(ric,
    interval) decides everything, with FOUR distinct states (see the
    module docstring's "LEGACY/UNKNOWN" section) --

    - Established LSEG -> plain incremental _missing_ranges()-driven
      fetching from LSEG only, exactly like the non-QuantHub-mapped
      path above (this is what keeps a 28/30-day-cached request from
      re-downloading all 30 days).
    - Established QuantHub -> the full window is re-requested from
      QuantHub whenever anything is missing (_fetch_established_quanthub
      -- a QuantHub API limitation, not a design choice: it has no
      start/end/pagination mechanism, see that function's docstring).
    - Not established AND no existing sync_ranges coverage at all
      (a genuinely new (ric, interval)) -> LSEG is tried once for the
      full window (_establish_provider_and_fetch), establishing LSEG or
      QuantHub as this (ric, interval)'s permanent provider.
    - Not established BUT existing sync_ranges coverage already exists
      (a LEGACY row -- e.g. cached before the provider column existed,
      migrated to provider=NULL) -> _fetch_legacy_unknown_provider:
      incremental LSEG-only fetching of ONLY the missing sub-range(s),
      exactly like the established-LSEG case, but NEVER establishing a
      provider and NEVER touching QuantHub -- the pre-existing history's
      true origin is unknown and must never be fabricated as either
      provider. See _fetch_legacy_unknown_provider's own docstring for
      why this is the safe choice.

    Never a mix of LSEG and QuantHub bars for one (ric, interval) -- see
    the module docstring for the full design.

    Columns/dtypes match both providers' shared canonical contract:
    [Date, Open, High, Low, Close, Volume].

    ONLY FULLY CLOSED BARS ARE EVER FETCHED, CACHED, OR RETURNED. The
    currently-forming bar for whatever interval is in progress at call
    time (e.g. today's still-open DAILY bar, the current HOURLY bar, or
    a 4H bar whose 4-hour window hasn't closed yet) is excluded from the
    request entirely -- see _effective_request_end, applied centrally
    here BEFORE any cache-coverage check or provider call, so it is
    never requested, never marked as synced, and simply never appears
    in the returned DataFrame until it actually closes. This replaces
    an earlier design where such a bar WAS still returned (never cached,
    but included in that one call's result) -- that meant a scan re-run
    during the same still-forming period re-requested the same partial
    bar from the provider every single time, for no benefit (see
    _effective_request_end's own docstring for the live-observed issue
    this fixes). A second identical call during the same still-forming
    period now makes zero further provider requests; once the period
    closes, the next call fetches exactly that newly-closed bar.
    """
    if isinstance(interval, str):
        interval = BarInterval(interval)

    start_dt = _coerce_start(start)
    end_dt = _coerce_end(end)
    if start_dt > end_dt:
        raise ValueError(f"start ({start_dt}) must be <= end ({end_dt})")

    now = datetime.utcnow()
    boundary = _last_completed_boundary(interval, now)
    effective_end_dt = _effective_request_end(end_dt, boundary)

    parsed = parse_ric(ric)
    has_quanthub_fallback = resolve_provider(parsed.market_key) is Provider.QUANTHUB

    with get_session() as session:
        sync_ranges = cache.get_sync_ranges(session, ric, interval.value)
        missing = (
            _missing_ranges(sync_ranges, start_dt, effective_end_dt)
            if effective_end_dt >= start_dt
            else []
        )

        if not missing:
            pass  # fully cached (through the last closed bar), or the
            # entire requested window is still-forming -- no provider contacted
        elif not has_quanthub_fallback:
            # Unchanged, original incremental behavior.
            for sub_start, sub_end in missing:
                logger.info(
                    "Cache miss for %s [%s]: downloading %s -> %s",
                    ric, interval.value, sub_start, sub_end,
                )
                downloaded = _download_from_provider(ric, interval, sub_start, sub_end)
                _persist_downloaded(session, ric, interval, downloaded, sub_start, sub_end, boundary)
        else:
            established = cache.get_established_provider(session, ric, interval.value)
            if established == Provider.LSEG.value:
                # Established LSEG: plain incremental fetching, exactly
                # like the non-QuantHub-mapped path -- only the missing
                # sub-range(s), never the full window. This is what
                # restores incremental caching for QuantHub-mapped
                # markets once a provider has been decided.
                for sub_start, sub_end in missing:
                    logger.info(
                        "Cache miss for %s [%s]: downloading %s -> %s from established LSEG",
                        ric, interval.value, sub_start, sub_end,
                    )
                    downloaded = download_history(ric, interval.value, sub_start, sub_end)
                    _persist_downloaded(
                        session, ric, interval, downloaded, sub_start, sub_end, boundary,
                        provider=Provider.LSEG.value,
                    )
            elif established == Provider.QUANTHUB.value:
                _fetch_established_quanthub(session, ric, interval, start_dt, effective_end_dt, boundary)
            elif sync_ranges:
                # No recorded provider, but coverage already exists --
                # a LEGACY row (see _fetch_legacy_unknown_provider).
                # Never establish a provider here, never touch QuantHub.
                _fetch_legacy_unknown_provider(session, ric, interval, missing, boundary)
            else:
                # No recorded provider AND no existing coverage at all
                # -- a genuinely new (ric, interval).
                _establish_provider_and_fetch(session, ric, interval, start_dt, effective_end_dt, boundary)

        result = cache.read_bars(session, ric, interval.value, start_dt, end_dt)

    return result


def _get_history_batch_quanthub(
    rics: list[str], interval: BarInterval, start_dt: datetime, end_dt: datetime
) -> dict[str, pd.DataFrame]:
    """QuantHub half of get_history_batch(): batches every `rics` entry
    that actually needs a fetch into as few core.quanthub.
    download_history_batch() HTTP requests as QUANTHUB_BATCH_SIZE allows,
    then persists/reads each RIC through the exact same cache-first path
    get_history() itself uses (_persist_downloaded + cache.read_bars) --
    a batched fetch is a cache-first citizen too, not a second cache.

    Deliberate simplification vs. get_history()'s own per-RIC gap
    tracking: any RIC with SOME missing coverage in [start_dt, end_dt]
    is fetched for the FULL window, not just its own narrow gap. This is
    safe (cache.insert_bars upserts with ON CONFLICT DO NOTHING -- re-
    inserting already-cached bars is a no-op) and is actually the only
    behavior that makes sense for QuantHub specifically: its count=
    parameter always means "most recent N bars ending now", never a
    caller-chosen historical sub-range, so there is no such thing as
    "fetch just this RIC's small gap" for this provider -- every fetch
    already covers "as much of the recent window as count allows"
    regardless of what particular sub-range prompted it. A RIC with NO
    missing coverage is excluded from the batch entirely.

    Every persisted result is recorded with provider="QUANTHUB" (see
    database.cache.record_sync_range/get_established_provider) -- this
    function is the shared QuantHub-fetch mechanism used both when a
    ric is newly establishing QuantHub as its provider and when a ric
    is already established as QuantHub from an earlier call; both cases
    need identical treatment (batched full-window fetch, provider
    recorded), so callers (get_history_batch's provenance-aware batch
    path) route both groups through this one function together.
    """
    now = datetime.utcnow()
    boundary = _last_completed_boundary(interval, now)
    effective_end_dt = _effective_request_end(end_dt, boundary)

    results: dict[str, pd.DataFrame] = {}
    with get_session() as session:
        rics_needing_fetch = (
            [
                ric
                for ric in rics
                if _missing_ranges(
                    cache.get_sync_ranges(session, ric, interval.value), start_dt, effective_end_dt
                )
            ]
            if effective_end_dt >= start_dt
            else []
        )

        if rics_needing_fetch:
            instrument_by_ric: dict[str, str] = {}
            for ric in rics_needing_fetch:
                parsed = parse_ric(ric)
                qh_root = qh_root_for_market(parsed.market_key)
                instrument_by_ric[ric] = build_instrument(qh_root, parsed.month, parsed.year)

            logger.info(
                "QuantHub batch cache miss for %d/%d RIC(s) [%s]: downloading %s -> %s",
                len(rics_needing_fetch), len(rics), interval.value, start_dt, effective_end_dt,
            )
            batch_downloaded = download_history_quanthub_batch(
                list(instrument_by_ric.values()), interval.value, start_dt, effective_end_dt
            )

            for ric in rics_needing_fetch:
                instrument = instrument_by_ric[ric]
                downloaded = batch_downloaded[instrument]
                _persist_downloaded(
                    session, ric, interval, downloaded, start_dt, effective_end_dt, boundary,
                    provider=Provider.QUANTHUB.value,
                )

        for ric in rics:
            results[ric] = cache.read_bars(session, ric, interval.value, start_dt, end_dt)

    return results


def _get_history_batch_with_provenance(
    rics: list[str], interval: BarInterval, start_dt: datetime, end_dt: datetime
) -> dict[str, pd.DataFrame]:
    """QuantHub-mapped half of get_history_batch(): applies the SAME
    per-(ric, interval) provider-provenance decision as get_history()
    (see the module docstring) to many RICs at once.

    For each `ric` with SOME missing cache coverage:
      - Already established LSEG -> plain incremental fetch (only the
        missing sub-range(s), never the full window) via LSEG directly,
        individually -- LSEG is never batched, matching its existing
        per-RIC nature both here and in get_history().
      - Already established QuantHub -> queued for ONE combined
        QuantHub batch call below (see _get_history_batch_quanthub).
      - No recorded provider BUT existing sync_ranges coverage already
        exists (a LEGACY row, e.g. cached before provider provenance
        existed) -> delegates to _fetch_legacy_unknown_provider(): LSEG
        tried first per missing sub-range, QuantHub fallback per sub-
        range whenever LSEG is unavailable/incomplete/empty, but
        provider=None is recorded explicitly EITHER way and the ric
        NEVER joins the QuantHub batch queue below -- see that
        function's own docstring for why a legacy row's true provenance
        must never be fabricated as either provider, even when other
        RICs in this same batch call establish QuantHub, and even when
        this ric's own missing range was itself served by QuantHub.
      - No recorded provider AND no existing coverage at all (a
        genuinely new (ric, interval)) -> queued for the same combined
        QuantHub batch call, but only AFTER a one-time LSEG trial for
        the full window fails -- both this case and the already-
        established-QuantHub case above need identical treatment (a
        full-window fetch, persisted with provider="QUANTHUB"), so
        they're queued together rather than as two separate QuantHub
        calls.
      - A ric with NO missing coverage is skipped entirely -- no
        provider contacted, matching get_history()'s own "fully cached
        -> no provider" behavior.

    QuantHub is never called speculatively for a ric already resolved
    via LSEG (established or freshly established this call). The
    QuantHub batch call itself reuses the EXISTING, unmodified
    _get_history_batch_quanthub() -- QUANTHUB_BATCH_SIZE chunking and
    the QUANTHUB_MAX_ROWS_PER_REQUEST cap are completely untouched.
    """
    now = datetime.utcnow()
    boundary = _last_completed_boundary(interval, now)
    effective_end_dt = _effective_request_end(end_dt, boundary)

    results: dict[str, pd.DataFrame] = {}
    qh_batch_rics: list[str] = []

    # If the whole requested window is still-forming (nothing could
    # possibly be complete yet), skip every ric entirely -- no provider
    # attempt for anyone.
    rics_to_consider = rics if effective_end_dt >= start_dt else []

    with get_session() as session:
        for ric in rics_to_consider:
            sync_ranges = cache.get_sync_ranges(session, ric, interval.value)
            missing = _missing_ranges(sync_ranges, start_dt, effective_end_dt)
            if not missing:
                continue  # already fully cached (through the last closed bar) -- no provider attempt

            established = cache.get_established_provider(session, ric, interval.value)

            if established == Provider.LSEG.value:
                for sub_start, sub_end in missing:
                    logger.info(
                        "%s [%s]: downloading %s -> %s from established LSEG (batch)",
                        ric, interval.value, sub_start, sub_end,
                    )
                    downloaded = download_history(ric, interval.value, sub_start, sub_end)
                    _persist_downloaded(
                        session, ric, interval, downloaded, sub_start, sub_end, boundary,
                        provider=Provider.LSEG.value,
                    )
            elif established == Provider.QUANTHUB.value:
                qh_batch_rics.append(ric)
            elif sync_ranges:
                # No recorded provider, but coverage already exists --
                # a LEGACY row. Delegates to the SAME helper get_history()
                # uses (_fetch_legacy_unknown_provider) rather than
                # duplicating its LSEG-first/QuantHub-fallback logic here
                # -- never establishes a provider, never joins the
                # QuantHub batch -- an unrelated ric's QuantHub
                # establishment/batching must never cause this ric's
                # unknown-provenance history to be touched.
                _fetch_legacy_unknown_provider(session, ric, interval, missing, boundary)
            else:
                # No recorded provider AND no existing coverage at all
                # -- a genuinely new (ric, interval). One-time LSEG
                # trial for the full (effective) window (missing ==
                # [(start_dt, effective_end_dt)] here by construction,
                # same invariant as get_history()).
                downloaded_lseg = None
                try:
                    downloaded_lseg = download_history(ric, interval.value, start_dt, effective_end_dt)
                except MarketDataUnavailableError:
                    downloaded_lseg = None

                if downloaded_lseg is not None and _is_complete_history(downloaded_lseg):
                    logger.info(
                        "%s [%s]: LSEG provided complete history for %s -> %s in batch -- "
                        "establishing LSEG as the provider",
                        ric, interval.value, start_dt, effective_end_dt,
                    )
                    _persist_downloaded(
                        session, ric, interval, downloaded_lseg, start_dt, effective_end_dt, boundary,
                        provider=Provider.LSEG.value,
                    )
                else:
                    logger.info(
                        "%s [%s]: LSEG could not provide complete history for %s -> %s in "
                        "batch -- establishing QuantHub as the provider (queued)",
                        ric, interval.value, start_dt, effective_end_dt,
                    )
                    qh_batch_rics.append(ric)

        # Every ric already resolved via LSEG above (or already fully
        # cached) is read back now, before the QuantHub batch call
        # below, so its result reflects exactly what was just persisted.
        for ric in rics:
            if ric not in qh_batch_rics:
                results[ric] = cache.read_bars(session, ric, interval.value, start_dt, end_dt)

    if qh_batch_rics:
        results.update(_get_history_batch_quanthub(qh_batch_rics, interval, start_dt, end_dt))

    return results


def get_history_batch(
    rics: list[str],
    interval: str | BarInterval,
    start: DateLike,
    end: DateLike,
) -> dict[str, pd.DataFrame]:
    """Cache-first historical OHLCV bars for MANY RICs at once, batching
    QuantHub-routed RICs into as few HTTP requests as QuantHub's own
    live-verified per-request instrument limit allows (core.quanthub.
    QUANTHUB_BATCH_SIZE) -- built for the scan/intermarket workflow,
    where the complete set of required legs is known up front (see
    strategy_engine.pricing.prewarm_leg_cache).

    get_history() itself is completely unmodified and remains the
    correct choice for any single-RIC caller; this is purely an
    additional, opt-in entry point for callers that already have many
    RICs to fetch for the SAME interval/date window. Columns/dtypes
    match get_history()'s own canonical contract for every value in the
    returned dict.

    Provider selection: internally partitions `rics` by core.providers.
    resolve_provider() into markets with NO QuantHub fallback target
    (fetched via the existing, completely unmodified per-RIC
    get_history() call -- zero behavior change) and QuantHub-mapped
    markets, which go through _get_history_batch_with_provenance():
    the SAME per-(ric, interval) provider-provenance decision
    get_history() itself uses (see the module docstring and database.
    cache.get_established_provider) -- a ric already established as
    LSEG is fetched incrementally (only its missing sub-range(s), never
    the full window); a ric already established as QuantHub, or not yet
    established at all, is resolved via the existing, unmodified
    _get_history_batch_quanthub() batching. QuantHub is never called
    for a ric already resolved via LSEG, not even speculatively. See
    _establish_provider_and_fetch (the single-RIC equivalent of the
    one-time establishment decision) for why LSEG/QuantHub bars are
    never mixed for one (ric, interval).

    core.downloader.MarketDataUnavailableError handling: an LSEG RIC
    with no QuantHub fallback target that's confirmed unavailable is
    simply OMITTED from the returned dict (not raised here) -- exactly
    like a leg_cache miss, this lets a caller such as strategy_engine.
    pricing.prewarm_leg_cache() leave that RIC unresolved so the
    existing per-instance MarketDataUnavailableError handling in
    template_scanner.scanner.run_scan_on_instances() (a later, lazy
    get_history() call for that same RIC) still fires exactly where it
    already does today -- this function does not change or duplicate
    that skip-and-continue policy. For a QuantHub-mapped RIC, the same
    exception instead triggers the QuantHub fallback (see above) rather
    than omission. A QuantHub core.quanthub.QuantHubRateLimitError, or
    any other exception, is NOT caught here and propagates unchanged.

    Duplicate RICs in the input are fetched/persisted once; the
    returned dict has one entry per unique RIC.
    """
    if isinstance(interval, str):
        interval = BarInterval(interval)

    start_dt = _coerce_start(start)
    end_dt = _coerce_end(end)
    if start_dt > end_dt:
        raise ValueError(f"start ({start_dt}) must be <= end ({end_dt})")

    unique_rics = list(dict.fromkeys(rics))

    lseg_only_rics: list[str] = []
    qh_mapped_rics: list[str] = []
    for ric in unique_rics:
        parsed = parse_ric(ric)
        provider = resolve_provider(parsed.market_key)
        (qh_mapped_rics if provider is Provider.QUANTHUB else lseg_only_rics).append(ric)

    results: dict[str, pd.DataFrame] = {}
    for ric in lseg_only_rics:
        try:
            results[ric] = get_history(ric, interval, start, end)
        except MarketDataUnavailableError:
            logger.debug(
                "get_history_batch: %s [%s] confirmed unavailable by LSEG -- "
                "omitted from batch result, not raised here (see docstring).",
                ric, interval.value,
            )

    if qh_mapped_rics:
        results.update(_get_history_batch_with_provenance(qh_mapped_rics, interval, start_dt, end_dt))

    return results
