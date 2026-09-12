"""
tests/test_strategy_sets_groups.py

Composite Strategy Sets -- Phase 1 of the "Group A x Group B" design:
StrategyGroup/StrategyGroupPair construction and validation, their
dict/JSON serialization, repository round-tripping, backward
compatibility with every pre-existing StrategySet shape, and the
explicit guarantee that carrying groups does NOT trigger any
combination/Cartesian-product expansion yet (that engine is a later
phase and does not exist).

Model/serialization tests are hand-built objects only, no I/O, matching
tests/test_strategy_sets_model.py and tests/test_strategy_sets_
serialization.py. Repository tests use a tmp_path-backed directory,
matching tests/test_strategy_sets_repository.py.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from strategy_engine.intermarket_definitions import IntermarketDefinition, LegSpec
from strategy_sets.composite import CompositeResolutionError
from strategy_sets.expansion import expand_strategy_set
from strategy_sets.model import (
    ExpansionSettings,
    IntermarketStrategySetEntry,
    StrategyGroup,
    StrategyGroupPair,
    StrategySet,
    StrategySetEntry,
)
from strategy_sets.repository import StrategySetRepository
from strategy_sets.serialization import (
    SCHEMA_VERSION,
    group_from_dict,
    group_pair_from_dict,
    group_pair_to_dict,
    group_to_dict,
    strategy_set_from_dict,
    strategy_set_from_json,
    strategy_set_to_dict,
    strategy_set_to_json,
)

_START, _END = "2026-01-01", "2027-12-31"


# ---------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------

def _fly_entry(name="SOFR Fly") -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1, -2, 1), interval=BarInterval.DAILY,
    )
    return StrategySetEntry(name=name, definition=definition, expansion=ExpansionSettings())


def _intermarket_entry(name="SOFR/SONIA basis") -> IntermarketStrategySetEntry:
    definition = IntermarketDefinition(
        legs=(
            LegSpec(market_key="SOFR", offset=0, weight=1.0),
            LegSpec(market_key="SONIA", offset=0, weight=-1.0),
        ),
        interval=BarInterval.DAILY,
    )
    return IntermarketStrategySetEntry(name=name, definition=definition)


def _group_a() -> StrategyGroup:
    return StrategyGroup(
        source_set_name="STIR Flys", selected_entry_names=("SR3 Fly", "SON Fly")
    )


def _group_b() -> StrategyGroup:
    return StrategyGroup(
        source_set_name="Other Flys", selected_entry_names=("SR3 Fly", "CRA Fly")
    )


@pytest.fixture
def repo(tmp_path):
    return StrategySetRepository(base_dir=str(tmp_path / "strategy_sets"))


# ---------------------------------------------------------------------
# StrategyGroup -- construction / validation
# ---------------------------------------------------------------------

def test_group_stores_source_set_name_and_selection():
    group = _group_a()
    assert group.source_set_name == "STIR Flys"
    assert group.selected_entry_names == ("SR3 Fly", "SON Fly")


def test_group_selection_defaults_to_empty():
    group = StrategyGroup(source_set_name="STIR Flys")
    assert group.selected_entry_names == ()


def test_group_empty_selection_is_valid_not_an_error():
    group = StrategyGroup(source_set_name="STIR Flys", selected_entry_names=())
    assert group.selected_entry_names == ()


def test_group_selection_normalized_to_tuple():
    group = StrategyGroup(
        source_set_name="STIR Flys", selected_entry_names=["SR3 Fly", "SON Fly"]
    )
    assert group.selected_entry_names == ("SR3 Fly", "SON Fly")


def test_group_selection_order_is_preserved_not_sorted():
    group = StrategyGroup(
        source_set_name="STIR Flys", selected_entry_names=("SON Fly", "CORRA Fly", "SR3 Fly")
    )
    assert group.selected_entry_names == ("SON Fly", "CORRA Fly", "SR3 Fly")


def test_group_selecting_every_source_strategy_is_stored_explicitly():
    # "select all" is persisted as the actual names, never as a flag --
    # this is the whole reproducibility point of the field.
    every_name = ("SR3 Fly", "SON Fly", "CORRA Fly")
    group = StrategyGroup(source_set_name="STIR Flys", selected_entry_names=every_name)
    assert group.selected_entry_names == every_name
    assert not hasattr(group, "select_all")


def test_group_partial_selection_keeps_only_what_was_selected():
    group = StrategyGroup(source_set_name="STIR Flys", selected_entry_names=("SON Fly",))
    assert group.selected_entry_names == ("SON Fly",)


def test_group_duplicate_selected_names_rejected():
    with pytest.raises(ValueError, match="unique"):
        StrategyGroup(
            source_set_name="STIR Flys", selected_entry_names=("SR3 Fly", "SR3 Fly")
        )


def test_group_empty_selected_name_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        StrategyGroup(source_set_name="STIR Flys", selected_entry_names=("SR3 Fly", "  "))


def test_group_non_string_selected_name_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        StrategyGroup(source_set_name="STIR Flys", selected_entry_names=("SR3 Fly", 3))


def test_group_selection_given_as_bare_string_rejected():
    # tuple("SR3 Fly") would silently become one entry name per character.
    with pytest.raises(TypeError, match="not a single string"):
        StrategyGroup(source_set_name="STIR Flys", selected_entry_names="SR3 Fly")


def test_group_empty_source_set_name_rejected():
    with pytest.raises(ValueError, match="source_set_name"):
        StrategyGroup(source_set_name="")


def test_group_non_string_source_set_name_rejected():
    with pytest.raises(ValueError, match="source_set_name"):
        StrategyGroup(source_set_name=None)


def test_group_source_set_name_must_be_a_valid_strategy_set_name():
    # Same pattern StrategySet.name itself enforces -- a reference that
    # could never name a real saved set is rejected up front.
    with pytest.raises(ValueError, match="source_set_name"):
        StrategyGroup(source_set_name="STIR/Flys")


def test_group_is_frozen():
    group = _group_a()
    with pytest.raises(dataclasses.FrozenInstanceError):
        group.source_set_name = "Other Flys"


# ---------------------------------------------------------------------
# StrategyGroupPair -- construction / validation
# ---------------------------------------------------------------------

def test_pair_group_a_only():
    pair = StrategyGroupPair(group_a=_group_a())
    assert pair.group_a == _group_a()
    assert pair.group_b is None


def test_pair_group_a_and_group_b():
    pair = StrategyGroupPair(group_a=_group_a(), group_b=_group_b())
    assert pair.group_a.source_set_name == "STIR Flys"
    assert pair.group_b.source_set_name == "Other Flys"


def test_pair_group_a_and_group_b_stay_distinguishable():
    # Same source set on both sides must not collapse the two groups.
    same = StrategyGroup(source_set_name="STIR Flys", selected_entry_names=("SR3 Fly",))
    other = StrategyGroup(source_set_name="STIR Flys", selected_entry_names=("SON Fly",))
    pair = StrategyGroupPair(group_a=same, group_b=other)
    assert pair.group_a.selected_entry_names == ("SR3 Fly",)
    assert pair.group_b.selected_entry_names == ("SON Fly",)


def test_pair_requires_group_a():
    with pytest.raises(TypeError):
        StrategyGroupPair(group_b=_group_b())


def test_pair_group_a_must_be_a_group():
    with pytest.raises(TypeError, match="group_a"):
        StrategyGroupPair(group_a="STIR Flys")


def test_pair_group_b_must_be_a_group_or_none():
    with pytest.raises(TypeError, match="group_b"):
        StrategyGroupPair(group_a=_group_a(), group_b="Other Flys")


def test_pair_stores_no_combination_results():
    pair = StrategyGroupPair(group_a=_group_a(), group_b=_group_b())
    assert {f.name for f in dataclasses.fields(pair)} == {"group_a", "group_b"}


# ---------------------------------------------------------------------
# StrategySet -- groups field
# ---------------------------------------------------------------------

def test_strategy_set_groups_defaults_to_none():
    strategy_set = StrategySet(name="Plain", entries=(_fly_entry(),))
    assert strategy_set.groups is None


def test_strategy_set_with_entries_and_groups():
    strategy_set = StrategySet(
        name="Mixed",
        entries=(_fly_entry(),),
        groups=StrategyGroupPair(group_a=_group_a(), group_b=_group_b()),
    )
    assert len(strategy_set.entries) == 1
    assert strategy_set.groups.group_b.selected_entry_names == ("SR3 Fly", "CRA Fly")


def test_strategy_set_with_groups_only_and_no_entries_is_valid():
    strategy_set = StrategySet(
        name="Combos", entries=(), groups=StrategyGroupPair(group_a=_group_a())
    )
    assert strategy_set.entries == ()
    assert strategy_set.intermarket_entries == ()
    assert strategy_set.groups.group_a.source_set_name == "STIR Flys"


def test_strategy_set_with_neither_entries_nor_groups_still_rejected():
    with pytest.raises(ValueError, match="at least 1 entry"):
        StrategySet(name="Empty", entries=())


def test_strategy_set_groups_must_be_a_group_pair():
    with pytest.raises(TypeError, match="groups"):
        StrategySet(name="Bad", entries=(_fly_entry(),), groups=_group_a())


def test_strategy_set_groups_survives_dataclasses_replace():
    # repository.duplicate()/rename() and execution.with_interval_override()
    # all go through dataclasses.replace().
    original = StrategySet(
        name="Combos", entries=(_fly_entry(),), groups=StrategyGroupPair(group_a=_group_a())
    )
    renamed = dataclasses.replace(original, name="Combos 2")
    assert renamed.groups == original.groups


# ---------------------------------------------------------------------
# Serialization -- group / pair level
# ---------------------------------------------------------------------

def test_group_to_dict_shape():
    assert group_to_dict(_group_a()) == {
        "source_set_name": "STIR Flys",
        "selected_entry_names": ["SR3 Fly", "SON Fly"],
    }


def test_group_round_trip():
    group = _group_a()
    assert group_from_dict(group_to_dict(group)) == group


def test_group_from_dict_selection_optional():
    group = group_from_dict({"source_set_name": "STIR Flys"})
    assert group.selected_entry_names == ()


def test_group_from_dict_missing_source_set_name_raises():
    with pytest.raises(ValueError, match="source_set_name"):
        group_from_dict({"selected_entry_names": ["SR3 Fly"]})


def test_group_from_dict_non_object_raises():
    with pytest.raises(ValueError, match="must be an object"):
        group_from_dict("STIR Flys")


def test_group_from_dict_propagates_domain_validation():
    with pytest.raises(ValueError, match="unique"):
        group_from_dict(
            {"source_set_name": "STIR Flys", "selected_entry_names": ["A", "A"]}
        )


def test_group_pair_to_dict_emits_null_group_b_when_absent():
    assert group_pair_to_dict(StrategyGroupPair(group_a=_group_a())) == {
        "group_a": {
            "source_set_name": "STIR Flys",
            "selected_entry_names": ["SR3 Fly", "SON Fly"],
        },
        "group_b": None,
    }


def test_group_pair_round_trip_with_group_b():
    pair = StrategyGroupPair(group_a=_group_a(), group_b=_group_b())
    assert group_pair_from_dict(group_pair_to_dict(pair)) == pair


def test_group_pair_from_dict_missing_group_b_key_means_none():
    pair = group_pair_from_dict({"group_a": {"source_set_name": "STIR Flys"}})
    assert pair.group_b is None


def test_group_pair_from_dict_missing_group_a_raises():
    with pytest.raises(ValueError, match="group_a"):
        group_pair_from_dict({"group_b": {"source_set_name": "Other Flys"}})


def test_group_pair_from_dict_non_object_raises():
    with pytest.raises(ValueError, match="must be an object"):
        group_pair_from_dict(["STIR Flys"])


def test_group_pair_from_dict_malformed_nested_group_raises():
    with pytest.raises(ValueError, match="source_set_name"):
        group_pair_from_dict({"group_a": {"selected_entry_names": ["SR3 Fly"]}})


# ---------------------------------------------------------------------
# Serialization -- StrategySet level
# ---------------------------------------------------------------------

def test_strategy_set_to_dict_omits_groups_key_when_absent():
    data = strategy_set_to_dict(StrategySet(name="Plain", entries=(_fly_entry(),)))
    assert "groups" not in data


def test_strategy_set_to_dict_emits_groups_key_when_present():
    strategy_set = StrategySet(
        name="Combos",
        entries=(),
        groups=StrategyGroupPair(group_a=_group_a(), group_b=_group_b()),
    )
    data = strategy_set_to_dict(strategy_set)
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["entries"] == []
    assert data["groups"] == {
        "group_a": {
            "source_set_name": "STIR Flys",
            "selected_entry_names": ["SR3 Fly", "SON Fly"],
        },
        "group_b": {
            "source_set_name": "Other Flys",
            "selected_entry_names": ["SR3 Fly", "CRA Fly"],
        },
    }


def test_strategy_set_json_round_trip_group_a_only():
    strategy_set = StrategySet(
        name="Combos", entries=(), groups=StrategyGroupPair(group_a=_group_a())
    )
    restored = strategy_set_from_json(strategy_set_to_json(strategy_set))
    assert restored == strategy_set
    assert restored.groups.group_b is None


def test_strategy_set_json_round_trip_group_a_and_b_preserves_order():
    strategy_set = StrategySet(
        name="Combos",
        entries=(),
        groups=StrategyGroupPair(group_a=_group_a(), group_b=_group_b()),
    )
    restored = strategy_set_from_json(strategy_set_to_json(strategy_set))
    assert restored == strategy_set
    assert restored.groups.group_a.source_set_name == "STIR Flys"
    assert restored.groups.group_a.selected_entry_names == ("SR3 Fly", "SON Fly")
    assert restored.groups.group_b.source_set_name == "Other Flys"
    assert restored.groups.group_b.selected_entry_names == ("SR3 Fly", "CRA Fly")


def test_strategy_set_json_round_trip_preserves_unsorted_selection_order():
    unsorted_selection = ("SON Fly", "CORRA Fly", "SR3 Fly")
    strategy_set = StrategySet(
        name="Combos",
        entries=(),
        groups=StrategyGroupPair(
            group_a=StrategyGroup(
                source_set_name="STIR Flys", selected_entry_names=unsorted_selection
            )
        ),
    )
    restored = strategy_set_from_json(strategy_set_to_json(strategy_set))
    assert restored.groups.group_a.selected_entry_names == unsorted_selection


def test_strategy_set_json_round_trip_empty_selection():
    strategy_set = StrategySet(
        name="Combos",
        entries=(),
        groups=StrategyGroupPair(
            group_a=StrategyGroup(source_set_name="STIR Flys", selected_entry_names=())
        ),
    )
    restored = strategy_set_from_json(strategy_set_to_json(strategy_set))
    assert restored.groups.group_a.selected_entry_names == ()


def test_strategy_set_json_round_trip_entries_intermarket_and_groups_together():
    strategy_set = StrategySet(
        name="Everything",
        entries=(_fly_entry("SOFR Fly"), _fly_entry("SOFR Fly 2")),
        intermarket_entries=(_intermarket_entry(),),
        description="mixed",
        groups=StrategyGroupPair(group_a=_group_a(), group_b=_group_b()),
    )
    restored = strategy_set_from_json(strategy_set_to_json(strategy_set))
    assert restored == strategy_set
    assert [e.name for e in restored.entries] == ["SOFR Fly", "SOFR Fly 2"]
    assert [e.name for e in restored.intermarket_entries] == ["SOFR/SONIA basis"]
    assert restored.description == "mixed"
    assert restored.groups == strategy_set.groups


def test_strategy_set_from_dict_explicit_null_groups_means_none():
    data = {
        "schema_version": SCHEMA_VERSION,
        "name": "Plain",
        "entries": [
            {
                "name": "SOFR Fly",
                "market_key": "SOFR",
                "offsets": [0, 1, 2],
                "weights": [1, -2, 1],
                "interval": "DAILY",
            }
        ],
        "groups": None,
    }
    assert strategy_set_from_dict(data).groups is None


def test_strategy_set_from_dict_malformed_groups_raises():
    data = {
        "schema_version": SCHEMA_VERSION,
        "name": "Plain",
        "entries": [],
        "groups": {"group_b": {"source_set_name": "Other Flys"}},
    }
    with pytest.raises(ValueError, match="group_a"):
        strategy_set_from_dict(data)


# ---------------------------------------------------------------------
# Backward compatibility -- pre-groups JSON
# ---------------------------------------------------------------------

_LEGACY_SINGLE_MARKET_JSON = """
{
  "schema_version": 1,
  "name": "6M Strategies",
  "description": "legacy file",
  "entries": [
    {
      "name": "SOFR 6M Fly",
      "enabled": true,
      "market_key": "SOFR",
      "offsets": [0, 2, 4],
      "weights": [1.0, -2.0, 1.0],
      "interval": "DAILY",
      "price_field": "Close",
      "expansion": {"max_curve_position": null, "eligible_rics": null}
    }
  ]
}
"""

_LEGACY_INTERMARKET_JSON = """
{
  "schema_version": 1,
  "name": "Cross Market",
  "description": "",
  "entries": [
    {
      "name": "SOFR minus SONIA",
      "enabled": true,
      "legs": [
        {"market_key": "SOFR", "offset": 0, "weight": 1.0},
        {"market_key": "SONIA", "offset": 0, "weight": -1.0}
      ],
      "interval": "DAILY",
      "price_field": "Close",
      "bp_per_point": null,
      "expansion": {"max_curve_position": null, "eligible_rics": null}
    }
  ]
}
"""


def test_legacy_single_market_json_still_loads_unchanged():
    strategy_set = strategy_set_from_json(_LEGACY_SINGLE_MARKET_JSON)
    assert strategy_set.name == "6M Strategies"
    assert strategy_set.description == "legacy file"
    assert strategy_set.groups is None
    assert len(strategy_set.entries) == 1
    entry = strategy_set.entries[0]
    assert entry.name == "SOFR 6M Fly"
    assert entry.definition.market_key == "SOFR"
    assert entry.definition.offsets == (0, 2, 4)
    assert entry.definition.weights == (1.0, -2.0, 1.0)
    assert entry.definition.interval is BarInterval.DAILY
    assert entry.enabled is True


def test_legacy_single_market_json_reserializes_without_a_groups_key():
    strategy_set = strategy_set_from_json(_LEGACY_SINGLE_MARKET_JSON)
    reserialized = json.loads(strategy_set_to_json(strategy_set))
    assert "groups" not in reserialized
    assert reserialized == json.loads(_LEGACY_SINGLE_MARKET_JSON)


def test_legacy_intermarket_json_still_loads_unchanged():
    strategy_set = strategy_set_from_json(_LEGACY_INTERMARKET_JSON)
    assert strategy_set.groups is None
    assert strategy_set.entries == ()
    assert len(strategy_set.intermarket_entries) == 1
    definition = strategy_set.intermarket_entries[0].definition
    assert [leg.market_key for leg in definition.legs] == ["SOFR", "SONIA"]
    assert [leg.weight for leg in definition.legs] == [1.0, -1.0]


def test_legacy_intermarket_json_reserializes_without_a_groups_key():
    strategy_set = strategy_set_from_json(_LEGACY_INTERMARKET_JSON)
    reserialized = json.loads(strategy_set_to_json(strategy_set))
    assert "groups" not in reserialized
    assert reserialized == json.loads(_LEGACY_INTERMARKET_JSON)


# ---------------------------------------------------------------------
# Repository persistence
# ---------------------------------------------------------------------

def test_repository_round_trips_groups(repo):
    strategy_set = StrategySet(
        name="Combos",
        entries=(_fly_entry(),),
        groups=StrategyGroupPair(group_a=_group_a(), group_b=_group_b()),
    )
    repo.save(strategy_set)
    assert repo.load("Combos") == strategy_set


def test_repository_round_trips_groups_only_set(repo):
    strategy_set = StrategySet(
        name="Combos", entries=(), groups=StrategyGroupPair(group_a=_group_a())
    )
    repo.save(strategy_set)
    loaded = repo.load("Combos")
    assert loaded == strategy_set
    assert loaded.entries == ()
    assert loaded.groups.group_b is None


def test_repository_saved_file_contains_the_groups_key(repo):
    strategy_set = StrategySet(
        name="Combos", entries=(), groups=StrategyGroupPair(group_a=_group_a())
    )
    path = repo.save(strategy_set)
    with open(path) as f:
        raw = json.load(f)
    assert raw["groups"]["group_a"]["selected_entry_names"] == ["SR3 Fly", "SON Fly"]
    assert raw["groups"]["group_b"] is None


def test_repository_duplicate_preserves_groups(repo):
    strategy_set = StrategySet(
        name="Combos",
        entries=(),
        groups=StrategyGroupPair(group_a=_group_a(), group_b=_group_b()),
    )
    repo.save(strategy_set)
    copy = repo.duplicate("Combos", "Combos 2")
    assert copy.groups == strategy_set.groups
    assert repo.load("Combos 2").groups == strategy_set.groups


def test_repository_rename_preserves_groups(repo):
    strategy_set = StrategySet(
        name="Combos", entries=(), groups=StrategyGroupPair(group_a=_group_a())
    )
    repo.save(strategy_set)
    repo.rename("Combos", "Combos Renamed")
    assert repo.load("Combos Renamed").groups == strategy_set.groups


def test_repository_saving_a_non_grouped_set_writes_no_groups_key(repo):
    repo.save(StrategySet(name="Plain", entries=(_fly_entry(),)))
    with open(repo._path_for("Plain")) as f:
        raw = json.load(f)
    assert "groups" not in raw


def test_repository_loads_a_hand_written_legacy_file(repo, tmp_path):
    import os

    os.makedirs(repo.base_dir, exist_ok=True)
    with open(os.path.join(repo.base_dir, "6M Strategies.json"), "w") as f:
        f.write(_LEGACY_SINGLE_MARKET_JSON)
    loaded = repo.load("6M Strategies")
    assert loaded.groups is None
    assert [e.name for e in loaded.entries] == ["SOFR 6M Fly"]


# ---------------------------------------------------------------------
# Expansion of a composite needs a repository
#
# Phase 2 (strategy_sets/composite.py) turned `groups` into a real
# Group A x Group B expansion, resolved against the saved source
# Strategy Sets a group references -- so expanding a composite requires
# a StrategySetRepository. These two tests cover only that boundary from
# this file's model/persistence perspective; the combination engine's
# own behaviour (the Cartesian product, deduplication, orientation,
# dangling references) lives in tests/test_strategy_sets_composite.py.
# ---------------------------------------------------------------------

def test_expanding_a_composite_without_a_repository_is_a_clear_error():
    # Never silently expanded as if it had no groups.
    composite = StrategySet(
        name="Plain",
        entries=(_fly_entry(),),
        groups=StrategyGroupPair(group_a=_group_a(), group_b=_group_b()),
    )
    with pytest.raises(CompositeResolutionError, match="repository"):
        expand_strategy_set(composite, _START, _END)


def test_expanding_a_groups_only_set_without_a_repository_is_a_clear_error():
    groups_only = StrategySet(
        name="Combos", entries=(), groups=StrategyGroupPair(group_a=_group_a(), group_b=_group_b())
    )
    with pytest.raises(CompositeResolutionError, match="repository"):
        expand_strategy_set(groups_only, _START, _END)


def test_expansion_of_a_non_composite_set_is_unchanged_and_needs_no_repository():
    plain = StrategySet(name="Plain", entries=(_fly_entry(),))
    instances = expand_strategy_set(plain, _START, _END)
    assert instances
    assert all(i.definition is plain.entries[0].definition for i in instances)
