"""Thread-safe and async-friendly SQLite WAL database manager."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .schema import SCHEMA_STATEMENTS, SCHEMA_VERSION

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_DATABASE_PATH = DEFAULT_DATA_DIR / "sokoban.sqlite3"


def dump_json(value: Any) -> str:
    """Serialize a Python value as compact, deterministic UTF-8 JSON."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def load_json(raw: str | None, default: Any = None) -> Any:
    """Deserialize JSON text or return `default` when input is None or empty."""
    if raw is None or raw == "":
        return default
    return json.loads(raw)


def _configure_connection(conn: sqlite3.Connection, *, enable_wal: bool) -> None:
    conn.row_factory = sqlite3.Row
    if enable_wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def _apply_schema(conn: sqlite3.Connection) -> None:
    version_row = conn.execute("PRAGMA user_version").fetchone()
    current_version = int(version_row[0]) if version_row is not None else 0
    if current_version == SCHEMA_VERSION:
        return
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported database schema version {current_version}; expected <= {SCHEMA_VERSION}"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        current_version = int(version_row[0]) if version_row is not None else 0
        if current_version == 0:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


class SQLiteDatabase:
    """Thread-safe SQLite WAL connection factory with short read/write transactions."""

    def __init__(self, db_path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        self.raw_path = str(db_path)
        self.is_memory = self.raw_path == ":memory:"
        self.db_path = Path(db_path)
        self._init_lock = threading.Lock()
        self._initialized = False
        self._memory_lock = threading.RLock()
        self._memory_conn: sqlite3.Connection | None = None

    def _open_file_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        _configure_connection(conn, enable_wal=True)
        return conn

    def initialize(self) -> None:
        """Create schema and stamp PRAGMA user_version once without table scans."""
        file_missing = not self.is_memory and not self.db_path.exists()
        if self._initialized and not file_missing:
            return
        with self._init_lock:
            file_missing = not self.is_memory and not self.db_path.exists()
            if self._initialized and not file_missing:
                return
            if self.is_memory:
                with self._memory_lock:
                    if self._memory_conn is None:
                        conn = sqlite3.connect(
                            ":memory:",
                            timeout=5.0,
                            isolation_level=None,
                            check_same_thread=False,
                        )
                        _configure_connection(conn, enable_wal=True)
                        self._memory_conn = conn
                    _apply_schema(self._memory_conn)
            else:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = self._open_file_connection()
                try:
                    _apply_schema(conn)
                finally:
                    conn.close()
            self._initialized = True

    @contextmanager
    def read_conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a read-only connection snapshot; never holds a write lock."""
        self.initialize()
        if self.is_memory:
            with self._memory_lock:
                assert self._memory_conn is not None
                conn = self._memory_conn
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                try:
                    yield conn
                finally:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    conn.execute("PRAGMA query_only=OFF")
            return

        conn = self._open_file_connection()
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn
        finally:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            finally:
                conn.close()

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """Yield a short BEGIN IMMEDIATE write transaction (<10ms commits only)."""
        self.initialize()
        if self.is_memory:
            with self._memory_lock:
                assert self._memory_conn is not None
                conn = self._memory_conn
                conn.execute("PRAGMA query_only=OFF")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            return

        conn = self._open_file_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def close(self) -> None:
        """Close persistent in-memory connection if open."""
        with self._memory_lock:
            if self._memory_conn is not None:
                self._memory_conn.close()
                self._memory_conn = None
                self._initialized = False
