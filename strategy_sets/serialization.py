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
"""

from __future__ import annotations

import json

from strategy_engine.definitions import StrategyDefinition

from strategy_sets.model import ExpansionSettings, StrategySet, StrategySetEntry

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


def strategy_set_to_dict(strategy_set: StrategySet) -> dict:
    """StrategySet -> a plain, JSON-serializable dict."""
    return {
        "schema_version": SCHEMA_VERSION,
        "name": strategy_set.name,
        "description": strategy_set.description,
        "entries": [entry_to_dict(e) for e in strategy_set.entries],
    }


def strategy_set_from_dict(data: dict) -> StrategySet:
    """dict -> StrategySet, running every nested object's own
    validation unchanged.

    Raises:
        ValueError: `data`'s schema_version is missing or not the one
            this function knows how to read; a required top-level key
            (name, entries) is missing; or any nested entry dict is
            itself malformed (see entry_from_dict).
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

    entries = tuple(entry_from_dict(e) for e in raw_entries)
    return StrategySet(name=name, description=data.get("description", ""), entries=entries)


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
