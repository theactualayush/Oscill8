"""
tests/test_strategy_import_naming.py

strategy_import.naming.unique_strategy_set_name() -- never overwrites,
generates "Name", "Name 2", "Name 3", ... (space + number, not "(2)" --
see naming.py's module docstring for why parentheses are rejected by
strategy_sets.model's own name pattern).
"""

from __future__ import annotations

from strategy_import.naming import unique_strategy_set_name


def test_returns_base_name_when_unused():
    assert unique_strategy_set_name("6M Strategies", lambda n: False) == "6M Strategies"


def test_appends_2_when_base_name_taken():
    existing = {"6M Strategies"}
    assert unique_strategy_set_name("6M Strategies", existing.__contains__) == "6M Strategies 2"


def test_increments_past_multiple_collisions():
    existing = {"Churning", "Churning 2", "Churning 3"}
    assert unique_strategy_set_name("Churning", existing.__contains__) == "Churning 4"


def test_never_reuses_an_existing_name():
    existing = {"Churning", "Churning 2"}
    result = unique_strategy_set_name("Churning", existing.__contains__)
    assert result not in existing


def test_suffix_has_no_parentheses():
    existing = {"Set"}
    result = unique_strategy_set_name("Set", existing.__contains__)
    assert "(" not in result and ")" not in result
    assert result == "Set 2"
