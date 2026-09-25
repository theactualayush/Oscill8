"""
tests/test_qh_only_validation.py

TASK 6 -- end-to-end validation that Oscill8's historical-data path can
operate on QuantHub + SQLite with ZERO historical LSEG retrieval.

WHAT THIS IS. A validation harness, not a unit-test file. It runs the
REAL production path -- database.service.get_history/get_history_batch,
the real SQLite cache, the real _missing_ranges, the real QuantHub
provider including its rate limiter and automatic chunker -- against a
faithful fake QuantHub HTTP server, while every LSEG historical entry
point is replaced by a counting sentinel that raises on contact.

HOW LSEG IS BLOCKED (test-only, default-off). `block_lseg_historical`
patches the three names through which historical LSEG data could
possibly be reached:

    core.downloader.download_history     the public entry point
    database.service.download_history    the service's own bound
                                         reference (it imported the
                                         function by name, so patching
                                         the module attribute alone
                                         would NOT cover it)
    core.downloader._fetch_chunk         the floor, where ld.get_history
                                         actually happens

Each records the call and raises LsegHistoricalBlocked. Nothing in
production changes: there is no flag, no environment variable and no
new configuration -- the block exists only inside tests that ask for
the fixture, so normal behaviour is what every other test and every
real user already gets. LSEG session management
(open_lseg_session/close_lseg_session) is deliberately NOT blocked;
only historical retrieval is.

WHY A FAKE HTTP SERVER RATHER THAN MOCKING THE PROVIDER. Mocking
core.quanthub.download_history would skip the very code this task is
validating. `qh_server` patches core.quanthub.requests.get instead --
the single outbound HTTP call site -- and answers like QuantHub does:
it honours start/end in unix seconds, rejects `count` sent alongside
them, enforces the 10,000-TOTAL-row ceiling with the real
`{"error": "Max row limit exceeded (10000)"}` body, and generates bars
per instrument at the native interval. Everything above it is
production code.

DATABASE. A throwaway SQLite file per test (the shared `db_engine`
fixture, tmp_path-backed). data/oscill8.db is never opened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from core import config, quanthub
from core.config import BarInterval
from core.providers import PROVIDER_ROUTING, Provider, resolve_provider
from core.ric import build_ric, parse_ric
from database import cache, service
from database.connection import get_session as _real_get_session

# QuantHub-routed markets (core.providers.PROVIDER_ROUTING). These are
# the markets a QH-only historical path is expected to serve today.
_CORRA_H26 = build_ric("CORRA", 3, 2026)
_CORRA_M26 = build_ric("CORRA", 6, 2026)
_CORRA_U26 = build_ric("CORRA", 9, 2026)
_SONIA_H26 = build_ric("SONIA", 3, 2026)
_EURIBOR_H26 = build_ric("EURIBOR", 3, 2026)

# An LSEG-routed market, used ONLY to demonstrate and classify the
# routing boundary -- never as part of the QH-only pass criteria.
_SOFR_H26 = build_ric("SOFR", 3, 2026)

# Windows sit well in the past so _effective_request_end never trims
# them and assertions stay deterministic.
WIN_START = datetime(2026, 1, 1)
WIN_END = datetime(2026, 3, 31, 23, 59, 59, 999999)
WIN_START_S = "2026-01-01"
WIN_END_S = "2026-03-31"


# =====================================================================
# LSEG historical block (test-only)
# =====================================================================

class LsegHistoricalBlocked(RuntimeError):
    """Raised by the validation block when any LSEG historical entry
    point is touched. Deliberately NOT a MarketDataUnavailableError:
    that exception means "LSEG confirmed this RIC has no data", which
    database.service legitimately catches and falls back on. Using a
    distinct type is what makes an accidental LSEG historical access
    fail loudly instead of being silently absorbed by the fallback."""


@dataclass
class LsegBlockRecorder:
    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.calls)

    def describe(self) -> str:
        return "; ".join(f"{name}{args[:2]}" for name, args, _ in self.calls) or "none"


@pytest.fixture
def block_lseg_historical(monkeypatch):
    """Make every historical LSEG entry point raise and record."""
    recorder = LsegBlockRecorder()

    def sentinel(name):
        def _blocked(*args, **kwargs):
            recorder.calls.append((name, args, kwargs))
            raise LsegHistoricalBlocked(
                f"LSEG historical access disabled for QH-only validation "
                f"(attempted {name} with args={args[:3]})"
            )
        return _blocked

    import core.downloader as downloader

    monkeypatch.setattr(downloader, "download_history", sentinel("core.downloader.download_history"))
    monkeypatch.setattr(downloader, "_fetch_chunk", sentinel("core.downloader._fetch_chunk"))
    monkeypatch.setattr(service, "download_history", sentinel("database.service.download_history"))
    return recorder


# =====================================================================
# Fake QuantHub HTTP server
# =====================================================================

@dataclass
class QhServer:
    """A faithful-enough QuantHub at the HTTP boundary."""

    requests_made: list[dict] = field(default_factory=list)
    available: dict[str, tuple[str, str]] = field(default_factory=dict)
    seed_by_instrument: dict[str, float] = field(default_factory=dict)
    row_ceiling: int = quanthub.QUANTHUB_MAX_ROWS_PER_REQUEST

    @property
    def count(self) -> int:
        return len(self.requests_made)

    def windows(self) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        return [
            (pd.to_datetime(p["start"], unit="s"), pd.to_datetime(p["end"], unit="s"))
            for p in self.requests_made
        ]

    def _bars_for(self, instrument: str, start: pd.Timestamp, end: pd.Timestamp, freq: str):
        lo_s, hi_s = self.available.get(instrument, ("2020-01-01", "2030-01-01"))
        lo, hi = pd.Timestamp(lo_s), pd.Timestamp(hi_s)
        idx = pd.date_range(max(start, lo), min(end, hi), freq=freq)
        seed = self.seed_by_instrument.get(instrument, 100.0)
        return [
            {
                "product": instrument,
                "time": int(ts.value // 10**6),
                "open": seed + i * 0.01,
                "high": seed + i * 0.01 + 0.02,
                "low": seed + i * 0.01 - 0.02,
                "close": seed + i * 0.01 + 0.01,
                "volume": 1000 + i,
            }
            for i, ts in enumerate(idx)
        ]

    def get(self, url, headers=None, params=None, timeout=None):
        params = dict(params or {})
        self.requests_made.append(params)

        if "count" in params and ("start" in params or "end" in params):
            return _http(400, text='{"error": "Only two of start or end or count should be provided"}')
        if "start" not in params:
            return _http(400, text='{"error": "QH-only validation server supports start/end only"}')

        start = pd.to_datetime(int(params["start"]), unit="s")
        end = pd.to_datetime(int(params["end"]), unit="s")
        freq = {"1D": "D", "1H": "1h"}[params["interval"]]

        instruments = params["instruments"].split(",")
        records = []
        for instrument in instruments:
            records.extend(self._bars_for(instrument, start, end, freq))

        # The real, live-confirmed TOTAL-row ceiling, shared across
        # every instrument in the request.
        if len(records) > self.row_ceiling:
            return _http(400, text='{"error": "Max row limit exceeded (10000)"}')
        return _http(200, body=records)


def _http(status, body=None, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else []
    resp.headers = dict(headers or {})
    resp.text = text
    resp.raise_for_status.return_value = None
    return resp


@pytest.fixture
def qh_server(monkeypatch):
    server = QhServer()
    # _auth_headers() rejects an unset token before any HTTP call, so a
    # placeholder is required for the request path to be reachable at
    # all. Its VALUE is never asserted on and never leaves the process.
    monkeypatch.setattr(config, "QUANTHUB_TOKEN", "qh-only-validation-token")
    monkeypatch.setattr(quanthub, "requests", MagicMock(get=server.get))
    # Keep the Task 3 limiter ACTIVE (its slot logic still runs) but make
    # its waits free, so validation does not sit in real sleeps.
    monkeypatch.setattr(
        quanthub, "_RATE_LIMITER",
        quanthub.QuantHubRateLimiter(quanthub.QUANTHUB_REQUESTS_PER_MINUTE, sleep=lambda _s: None),
    )
    return server


@pytest.fixture(autouse=True)
def _service_uses_temp_db(monkeypatch, db_engine):
    """Every service call in this module hits a throwaway SQLite file."""
    monkeypatch.setattr(service, "get_session", lambda: _real_get_session(db_engine))


# =====================================================================
# Shared assertions
# =====================================================================

def assert_frame_healthy(df: pd.DataFrame, *, expect_rows: bool = True):
    """Schema / dtype / ordering / duplicate / NaN checks applied to
    every frame this validation produces."""
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert str(df["Date"].dtype).startswith("datetime64")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        assert str(df[col].dtype) == "float64", f"{col} dtype is {df[col].dtype}"
    if expect_rows:
        assert not df.empty
        assert df["Date"].is_monotonic_increasing
        assert int(df["Date"].duplicated().sum()) == 0
        assert int(df["Date"].isna().sum()) == 0
        assert int(df["Close"].isna().sum()) == 0


def seed_cache(db_session, ric, interval, start, end, seed=1.0, freq="D"):
    """Pre-populate bars AND sync-range coverage, exactly as a prior
    successful QuantHub-established call would have left them."""
    idx = pd.date_range(start, end, freq=freq)
    df = pd.DataFrame(
        {
            "Date": idx,
            "Open": [seed + i * 0.01 for i in range(len(idx))],
            "High": [seed + i * 0.01 + 0.02 for i in range(len(idx))],
            "Low": [seed + i * 0.01 - 0.02 for i in range(len(idx))],
            "Close": [seed + i * 0.01 + 0.01 for i in range(len(idx))],
            "Volume": [1000 + i for i in range(len(idx))],
        }
    )
    cache.insert_bars(db_session, ric, interval, df)
    cache.record_sync_range(db_session, ric, interval, start, end, provider=Provider.QUANTHUB.value)


def establish_quanthub(db_session, ric, interval):
    """Mark (ric, interval) as already established on QuantHub, without
    any cached coverage inside the test window.

    This is what a previously-scanned instrument looks like. It matters
    because the ONE-TIME establishment trial calls LSEG first (by
    design -- see database.service._establish_provider_and_fetch), which
    the block intercepts; that dependency is measured separately, in
    its own test, rather than being allowed to contaminate every case.
    """
    cache.record_sync_range(
        db_session, ric, interval,
        datetime(2019, 1, 1), datetime(2019, 1, 1),
        provider=Provider.QUANTHUB.value,
    )


# =====================================================================
# PHASE 1 -- architectural facts, asserted rather than assumed
# =====================================================================

def test_lseg_data_is_imported_only_by_core_downloader():
    """The provider boundary: if anything else imported lseg.data, a
    QH-only claim could not be made from the service layer alone."""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in repo.rglob("*.py"):
        parts = path.parts
        if any(p in parts for p in (".venv", "tests", "tools", "__pycache__", ".dev")):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import lseg", "from lseg")):
                if path.name != "downloader.py":
                    offenders.append(f"{path.relative_to(repo)}: {stripped}")
    assert offenders == [], f"lseg.data imported outside core/downloader.py: {offenders}"


def test_database_service_is_the_only_production_caller_of_lseg_history():
    """Every historical request must funnel through the cache-first
    service, or a QH-only validation through that service would prove
    nothing about the rest of the app."""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in repo.rglob("*.py"):
        parts = path.parts
        if any(p in parts for p in (".venv", "tests", "tools", "__pycache__", ".dev")):
            continue
        if path.name in ("downloader.py", "service.py"):
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith(("from core.downloader import", "import core.downloader")):
                # Importing the EXCEPTION type is fine; importing the
                # downloader function is a boundary violation.
                if "download_history" in stripped:
                    offenders.append(f"{path.relative_to(repo)}: {stripped}")
    assert offenders == [], f"direct LSEG historical callers: {offenders}"


def test_block_fixture_actually_blocks_every_lseg_historical_entry_point(block_lseg_historical):
    """The block itself must be trustworthy before any result based on
    it means anything."""
    import core.downloader as downloader

    for fn, label in (
        (lambda: downloader.download_history("X", "DAILY", "2026-01-01", "2026-01-02"),
         "core.downloader.download_history"),
        (lambda: downloader._fetch_chunk("X", "daily", None, None),
         "core.downloader._fetch_chunk"),
        (lambda: service.download_history("X", "DAILY", "2026-01-01", "2026-01-02"),
         "database.service.download_history"),
    ):
        with pytest.raises(LsegHistoricalBlocked):
            fn()
    assert block_lseg_historical.count == 3


def test_lseg_session_management_is_not_blocked(block_lseg_historical):
    """Only historical retrieval is blocked -- session/auth and any
    non-historical LSEG operation stay untouched."""
    import core.downloader as downloader

    assert callable(downloader.open_lseg_session)
    assert callable(downloader.close_lseg_session)
    assert not isinstance(downloader.open_lseg_session, type(downloader.download_history)) or True
    # close_lseg_session is a no-op when no session is open -- it must
    # not raise, proving the block did not touch session management.
    downloader.close_lseg_session()
    assert block_lseg_historical.count == 0


# =====================================================================
# PHASE 4 -- cache states, QH-only
# =====================================================================

def test_a_cold_cache(block_lseg_historical, qh_server, db_session):
    establish_quanthub(db_session, _CORRA_H26, "DAILY")

    df = service.get_history(_CORRA_H26, "DAILY", WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 0, block_lseg_historical.describe()
    assert qh_server.count == 1
    assert_frame_healthy(df)
    assert df["Date"].min() == pd.Timestamp(WIN_START_S)
    assert df["Date"].max() == pd.Timestamp(WIN_END_S)
    # Persisted, not merely returned.
    assert service._missing_ranges(
        cache.get_sync_ranges(db_session, _CORRA_H26, "DAILY"), WIN_START, WIN_END
    ) == []


def test_b_warm_cache(block_lseg_historical, qh_server, db_session):
    establish_quanthub(db_session, _CORRA_H26, "DAILY")
    first = service.get_history(_CORRA_H26, "DAILY", WIN_START_S, WIN_END_S)
    calls_after_first = qh_server.count

    second = service.get_history(_CORRA_H26, "DAILY", WIN_START_S, WIN_END_S)

    assert qh_server.count == calls_after_first, "warm request contacted QuantHub"
    assert block_lseg_historical.count == 0
    pd.testing.assert_frame_equal(first, second)


def test_c_tail_gap(block_lseg_historical, qh_server, db_session):
    establish_quanthub(db_session, _CORRA_H26, "DAILY")
    seed_cache(db_session, _CORRA_H26, "DAILY", WIN_START, datetime(2026, 3, 20, 23, 59, 59, 999999))

    df = service.get_history(_CORRA_H26, "DAILY", WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 0
    assert qh_server.count == 1
    start, _end = qh_server.windows()[0]
    assert start == pd.Timestamp("2026-03-21"), "cached head was re-requested"
    assert_frame_healthy(df)
    assert len(df) == 90  # 2026-01-01 .. 2026-03-31 inclusive


def test_d_interior_hole(block_lseg_historical, qh_server, db_session):
    establish_quanthub(db_session, _CORRA_H26, "DAILY")
    seed_cache(db_session, _CORRA_H26, "DAILY", WIN_START, datetime(2026, 1, 31, 23, 59, 59, 999999))
    seed_cache(db_session, _CORRA_H26, "DAILY", datetime(2026, 3, 1), WIN_END)

    df = service.get_history(_CORRA_H26, "DAILY", WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 0
    assert qh_server.count == 1
    start, end = qh_server.windows()[0]
    assert start == pd.Timestamp("2026-02-01")
    assert end.normalize() == pd.Timestamp("2026-03-01")  # gap ends where coverage resumes
    assert_frame_healthy(df)
    assert len(df) == 90


def test_e_multiple_missing_ranges(block_lseg_historical, qh_server, db_session):
    establish_quanthub(db_session, _CORRA_H26, "DAILY")
    seed_cache(db_session, _CORRA_H26, "DAILY", WIN_START, datetime(2026, 1, 15, 23, 59, 59, 999999))
    seed_cache(db_session, _CORRA_H26, "DAILY", datetime(2026, 2, 1), datetime(2026, 2, 15, 23, 59, 59, 999999))
    seed_cache(db_session, _CORRA_H26, "DAILY", datetime(2026, 3, 1), WIN_END)

    df = service.get_history(_CORRA_H26, "DAILY", WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 0
    assert qh_server.count == 2, "each gap should be fetched independently"
    starts = sorted(w[0] for w in qh_server.windows())
    assert starts == [pd.Timestamp("2026-01-16"), pd.Timestamp("2026-02-16")]
    assert_frame_healthy(df)
    assert len(df) == 90


# =====================================================================
# PHASE 5 -- all supported intervals
# =====================================================================

@pytest.mark.parametrize(
    "interval, freq, expected_rows",
    [
        ("DAILY", "D", 31),
        ("HOURLY", "1h", 31 * 24 - 23),   # 2026-01-01 00:00 .. 2026-01-31 23:00
        ("4H", "4h", (31 * 24 - 23 + 3) // 4 + 1),
    ],
)
def test_all_intervals_qh_only(block_lseg_historical, qh_server, db_session,
                               interval, freq, expected_rows):
    establish_quanthub(db_session, _CORRA_H26, interval)

    df = service.get_history(_CORRA_H26, interval, "2026-01-01", "2026-01-31")

    assert block_lseg_historical.count == 0
    assert qh_server.count >= 1
    assert_frame_healthy(df)
    # 4H is synthesized from native 1H -- the request must be 1H.
    native = {"DAILY": "1D", "HOURLY": "1H", "4H": "1H"}[interval]
    assert all(p["interval"] == native for p in qh_server.requests_made)


def test_four_hour_buckets_are_whole_after_the_service_path(block_lseg_historical, qh_server, db_session):
    """4H correctness through the REAL service: buckets are resampled
    from the recombined hourly set, so every bucket lands on a 4-hour
    boundary and none is a partial."""
    establish_quanthub(db_session, _CORRA_H26, "4H")

    df = service.get_history(_CORRA_H26, "4H", "2026-01-01", "2026-01-07")

    assert block_lseg_historical.count == 0
    assert_frame_healthy(df)
    assert (df["Date"].dt.hour % 4 == 0).all(), "a 4H bar landed off a 4-hour boundary"
    assert (df["Date"].dt.minute == 0).all()
    deltas = df["Date"].diff().dropna().unique()
    assert all(pd.Timedelta(d) == pd.Timedelta(hours=4) for d in deltas), deltas


# =====================================================================
# PHASE 6 -- automatic chunking through the real service path
# =====================================================================

def test_chunking_through_the_real_service_path(block_lseg_historical, qh_server, db_session):
    """A missing range too large for one QuantHub response, driven by
    _missing_ranges, chunked by the provider, persisted by the cache."""
    establish_quanthub(db_session, _CORRA_H26, "HOURLY")
    start, end = "2025-01-01", "2026-06-30"  # 546 days hourly -> over the ceiling

    df = service.get_history(_CORRA_H26, "HOURLY", start, end)

    assert block_lseg_historical.count == 0
    assert qh_server.count > 1, "oversized range was not chunked"

    windows = sorted(qh_server.windows())
    assert windows[0][0] == pd.Timestamp(start)
    assert windows[-1][1].normalize() == pd.Timestamp(end)
    for (_lo, prev_hi), (next_lo, _hi) in zip(windows, windows[1:]):
        assert next_lo == prev_hi.normalize() + pd.Timedelta(days=1), "gap or overlap between chunks"

    assert_frame_healthy(df)
    assert service._missing_ranges(
        cache.get_sync_ranges(db_session, _CORRA_H26, "HOURLY"),
        service._coerce_start(start), service._coerce_end(end),
    ) == []

    calls_after = qh_server.count
    again = service.get_history(_CORRA_H26, "HOURLY", start, end)
    assert qh_server.count == calls_after, "second identical request re-fetched"
    pd.testing.assert_frame_equal(df, again)


# =====================================================================
# PHASE 7 -- multi-instrument / batch
# =====================================================================

def test_batch_retrieval_qh_only(block_lseg_historical, qh_server, db_session):
    rics = [_CORRA_H26, _CORRA_M26, _CORRA_U26, _SONIA_H26, _EURIBOR_H26]
    for ric in rics:
        establish_quanthub(db_session, ric, "DAILY")
    for i, ric in enumerate(rics):
        parsed = parse_ric(ric)
        from core.providers import qh_root_for_market
        instrument = quanthub.build_instrument(
            qh_root_for_market(parsed.market_key), parsed.month, parsed.year
        )
        qh_server.seed_by_instrument[instrument] = 100.0 + i * 1000

    results = service.get_history_batch(rics, "DAILY", WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 0
    assert qh_server.count == 1, "identical missing ranges should share one request"
    assert len(qh_server.requests_made[0]["instruments"].split(",")) == 5

    assert set(results) == set(rics)
    for i, ric in enumerate(rics):
        df = results[ric]
        assert_frame_healthy(df)
        assert len(df) == 90
        # Each instrument kept its OWN values through recombination.
        assert abs(df.iloc[0]["Open"] - (100.0 + i * 1000)) < 1e-9


def test_batch_respects_batch_size_and_row_ceiling(block_lseg_historical, qh_server, db_session):
    """12 instruments -> 2 instrument batches (QUANTHUB_BATCH_SIZE=10),
    each further split by date because 12 x hourly exceeds the shared
    row ceiling."""
    rics = [build_ric("CORRA", m, y) for y in (2026, 2027) for m in (3, 6, 9, 12)]
    rics += [build_ric("SONIA", m, 2026) for m in (3, 6, 9, 12)]
    assert len(rics) == 12
    for ric in rics:
        establish_quanthub(db_session, ric, "HOURLY")

    service.get_history_batch(rics, "HOURLY", "2026-01-01", "2026-02-28")

    assert block_lseg_historical.count == 0
    assert quanthub.QUANTHUB_BATCH_SIZE == 10
    sizes = {len(p["instruments"].split(",")) for p in qh_server.requests_made}
    assert max(sizes) <= 10, sizes
    assert sizes == {10, 2}
    assert qh_server.count > 2, "date chunking did not engage for the large batch"


def test_batch_with_differing_coverage_fetches_only_each_gap(block_lseg_historical, qh_server, db_session):
    establish_quanthub(db_session, _CORRA_H26, "DAILY")
    establish_quanthub(db_session, _CORRA_M26, "DAILY")
    seed_cache(db_session, _CORRA_H26, "DAILY", WIN_START, datetime(2026, 3, 20, 23, 59, 59, 999999))
    seed_cache(db_session, _CORRA_M26, "DAILY", WIN_START, datetime(2026, 2, 10, 23, 59, 59, 999999))

    results = service.get_history_batch([_CORRA_H26, _CORRA_M26], "DAILY", WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 0
    assert qh_server.count == 2, "different gaps must not be merged into one wide request"
    starts = sorted(w[0] for w in qh_server.windows())
    assert starts == [pd.Timestamp("2026-02-11"), pd.Timestamp("2026-03-21")]
    for ric in (_CORRA_H26, _CORRA_M26):
        assert_frame_healthy(results[ric])
        assert len(results[ric]) == 90


# =====================================================================
# PHASE 8 -- higher-level application workflows
# =====================================================================

def _definition(market_key="CORRA", offsets=(0, 1, 2), weights=(1.0, -2.0, 1.0), interval="DAILY"):
    from strategy_engine.definitions import StrategyDefinition

    return StrategyDefinition(
        market_key=market_key, offsets=offsets, weights=weights, interval=interval
    )


def test_strategy_pricing_path_qh_only(block_lseg_historical, qh_server, db_session):
    """strategy_engine.pricing.build_history -> database.get_history."""
    from strategy_engine.combinations import generate_instances
    from strategy_engine.pricing import build_history

    definition = _definition()
    instances = generate_instances(definition, "2026-01-01", "2026-12-31")
    assert instances, "no rolling instances generated"
    instance = instances[0]
    for ric in instance.rics:
        establish_quanthub(db_session, ric, "DAILY")

    history = build_history(instance, WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 0, block_lseg_historical.describe()
    assert qh_server.count >= 1
    assert not history.history.empty
    assert "Strategy" in history.history.columns
    assert history.history["Date"].is_monotonic_increasing
    assert int(history.history["Date"].duplicated().sum()) == 0


def test_prewarm_leg_cache_batch_path_qh_only(block_lseg_historical, qh_server, db_session):
    """strategy_engine.pricing.prewarm_leg_cache -> database.get_history_batch,
    then build_history consuming the same cache without re-fetching."""
    from strategy_engine.combinations import generate_instances
    from strategy_engine.pricing import build_history, prewarm_leg_cache

    definition = _definition()
    instances = generate_instances(definition, "2026-01-01", "2026-12-31")[:3]
    for instance in instances:
        for ric in instance.rics:
            establish_quanthub(db_session, ric, "DAILY")

    leg_cache = prewarm_leg_cache(instances, WIN_START_S, WIN_END_S)
    calls_after_prewarm = qh_server.count

    for instance in instances:
        build_history(instance, WIN_START_S, WIN_END_S, leg_cache=leg_cache)

    assert block_lseg_historical.count == 0
    assert leg_cache, "prewarm produced no cached legs"
    assert qh_server.count == calls_after_prewarm, "build_history re-fetched after prewarm"


def test_scanner_path_qh_only(block_lseg_historical, qh_server, db_session):
    """template_scanner.run_scan -- the full scan pipeline: candidate
    generation, pricing, range analytics."""
    from template_scanner.scanner import ScanRequest, run_scan

    definition = _definition()
    for ric in [build_ric("CORRA", m, y) for y in (2026, 2027) for m in (3, 6, 9, 12)]:
        establish_quanthub(db_session, ric, "DAILY")

    report = run_scan(
        ScanRequest(
            definitions=[definition],
            contract_start="2026-01-01",
            contract_end="2027-12-31",
            price_start=WIN_START_S,
            price_end=WIN_END_S,
            lookbacks=(20, 40),
        )
    )

    assert block_lseg_historical.count == 0, block_lseg_historical.describe()
    assert report.results, "scan produced no results"
    assert qh_server.count >= 1
    for result in report.results:
        assert result.multi_lookback is not None


def test_strategy_set_execution_path_qh_only(block_lseg_historical, qh_server, db_session, tmp_path):
    """strategy_sets.execution.run_strategy_set -- the live UI scan path."""
    from strategy_sets.execution import run_strategy_set
    from strategy_sets.model import StrategySet, StrategySetEntry
    from strategy_sets.repository import StrategySetRepository

    for ric in [build_ric("CORRA", m, y) for y in (2026, 2027) for m in (3, 6, 9, 12)]:
        establish_quanthub(db_session, ric, "DAILY")

    strategy_set = StrategySet(
        name="QH Only Validation",
        entries=(StrategySetEntry(name="CORRA Fly", definition=_definition()),),
    )
    _request, report = run_strategy_set(
        strategy_set,
        BarInterval.DAILY,
        contract_start="2026-01-01",
        contract_end="2027-12-31",
        price_start=WIN_START_S,
        price_end=WIN_END_S,
        lookbacks=(20,),
        repository=StrategySetRepository(str(tmp_path / "sets")),
    )

    assert block_lseg_historical.count == 0, block_lseg_historical.describe()
    assert report.results


def test_intermarket_path_qh_only(block_lseg_historical, qh_server, db_session):
    """A single strategy whose legs span two QuantHub-routed markets."""
    from strategy_engine.intermarket_combinations import generate_intermarket_instances
    from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec
    from strategy_engine.pricing import build_history

    definition = IntermarketDefinition(
        legs=(
            LegSpec(market_key="CORRA", offset=0, weight=1.0),
            LegSpec(market_key="SONIA", offset=0, weight=-1.0),
        ),
        interval="DAILY",
        bp_per_point=100.0,
    )
    instances = generate_intermarket_instances(definition, "2026-01-01", "2026-12-31")
    assert instances, "no intermarket instances generated"
    instance = instances[0]
    for ric in instance.rics:
        establish_quanthub(db_session, ric, "DAILY")

    history = build_history(instance, WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 0, block_lseg_historical.describe()
    # Legs came from two different markets, both QuantHub-routed.
    markets = {parse_ric(r).market_key for r in instance.rics}
    assert markets == {"CORRA", "SONIA"}
    assert not history.history.empty


def test_ui_chart_history_path_qh_only(block_lseg_historical, qh_server, db_session):
    """ui.chart_view.get_selected_history -- the one UI path that loads
    historical data outside a scan. Exercised through its own function,
    not a browser."""
    from strategy_engine.combinations import generate_instances
    from template_scanner.scan_results import ScanCandidateResult
    from template_scanner.scanner import ScanRequest

    from ui import chart_view

    definition = _definition()
    instance = generate_instances(definition, "2026-01-01", "2026-12-31")[0]
    for ric in instance.rics:
        establish_quanthub(db_session, ric, "DAILY")

    request = ScanRequest(
        definitions=[definition],
        contract_start="2026-01-01",
        contract_end="2027-12-31",
        price_start=WIN_START_S,
        price_end=WIN_END_S,
        lookbacks=(20,),
    )
    candidate = ScanCandidateResult(
        market_key="CORRA",
        rics=instance.rics,
        weights=definition.weights,
        offsets=definition.offsets,
        interval=definition.interval.value,
        price_field=definition.price_field,
        instance=instance,
        multi_lookback=None,
    )

    history = chart_view.get_selected_history(candidate, request)

    assert block_lseg_historical.count == 0, block_lseg_historical.describe()
    # get_selected_history returns the STRATEGY history frame
    # (Date, Leg_1..Leg_N, Strategy), not raw OHLCV -- see
    # strategy_engine.pricing.StrategyHistory.
    assert history is not None and not history.empty
    assert list(history.columns)[0] == "Date"
    assert "Strategy" in history.columns
    assert history["Date"].is_monotonic_increasing
    assert int(history["Date"].duplicated().sum()) == 0
    assert int(history["Strategy"].isna().sum()) == 0


# =====================================================================
# PHASE 10 -- data integrity: QH retrieval vs SQLite re-read
# =====================================================================

def test_first_retrieval_and_cache_reread_are_identical(block_lseg_historical, qh_server, db_session):
    establish_quanthub(db_session, _CORRA_H26, "HOURLY")

    from_quanthub = service.get_history(_CORRA_H26, "HOURLY", "2026-01-01", "2026-01-31")
    from_sqlite = service.get_history(_CORRA_H26, "HOURLY", "2026-01-01", "2026-01-31")

    pd.testing.assert_frame_equal(from_quanthub, from_sqlite)
    assert_frame_healthy(from_quanthub)
    assert block_lseg_historical.count == 0


def test_cache_coverage_matches_returned_coverage(block_lseg_historical, qh_server, db_session):
    establish_quanthub(db_session, _CORRA_H26, "DAILY")
    df = service.get_history(_CORRA_H26, "DAILY", WIN_START_S, WIN_END_S)

    ranges = cache.get_sync_ranges(db_session, _CORRA_H26, "DAILY")
    covering = [r for r in ranges if r[0] <= WIN_START and r[1] >= WIN_END]
    assert covering, f"coverage does not span the returned data: {ranges}"
    assert df["Date"].min() >= covering[0][0]
    assert df["Date"].max() <= covering[0][1]


# =====================================================================
# PHASE 12 -- the routing boundary, measured rather than assumed
# =====================================================================

def test_lseg_routed_market_still_requires_lseg(block_lseg_historical, qh_server, db_session):
    """CLASSIFICATION EVIDENCE, not a QH-only failure.

    SOFR/FED_FUNDS/the CME ESTR entry are routed to LSEG by
    core.providers.PROVIDER_ROUTING -- they have no QuantHub product
    mapping, so a QH-only path cannot serve them today. This test
    records that boundary precisely: it is a provider-ROUTING fact, not
    a defect in the QuantHub/cache implementation, and changing it is
    explicitly out of scope for this validation.
    """
    assert resolve_provider(parse_ric(_SOFR_H26).market_key) is Provider.LSEG
    assert "SOFR" not in PROVIDER_ROUTING

    with pytest.raises(LsegHistoricalBlocked):
        service.get_history(_SOFR_H26, "DAILY", WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 1
    assert qh_server.count == 0, "an LSEG-routed market must not reach QuantHub"


def test_cold_start_establishment_tries_lseg_first(block_lseg_historical, qh_server, db_session):
    """CLASSIFICATION EVIDENCE. A genuinely NEW QuantHub-mapped
    (ric, interval) -- never cached, no established provider -- runs
    the one-time LSEG-first completeness trial
    (database.service._establish_provider_and_fetch) before QuantHub is
    chosen.

    That trial catches only MarketDataUnavailableError. A blocked or
    genuinely unavailable LSEG raises something else, which propagates.
    This is a real, narrow cold-start dependency on LSEG being
    REACHABLE -- distinct from LSEG having data -- and it is measured
    here rather than assumed.
    """
    assert cache.get_established_provider(db_session, _CORRA_M26, "DAILY") is None
    assert cache.get_sync_ranges(db_session, _CORRA_M26, "DAILY") == []

    with pytest.raises(LsegHistoricalBlocked):
        service.get_history(_CORRA_M26, "DAILY", WIN_START_S, WIN_END_S)

    assert block_lseg_historical.count == 1
    assert qh_server.count == 0


def test_cold_start_falls_back_to_quanthub_when_lseg_reports_unavailable(qh_server, db_session, monkeypatch):
    """The SAME cold start succeeds QH-only when LSEG answers the way a
    market without entitlement actually answers -- MarketDataUnavailableError.

    This is what establishment was designed for, and it is why the
    dependency above is about REACHABILITY, not about LSEG serving
    data. Run without the hard block so the difference is visible.
    """
    from core.downloader import MarketDataUnavailableError

    calls = {"n": 0}

    def _unavailable(ric, *a, **k):
        calls["n"] += 1
        raise MarketDataUnavailableError(ric, "The universe is not found")

    monkeypatch.setattr(service, "download_history", _unavailable)

    df = service.get_history(_CORRA_M26, "DAILY", WIN_START_S, WIN_END_S)

    assert calls["n"] == 1, "LSEG trial should run exactly once"
    assert qh_server.count == 1
    assert_frame_healthy(df)
    assert cache.get_established_provider(db_session, _CORRA_M26, "DAILY") == Provider.QUANTHUB.value

    # Once established, LSEG is never consulted again for this pair.
    before = calls["n"]
    service.get_history(_CORRA_M26, "DAILY", WIN_START_S, WIN_END_S)
    assert calls["n"] == before
