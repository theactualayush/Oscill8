"""
tests/test_strategy_sets_repository.py

StrategySetRepository tested against an isolated tmp_path directory,
so tests never touch the real data/strategy_sets/ -- same pattern
tests/conftest.py already uses for database/ (tmp_path-backed SQLite
engine instead of the real data/oscill8.db).
"""

from __future__ import annotations

import os

import pytest

from core import config
from core.config import BarInterval
from strategy_engine.definitions import StrategyDefinition
from strategy_sets.model import ExpansionSettings, StrategySet, StrategySetEntry
from strategy_sets.repository import StrategySetRepository


def _expansion(**overrides) -> ExpansionSettings:
    return ExpansionSettings(**overrides)


def _fly_entry(name="SOFR Fly") -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1, 2), weights=(1, -2, 1), interval=BarInterval.DAILY,
    )
    return StrategySetEntry(name=name, definition=definition, expansion=_expansion())


def _spread_entry(name="SOFR Spread") -> StrategySetEntry:
    definition = StrategyDefinition(
        market_key="SOFR", offsets=(0, 1), weights=(1, -1), interval=BarInterval.DAILY,
    )
    return StrategySetEntry(name=name, definition=definition, expansion=_expansion())


@pytest.fixture
def repo(tmp_path):
    return StrategySetRepository(base_dir=str(tmp_path / "strategy_sets"))


# ---------------------------------------------------------------------
# Construction / defaults
# ---------------------------------------------------------------------

def test_default_base_dir_is_config_strategy_sets_dir():
    r = StrategySetRepository()
    assert r.base_dir == config.STRATEGY_SETS_DIR


def test_constructing_repository_does_not_touch_filesystem(tmp_path):
    base_dir = tmp_path / "never_created"
    StrategySetRepository(base_dir=str(base_dir))
    assert not base_dir.exists()


# ---------------------------------------------------------------------
# save / load
# ---------------------------------------------------------------------

def test_save_creates_directory_and_json_file(repo):
    s = StrategySet(name="Churning", entries=(_fly_entry(),))
    path = repo.save(s)
    assert os.path.isdir(repo.base_dir)
    assert path == os.path.join(repo.base_dir, "Churning.json")
    assert os.path.isfile(path)


def test_save_then_load_round_trips(repo):
    original = StrategySet(name="6M Strategies", entries=(_fly_entry(), _spread_entry()), description="calendar shapes")
    repo.save(original)
    restored = repo.load("6M Strategies")
    assert restored == original


def test_save_overwrites_existing_set_with_same_name(repo):
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    repo.save(StrategySet(name="Churning", entries=(_spread_entry(),)))
    restored = repo.load("Churning")
    assert restored.entries == (_spread_entry(),)


def test_load_missing_set_raises_file_not_found(repo):
    with pytest.raises(FileNotFoundError, match="Nonexistent"):
        repo.load("Nonexistent")


def test_saved_file_is_human_readable_json(repo):
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    path = os.path.join(repo.base_dir, "Churning.json")
    with open(path) as f:
        text = f.read()
    assert '"name": "Churning"' in text
    assert '"market_key": "SOFR"' in text
    assert "\n" in text


# ---------------------------------------------------------------------
# list / exists
# ---------------------------------------------------------------------

def test_list_names_empty_before_first_save(repo):
    assert repo.list_names() == []


def test_list_names_returns_sorted_saved_names(repo):
    repo.save(StrategySet(name="Medium Vol", entries=(_fly_entry(),)))
    repo.save(StrategySet(name="Churning", entries=(_spread_entry(),)))
    repo.save(StrategySet(name="6M Strategies", entries=(_fly_entry(),)))
    assert repo.list_names() == ["6M Strategies", "Churning", "Medium Vol"]


def test_exists_true_after_save_false_before(repo):
    assert repo.exists("Churning") is False
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    assert repo.exists("Churning") is True


# ---------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------

def test_delete_removes_saved_set_and_returns_true(repo):
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    assert repo.delete("Churning") is True
    assert repo.exists("Churning") is False
    assert repo.list_names() == []


def test_delete_nonexistent_set_returns_false_not_error(repo):
    assert repo.delete("Nonexistent") is False


# ---------------------------------------------------------------------
# duplicate
# ---------------------------------------------------------------------

def test_duplicate_creates_a_copy_under_new_name(repo):
    original = StrategySet(name="Churning", entries=(_fly_entry(), _spread_entry()), description="d")
    repo.save(original)
    copy = repo.duplicate("Churning", "Churning Copy")
    assert copy.name == "Churning Copy"
    assert copy.entries == original.entries
    assert copy.description == original.description
    assert repo.load("Churning Copy") == copy


def test_duplicate_leaves_original_untouched(repo):
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    repo.duplicate("Churning", "Churning Copy")
    assert repo.exists("Churning") is True
    assert repo.load("Churning").name == "Churning"


def test_duplicate_missing_source_raises_file_not_found(repo):
    with pytest.raises(FileNotFoundError):
        repo.duplicate("Nonexistent", "New Name")


def test_duplicate_onto_existing_name_raises_file_exists(repo):
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    repo.save(StrategySet(name="Medium Vol", entries=(_spread_entry(),)))
    with pytest.raises(FileExistsError, match="Medium Vol"):
        repo.duplicate("Churning", "Medium Vol")


def test_duplicate_onto_invalid_name_raises_value_error(repo):
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    with pytest.raises(ValueError):
        repo.duplicate("Churning", "bad/name")


# ---------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------

def test_rename_moves_set_to_new_name(repo):
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    renamed = repo.rename("Churning", "Aggressive Churning")
    assert renamed.name == "Aggressive Churning"
    assert repo.exists("Churning") is False
    assert repo.exists("Aggressive Churning") is True
    assert repo.load("Aggressive Churning").entries == (_fly_entry(),)


def test_rename_to_same_name_is_a_no_op(repo):
    original = StrategySet(name="Churning", entries=(_fly_entry(),))
    repo.save(original)
    result = repo.rename("Churning", "Churning")
    assert result == original
    assert repo.exists("Churning") is True


def test_rename_missing_source_raises_file_not_found(repo):
    with pytest.raises(FileNotFoundError):
        repo.rename("Nonexistent", "New Name")


def test_rename_onto_existing_different_name_raises_file_exists(repo):
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    repo.save(StrategySet(name="Medium Vol", entries=(_spread_entry(),)))
    with pytest.raises(FileExistsError, match="Medium Vol"):
        repo.rename("Churning", "Medium Vol")
    # Neither set should have been touched by the failed rename.
    assert repo.exists("Churning") is True
    assert repo.load("Medium Vol").entries == (_spread_entry(),)


def test_rename_onto_invalid_name_raises_value_error(repo):
    repo.save(StrategySet(name="Churning", entries=(_fly_entry(),)))
    with pytest.raises(ValueError):
        repo.rename("Churning", "bad/name")
    assert repo.exists("Churning") is True  # original untouched on failure
