"""
database package

Local SQLite market-data cache sitting between the provider layer
(core.downloader for LSEG, core.quanthub for QuantHub) and consumers.
The advertised public entry points are get_history (single RIC) and
get_history_batch (many RICs, batching QuantHub-routed ones into fewer
HTTP requests) -- connection.py/models.py/cache.py are internal
implementation modules.
"""

from database.service import get_history, get_history_batch

__all__ = ["get_history", "get_history_batch"]
