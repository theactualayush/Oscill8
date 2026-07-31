"""
utils.py

Small, generic helpers shared across modules (logging setup, date
coercion, etc.). Nothing here should import from any other project
module, to keep this safe to import from anywhere.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from typing import Union

import config

DateLike = Union[str, date, datetime]


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger that writes to both console and file.

    Safe to call repeatedly (e.g. once per module via
    `logger = get_logger(__name__)`) -- handlers are only attached once
    per logger name.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(config.LOG_LEVEL)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(config.LOG_FILE)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def to_date(value: DateLike) -> date:
    """Coerce a str / date / datetime into a plain `date`.

    Accepts ISO format strings ("2026-07-31").
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d").date()
    raise TypeError(f"Cannot coerce {type(value)} to date")
