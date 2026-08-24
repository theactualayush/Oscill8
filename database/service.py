"""
service.py

Cache-first market-data access: the single public entry point for
Module 2. This is the only module in database/ that imports the
provider layer (core.downloader for LSEG, core.quanthub for QuantHub,
core.providers for the market->provider routing decision) -- everything
else in database/ operates purely on SQLAlchemy models and Pandas
DataFrames.

    get_history(ric, interval, start, end) -> pd.DataFrame

Request
   |
Check SQLite (sync_ranges)
   |
Is requested history fully covered?
   |-- Yes -> return cached DataFrame
   |
   \-- No
        |
   Determine missing sub-range(s)
        |
   core.ric.parse_ric(ric) -> market_key
        |
   core.providers.resolve_provider(market_key)
        |-- LSEG -----> core.downloader.download_history(ric, ...)      (unchanged)
        \-- QUANTHUB -> core.providers.qh_root_for_market(market_key)
                         + core.quanthub.build_instrument(root, month, year)
                         -> core.quanthub.download_history(instrument, ...)
        |
   Store in SQLite (same canonical DataFrame shape either way)
        |
   Return complete DataFrame

Callers cannot tell whether any given bar came from the cache, a fresh
LSEG call, or a fresh QuantHub call -- get_history's own signature is
unchanged; provider selection is entirely internal, keyed off the RIC's
own market (via core.ric.parse_ric), never a new parameter callers must
supply. The cache itself is provider-agnostic: it's keyed on the RIC
string exactly as before, since a market's RIC identity doesn't change
depending on which provider happens to serve it.
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
from core.utils import DateLike, get_logger, to_date

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
    """
    delta = cache.bar_delta(interval.value)
    epoch = datetime(1970, 1, 1)
    periods_elapsed = (now - epoch) // delta
    return epoch + periods_elapsed * delta


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


def _persist_downloaded(
    session,
    ric: str,
    interval: BarInterval,
    downloaded: pd.DataFrame,
    sub_start: datetime,
    sub_end: datetime,
    boundary: datetime,
) -> pd.DataFrame | None:
    """Persist one already-downloaded frame for (ric, interval) covering
    [sub_start, sub_end] -- shared by get_history() and get_history_batch()
    so there is exactly one implementation of the completed/in-progress
    split + sync-range recording, not two.

    Completed bars (Date < boundary) are inserted and [sub_start,
    min(sub_end, boundary)] is recorded as synced; any still-in-progress
    bar (Date >= boundary) is returned (NOT persisted) for the caller to
    merge into its own result -- same contract get_history() has always
    had for today's still-forming bar.
    """
    pending = None
    if not downloaded.empty:
        completed_df = downloaded[downloaded["Date"] < boundary]
        in_progress_df = downloaded[downloaded["Date"] >= boundary]
        if not completed_df.empty:
            cache.insert_bars(session, ric, interval.value, completed_df)
        if not in_progress_df.empty:
            pending = in_progress_df

    synced_end = min(sub_end, boundary)
    if synced_end > sub_start:
        cache.record_sync_range(session, ric, interval.value, sub_start, synced_end)
    else:
        logger.debug(
            "%s [%s] sub-range %s -> %s is entirely in-progress/future; "
            "not marking as synced",
            ric, interval.value, sub_start, sub_end,
        )
    return pending


def get_history(
    ric: str,
    interval: str | BarInterval,
    start: DateLike,
    end: DateLike,
) -> pd.DataFrame:
    """Cache-first historical OHLCV bars for a single RIC.

    Checks the local SQLite cache first. If the requested range isn't
    fully covered, downloads and persists only the missing sub-range(s)
    from whichever provider `ric`'s market is routed to (see
    core.providers.resolve_provider -- LSEG via core.downloader, or
    QuantHub via core.quanthub, chosen internally by _download_from_
    provider), then returns the complete requested range. Columns/
    dtypes match both providers' shared canonical contract:
    [Date, Open, High, Low, Close, Volume].

    The most recent, still-forming bar for an interval currently in
    progress (e.g. today's DAILY bar before day-end, or the current
    HOURLY bar) is never written to the cache or marked as synced -- it
    is re-downloaded on every call until it closes, then cached
    normally like any historical bar. This is deliberate: upserts skip
    rows that already exist, so a cached partial bar could otherwise
    never be refreshed with its final values once complete. Any such
    in-progress bars are still included in the returned DataFrame for
    this call (so the caller isn't denied today's partial data), just
    not persisted.
    """
    if isinstance(interval, str):
        interval = BarInterval(interval)

    start_dt = _coerce_start(start)
    end_dt = _coerce_end(end)
    if start_dt > end_dt:
        raise ValueError(f"start ({start_dt}) must be <= end ({end_dt})")

    now = datetime.utcnow()
    boundary = _last_completed_boundary(interval, now)

    pending_frames: list[pd.DataFrame] = []
    with get_session() as session:
        sync_ranges = cache.get_sync_ranges(session, ric, interval.value)
        missing = _missing_ranges(sync_ranges, start_dt, end_dt)

        for sub_start, sub_end in missing:
            logger.info(
                "Cache miss for %s [%s]: downloading %s -> %s",
                ric, interval.value, sub_start, sub_end,
            )
            downloaded = _download_from_provider(ric, interval, sub_start, sub_end)
            pending = _persist_downloaded(session, ric, interval, downloaded, sub_start, sub_end, boundary)
            if pending is not None:
                pending_frames.append(pending)

        result = cache.read_bars(session, ric, interval.value, start_dt, end_dt)

    if pending_frames:
        extra = pd.concat(pending_frames, ignore_index=True)
        extra = extra[(extra["Date"] >= start_dt) & (extra["Date"] <= end_dt)]
        result = pd.concat([result, extra], ignore_index=True)
        result = result.drop_duplicates(subset="Date").sort_values("Date").reset_index(drop=True)

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
    """
    now = datetime.utcnow()
    boundary = _last_completed_boundary(interval, now)

    results: dict[str, pd.DataFrame] = {}
    with get_session() as session:
        rics_needing_fetch = [
            ric
            for ric in rics
            if _missing_ranges(cache.get_sync_ranges(session, ric, interval.value), start_dt, end_dt)
        ]

        pending_by_ric: dict[str, pd.DataFrame] = {}
        if rics_needing_fetch:
            instrument_by_ric: dict[str, str] = {}
            for ric in rics_needing_fetch:
                parsed = parse_ric(ric)
                qh_root = qh_root_for_market(parsed.market_key)
                instrument_by_ric[ric] = build_instrument(qh_root, parsed.month, parsed.year)

            logger.info(
                "QuantHub batch cache miss for %d/%d RIC(s) [%s]: downloading %s -> %s",
                len(rics_needing_fetch), len(rics), interval.value, start_dt, end_dt,
            )
            batch_downloaded = download_history_quanthub_batch(
                list(instrument_by_ric.values()), interval.value, start_dt, end_dt
            )

            for ric in rics_needing_fetch:
                instrument = instrument_by_ric[ric]
                downloaded = batch_downloaded[instrument]
                pending = _persist_downloaded(session, ric, interval, downloaded, start_dt, end_dt, boundary)
                if pending is not None:
                    pending_by_ric[ric] = pending

        for ric in rics:
            result = cache.read_bars(session, ric, interval.value, start_dt, end_dt)
            pending = pending_by_ric.get(ric)
            if pending is not None:
                extra = pending[(pending["Date"] >= start_dt) & (pending["Date"] <= end_dt)]
                if not extra.empty:
                    result = pd.concat([result, extra], ignore_index=True)
                    result = result.drop_duplicates(subset="Date").sort_values("Date").reset_index(drop=True)
            results[ric] = result

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

    Provider isolation: internally partitions `rics` by core.providers.
    resolve_provider() -- exactly the same routing decision _download_
    from_provider() makes inside get_history(). An LSEG-routed RIC is
    fetched via the existing, completely unmodified per-RIC get_history()
    call (never batched, never sent to QuantHub); QuantHub-routed RICs
    are batched via core.quanthub.download_history_batch(). Neither list
    ever crosses into the other's code path.

    core.downloader.MarketDataUnavailableError handling: an LSEG RIC
    confirmed unavailable is simply OMITTED from the returned dict
    (not raised here) -- exactly like a leg_cache miss, this lets a
    caller such as strategy_engine.pricing.prewarm_leg_cache() leave
    that RIC unresolved so the existing per-instance
    MarketDataUnavailableError handling in template_scanner.scanner.
    run_scan_on_instances() (a later, lazy get_history() call for that
    same RIC) still fires exactly where it already does today -- this
    function does not change or duplicate that skip-and-continue policy.
    A QuantHub core.quanthub.QuantHubRateLimitError, or any other
    exception, is NOT caught here and propagates unchanged.

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

    lseg_rics: list[str] = []
    quanthub_rics: list[str] = []
    for ric in unique_rics:
        parsed = parse_ric(ric)
        provider = resolve_provider(parsed.market_key)
        (quanthub_rics if provider is Provider.QUANTHUB else lseg_rics).append(ric)

    results: dict[str, pd.DataFrame] = {}
    for ric in lseg_rics:
        try:
            results[ric] = get_history(ric, interval, start, end)
        except MarketDataUnavailableError:
            logger.debug(
                "get_history_batch: %s [%s] confirmed unavailable by LSEG -- "
                "omitted from batch result, not raised here (see docstring).",
                ric, interval.value,
            )

    if quanthub_rics:
        results.update(_get_history_batch_quanthub(quanthub_rics, interval, start_dt, end_dt))

    return results
