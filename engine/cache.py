"""
E2 — SQLite Build Trace Cache

Maintains persistent records of previous successful builds:
- Task ID and rule name
- Command hash
- Input content fingerprints (SHA-256)
- Full task fingerprints
- Output file fingerprints
- Build timestamps and metadata

Ensures:
- Transactions for data integrity
- Failed builds are NEVER recorded as successful
- Corrupted database recovery
- Schema initialization and versioning
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class BuildTrace:
    """Persistent trace of a single successful task execution."""
    task_id: str
    rule_name: str
    command_hash: str
    input_fingerprint: str
    full_fingerprint: str
    output_fingerprints: dict[str, str] = field(default_factory=dict)
    build_timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class CacheError(Exception):
    """Exception raised for database cache errors."""
    pass


SCHEMA_VERSION = 1

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS build_traces (
    task_id TEXT PRIMARY KEY,
    rule_name TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    full_fingerprint TEXT NOT NULL,
    output_fingerprints TEXT NOT NULL,
    build_timestamp REAL NOT NULL,
    metadata TEXT NOT NULL
);
"""

CREATE_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class BuildCache:
    """
    SQLite-backed persistent build trace cache.

    Thread-safe connection handling and automatic database initialization.
    """

    def __init__(self, db_path: str = "data/build_state.db"):
        self.db_path = db_path if db_path == ":memory:" else os.path.abspath(db_path)
        if self.db_path != ":memory:":
            self._ensure_dir()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _ensure_dir(self) -> None:
        """Ensure the directory containing the database exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create the SQLite connection."""
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(
                    self.db_path,
                    timeout=30.0,
                    check_same_thread=False,
                )
                self._conn.row_factory = sqlite3.Row
                # Enable WAL mode for better concurrency and write speed
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA synchronous=NORMAL;")
            except sqlite3.DatabaseError as e:
                # Corrupt database recovery
                self._recover_corrupt_db()
                self._conn = sqlite3.connect(
                    self.db_path,
                    timeout=30.0,
                    check_same_thread=False,
                )
                self._conn.row_factory = sqlite3.Row
        return self._conn

    def _recover_corrupt_db(self) -> None:
        """Recover from a corrupted database file by renaming it."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if os.path.exists(self.db_path):
            backup_path = f"{self.db_path}.corrupt.{os.getpid()}"
            try:
                os.rename(self.db_path, backup_path)
            except OSError:
                try:
                    os.remove(self.db_path)
                except OSError:
                    pass

    def _init_db(self) -> None:
        """Initialize schema if not present."""
        try:
            conn = self._get_connection()
            with conn:
                conn.execute(CREATE_META_TABLE_SQL)
                conn.execute(CREATE_TABLE_SQL)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_info (key, value) VALUES ('version', ?);",
                    (str(SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            conn = self._get_connection()
            with conn:
                conn.execute(CREATE_META_TABLE_SQL)
                conn.execute(CREATE_TABLE_SQL)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_info (key, value) VALUES ('version', ?);",
                    (str(SCHEMA_VERSION),),
                )

    # ─────────────────────────────────────────────────────────
    # Public Cache Operations
    # ─────────────────────────────────────────────────────────

    def get_trace(self, task_id: str) -> Optional[BuildTrace]:
        """
        Retrieve the recorded build trace for a task ID, or None if no trace exists.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM build_traces WHERE task_id = ?;", (task_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None

            return BuildTrace(
                task_id=row["task_id"],
                rule_name=row["rule_name"],
                command_hash=row["command_hash"],
                input_fingerprint=row["input_fingerprint"],
                full_fingerprint=row["full_fingerprint"],
                output_fingerprints=json.loads(row["output_fingerprints"]),
                build_timestamp=row["build_timestamp"],
                metadata=json.loads(row["metadata"]),
            )
        except sqlite3.DatabaseError:
            self._recover_corrupt_db()
            return None

    def record_success(self, trace: BuildTrace) -> None:
        """
        Record a successful task execution trace into SQLite.
        Atomic upsert.
        """
        try:
            conn = self._get_connection()
            with conn:
                conn.execute(
                    """
                    INSERT INTO build_traces (
                        task_id, rule_name, command_hash, input_fingerprint,
                        full_fingerprint, output_fingerprints, build_timestamp, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        rule_name=excluded.rule_name,
                        command_hash=excluded.command_hash,
                        input_fingerprint=excluded.input_fingerprint,
                        full_fingerprint=excluded.full_fingerprint,
                        output_fingerprints=excluded.output_fingerprints,
                        build_timestamp=excluded.build_timestamp,
                        metadata=excluded.metadata;
                    """,
                    (
                        trace.task_id,
                        trace.rule_name,
                        trace.command_hash,
                        trace.input_fingerprint,
                        trace.full_fingerprint,
                        json.dumps(trace.output_fingerprints, sort_keys=True),
                        trace.build_timestamp,
                        json.dumps(trace.metadata, sort_keys=True),
                    ),
                )
        except sqlite3.DatabaseError as e:
            raise CacheError(f"Failed to record build trace: {e}")

    def invalidate(self, task_id: str) -> None:
        """Remove the recorded trace for a task ID."""
        try:
            conn = self._get_connection()
            with conn:
                conn.execute("DELETE FROM build_traces WHERE task_id = ?;", (task_id,))
        except sqlite3.DatabaseError:
            pass

    def clear(self) -> None:
        """Clear all build traces from the cache."""
        try:
            conn = self._get_connection()
            with conn:
                conn.execute("DELETE FROM build_traces;")
        except sqlite3.DatabaseError:
            pass

    def get_all_traces(self) -> dict[str, BuildTrace]:
        """Return all stored build traces indexed by task_id."""
        result: dict[str, BuildTrace] = {}
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM build_traces;")
            for row in cursor.fetchall():
                result[row["task_id"]] = BuildTrace(
                    task_id=row["task_id"],
                    rule_name=row["rule_name"],
                    command_hash=row["command_hash"],
                    input_fingerprint=row["input_fingerprint"],
                    full_fingerprint=row["full_fingerprint"],
                    output_fingerprints=json.loads(row["output_fingerprints"]),
                    build_timestamp=row["build_timestamp"],
                    metadata=json.loads(row["metadata"]),
                )
        except sqlite3.DatabaseError:
            pass
        return result

    def close(self) -> None:
        """Close the database connection cleanly."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
