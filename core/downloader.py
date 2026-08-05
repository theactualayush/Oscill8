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

logger = get_logger(__name__)


class MarketDataUnavailableError(Exception):
    """Raised when LSEG has confirmed a requested RIC has no market data
    available at all (TS.Interday.UserRequestError.70005, "The universe
    is not found") -- a permanent, per-RIC condition.

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

_CANONICAL_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename whatever columns LSEG returned to our canonical schema.

    Raises a clear, actionable error (listing the actual columns seen)
    if a required field can't be matched -- this is meant to be a fast
    fix the first time this runs against a live session, not a silent
    guess.
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
    # A confirmed "universe is not found" is a permanent, per-RIC
    # condition -- retrying it 3x with backoff can never succeed, so it
    # is excluded here (translated to MarketDataUnavailableError before
    # this predicate ever sees it, immediately below). Every other
    # exception keeps its existing retry behaviour unchanged.
    retry=retry_if_exception_type(Exception) & retry_if_not_exception_type(MarketDataUnavailableError),
)
def _fetch_chunk(ric: str, native_interval: str, start: date, end: date) -> pd.DataFrame:
    """Fetch a single chunk of history from LSEG, with retry on failure.

    A confirmed "universe is not found" LDError (see
    _is_confirmed_universe_not_found) is translated to
    MarketDataUnavailableError right here, before the @retry decorator
    above ever evaluates whether to retry it -- this is what lets the
    retry predicate exclude it cleanly rather than retrying a condition
    that can never succeed.
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
        if _is_confirmed_universe_not_found(exc):
            raise MarketDataUnavailableError(ric, getattr(exc, "message", None) or str(exc)) from exc
        raise

    if df is None or df.empty:
        logger.warning("No data returned for %s [%s -> %s]", ric, start, end)
        return pd.DataFrame(columns=_CANONICAL_COLUMNS)

    return _normalize_columns(df)


# --------------------------------------------------------------------------
# Resampling (used to synthesize 4H bars from hourly bars)
# --------------------------------------------------------------------------

def _resample_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate hourly OHLCV bars into 4-hour bars.

    Standard OHLCV resampling rules:
        Open   = first
        High   = max
        Low    = min
        Close  = last
        Volume = sum
    """
    if df.empty:
        return df

    indexed = df.set_index("Date")
    agg = indexed.resample(config.RESAMPLE_RULE[BarInterval.FOUR_HOUR]).agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    agg = agg.dropna(subset=["Open", "High", "Low", "Close"], how="all")
    return agg.reset_index()[_CANONICAL_COLUMNS]


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
