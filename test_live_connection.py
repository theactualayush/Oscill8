"""
test_live_connection.py

Standalone script (NOT a pytest file) to run manually on a machine with
LSEG Workspace open. Verifies that:
    1. A live session can actually be opened.
    2. Each verified market in the registry returns real data for a
       recent date range, for all three intervals.
    3. The column-normalization logic in downloader.py matches what
       your live LSEG feed actually returns (if it doesn't, you'll get
       a clear ValueError telling you which alias to add).

Run it directly:
    python test_live_connection.py

Exit code is 0 if everything passed, 1 if anything failed -- so it's
safe to wire into a CI/scheduled health check later if useful.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

import config
import ric
from downloader import close_lseg_session, download_history, open_lseg_session
from utils import get_logger

logger = get_logger(__name__)

# Small, recent window -- enough to confirm real bars come back without
# hammering the API. Adjust if you want a longer sanity check.
END_DATE = date.today()
START_DATE = END_DATE - timedelta(days=10)

INTERVALS_TO_CHECK = ["DAILY", "HOURLY", "4H"]


def _front_month_ric(market_key: str) -> str:
    """Build a plausible front-month-ish RIC to test against.

    Not guaranteed to be the exact live front month (that requires a
    chain lookup, which is out of scope for this quick smoke test) --
    picks the nearest quarterly IMM month (Mar/Jun/Sep/Dec) at or after
    today, which is virtually always a listed, liquid STIR contract.
    """
    quarterly_months = [3, 6, 9, 12]
    today = date.today()
    for months_ahead in range(0, 13):
        probe_month = today.month + months_ahead
        probe_year = today.year + (probe_month - 1) // 12
        probe_month = (probe_month - 1) % 12 + 1
        if probe_month in quarterly_months:
            return ric.build_ric(market_key, probe_month, probe_year)
    raise RuntimeError("Could not find a quarterly month in the next 12 months")


def check_market(market_key: str) -> bool:
    market = config.get_market(market_key)
    if not market.verified:
        print(f"  SKIPPING {market_key} ({market.name}) -- ric_root not yet "
              f"verified in config.py (currently '{market.ric_root}'). "
              f"Confirm the RIC root first, then re-run this script.")
        return True  # not a failure, just not tested yet

    test_ric = _front_month_ric(market_key)
    print(f"  Testing {market_key} ({market.name}) -> {test_ric}")

    all_ok = True
    for interval in INTERVALS_TO_CHECK:
        try:
            df = download_history(test_ric, interval, START_DATE, END_DATE)
        except Exception as exc:
            print(f"    [FAIL] {interval}: {exc}")
            all_ok = False
            continue

        if df.empty:
            print(f"    [WARN] {interval}: request succeeded but returned 0 rows. "
                  f"Contract may not be listed yet, or may be outside trading "
                  f"hours for intraday data. Not necessarily a bug.")
        else:
            last_row = df.iloc[-1]
            print(f"    [OK]   {interval}: {len(df)} bars. "
                  f"Last bar {last_row['Date']} Close={last_row['Close']}")

    return all_ok


def main() -> int:
    print("=" * 70)
    print("LIVE LSEG CONNECTION TEST")
    print(f"Window: {START_DATE} -> {END_DATE}")
    print("=" * 70)

    try:
        open_lseg_session()
    except Exception as exc:
        print(f"\n[FATAL] Could not open LSEG session: {exc}")
        print("Check that LSEG Workspace is running and logged in, and that")
        print("config.LSEG_SESSION_TYPE / RBS_LSEG_APP_KEY are set correctly.")
        return 1

    print("\n[OK] Session opened.\n")

    overall_ok = True
    for market_key in config.MARKETS:
        print(f"\n--- {market_key} ---")
        ok = check_market(market_key)
        overall_ok = overall_ok and ok

    close_lseg_session()

    print("\n" + "=" * 70)
    if overall_ok:
        print("RESULT: PASS (see [WARN]/SKIPPING lines above for anything")
        print("        that still needs attention, e.g. unverified RICs)")
    else:
        print("RESULT: FAIL -- see [FAIL] lines above")
    print("=" * 70)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
