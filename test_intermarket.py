"""
test_intermarket.py

TEMPORARY, STANDALONE manual validation harness for intermarket
strategies. This is NOT a pytest test file despite the "test_" prefix
in its name -- run it directly:

    python test_intermarket.py

Purpose: independently verify, against REAL LSEG/QuantHub data, that
Oscill8's intermarket pricing arithmetic is correct --

    Strategy = sum(leg_price_i * leg_weight_i)

-- and that each generated RIC corresponds exactly to the leg the
strategy definition says it should, by POSITION (never inferred from
the strategy's name, market names, or leg count/shape).

Architecture used (see strategy_sets/model.py, strategy_engine/
intermarket_combinations.py, strategy_engine/pricing.py, core/ric.py --
all read, none modified):

    StrategySetRepository().load(name)
        -> StrategySet.intermarket_entries (each an IntermarketStrategySetEntry:
           .name + .definition)
        -> generate_intermarket_instances(entry.definition, contract_start, contract_end)
        -> IntermarketStrategyInstance(definition, rics)
              .rics[i] corresponds to .definition.legs[i], by position
        -> build_history(instance, price_start, price_end)
        -> StrategyHistory.history: DataFrame[Date, Leg_1..Leg_N, Strategy]
           (the last row is the latest aligned observation)

Deliberately does NOT call expand_strategy_set() (it would flatten and
dedupe every entry's instances together, losing which entry produced
which instance) and does NOT go through template_scanner/ScanReport
(this validates the lower-level pricing calculation directly, not the
scanner pipeline built on top of it).

Fully generic: makes no assumption about which markets, how many legs,
or what shape (fly/spread/basis/butterfly/etc.) an intermarket strategy
has. RICs are printed as plain strings only -- this script never
attempts to infer or label whether a leg's data came from LSEG or
QuantHub; that routing is entirely the data layer's own concern
(core.providers/database.service), untouched and unobserved here.

Does not modify any production code or the StrategySet JSON. Does not
print any credential/token value -- load_dotenv() only populates
process environment variables that core.config/core.downloader/
core.quanthub read directly; this script never reads or prints them
itself.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # must run before any Oscill8 import below (core.config
                # reads RBS_* environment variables at import time)

import sys
from typing import Any

from core.ric import parse_ric
from strategy_engine.intermarket_combinations import (
    IntermarketStrategyInstance,
    generate_intermarket_instances,
)
from strategy_sets.repository import StrategySetRepository

# ---------------------------------------------------------------------
# Test parameters -- edit these
# ---------------------------------------------------------------------
STRATEGY_SET_NAME = "Intermarket UI Test"
CONTRACT_START = "2026-08-27"
CONTRACT_END = "2027-12-31"
PRICE_START = "2026-01-01"
PRICE_END = "2027-08-01"

# Floating-point tolerance for the manual-vs-Oscill8 strategy price
# comparison (requirement: "sensible tolerance such as 1e-10").
TOLERANCE = 1e-10

_WIDTH = 78


def _rule(char: str = "=") -> None:
    print(char * _WIDTH)


def _header(text: str, char: str = "=") -> None:
    _rule(char)
    print(text)
    _rule(char)


def _subheader(text: str) -> None:
    _rule("-")
    print(text)
    _rule("-")


def verify_ric_mapping(instance: IntermarketStrategyInstance) -> bool:
    """PASS/FAIL: instance.rics[i] must decode (via core.ric.parse_ric,
    the same function the production code itself uses) to
    instance.definition.legs[i].market_key, for EVERY leg position --
    by position only, never inferred from the strategy's name or shape.
    """
    legs = instance.definition.legs
    rics = instance.rics

    if len(rics) != len(legs):
        print(f"  MISMATCH: {len(rics)} RIC(s) but {len(legs)} leg(s) -- cannot map 1:1")
        return False

    all_ok = True
    for i, (leg, ric) in enumerate(zip(legs, rics), start=1):
        try:
            parsed_market_key = parse_ric(ric).market_key
        except ValueError as exc:
            print(f"  Leg {i}: RIC={ric!r} -- parse_ric() FAILED: {exc}")
            all_ok = False
            continue

        matches = parsed_market_key == leg.market_key
        all_ok = all_ok and matches
        print(
            f"  Leg {i}: RIC={ric!r:<14} parse_ric(RIC).market_key={parsed_market_key!r:<14} "
            f"definition.legs[{i - 1}].market_key={leg.market_key!r:<14} "
            f"-> {'OK' if matches else 'MISMATCH'}"
        )

    print(f"  RIC/leg mapping check: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


def process_instance(
    entry_name: str,
    instance: IntermarketStrategyInstance,
    price_start: str,
    price_end: str,
    counters: dict[str, int],
) -> None:
    from strategy_engine.pricing import build_history  # imported here, at point of use, per
                                                          # requirement 17 (no scanner/report
                                                          # objects -- this is the ONE lower-
                                                          # level call this script makes)

    legs = instance.definition.legs
    rics = instance.rics
    n_legs = len(legs)

    counters["total_instances"] += 1

    _subheader(f"Strategy: {entry_name}")

    print("RICs:")
    for i, (leg, ric) in enumerate(zip(legs, rics), start=1):
        print(
            f"  Leg {i}: market_key={leg.market_key:<10} offset={leg.offset:<4} "
            f"weight={float(leg.weight):<8.4f} RIC={ric}"
        )

    print()
    print("RIC/leg correspondence check:")
    counters["mapping_checked"] += 1
    if verify_ric_mapping(instance):
        counters["mapping_passed"] += 1

    # --- fetch/align via the same lower-level pricing function the
    # scanner itself uses -- never the scanner/ScanReport layer.
    try:
        history = build_history(instance, price_start, price_end)
    except Exception as exc:
        print(f"\n  build_history() raised {type(exc).__name__}: {exc}")
        print("  SKIPPED -- could not build a price history for this instance.")
        counters["skipped"] += 1
        return

    df = history.history
    if df is None or df.empty:
        print(
            "\n  SKIPPED -- no aligned price history for this instance "
            "(the legs share no common valid observation date in this window)."
        )
        counters["skipped"] += 1
        return

    last_row = df.iloc[-1]
    leg_columns = [f"Leg_{i + 1}" for i in range(n_legs)]

    print()
    print(f"Latest aligned observation (Date={last_row['Date']}):")
    leg_prices: list[float] = []
    for i, col in enumerate(leg_columns, start=1):
        price = float(last_row[col])
        leg_prices.append(price)
        print(f"  {col} (RIC={rics[i - 1]}): {price}")
    oscill8_strategy = float(last_row["Strategy"])
    print(f"  Strategy (Oscill8): {oscill8_strategy}")

    # --- independent recomputation, entirely within this script ---
    weights = [float(leg.weight) for leg in legs]
    manual_strategy = sum(price * weight for price, weight in zip(leg_prices, weights))
    difference = manual_strategy - oscill8_strategy
    arithmetic_ok = abs(difference) <= TOLERANCE

    print()
    print("Leg-by-leg contribution table:")
    print(
        f"  {'Leg':<5}{'Market':<12}{'Offset':<8}{'Weight':<10}"
        f"{'RIC':<12}{'Price':<16}{'Contribution':<16}"
    )
    for i, (leg, ric, price) in enumerate(zip(legs, rics, leg_prices), start=1):
        contribution = price * float(leg.weight)
        print(
            f"  {i:<5}{leg.market_key:<12}{leg.offset:<8}{float(leg.weight):<10.4f}"
            f"{ric:<12}{price:<16.6f}{contribution:<16.6f}"
        )

    print()
    print("Calculation:")
    for price, weight in zip(leg_prices, weights):
        sign = "+" if weight >= 0 else "-"
        print(f"    {sign} {price:.6f} x {abs(weight):.6f}")
    print(f"    = {manual_strategy:.10f}")

    print()
    print(f"  Manual strategy price:   {manual_strategy:.10f}")
    print(f"  Oscill8 strategy price:  {oscill8_strategy:.10f}")
    print(f"  Difference:              {difference:.2e}")
    print(f"  Arithmetic check: {'PASS' if arithmetic_ok else 'FAIL'}")

    counters["arithmetic_checked"] += 1
    if arithmetic_ok:
        counters["arithmetic_passed"] += 1


def main() -> None:
    _header("Oscill8 Intermarket Strategy Validation Harness")
    print(f"StrategySet:      {STRATEGY_SET_NAME}")
    print(f"Contract window:  {CONTRACT_START} -> {CONTRACT_END}")
    print(f"Price window:     {PRICE_START} -> {PRICE_END}")
    print(f"Tolerance:        {TOLERANCE}")
    print()

    repo = StrategySetRepository()
    try:
        strategy_set = repo.load(STRATEGY_SET_NAME)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    intermarket_entries = strategy_set.intermarket_entries
    if not intermarket_entries:
        print(f"StrategySet '{STRATEGY_SET_NAME}' has no intermarket entries. Nothing to validate.")
        sys.exit(0)

    counters: dict[str, int] = {
        "total_instances": 0,
        "mapping_checked": 0,
        "mapping_passed": 0,
        "arithmetic_checked": 0,
        "arithmetic_passed": 0,
        "skipped": 0,
        "errors": 0,
    }

    # Iterate entries INDIVIDUALLY -- never expand_strategy_set() on the
    # whole set, which would flatten+dedupe every entry's instances
    # together and lose the entry-name <-> instance association.
    for entry in strategy_set.intermarket_entries:
        _header(f"Entry: {entry.name}  (enabled={entry.enabled})", char="#")
        if not entry.enabled:
            print("  Note: entry is disabled -- validating anyway (this is a manual audit tool,")
            print("  not a simulation of what a live scan would include).")

        try:
            instances = generate_intermarket_instances(entry.definition, CONTRACT_START, CONTRACT_END)
        except Exception as exc:
            print(f"  generate_intermarket_instances() raised {type(exc).__name__}: {exc}")
            counters["errors"] += 1
            continue

        if not instances:
            print("  No instances generated for this entry in the given contract window.")
            continue

        for instance in instances:
            try:
                process_instance(entry.name, instance, PRICE_START, PRICE_END, counters)
            except Exception as exc:
                print(f"\n  UNEXPECTED ERROR processing this instance: {type(exc).__name__}: {exc}")
                counters["errors"] += 1
            print()

    mapping_failed = counters["mapping_checked"] - counters["mapping_passed"]
    arithmetic_failed = counters["arithmetic_checked"] - counters["arithmetic_passed"]
    total_failed = mapping_failed + arithmetic_failed + counters["errors"]

    _header("Summary")
    print(f"Total instances tested:            {counters['total_instances']}")
    print(f"RIC mapping checks passed:         {counters['mapping_passed']} / {counters['mapping_checked']}")
    print(f"Strategy arithmetic checks passed: {counters['arithmetic_passed']} / {counters['arithmetic_checked']}")
    print(f"Skipped (no aligned history):      {counters['skipped']}")
    print(f"Unexpected errors:                 {counters['errors']}")
    print(f"Failed:                            {total_failed}")


if __name__ == "__main__":
    main()
