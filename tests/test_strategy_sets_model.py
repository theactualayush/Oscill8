"""
tests/test_strategy_sets_model.py

StrategySet/StrategySetEntry/ExpansionSettings construction and
validation, tested with hand-built objects -- no I/O.
"""

from __future__ import annotations

import dataclasses

import pytest

from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.model import ExpansionSettings, StrategySet, StrategySetEntry


def _expansion(**overrides) -> ExpansionSettings:
    return ExpansionSettings(**overrides)


def _definition(market_key="SOFR", offsets=(0, 1, 2), weights=(1, -2, 1)) -> StrategyDefinition:
    return StrategyDefinition(
        market_key=market_key, offsets=offsets, weights=weights, interval=BarInterval.DAILY,
    )


def _entry(name="SOFR Fly", **overrides) -> StrategySetEntry:
    defaults = dict(name=name, definition=_definition(), expansion=_expansion())
    defaults.update(overrides)
    return StrategySetEntry(**defaults)


# ---------------------------------------------------------------------
# ExpansionSettings
#
# Deliberately no contract_start/contract_end here -- see model.py's
# "Design correction" note: the contract window is a call-time
# expand_strategy_set() argument (matching ScanRequest), never part of
# this per-entry, persisted object.
# ---------------------------------------------------------------------

def test_expansion_settings_constructs_with_no_arguments():
    e = ExpansionSettings()
    assert e.max_curve_position is None
    assert e.eligible_rics is None


def test_expansion_settings_max_curve_position_negative_rejected():
    with pytest.raises(ValueError, match="max_curve_position"):
        _expansion(max_curve_position=-1)


def test_expansion_settings_max_curve_position_zero_allowed():
    e = _expansion(max_curve_position=0)
    assert e.max_curve_position == 0


def test_expansion_settings_eligible_rics_empty_rejected():
    with pytest.raises(ValueError, match="eligible_rics"):
        _expansion(eligible_rics=())


def test_expansion_settings_eligible_rics_stored_as_tuple():
    e = _expansion(eligible_rics=["SRAH26", "SRAM26"])
    assert e.eligible_rics == ("SRAH26", "SRAM26")


# ---------------------------------------------------------------------
# StrategySetEntry
# ---------------------------------------------------------------------

def test_entry_constructs_with_defaults_enabled():
    entry = _entry()
    assert entry.enabled is True
    assert entry.name == "SOFR Fly"


def test_entry_defaults_expansion_when_omitted():
    entry = StrategySetEntry(name="SOFR Fly", definition=_definition())
    assert entry.expansion == ExpansionSettings()


def test_entry_can_be_constructed_disabled():
    entry = _entry(enabled=False)
    assert entry.enabled is False


def test_entry_empty_name_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        _entry(name="")


def test_entry_whitespace_only_name_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        _entry(name="   ")


def test_entry_wrong_definition_type_rejected():
    with pytest.raises(TypeError, match="StrategyDefinition"):
        _entry(definition={"not": "a definition"})


def test_entry_wrong_expansion_type_rejected():
    with pytest.raises(TypeError, match="ExpansionSettings"):
        _entry(expansion={"not": "expansion settings"})


def test_entry_is_frozen():
    entry = _entry()
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.enabled = False


# ---------------------------------------------------------------------
# StrategySet construction
# ---------------------------------------------------------------------

def test_valid_strategy_set_constructs():
    s = StrategySet(name="Churning", entries=(_entry("SOFR Fly"), _entry("SONIA Fly", definition=_definition("SONIA", (0,), (1,)))))
    assert s.name == "Churning"
    assert len(s.entries) == 2
    assert s.description == ""


def test_description_is_optional_and_stored():
    s = StrategySet(name="6M Strategies", entries=(_entry(),), description="6-month calendar shapes")
    assert s.description == "6-month calendar shapes"


def test_entries_list_input_is_stored_as_tuple():
    s = StrategySet(name="Medium Vol", entries=[_entry()])
    assert isinstance(s.entries, tuple)


def test_entries_from_different_markets_allowed_in_one_set():
    # "Intermarket Churning" is a named GROUPING of same-market entries
    # across different markets, not a genuine cross-market leg strategy
    # -- each entry still expands independently on its own market's
    # curve (see expansion.py).
    sofr = _entry("SOFR Fly")
    sonia = _entry("SONIA Fly", definition=_definition("SONIA", (0,), (1,)))
    s = StrategySet(name="Intermarket Churning", entries=(sofr, sonia))
    assert {e.definition.market_key for e in s.entries} == {"SOFR", "SONIA"}


def test_strategy_set_is_frozen():
    s = StrategySet(name="Churning", entries=(_entry(),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.name = "Renamed"


# ---------------------------------------------------------------------
# StrategySet.name validation
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    ["Churning", "6M Strategies", "Medium Vol", "Intermarket Churning", "FOMC", "a", "A" * 80, "leg_1-set"],
)
def test_valid_set_names_accepted(name):
    StrategySet(name=name, entries=(_entry(),))


@pytest.mark.parametrize(
    "name",
    [
        "",
        " Churning",
        "-Churning",
        "Churning/2026",
        "Churning.json",
        "../etc",
        "A" * 81,
    ],
)
def test_invalid_set_names_rejected(name):
    with pytest.raises(ValueError):
        StrategySet(name=name, entries=(_entry(),))


# ---------------------------------------------------------------------
# StrategySet.entries validation
# ---------------------------------------------------------------------

def test_empty_entries_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        StrategySet(name="Empty", entries=())


def test_non_entry_objects_rejected():
    with pytest.raises(TypeError):
        StrategySet(name="Bad", entries=({"not": "an entry"},))


def test_duplicate_entry_names_within_a_set_rejected():
    with pytest.raises(ValueError, match="unique"):
        StrategySet(name="Churning", entries=(_entry("SOFR Fly"), _entry("SOFR Fly")))


def test_entries_with_identical_shape_but_different_names_are_allowed():
    a = _entry("SOFR Fly A")
    b = _entry("SOFR Fly B")  # same definition/expansion, different name
    s = StrategySet(name="Churning", entries=(a, b))
    assert len(s.entries) == 2
