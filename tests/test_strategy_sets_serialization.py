"""
tests/test_strategy_sets_serialization.py

StrategySetEntry/ExpansionSettings/StrategySet <-> dict <-> JSON
round-trip, tested with hand-built objects -- no I/O.
"""

from __future__ import annotations

import json

import pytest

from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.model import ExpansionSettings, StrategySet, StrategySetEntry
from strategy_sets.serialization import (
    SCHEMA_VERSION,
    entry_from_dict,
    entry_to_dict,
    expansion_from_dict,
    expansion_to_dict,
    strategy_set_from_dict,
    strategy_set_from_json,
    strategy_set_to_dict,
    strategy_set_to_json,
)


def _expansion(**overrides) -> ExpansionSettings:
    return ExpansionSettings(**overrides)


def _fly_entry() -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1, -2, 1), interval=BarInterval.DAILY,
    )
    return StrategySetEntry(name="SOFR Fly", definition=definition, expansion=_expansion())


def _sonia_entry(enabled: bool = True) -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key="SONIA", offsets=(0, 1), weights=(1.0, -1.0), interval=BarInterval.HOURLY, price_field="High",
    )
    return StrategySetEntry(
        name="SONIA Curve",
        definition=definition,
        expansion=_expansion(max_curve_position=3, eligible_rics=("SONU6", "SONZ6")),
        enabled=enabled,
    )


# ---------------------------------------------------------------------
# ExpansionSettings <-> dict
#
# No contract_start/contract_end anywhere here -- the contract window
# is a call-time expand_strategy_set() argument, never persisted (see
# model.py's "Design correction" note).
# ---------------------------------------------------------------------

def test_expansion_to_dict_shape():
    data = expansion_to_dict(_expansion(max_curve_position=2, eligible_rics=("A", "B")))
    assert data == {
        "max_curve_position": 2,
        "eligible_rics": ["A", "B"],
    }


def test_expansion_to_dict_null_fields_when_unset():
    data = expansion_to_dict(_expansion())
    assert data["max_curve_position"] is None
    assert data["eligible_rics"] is None


def test_expansion_round_trip():
    original = _expansion(max_curve_position=1, eligible_rics=("SRAH26",))
    restored = expansion_from_dict(expansion_to_dict(original))
    assert restored == original


def test_expansion_from_dict_empty_dict_is_valid():
    restored = expansion_from_dict({})
    assert restored == ExpansionSettings()


def test_expansion_from_dict_negative_max_curve_position_propagates_unmodified():
    data = expansion_to_dict(_expansion())
    data["max_curve_position"] = -1
    with pytest.raises(ValueError, match="max_curve_position"):
        expansion_from_dict(data)


# ---------------------------------------------------------------------
# StrategySetEntry <-> dict
# ---------------------------------------------------------------------

def test_entry_to_dict_shape():
    data = entry_to_dict(_fly_entry())
    assert data == {
        "name": "SOFR Fly",
        "enabled": True,
        "market_key": "SOFR",
        "offsets": [0, 1, 2],
        "weights": [1.0, -2.0, 1.0],
        "interval": "DAILY",
        "price_field": "Close",
        "expansion": expansion_to_dict(_expansion()),
    }


def test_entry_to_dict_is_json_serializable():
    json.dumps(entry_to_dict(_sonia_entry()))  # must not raise


def test_entry_round_trip():
    original = _sonia_entry(enabled=False)
    restored = entry_from_dict(entry_to_dict(original))
    assert restored == original


def test_entry_from_dict_defaults_price_field_when_omitted():
    data = entry_to_dict(_fly_entry())
    del data["price_field"]
    restored = entry_from_dict(data)
    assert restored.definition.price_field == "Close"


def test_entry_from_dict_defaults_enabled_true_when_omitted():
    data = entry_to_dict(_fly_entry())
    del data["enabled"]
    restored = entry_from_dict(data)
    assert restored.enabled is True


def test_entry_from_dict_defaults_expansion_when_omitted():
    data = entry_to_dict(_fly_entry())
    del data["expansion"]
    restored = entry_from_dict(data)
    assert restored.expansion == ExpansionSettings()


def test_entry_from_dict_interval_restored_as_enum():
    restored = entry_from_dict(entry_to_dict(_sonia_entry()))
    assert restored.definition.interval is BarInterval.HOURLY


@pytest.mark.parametrize(
    "missing_key", ["name", "market_key", "offsets", "weights", "interval"]
)
def test_entry_from_dict_missing_key_raises_value_error(missing_key):
    data = entry_to_dict(_fly_entry())
    del data[missing_key]
    with pytest.raises(ValueError, match=missing_key):
        entry_from_dict(data)


def test_entry_from_dict_unknown_market_propagates_unmodified():
    data = entry_to_dict(_fly_entry())
    data["market_key"] = "NOT_A_MARKET"
    with pytest.raises(KeyError, match="Unknown market"):
        entry_from_dict(data)


def test_entry_from_dict_bad_offsets_propagates_strategy_definition_error():
    data = entry_to_dict(_fly_entry())
    data["offsets"] = [1, 2, 3]  # must start at 0
    with pytest.raises(ValueError, match="offsets must start at 0"):
        entry_from_dict(data)


def test_entry_from_dict_duplicate_offsets_rejected_by_strategy_definition():
    data = entry_to_dict(_fly_entry())
    data["offsets"] = [0, 1, 1]  # not strictly increasing
    with pytest.raises(ValueError, match="strictly increasing"):
        entry_from_dict(data)


def test_entry_from_dict_all_zero_weights_rejected():
    data = entry_to_dict(_fly_entry())
    data["weights"] = [0, 0, 0]
    with pytest.raises(ValueError, match="weights cannot be all zero"):
        entry_from_dict(data)


def test_entry_from_dict_empty_name_propagates_entry_error():
    data = entry_to_dict(_fly_entry())
    data["name"] = ""
    with pytest.raises(ValueError, match="non-empty"):
        entry_from_dict(data)


# ---------------------------------------------------------------------
# StrategySet <-> dict
# ---------------------------------------------------------------------

def test_strategy_set_to_dict_shape():
    s = StrategySet(name="Churning", entries=(_fly_entry(), _sonia_entry()), description="desc")
    data = strategy_set_to_dict(s)
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["name"] == "Churning"
    assert data["description"] == "desc"
    assert data["entries"] == [entry_to_dict(_fly_entry()), entry_to_dict(_sonia_entry())]


def test_strategy_set_to_dict_is_json_serializable():
    s = StrategySet(name="Churning", entries=(_fly_entry(),))
    json.dumps(strategy_set_to_dict(s))  # must not raise


def test_strategy_set_from_dict_round_trip():
    original = StrategySet(name="6M Strategies", entries=(_fly_entry(), _sonia_entry(enabled=False)), description="desc")
    restored = strategy_set_from_dict(strategy_set_to_dict(original))
    assert restored == original


def test_strategy_set_from_dict_preserves_entry_order():
    original = StrategySet(name="Ordered", entries=(_sonia_entry(), _fly_entry()))
    restored = strategy_set_from_dict(strategy_set_to_dict(original))
    assert restored.entries == (_sonia_entry(), _fly_entry())


def test_strategy_set_from_dict_defaults_description_when_omitted():
    data = strategy_set_to_dict(StrategySet(name="NoDesc", entries=(_fly_entry(),)))
    del data["description"]
    restored = strategy_set_from_dict(data)
    assert restored.description == ""


def test_strategy_set_from_dict_missing_schema_version_raises():
    data = strategy_set_to_dict(StrategySet(name="Churning", entries=(_fly_entry(),)))
    del data["schema_version"]
    with pytest.raises(ValueError, match="schema_version"):
        strategy_set_from_dict(data)


def test_strategy_set_from_dict_unsupported_schema_version_raises():
    data = strategy_set_to_dict(StrategySet(name="Churning", entries=(_fly_entry(),)))
    data["schema_version"] = 999
    with pytest.raises(ValueError, match="schema_version"):
        strategy_set_from_dict(data)


@pytest.mark.parametrize("missing_key", ["name", "entries"])
def test_strategy_set_from_dict_missing_top_level_key_raises(missing_key):
    data = strategy_set_to_dict(StrategySet(name="Churning", entries=(_fly_entry(),)))
    del data[missing_key]
    with pytest.raises(ValueError, match=missing_key):
        strategy_set_from_dict(data)


def test_strategy_set_from_dict_invalid_name_propagates_strategy_set_error():
    data = strategy_set_to_dict(StrategySet(name="Churning", entries=(_fly_entry(),)))
    data["name"] = "bad/name"
    with pytest.raises(ValueError, match="StrategySet name"):
        strategy_set_from_dict(data)


def test_strategy_set_from_dict_empty_entries_rejected():
    data = strategy_set_to_dict(StrategySet(name="Churning", entries=(_fly_entry(),)))
    data["entries"] = []
    with pytest.raises(ValueError, match="at least 1"):
        strategy_set_from_dict(data)


def test_strategy_set_from_dict_duplicate_entry_names_rejected():
    data = strategy_set_to_dict(StrategySet(name="Churning", entries=(_fly_entry(),)))
    data["entries"].append(dict(data["entries"][0]))  # same "SOFR Fly" name twice
    with pytest.raises(ValueError, match="unique"):
        strategy_set_from_dict(data)


def test_strategy_set_from_dict_malformed_nested_entry_raises():
    data = strategy_set_to_dict(StrategySet(name="Churning", entries=(_fly_entry(),)))
    del data["entries"][0]["market_key"]
    with pytest.raises(ValueError, match="market_key"):
        strategy_set_from_dict(data)


# ---------------------------------------------------------------------
# StrategySet <-> JSON string
# ---------------------------------------------------------------------

def test_strategy_set_json_round_trip():
    original = StrategySet(name="Churning", entries=(_fly_entry(), _sonia_entry()), description="d")
    text = strategy_set_to_json(original)
    assert isinstance(text, str)
    restored = strategy_set_from_json(text)
    assert restored == original


def test_strategy_set_to_json_is_valid_and_readable():
    text = strategy_set_to_json(StrategySet(name="Churning", entries=(_fly_entry(),)))
    json.loads(text)  # must not raise
    assert "\n" in text  # indented/readable, not single-line compact


def test_strategy_set_from_json_rejects_malformed_json():
    with pytest.raises(json.JSONDecodeError):
        strategy_set_from_json("{not valid json")


def test_strategy_set_json_matches_documented_example_shape():
    # Matches the schema documented in serialization.py's module
    # docstring -- a hand-authored/edited JSON file in this exact shape
    # must load correctly (backward-compatibility with the chosen
    # on-disk format, not just whatever round-trips through our own
    # to_dict()).
    text = json.dumps(
        {
            "schema_version": 1,
            "name": "6M Strategies",
            "description": "",
            "entries": [
                {
                    "name": "SOFR 6M Fly",
                    "enabled": True,
                    "market_key": "SOFR",
                    "offsets": [0, 2, 4],
                    "weights": [1.0, -2.0, 1.0],
                    "interval": "DAILY",
                    "price_field": "Close",
                    "expansion": {
                        "max_curve_position": None,
                        "eligible_rics": None,
                    },
                }
            ],
        }
    )
    restored = strategy_set_from_json(text)
    assert restored.name == "6M Strategies"
    assert restored.entries[0].name == "SOFR 6M Fly"
    assert restored.entries[0].definition.offsets == (0, 2, 4)


def test_strategy_set_json_loads_without_a_persisted_contract_window():
    # A file saved under the OLD design (contract_start/contract_end
    # nested inside "expansion") is not something this version ever
    # produces, but forward/backward tolerance is still worth locking
    # in: expansion_from_dict only reads the keys it knows about, so
    # stray keys are silently ignored rather than rejected.
    text = json.dumps(
        {
            "schema_version": 1,
            "name": "Churning",
            "description": "",
            "entries": [
                {
                    "name": "SOFR Fly",
                    "enabled": True,
                    "market_key": "SOFR",
                    "offsets": [0, 1, 2],
                    "weights": [1.0, -2.0, 1.0],
                    "interval": "DAILY",
                    "price_field": "Close",
                    "expansion": {"max_curve_position": None, "eligible_rics": None, "stray_key": "ignored"},
                }
            ],
        }
    )
    restored = strategy_set_from_json(text)
    assert restored.entries[0].expansion == ExpansionSettings()
