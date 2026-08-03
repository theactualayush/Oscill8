"""
database package

Local SQLite market-data cache sitting between core.downloader (LSEG)
and future consumers. The only advertised public entry point is
get_history -- connection.py/models.py/cache.py are internal
implementation modules.
"""

from database.service import get_history

__all__ = ["get_history"]
