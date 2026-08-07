"""
repository.py

File-backed persistence for StrategySet: one JSON file per set, named
after the set's own `name`. This is the only module in strategy_sets/
that touches the filesystem -- serialization.py stays pure dict/JSON
conversion, exactly as database/cache.py (DB I/O) and
database/service.py (orchestration) are kept separate in Module 2.

Responsible ONLY for save / load / list / duplicate / rename / delete
-- no validation logic of its own beyond what StrategySet/
StrategySetEntry/ExpansionSettings already enforce on construction
(dataclasses.replace() re-runs __post_init__, so a rename/duplicate
into an invalid name is rejected the same way a plain construction
would be), and no expansion (see expansion.py).

Filename safety relies on StrategySet.__post_init__'s own name
validation (strategy_sets/model.py's _SET_NAME_PATTERN): a valid
StrategySet name can only ever contain letters, digits, spaces, '-',
or '_', which is already a safe, unambiguous filename on every common
filesystem -- so `name` is used directly (plus a ".json" suffix) with
no separate slugification step, and a saved set's filename always
round-trips losslessly back to its exact `name`.
"""

from __future__ import annotations

import os
from dataclasses import replace

from core import config
from core.utils import get_logger

from strategy_sets.model import StrategySet
from strategy_sets.serialization import strategy_set_from_json, strategy_set_to_json

logger = get_logger(__name__)


class StrategySetRepository:
    """Save/load/list/duplicate/rename/delete named StrategySets as
    JSON files in one directory.

    Directory creation is deferred to the first save() call, matching
    database/connection.py's "create the parent directory at call
    time, not as an import/construction side effect" convention --
    instantiating a repository never touches the filesystem.
    """

    def __init__(self, base_dir: str | None = None):
        """`base_dir` defaults to core.config.STRATEGY_SETS_DIR
        (override via the RBS_STRATEGY_SETS_DIR env var). Tests should
        always pass an explicit tmp_path-backed directory instead of
        relying on the default, exactly as database/ tests never touch
        the real data/oscill8.db."""
        self.base_dir = base_dir or config.STRATEGY_SETS_DIR

    def _path_for(self, name: str) -> str:
        return os.path.join(self.base_dir, f"{name}.json")

    def save(self, strategy_set: StrategySet) -> str:
        """Write `strategy_set` to its own JSON file, overwriting any
        existing file already saved under the same name (upsert
        semantics -- saving is never an error just because a set with
        that name already exists). Returns the file path written.
        """
        os.makedirs(self.base_dir, exist_ok=True)
        path = self._path_for(strategy_set.name)
        with open(path, "w") as f:
            f.write(strategy_set_to_json(strategy_set))
        logger.info("Saved StrategySet '%s' -> %s", strategy_set.name, path)
        return path

    def load(self, name: str) -> StrategySet:
        """Load the StrategySet previously saved under `name`.

        Raises:
            FileNotFoundError: no StrategySet has been saved under
                `name` (a clear, actionable message listing the
                directory searched, not a raw OS-level traceback).
        """
        path = self._path_for(name)
        if not os.path.exists(path):
            raise FileNotFoundError(f"No StrategySet named '{name}' found in {self.base_dir}")
        with open(path) as f:
            return strategy_set_from_json(f.read())

    def list_names(self) -> list[str]:
        """Names of every StrategySet currently saved, sorted
        alphabetically.

        Returns [] (not an error) if the directory doesn't exist yet,
        i.e. before the first save() call.
        """
        if not os.path.isdir(self.base_dir):
            return []
        names = [
            os.path.splitext(filename)[0]
            for filename in os.listdir(self.base_dir)
            if filename.endswith(".json")
        ]
        return sorted(names)

    def exists(self, name: str) -> bool:
        return os.path.exists(self._path_for(name))

    def delete(self, name: str) -> bool:
        """Delete the saved StrategySet named `name`.

        Returns:
            True if a file was actually removed, False if none existed
            under that name -- delete() is idempotent, never an error
            for a name that isn't (or is no longer) saved.
        """
        path = self._path_for(name)
        if not os.path.exists(path):
            return False
        os.remove(path)
        logger.info("Deleted StrategySet '%s' (%s)", name, path)
        return True

    def duplicate(self, name: str, new_name: str) -> StrategySet:
        """Load `name` and save an identical copy (same entries/
        description) under `new_name`, leaving the original saved set
        under `name` untouched.

        Raises:
            FileNotFoundError: `name` doesn't exist.
            ValueError: `new_name` is not a valid StrategySet name
                (StrategySet's own name validation, re-run by
                dataclasses.replace()).
            FileExistsError: `new_name` is already in use -- duplicate
                never silently overwrites an existing set (unlike
                save(), which is an intentional upsert).
        """
        if self.exists(new_name):
            raise FileExistsError(f"A StrategySet named '{new_name}' already exists")
        original = self.load(name)
        copy = replace(original, name=new_name)
        self.save(copy)
        logger.info("Duplicated StrategySet '%s' -> '%s'", name, new_name)
        return copy

    def rename(self, name: str, new_name: str) -> StrategySet:
        """Rename the StrategySet saved under `name` to `new_name`.
        A no-op (just loads and returns it) if `new_name == name`.

        Raises:
            FileNotFoundError: `name` doesn't exist.
            ValueError: `new_name` is not a valid StrategySet name.
            FileExistsError: `new_name` is already in use by a
                DIFFERENT saved set.
        """
        if name == new_name:
            return self.load(name)
        if self.exists(new_name):
            raise FileExistsError(f"A StrategySet named '{new_name}' already exists")
        original = self.load(name)
        renamed = replace(original, name=new_name)
        self.save(renamed)
        self.delete(name)
        logger.info("Renamed StrategySet '%s' -> '%s'", name, new_name)
        return renamed
