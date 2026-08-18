"""
naming.py

Generates a unique Strategy Set name for import, without ever
overwriting an existing saved set. Pure function -- takes an `exists`
predicate rather than a StrategySetRepository directly, so it's
testable without touching the filesystem (matching the project-wide
convention of keeping filesystem I/O isolated to repository.py).

Suffix format is "Name 2", "Name 3", ... (space + number), NOT
"Name (2)" -- parentheses are not in strategy_sets.model's
_SET_NAME_PATTERN (letters, digits, spaces, '-', '_' only), so a
literal "(2)" suffix would fail StrategySet's own name validation the
moment it's constructed. This was confirmed with the user rather than
guessed; see the module docstring note in ui/strategy_import_view.py
for the same point made at the UI layer.
"""

from __future__ import annotations

from typing import Callable


def unique_strategy_set_name(base_name: str, exists: Callable[[str], bool]) -> str:
    """`base_name` if it doesn't already exist; otherwise "base_name 2",
    "base_name 3", ... -- the first suffix not already in use.

    Never mutates or truncates `base_name` itself beyond appending a
    suffix; a `base_name` that's already invalid (e.g. fails
    StrategySet's own name pattern) is not this function's concern --
    callers validate the base name separately (see
    strategy_import.preview, which rejects an invalid sheet/file name
    before ever reaching here) and never call this with one.
    """
    if not exists(base_name):
        return base_name

    suffix = 2
    while True:
        candidate = f"{base_name} {suffix}"
        if not exists(candidate):
            return candidate
        suffix += 1


__all__ = ["unique_strategy_set_name"]
