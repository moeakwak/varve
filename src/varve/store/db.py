"""SQLite ``state.db``: schema, connections, and metadata checks."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storages (
    storage_name TEXT PRIMARY KEY,
    root_path    TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
    branch_id        TEXT PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    is_temp          INTEGER NOT NULL DEFAULT 0,
    manual           INTEGER NOT NULL DEFAULT 0,
    config_json      TEXT NOT NULL,
    active_axes_json TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    branch_id     TEXT NOT NULL REFERENCES branches(branch_id),
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    status        TEXT NOT NULL,
    cli_argv_json TEXT NOT NULL,
    git_commit    TEXT,
    git_dirty     INTEGER,
    total_wall_ms INTEGER,
    host          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_branch ON runs(branch_id, started_at);

CREATE TABLE IF NOT EXISTS stage_runs (
    run_id               TEXT NOT NULL REFERENCES runs(run_id),
    branch_id            TEXT NOT NULL,
    stage_name           TEXT NOT NULL,
    status               TEXT NOT NULL,
    executed_units       INTEGER NOT NULL,
    hit_units            INTEGER NOT NULL,
    failed_units         INTEGER NOT NULL,
    total_units          INTEGER NOT NULL,
    workers              INTEGER NOT NULL,
    wall_ms              INTEGER NOT NULL,
    unit_p50_ms          INTEGER,
    unit_p95_ms          INTEGER,
    unit_max_ms          INTEGER,
    peak_rss_mb          REAL,
    metrics_summary_json TEXT,
    finished_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, stage_name)
);
CREATE INDEX IF NOT EXISTS stage_runs_history ON stage_runs(branch_id, stage_name, finished_at);

CREATE TABLE IF NOT EXISTS file_sets (
    fileset_hash TEXT PRIMARY KEY,
    file_count   INTEGER NOT NULL,
    total_bytes  INTEGER NOT NULL,
    entries_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS key_parts (
    part_key  TEXT PRIMARY KEY,
    part_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_fp   TEXT PRIMARY KEY,
    detail_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    branch_id              TEXT NOT NULL REFERENCES branches(branch_id),
    stage_name             TEXT NOT NULL,
    unit_key               TEXT NOT NULL,
    storage_name           TEXT NOT NULL REFERENCES storages(storage_name),
    input_key              TEXT NOT NULL,
    key_components_json    TEXT NOT NULL,
    config_access_json     TEXT,
    source_fp              TEXT NOT NULL,
    reused_source_fp       TEXT,
    status                 TEXT NOT NULL,
    artifact_relpath       TEXT,
    artifact_fp            TEXT,
    artifact_manifest_json TEXT,
    artifact_files         INTEGER,
    artifact_bytes         INTEGER,
    metrics_json           TEXT,
    duration_ms            INTEGER,
    peak_rss_mb            REAL,
    error_type             TEXT,
    error_summary          TEXT,
    error_traceback        TEXT,
    last_run_id            TEXT REFERENCES runs(run_id),
    linked_from            TEXT,
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (branch_id, stage_name, unit_key)
);
CREATE INDEX IF NOT EXISTS units_by_stage ON units(stage_name, status);

CREATE TABLE IF NOT EXISTS stat_cache (
    norm_path    TEXT PRIMARY KEY,
    inode        INTEGER NOT NULL,
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    ast_hash     TEXT
) WITHOUT ROWID;
"""


class SchemaError(RuntimeError):
    """``state.db`` was written by an incompatible varve version."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open ``state.db`` (WAL, ``synchronous=NORMAL``); create the schema when writable.

    Read-only opens use a ``mode=ro`` URI and never create files. Connections
    run in autocommit mode; group writes with :func:`transaction`.
    """

    if readonly:
        if not path.exists():
            raise FileNotFoundError(f"No varve state database at {path}")
        conn = _open(path, readonly=True)
        try:
            if _is_blank(conn):
                # A writer creates the file and its schema under the init lock;
                # wait for it rather than reporting a half-created store as foreign.
                conn.close()
                _wait_for_init(path)
                conn = _open(path, readonly=True)
            _check_schema(conn, path, readonly=True)
        except BaseException:
            conn.close()
            raise
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    # Switching to WAL needs an exclusive lock that SQLite does not wait for,
    # and creating the schema must not race another process that sees the same
    # empty file, so writable opens initialize one at a time. The lock is taken
    # before the file can be created, so readers can wait for a blank store.
    with _init_lock(path):
        conn = _open(path, readonly=False)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            _check_schema(conn, path, readonly=False)
        except BaseException:
            conn.close()
            raise
    return conn


def _open(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        target, uri = f"{path.absolute().as_uri()}?mode=ro", True
    else:
        target, uri = str(path), False
    conn = sqlite3.connect(target, uri=uri, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        conn.close()
        raise
    return conn


def _is_blank(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None


def _init_lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".init-lock")


def _wait_for_init(path: Path) -> None:
    """Block until no writable open is initializing ``path`` (never creates files)."""

    try:
        fd = os.open(_init_lock_path(path), os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
    finally:
        os.close(fd)


@contextmanager
def _init_lock(path: Path) -> Iterator[None]:
    fd = os.open(_init_lock_path(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _check_schema(conn: sqlite3.Connection, path: Path, *, readonly: bool) -> None:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if has_meta is None:
        has_tables = conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        if readonly or has_tables is not None:
            raise SchemaError(
                f"{path} is not a varve {SCHEMA_VERSION} state database; "
                "remove it with `clean` to start over"
            )
        # executescript commits any open transaction, so the script carries its own.
        conn.executescript(
            f"BEGIN IMMEDIATE;{SCHEMA}"
            f"INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');"
            "COMMIT;"
        )
        return
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    found = row[0] if row is not None else None
    if found != SCHEMA_VERSION:
        raise SchemaError(
            f"{path} has state schema {found!r}, this varve expects {SCHEMA_VERSION!r}; "
            "there is no migration: run `clean` to start over"
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` … ``COMMIT``; nested use joins the outer transaction."""

    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}


def write_meta(conn: sqlite3.Connection, values: Mapping[str, str]) -> None:
    """Upsert metadata keys (``pipeline_name``, ``module``, ``qualname``, ``root``, ``graph_json``)."""

    with transaction(conn):
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(key, value) for key, value in values.items() if key != "schema_version"],
        )
