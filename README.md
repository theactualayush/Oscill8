# Range Bound Strategy Scanner — Module 1: LSEG Downloader

## Status: Built & unit-tested (32/32 passing). Live LSEG connectivity NOT yet verified — run `test_live_connection.py` on a machine with Workspace open.

## Files in this module
- `config.py`     — market registry (RICs), interval definitions, DB/session settings (pure data, no logic)
- `ric.py`        — RIC construction (`build_ric`) and parsing (`parse_ric`)
- `utils.py`      — logging + date helpers
- `downloader.py` — `download_history(ric, interval, start, end)` -> DataFrame
- `tests/test_downloader.py` — unit tests (LSEG mocked)
- `tests/test_ric.py` — unit tests for RIC build/parse, incl. round-trip tests
- `test_live_connection.py` — **run this manually** against live Workspace (not pytest)

## Changes since last review
1. **SOFR RIC root fixed**: `SR3` → `SRA` (confirmed via your Workspace).
2. **Fed Funds flagged for re-verification**: `verified=False` until you confirm the root the same way SOFR was confirmed — don't trust `FF` yet.
3. **`ric.py` added**: `build_ric()` moved out of `config.py` (which now stays pure data), plus new `parse_ric()` for going RIC → (market, month, year). Handles both 1-digit and 2-digit year conventions, with sensible near-term disambiguation for 1-digit years.
4. **`test_live_connection.py` added**: run it directly (`python test_live_connection.py`) on your machine. It opens a real session, builds a near-term quarterly RIC for each *verified* market, pulls DAILY/HOURLY/4H bars, and prints PASS/FAIL/WARN per market+interval. Unverified markets (currently Fed Funds, SONIA, CORRA, €STR) are automatically skipped with a clear message rather than tested against a guessed RIC.

## What's verified (via mocked/unit tests, no live session needed)
- Session open/close is idempotent
- Column normalization handles multiple LSEG field-name variants, fails loudly with actual columns received if none match
- Date range chunking for intraday pulls is gapless and non-overlapping
- Retry logic (3 attempts, exponential backoff)
- 4H bars correctly synthesized from hourly bars (O=first, H=max, L=min, C=last, V=sum)
- `build_ric` / `parse_ric` round-trip correctly for both 1- and 2-digit-year markets

## What YOU still need to do
1. **Run `test_live_connection.py`** with Workspace open — confirms real connectivity end to end.
2. **Confirm the Fed Funds RIC root** the same way you confirmed SOFR (`SRA`), then flip `verified=True` in `config.py`.
3. **Verify SONIA / CORRA / €STR RIC roots** via `ld.discovery.search(...)` or chain search, same process.
4. If `_normalize_columns` raises a `ValueError` naming unmatched columns, add the alias to `_COLUMN_ALIASES` in `downloader.py` — one-line fix.

## Run tests
```
pip install -r requirements.txt
pytest tests/ -v                    # unit tests, no live session needed
python test_live_connection.py      # live smoke test, needs Workspace open
```

