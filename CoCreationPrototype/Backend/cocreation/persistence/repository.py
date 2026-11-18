"""Persistence facade for the 8010 SQLite repository (zero import-time disk I/O)."""
from . import backfill, database, session_projection
from .database import SQLiteRepository

_EXPORT_MODULES = (database, backfill, session_projection)
for _mod in _EXPORT_MODULES:
    for _k, _v in vars(_mod).items():
        if not (_k.startswith("__") and _k.endswith("__")):
            globals()[_k] = _v
del _mod, _k, _v, _EXPORT_MODULES
