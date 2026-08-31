"""
downloader.py

Responsible for ONE thing: getting clean OHLCV bars out of LSEG
Workspace for a given RIC / interval / date range.

Public API:
    open_lseg_session()      -> opens (once) the desktop session
    close_lseg_session()     -> closes it
    download_history(...)    -> pd.DataFrame[Date, Open, High, Low, Close, Volume]

Nothing in this module talks to PostgreSQL or knows about strategies.
database.py decides what's missing and calls this module only for the
missing pieces.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from tenacity import (
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core import config
from core.config import BarInterval
from core.utils import get_logger, to_date, DateLike
from core.utils import resample_to_4h as _shared_resample_to_4h

logger = get_logger(__name__)


class MarketDataUnavailableError(Exception):
    """Raised when LSEG has confirmed a requested RIC has no market data
    available at all -- a permanent, per-RIC condition. Three narrow,
    LSEG-confirmed conditions are translated to this today:
    - TS.Interday.UserRequestError.70005, "The universe is not found"
    - TS.Interday.UserNotPermission.70112, "User does not have
      permission for this universe" (e.g. CORRA's documented current
      entitlement gap -- see CLAUDE.md's Module 1 findings; a
      permissions issue, not an Oscill8 RIC bug, but functionally the
      same "this RIC's data is not accessible to us right now"
      condition a caller needs to skip-and-continue past)
    - *.UserNotPermission.92000 (any service/product prefix -- observed
      in production as both "TS.Intraday.UserNotPermission.92000" and
      "TSCC.QS.UserNotPermission.92000"), the same "no permission"
      condition on an Intraday (HOURLY/4H) request -- live-confirmed in
      production for CRAU7 [4H]; a different numeric code from 70112,
      not a duplicate of it, and matched on the code alone regardless
      of which prefix precedes it (see _is_confirmed_no_intraday_
      permission()'s own docstring for why)

    Distinct from:
    - a valid RIC with no bars in the requested date range (returns an
      empty DataFrame from download_history, not an exception -- see
      _fetch_chunk)
    - a transient network/session/auth/vendor error (retried and, if it
      persists, raised as whatever type LSEG's SDK originally raised,
      unchanged)
    - a programming/parsing error (e.g. _normalize_columns' ValueError
      for an unrecognized column shape -- a real bug, not a data-
      availability condition)

    Callers may treat this as safe to skip and never retry.
    """

    def __init__(self, ric: str, message: str):
        super().__init__(f"No market data available for {ric}: {message}")
        self.ric = ric
        self.message = message


def _is_confirmed_universe_not_found(exc: Exception) -> bool:
    """True only for the narrow, LSEG-confirmed "universe is not found"
    condition (TS.Interday.UserRequestError.70005).

    Empirically verified against a live LSEG session before this was
    written (see the Module 5B.1 design review): the SDK's LDError
    exposes no structured error code for this -- exc.code is None on
    real instances, and exc.args is empty -- the only place the
    identifying detail lives is exc.message. Message-text matching is
    therefore used deliberately, not as a shortcut, and only inside
    this one function -- no other module inspects LSEG message text.

    Duck-types the exception's module/class name (rather than
    `isinstance` against an imported `lseg.data` exception class) so
    this function needs no import of lseg.data at all, consistent with
    every other lazy-import in this module.

    Requires ALL of:
    - the exception is LSEG's LDError type (module "lseg.data._errors",
      class "LDError")
    - its message contains the exact error code
      "TS.Interday.UserRequestError.70005"
    - its message contains the exact phrase "The universe is not found"

    A generic "No data to return" LDError, or an LDError for a
    different error code, or a non-LDError exception whose text happens
    to mention "universe", all return False and are left untranslated.
    """
    exc_type = type(exc)
    if exc_type.__module__ != "lseg.data._errors" or exc_type.__name__ != "LDError":
        return False

    message = getattr(exc, "message", None) or str(exc)
    return (
        "TS.Interday.UserRequestError.70005" in message
        and "The universe is not found" in message
    )


def _is_confirmed_no_permission(exc: Exception) -> bool:
    """True only for the narrow, LSEG-confirmed "no permission for this
    universe" condition (TS.Interday.UserNotPermission.70112) -- CORRA's
    own documented, live-confirmed entitlement gap (see CLAUDE.md's
    Module 1 findings; the exact wording below is quoted verbatim from
    that live confirmation, not guessed). Same duck-typing/exact-match
    philosophy as _is_confirmed_universe_not_found() above -- a
    different, equally permanent, per-RIC condition LSEG itself
    confirms, so it gets the same narrow treatment; this is NOT a
    broadening of what counts as "unavailable", it is one more
    specific, confirmed code recognized alongside 70005.

    Requires ALL of:
    - the exception is LSEG's LDError type (module "lseg.data._errors",
      class "LDError")
    - its message contains the exact error code
      "TS.Interday.UserNotPermission.70112"
    - its message contains the exact phrase "User does not have
      permission for this universe"

    A generic permission-flavored message without this exact code, or
    this exact code with different wording, or a non-LDError exception,
    all return False and are left untranslated -- exactly as narrow as
    _is_confirmed_universe_not_found(). No other error code is treated
    as unavailable by this function; a market whose actual failure mode
    turns out to be something else (e.g. SONIA's current, undocumented
    failure) is deliberately left unclassified rather than guessed at.
    """
    exc_type = type(exc)
    if exc_type.__module__ != "lseg.data._errors" or exc_type.__name__ != "LDError":
        return False

    message = getattr(exc, "message", None) or str(exc)
    return (
        "TS.Interday.UserNotPermission.70112" in message
        and "User does not have permission for this universe" in message
    )


def _is_confirmed_no_intraday_permission(exc: Exception) -> bool:
    """True only for the narrow, LSEG-confirmed "no permission for this
    universe" condition carrying the 92000 UserNotPermission code --
    live-confirmed in production for a 4H request (CRAU7), distinct
    from _is_confirmed_no_permission()'s Interday-scoped 70112: same
    "UserNotPermission" shape, a different numeric code. Same
    duck-typing philosophy as _is_confirmed_universe_not_found()/
    _is_confirmed_no_permission() above -- one more specific, confirmed
    code recognized alongside 70005/70112, NOT a broadening of what
    counts as "unavailable".

    Requires ALL of:
    - the exception is LSEG's LDError type (module "lseg.data._errors",
      class "LDError")
    - its message contains "UserNotPermission.92000", where "92000" is
      not itself the leading digits of a longer number (so a
      hypothetical, unrelated ".920001" code can never false-positive)

    Matches "UserNotPermission.92000" REGARDLESS of the service/product
    prefix in front of it -- deliberately NOT anchored to a specific
    prefix like "TS.Intraday." or "TSCC.QS.". This was corrected after
    real production logs showed the SAME 92000 permission-denied
    condition surfacing under at least two different prefixes:
    "TS.Intraday.UserNotPermission.92000" (the originally observed
    form) and "TSCC.QS.UserNotPermission.92000" (confirmed later,
    silently NOT recognized by the previous prefix-anchored check,
    which let the raw LDError propagate and abort the scan instead of
    falling back to QuantHub). The numeric code plus the
    "UserNotPermission" shape is the stable, LSEG-confirmed signal here;
    the service/product prefix in front of it is not, and a fix that
    simply added "TSCC.QS.UserNotPermission.92000" as a second
    hardcoded prefix would only defer the next occurrence of this exact
    bug to the next new prefix LSEG happens to use.

    Also deliberately does NOT require an exact trailing-phrase match,
    unlike the other two classifiers above -- live production traffic
    has shown this code's own English wording vary too ("User does not
    have permission for this universe" was the originally observed
    text; "User has no permission" is what real production requests
    have actually returned under both prefixes). The error CODE alone
    is the authoritative, confirmed-permanent-condition signal here --
    still one single, specific, exact code (not a family-wide or
    wildcard match), so this remains exactly as narrow in spirit as the
    other two classifiers; it just doesn't anchor to an unreliable
    prefix or phrase on top of it. A non-LDError exception, or any
    other error code (including the Interday-scoped 70112, or an
    unrelated code that merely starts with the digits "92000"), returns
    False and is left untranslated.
    """
    exc_type = type(exc)
    if exc_type.__module__ != "lseg.data._errors" or exc_type.__name__ != "LDError":
        return False

    message = getattr(exc, "message", None) or str(exc)
    marker = "UserNotPermission.92000"
    idx = message.find(marker)
    if idx == -1:
        return False

    after = idx + len(marker)
    return after >= len(message) or not message[after].isdigit()

# --------------------------------------------------------------------------
# Session management
# --------------------------------------------------------------------------

_session_open = False


def open_lseg_session() -> None:
    """Open the LSEG Workspace desktop session, if not already open.

    Uses the locally running Workspace/Eikon app for authentication --
    no credentials are stored by this application.
    """
    global _session_open
    if _session_open:
        return

    import lseg.data as ld  # imported lazily so the rest of the app can
                             # be developed/tested without the library
                             # or a Workspace session present.

    logger.info("Opening LSEG session (%s)...", config.LSEG_SESSION_TYPE)
    try:
        if config.LSEG_APP_KEY:
            session = ld.open_session(name=config.LSEG_SESSION_TYPE, app_key=config.LSEG_APP_KEY)
        else:
            session = ld.open_session(name=config.LSEG_SESSION_TYPE)
        _session_open = True

        # open_session() doesn't always raise on a non-open result (e.g. a
        # pending/closed state from a misconfigured Workspace connection),
        # so only claim success once the session actually confirms it.
        open_state = getattr(session, "open_state", None)
        if open_state is not None and str(open_state) != "OpenState.Opened":
            logger.warning(
                "LSEG open_session() call completed but reported state=%s "
                "(expected OpenState.Opened) -- session may not be usable.",
                open_state,
            )
        else:
            logger.info("LSEG session opened successfully.")
    except Exception:
        logger.exception("Failed to open LSEG session.")
        raise


def close_lseg_session() -> None:
    """Close the LSEG session if one is open. Safe to call multiple times."""
    global _session_open
    if not _session_open:
        return
    import lseg.data as ld

    ld.close_session()
    _session_open = False
    logger.info("LSEG session closed.")


# --------------------------------------------------------------------------
# Column normalization
# --------------------------------------------------------------------------

# LSEG historical-pricing summaries can return slightly different field
# names depending on asset class / interval / library version -- e.g. a
# live pull for SRAZ26 (SOFR) returns OPEN_PRC/HIGH_1/LOW_1/TRDPRC_1/
# ACVOL_UNS, not the generic OPEN/HIGH/LOW/CLOSE/VOLUME names. This map
# covers the known variants and normalizes them to our canonical
# schema: Date, Open, High, Low, Close, Volume. Add more aliases here as
# other markets/instruments turn out to use yet other vendor field names.
_COLUMN_ALIASES = {
    "Open": {"OPEN", "OPEN_PRC", "OPEN_1"},
    "High": {"HIGH", "HIGH_1"},
    "Low": {"LOW", "LOW_1"},
    "Close": {"CLOSE", "TRDPRC_1", "CLOSE_1"},
    "Volume": {"VOLUME", "ACVOL_UNS", "VOLUME_1"},
}

# SETTLE is not part of the canonical schema and is never a *required*
# field (unlike _COLUMN_ALIASES above, its absence is not an error) --
# it's an optional DAILY-only fallback source for Close. Some STIR
# markets (e.g. SONIA at DAILY) return TRDPRC_1 entirely NA while
# SETTLE carries the real daily price; others (SOFR, Fed Funds) have
# both populated, and can differ slightly -- for those, the existing
# TRDPRC_1-derived Close must remain the source of truth. Add more
# aliases here if another market's live response uses a different
# settlement-field name.
_SETTLE_ALIASES = {"SETTLE"}

_CANONICAL_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


def _normalize_columns(df: pd.DataFrame, *, settle_fallback_for_close: bool = False) -> pd.DataFrame:
    """Rename whatever columns LSEG returned to our canonical schema.

    Raises a clear, actionable error (listing the actual columns seen)
    if a required field can't be matched -- this is meant to be a fast
    fix the first time this runs against a live session, not a silent
    guess.

    settle_fallback_for_close: when True (DAILY interval only -- see
    _fetch_chunk), a "SETTLE" column, if present, row-wise fills any
    NaN remaining in canonical Close after the primary Close alias
    (TRDPRC_1/CLOSE/CLOSE_1) is coerced to numeric. It never overwrites
    an already-populated Close value -- markets whose primary Close
    source is fully populated (SOFR, Fed Funds) are unaffected even
    though they also carry a populated SETTLE column. Never applied for
    HOURLY/4H fetches -- SETTLE is a daily-only LSEG concept and
    intraday semantics are unchanged.
    """
    rename_map = {}
    upper_cols = {c: str(c).upper() for c in df.columns}

    for canonical, aliases in _COLUMN_ALIASES.items():
        match = next(
            (orig for orig, up in upper_cols.items() if up in aliases), None
        )
        if match is None:
            raise ValueError(
                f"Could not find a column for '{canonical}' in LSEG response. "
                f"Columns received: {list(df.columns)}. "
                f"Update _COLUMN_ALIASES in downloader.py to add the missing alias."
            )
        rename_map[match] = canonical

    settle_values = None
    if settle_fallback_for_close:
        settle_col = next(
            (orig for orig, up in upper_cols.items() if up in _SETTLE_ALIASES), None
        )
        if settle_col is not None:
            # Captured from the original (pre-rename/reset_index) frame,
            # whose row order is preserved unchanged all the way through
            # to the positional fillna below -- the chronological sort
            # only happens on the final return line.
            settle_values = pd.to_numeric(df[settle_col], errors="coerce").astype("float64").to_numpy()

    out = df.rename(columns=rename_map)
    out = out.reset_index().rename(columns={out.index.name or "index": "Date", "Date": "Date"})

    # After reset_index the timestamp column may be named "Date" already,
    # or it may be named after the index (commonly "Date" or "index" or
    # the RIC itself for single-instrument pulls). Handle robustly:
    if "Date" not in out.columns:
        first_col = out.columns[0]
        out = out.rename(columns={first_col: "Date"})

    out = out[_CANONICAL_COLUMNS]
    out["Date"] = pd.to_datetime(out["Date"])
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        # pd.to_numeric preserves a pandas nullable extension dtype (e.g.
        # "Float64") if the input already has one, which represents a
        # missing value as pd.NA rather than np.nan. LSEG can legitimately
        # return such a column for a thin intraday bar (e.g. an hourly
        # bucket with no trade printed) -- explicitly cast to plain numpy
        # float64 so every downstream consumer only ever sees np.nan for
        # a missing value, never pd.NA. The missing value itself is
        # preserved, not filled or dropped.
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")

    if settle_values is not None:
        out["Close"] = out["Close"].fillna(pd.Series(settle_values, index=out.index))

    return out.sort_values("Date").reset_index(drop=True)


# --------------------------------------------------------------------------
# Chunked fetching (LSEG limits how much intraday history you get per call)
# --------------------------------------------------------------------------

def _chunk_date_range(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Split [start, end] into consecutive chunks no longer than max_days."""
    chunks = []
    cur_start = start
    while cur_start <= end:
        cur_end = min(cur_start + timedelta(days=max_days), end)
        chunks.append((cur_start, cur_end))
        cur_start = cur_end + timedelta(days=1)
    return chunks


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    # A confirmed "universe is not found" or "no permission for this
    # universe" is a permanent, per-RIC condition -- retrying it 3x
    # with backoff can never succeed, so both are excluded here
    # (translated to MarketDataUnavailableError before this predicate
    # ever sees them, immediately below). Every other exception keeps
    # its existing retry behaviour unchanged.
    retry=retry_if_exception_type(Exception) & retry_if_not_exception_type(MarketDataUnavailableError),
)
def _fetch_chunk(ric: str, native_interval: str, start: date, end: date) -> pd.DataFrame:
    """Fetch a single chunk of history from LSEG, with retry on failure.

    A confirmed "universe is not found" or "no permission for this
    universe" LDError -- Interday (_is_confirmed_universe_not_found /
    _is_confirmed_no_permission) or Intraday
    (_is_confirmed_no_intraday_permission) -- is translated to
    MarketDataUnavailableError right here, before the @retry decorator
    above ever evaluates whether to retry it -- this is what lets the
    retry predicate exclude it cleanly rather than retrying a condition
    that can never succeed. Every other LDError (or any other exception)
    is left untranslated and IS retried by the decorator above.
    """
    import lseg.data as ld

    logger.debug("Fetching %s [%s -> %s] interval=%s", ric, start, end, native_interval)
    # Some instruments (e.g. SRAZ26) reject the generic OPEN/HIGH/LOW/
    # CLOSE/VOLUME field names outright. Request LSEG's own default field
    # set per instrument instead (fields=None) and let _normalize_columns'
    # alias table map whatever vendor-specific names come back.
    try:
        df = ld.get_history(
            universe=ric,
            interval=native_interval,
            start=start.isoformat(),
            end=end.isoformat(),
            fields=None,
        )
    except Exception as exc:
        if (
            _is_confirmed_universe_not_found(exc)
            or _is_confirmed_no_permission(exc)
            or _is_confirmed_no_intraday_permission(exc)
        ):
            raise MarketDataUnavailableError(ric, getattr(exc, "message", None) or str(exc)) from exc
        raise

    if df is None or df.empty:
        logger.warning("No data returned for %s [%s -> %s]", ric, start, end)
        return pd.DataFrame(columns=_CANONICAL_COLUMNS)

    # native_interval is "daily" only when the caller's top-level requested
    # BarInterval is DAILY (see config.LSEG_NATIVE_INTERVAL) -- FOUR_HOUR
    # also resolves to a native "hourly" fetch, so this correctly excludes
    # it too, unchanged.
    return _normalize_columns(df, settle_fallback_for_close=(native_interval == "daily"))


# --------------------------------------------------------------------------
# Resampling (used to synthesize 4H bars from hourly bars)
# --------------------------------------------------------------------------

def _resample_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate hourly OHLCV bars into 4-hour bars.

    Thin LSEG-side wrapper around the shared core.utils.resample_to_4h --
    see that function for the actual aggregation rules. Kept as a
    private, no-argument-beyond-df wrapper here so every existing call
    site/behavior in this module is unchanged.
    """
    return _shared_resample_to_4h(df, config.RESAMPLE_RULE[BarInterval.FOUR_HOUR])


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def download_history(
    ric: str,
    interval: str | BarInterval,
    start: DateLike,
    end: DateLike,
) -> pd.DataFrame:
    """Download historical OHLCV bars for a single RIC from LSEG.

    Args:
        ric: Instrument RIC, e.g. "SRAZ26".
        interval: "DAILY", "HOURLY", or "4H" (see config.BarInterval).
        start: Start date (inclusive), str "YYYY-MM-DD", date, or datetime.
        end: End date (inclusive), str "YYYY-MM-DD", date, or datetime.

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume.
        Empty DataFrame (with correct columns) if no data was available
        for the requested window -- callers should treat that as "no
        history", not as an error.

    Note:
        Works identically for DAILY / HOURLY / 4H -- callers never need
        to know that 4H bars are synthesized from hourly data under the
        hood.
    """
    if isinstance(interval, str):
        interval = BarInterval(interval)

    start_d = to_date(start)
    end_d = to_date(end)
    if start_d > end_d:
        raise ValueError(f"start ({start_d}) must be <= end ({end_d})")

    open_lseg_session()

    native_interval = config.LSEG_NATIVE_INTERVAL[interval]
    max_days = config.MAX_LOOKBACK_DAYS[interval]
    chunks = _chunk_date_range(start_d, end_d, max_days)

    logger.info(
        "Downloading %s interval=%s from %s to %s (%d chunk(s))",
        ric, interval.value, start_d, end_d, len(chunks),
    )

    frames = []
    for chunk_start, chunk_end in chunks:
        try:
            frame = _fetch_chunk(ric, native_interval, chunk_start, chunk_end)
            frames.append(frame)
        except Exception:
            logger.exception(
                "Failed to fetch %s [%s -> %s] after retries", ric, chunk_start, chunk_end
            )
            raise

    if not frames:
        return pd.DataFrame(columns=_CANONICAL_COLUMNS)

    result = pd.concat(frames, ignore_index=True)
    result = result.drop_duplicates(subset="Date").sort_values("Date").reset_index(drop=True)

    if interval == BarInterval.FOUR_HOUR:
        result = _resample_to_4h(result)

    logger.info("Downloaded %d bars for %s", len(result), ric)
    return result
