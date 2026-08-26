"""
serialization.py

Pure StrategySet <-> dict <-> JSON conversion. No filesystem access
here -- StrategySetRepository (repository.py) is the only module in
this package that reads or writes files.

JSON schema chosen (readable over compact, per the design brief): one
JSON object per StrategySet, top-level name/description/schema_version
plus an `entries` array. Each entry is readable top-to-bottom as
"name, enabled, shape, filters" -- a StrategyDefinition's four shape
fields (market_key/offsets/weights/interval/price_field) are flattened
directly onto the entry rather than nested under a separate
"definition" key, since they ARE the entry's own definition, one level
flatter than the Python object graph and easier to hand-edit. Note
there is no contract window anywhere in this file -- contract_start/
contract_end are supplied at expand_strategy_set() call time, not
persisted (see strategy_sets/model.py's "Design correction" note):

    {
      "schema_version": 1,
      "name": "6M Strategies",
      "description": "",
      "entries": [
        {
          "name": "SOFR 6M Fly",
          "enabled": true,
          "market_key": "SOFR",
          "offsets": [0, 2, 4],
          "weights": [1.0, -2.0, 1.0],
          "interval": "DAILY",
          "price_field": "Close",
          "expansion": {
            "max_curve_position": null,
            "eligible_rics": null
          }
        }
      ]
    }

Every dict->object function only guards the dict *lookups* it performs
directly -- once required keys are in hand, StrategyDefinition's/
StrategySetEntry's/ExpansionSettings'/StrategySet's own __post_init__
validation runs and raises unmodified, so a genuine domain-validation
error (e.g. an unknown market, non-increasing offsets, entry names that
collide) is never mistaken for, or mislabeled as, a missing-JSON-key
error.

INTERMARKET ENTRIES (additive, Phase 2): the SAME `entries` array above
can also contain entries shaped like this, discriminated purely by the
presence of a `"legs"` key (never a "type"/"kind" tag, and never
inferred from the entry's name):

    {
      "name": "Cross-market basis",
      "enabled": true,
      "legs": [
        {"market_key": "MARKET_A", "offset": 0, "weight": 1.0},
        {"market_key": "MARKET_B", "offset": 0, "weight": -1.0}
      ],
      "interval": "DAILY",
      "price_field": "Close",
      "bp_per_point": null,
      "expansion": {"max_curve_position": null, "eligible_rics": null}
    }

A raw entry dict with a `"legs"` key routes to intermarket_entry_from_
dict() -> StrategySet.intermarket_entries; one without it routes to the
existing, completely unchanged entry_from_dict() -> StrategySet.entries.
Every existing single-market JSON file therefore parses byte-for-byte
identically to before this addition -- entry_from_dict() itself was not
touched. On write, strategy_set_to_dict() emits every `entries` item
before every `intermarket_entries` item into the one output array (the
two Python collections are always kept separate internally -- see
strategy_sets/model.py -- so exact original interleaving order across
the two shapes is not preserved on a save/load round-trip; order WITHIN
each shape is).
"""

from __future__ import annotations

import json

from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec

from strategy_sets.model import (
    ExpansionSettings,
    IntermarketStrategySetEntry,
    StrategySet,
    StrategySetEntry,
)

# Bumped only if the on-disk shape changes incompatibly. Recorded on
# every serialized StrategySet so a future format change can detect
# and migrate/reject old files explicitly instead of guessing.
SCHEMA_VERSION = 1


def _require(data: dict, *keys: str) -> list:
    missing = [k for k in keys if k not in data]
    if missing:
        raise ValueError(f"missing required key(s): {', '.join(missing)}")
    return [data[k] for k in keys]


def expansion_to_dict(expansion: ExpansionSettings) -> dict:
    """ExpansionSettings -> a plain, JSON-serializable dict."""
    return {
        "max_curve_position": expansion.max_curve_position,
        "eligible_rics": list(expansion.eligible_rics) if expansion.eligible_rics else None,
    }


def expansion_from_dict(data: dict) -> ExpansionSettings:
    """dict -> ExpansionSettings, running its own validation unchanged.

    Both fields are optional, so an empty dict is valid input.

    Raises:
        ValueError: ExpansionSettings' own validation rejects the
            values given (e.g. a negative max_curve_position).
    """
    eligible = data.get("eligible_rics")
    return ExpansionSettings(
        max_curve_position=data.get("max_curve_position"),
        eligible_rics=tuple(eligible) if eligible else None,
    )


def entry_to_dict(entry: StrategySetEntry) -> dict:
    """StrategySetEntry -> a plain, JSON-serializable dict."""
    definition = entry.definition
    return {
        "name": entry.name,
        "enabled": entry.enabled,
        "market_key": definition.market_key,
        "offsets": list(definition.offsets),
        "weights": list(definition.weights),
        "interval": definition.interval.value,
        "price_field": definition.price_field,
        "expansion": expansion_to_dict(entry.expansion),
    }


def entry_from_dict(data: dict) -> StrategySetEntry:
    """dict -> StrategySetEntry, running StrategyDefinition's/
    StrategySetEntry's/ExpansionSettings' own validation unchanged.

    `expansion` is optional in the JSON (both its own fields are
    optional) -- an entry that needs no curve-position/eligibility
    filtering can omit the key entirely, defaulting to an unfiltered
    ExpansionSettings().

    Raises:
        ValueError: `data` is missing a required key (name, market_key,
            offsets, weights, or interval).
    """
    try:
        name, market_key, offsets, weights, interval = _require(
            data, "name", "market_key", "offsets", "weights", "interval"
        )
    except ValueError as exc:
        raise ValueError(f"StrategySetEntry JSON is {exc}") from exc

    definition = StrategyDefinition(
        market_key=market_key,
        offsets=tuple(offsets),
        weights=tuple(weights),
        interval=interval,
        price_field=data.get("price_field", "Close"),
    )
    return StrategySetEntry(
        name=name,
        definition=definition,
        expansion=expansion_from_dict(data.get("expansion") or {}),
        enabled=data.get("enabled", True),
    )


def leg_to_dict(leg: LegSpec) -> dict:
    """LegSpec -> a plain, JSON-serializable dict."""
    return {"market_key": leg.market_key, "offset": leg.offset, "weight": leg.weight}


def leg_from_dict(data: dict) -> LegSpec:
    """dict -> LegSpec, running its own validation unchanged.

    Raises:
        ValueError: `data` is missing a required key (market_key,
            offset, or weight).
    """
    try:
        market_key, offset, weight = _require(data, "market_key", "offset", "weight")
    except ValueError as exc:
        raise ValueError(f"LegSpec JSON is {exc}") from exc
    return LegSpec(market_key=market_key, offset=offset, weight=weight)


def intermarket_entry_to_dict(entry: IntermarketStrategySetEntry) -> dict:
    """IntermarketStrategySetEntry -> a plain, JSON-serializable dict."""
    definition = entry.definition
    return {
        "name": entry.name,
        "enabled": entry.enabled,
        "legs": [leg_to_dict(leg) for leg in definition.legs],
        "interval": definition.interval.value,
        "price_field": definition.price_field,
        "bp_per_point": definition.bp_per_point,
        "expansion": expansion_to_dict(entry.expansion),
    }


def intermarket_entry_from_dict(data: dict) -> IntermarketStrategySetEntry:
    """dict -> IntermarketStrategySetEntry, running IntermarketDefinition's/
    LegSpec's/IntermarketStrategySetEntry's/ExpansionSettings' own
    validation unchanged.

    `price_field`/`bp_per_point`/`expansion` are all optional in the
    JSON, exactly mirroring entry_from_dict()'s own optional-field
    conventions.

    Raises:
        ValueError: `data` is missing a required key (name, legs, or
            interval), or any leg dict is itself malformed (see
            leg_from_dict).
    """
    try:
        name, raw_legs, interval = _require(data, "name", "legs", "interval")
    except ValueError as exc:
        raise ValueError(f"IntermarketStrategySetEntry JSON is {exc}") from exc

    definition = IntermarketDefinition(
        legs=tuple(leg_from_dict(leg) for leg in raw_legs),
        interval=interval,
        price_field=data.get("price_field", "Close"),
        bp_per_point=data.get("bp_per_point"),
    )
    return IntermarketStrategySetEntry(
        name=name,
        definition=definition,
        expansion=expansion_from_dict(data.get("expansion") or {}),
        enabled=data.get("enabled", True),
    )


def strategy_set_to_dict(strategy_set: StrategySet) -> dict:
    """StrategySet -> a plain, JSON-serializable dict.

    Both entry shapes (single-market and intermarket) are emitted into
    the SAME `entries` array -- see the module docstring's "Intermarket
    entries" section for the exact discriminated shape and the resulting
    interleaving-order caveat.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "name": strategy_set.name,
        "description": strategy_set.description,
        "entries": (
            [entry_to_dict(e) for e in strategy_set.entries]
            + [intermarket_entry_to_dict(e) for e in strategy_set.intermarket_entries]
        ),
    }


def strategy_set_from_dict(data: dict) -> StrategySet:
    """dict -> StrategySet, running every nested object's own
    validation unchanged.

    Each raw entry in `entries` is routed by the presence of a `"legs"`
    key: present -> intermarket_entry_from_dict() -> StrategySet.
    intermarket_entries; absent -> entry_from_dict() (completely
    unmodified) -> StrategySet.entries. See the module docstring.

    Raises:
        ValueError: `data`'s schema_version is missing or not the one
            this function knows how to read; a required top-level key
            (name, entries) is missing; or any nested entry dict is
            itself malformed (see entry_from_dict/intermarket_entry_from_dict).
    """
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported StrategySet schema_version {version!r} (expected {SCHEMA_VERSION})"
        )
    try:
        name, raw_entries = _require(data, "name", "entries")
    except ValueError as exc:
        raise ValueError(f"StrategySet JSON is {exc}") from exc

    entries = []
    intermarket_entries = []
    for raw_entry in raw_entries:
        if "legs" in raw_entry:
            intermarket_entries.append(intermarket_entry_from_dict(raw_entry))
        else:
            entries.append(entry_from_dict(raw_entry))

    return StrategySet(
        name=name,
        description=data.get("description", ""),
        entries=tuple(entries),
        intermarket_entries=tuple(intermarket_entries),
    )


def strategy_set_to_json(strategy_set: StrategySet, *, indent: int | None = 2) -> str:
    """StrategySet -> a JSON string (indented/readable by default)."""
    return json.dumps(strategy_set_to_dict(strategy_set), indent=indent)


def strategy_set_from_json(text: str) -> StrategySet:
    """JSON string -> StrategySet.

    Raises:
        json.JSONDecodeError: `text` is not valid JSON at all.
        ValueError: `text` is valid JSON but not a valid StrategySet
            (see strategy_set_from_dict).
    """
    return strategy_set_from_dict(json.loads(text))
