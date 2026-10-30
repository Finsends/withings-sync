"""Private, lossless SQLite storage for the local Withings collector."""

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, mode TEXT NOT NULL, group_count INTEGER NOT NULL DEFAULT 0,
 measurement_count INTEGER NOT NULL DEFAULT 0, response_count INTEGER NOT NULL DEFAULT 0,
 error_code TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
 account_id TEXT PRIMARY KEY, backfill_complete INTEGER NOT NULL,
 last_source_sync_at INTEGER NOT NULL, last_success_at TEXT NOT NULL,
 window_end_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_responses (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES sync_runs(id),
 category INTEGER NOT NULL, page_index INTEGER NOT NULL,
 requested_lastupdate INTEGER NOT NULL, requested_offset INTEGER,
 response_sha256 TEXT NOT NULL, response_json TEXT NOT NULL,
 UNIQUE(run_id, category, page_index)
);
CREATE TABLE IF NOT EXISTS measurement_groups (
 account_id TEXT NOT NULL, category INTEGER NOT NULL, group_id INTEGER NOT NULL,
 measured_at INTEGER NOT NULL, modified_at INTEGER, raw_json TEXT NOT NULL,
 PRIMARY KEY(account_id, category, group_id)
);
CREATE TABLE IF NOT EXISTS measurements (
 account_id TEXT NOT NULL, category INTEGER NOT NULL, group_id INTEGER NOT NULL,
 measurement_index INTEGER NOT NULL, measure_type INTEGER NOT NULL,
 raw_value INTEGER NOT NULL, unit INTEGER NOT NULL, raw_json TEXT NOT NULL,
 PRIMARY KEY(account_id, category, group_id, measurement_index),
 FOREIGN KEY(account_id, category, group_id)
 REFERENCES measurement_groups(account_id, category, group_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS measurements_type ON measurements(measure_type);
CREATE INDEX IF NOT EXISTS groups_date ON measurement_groups(measured_at);
PRAGMA user_version=1;
"""


def private_directory(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Private runtime must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def connect(dbpath: Path, readonly: bool = False) -> sqlite3.Connection:
    """Open with foreign keys, bounded contention, and dictionary-like rows."""
    dbpath = Path(dbpath)
    if dbpath.is_symlink():
        raise ValueError("Database must not be a symlink")
    if readonly:
        connection = sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        private_directory(dbpath.parent)
        fd = os.open(dbpath, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        dbpath.chmod(0o600)
        connection = sqlite3.connect(dbpath)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if not readonly:
        # WAL permits dashboard readers while one collector publishes a generation.
        # SQLite derives sidecar modes from the pre-created 0600 database.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        raise ValueError("Unsupported local database schema")
    connection.executescript(SCHEMA)
