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
    stop_after_attempt,
    wait_exponential,
)

from core import config
from core.config import BarInterval
from core.utils import get_logger, to_date, DateLike

logger = get_logger(__name__)

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
            ld.open_session(name=config.LSEG_SESSION_TYPE, app_key=config.LSEG_APP_KEY)
        else:
            ld.open_session(name=config.LSEG_SESSION_TYPE)
        _session_open = True
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
# names depending on asset class / interval / library version. This map
# covers the common variants and normalizes them to our canonical
# schema: Date, Open, High, Low, Close, Volume.
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
        out[col] = pd.to_numeric(out[col], errors="coerce")

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
    retry=retry_if_exception_type(Exception),
)
def _fetch_chunk(ric: str, native_interval: str, start: date, end: date) -> pd.DataFrame:
    """Fetch a single chunk of history from LSEG, with retry on failure."""
    import lseg.data as ld

    logger.debug("Fetching %s [%s -> %s] interval=%s", ric, start, end, native_interval)
    df = ld.get_history(
        universe=ric,
        interval=native_interval,
        start=start.isoformat(),
        end=end.isoformat(),
        fields=["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"],
    )
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
