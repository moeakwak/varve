"""Typed operations over ``state.db``.

Row dataclasses hold decoded JSON columns (``*_json`` columns become Python
values on fields without the suffix). Every write runs in a transaction;
wrap several calls in :meth:`Repo.transaction` to batch them into one.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from varve.keying.fingerprint import CachedStat
from varve.keying.keys import FileSetRow
from varve.store.db import now_iso, read_meta, transaction, write_meta


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


@dataclass
class BranchRow:
    """``active_axes`` maps an axis name to its active ids, or ``{"sample": n, "seed": s}``."""

    branch_id: str
    name: str
    config: dict[str, Any]
    active_axes: dict[str, Any]
    is_temp: bool = False
    manual: bool = False
    created_at: str = ""
    updated_at: str = ""


@dataclass
class RunRow:
    run_id: str
    branch_id: str
    started_at: str
    status: str
    cli_argv: list[str]
    host: str
    ended_at: str | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    total_wall_ms: int | None = None


@dataclass
class StageRunRow:
    run_id: str
    branch_id: str
    stage_name: str
    status: str
    executed_units: int
    hit_units: int
    failed_units: int
    total_units: int
    workers: int
    wall_ms: int
    finished_at: str
    unit_p50_ms: int | None = None
    unit_p95_ms: int | None = None
    unit_max_ms: int | None = None
    peak_rss_mb: float | None = None
    metrics_summary: dict[str, Any] | None = None


@dataclass
class UnitRow:
    branch_id: str
    stage_name: str
    unit_key: str
    storage_name: str
    input_key: str
    key_components: dict[str, Any]
    source_fp: str
    source_detail: dict[str, Any]
    status: str
    config_access: list[str] | None = None
    reused_source_fp: str | None = None
    artifact_relpath: str | None = None
    artifact_fp: str | None = None
    artifact_manifest: list[list[Any]] | None = None
    artifact_files: int | None = None
    artifact_bytes: int | None = None
    metrics: dict[str, Any] | None = None
    duration_ms: int | None = None
    peak_rss_mb: float | None = None
    error_type: str | None = None
    error_summary: str | None = None
    error_traceback: str | None = None
    last_run_id: str | None = None
    linked_from: str | None = None
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ProbeRow:
    """Lightweight unit row for probes (JSON columns undecoded)."""

    unit_key: str
    status: str
    storage_name: str
    input_key: str
    config_access_json: str | None
    source_fp: str
    reused_source_fp: str | None
    artifact_relpath: str | None
    artifact_fp: str | None
    artifact_manifest_json: str | None
    linked_from: str | None
    error_summary: str | None

    @property
    def config_access(self) -> list[str] | None:
        return _loads(self.config_access_json)

    @property
    def artifact_manifest(self) -> list[list[Any]]:
        return _loads(self.artifact_manifest_json) or []


_JSON_FIELDS = {
    "config",
    "active_axes",
    "cli_argv",
    "metrics_summary",
    "key_components",
    "config_access",
    "source_detail",
    "artifact_manifest",
    "metrics",
}
_BOOL_FIELDS = {"is_temp", "manual", "git_dirty"}


def _column(name: str) -> str:
    return f"{name}_json" if name in _JSON_FIELDS else name


def _encode(row: Any) -> dict[str, Any]:
    encoded: dict[str, Any] = {}
    for item in fields(row):
        key, value = item.name, getattr(row, item.name)
        if key in _JSON_FIELDS:
            value = _dumps(value)
        elif key in _BOOL_FIELDS and value is not None:
            value = int(value)
        encoded[_column(key)] = value
    return encoded


def _decode(cls: type[Any], row: sqlite3.Row) -> Any:
    values: dict[str, Any] = {}
    for item in fields(cls):
        value = row[_column(item.name)]
        if item.name in _JSON_FIELDS:
            value = _loads(value)
        elif item.name in _BOOL_FIELDS and value is not None:
            value = bool(value)
        values[item.name] = value
    return cls(**values)


def _columns(cls: type[Any]) -> list[str]:
    return [_column(item.name) for item in fields(cls)]


class Repo:
    """Typed queries and commands over one open ``state.db`` connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with transaction(self.conn):
            yield

    def _upsert(self, table: str, rows: Iterable[Any], key: Iterable[str]) -> int:
        encoded = [_encode(row) for row in rows]
        if not encoded:
            return 0
        columns = list(encoded[0])
        keys = list(key)
        updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in keys)
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}"
        )
        with transaction(self.conn):
            self.conn.executemany(sql, [tuple(row[c] for c in columns) for row in encoded])
        return len(encoded)

    # -- branches -----------------------------------------------------------

    def upsert_branch(self, row: BranchRow) -> BranchRow:
        """Insert or update by name; ``created_at`` and ``branch_id`` of an existing row are kept."""

        now = now_iso()
        existing = self.get_branch(row.name)
        row.created_at = existing.created_at if existing else (row.created_at or now)
        row.branch_id = existing.branch_id if existing else row.branch_id
        row.updated_at = now
        self._upsert("branches", [row], ["branch_id"])
        return row

    def get_branch(self, name: str) -> BranchRow | None:
        found = self.conn.execute(
            f"SELECT {', '.join(_columns(BranchRow))} FROM branches WHERE name = ?", (name,)
        ).fetchone()
        return None if found is None else _decode(BranchRow, found)

    def list_branches(self) -> list[BranchRow]:
        rows = self.conn.execute(
            f"SELECT {', '.join(_columns(BranchRow))} FROM branches ORDER BY name"
        )
        return [_decode(BranchRow, row) for row in rows]

    def delete_branch(self, branch_id: str) -> None:
        """Delete a branch with its units, stage runs, and runs."""

        with transaction(self.conn):
            self.conn.execute("DELETE FROM units WHERE branch_id = ?", (branch_id,))
            self.conn.execute("DELETE FROM stage_runs WHERE branch_id = ?", (branch_id,))
            self.conn.execute("DELETE FROM runs WHERE branch_id = ?", (branch_id,))
            self.conn.execute("DELETE FROM branches WHERE branch_id = ?", (branch_id,))

    # -- storages -----------------------------------------------------------

    def upsert_storages(self, roots: Mapping[str, Path]) -> None:
        now = now_iso()
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT INTO storages(storage_name, root_path, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(storage_name) DO UPDATE SET root_path = excluded.root_path, "
                "updated_at = excluded.updated_at",
                [(name, str(path), now) for name, path in roots.items()],
            )

    def storages(self) -> dict[str, Path]:
        rows = self.conn.execute("SELECT storage_name, root_path FROM storages")
        return {row[0]: Path(row[1]) for row in rows}

    # -- runs ---------------------------------------------------------------

    def insert_run(self, row: RunRow) -> None:
        self._upsert("runs", [row], ["run_id"])

    def finish_run(self, run_id: str, *, status: str, total_wall_ms: int | None) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE runs SET status = ?, ended_at = ?, total_wall_ms = ? WHERE run_id = ?",
                (status, now_iso(), total_wall_ms, run_id),
            )

    def get_run(self, run_id: str) -> RunRow | None:
        found = self.conn.execute(
            f"SELECT {', '.join(_columns(RunRow))} FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if found is None else _decode(RunRow, found)

    def runs(self, branch_id: str, *, limit: int = 50) -> list[RunRow]:
        rows = self.conn.execute(
            f"SELECT {', '.join(_columns(RunRow))} FROM runs WHERE branch_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (branch_id, limit),
        )
        return [_decode(RunRow, row) for row in rows]

    def running_run_ids(self) -> set[str]:
        """Ids of runs of any branch still recorded as ``running``."""

        rows = self.conn.execute("SELECT run_id FROM runs WHERE status = 'running'")
        return {row[0] for row in rows}

    def put_stage_run(self, row: StageRunRow) -> None:
        self._upsert("stage_runs", [row], ["run_id", "stage_name"])

    def stage_runs(
        self, branch_id: str, *, stage: str | None = None, limit: int = 50
    ) -> list[StageRunRow]:
        where, params = "branch_id = ?", [branch_id]
        if stage is not None:
            where, params = f"{where} AND stage_name = ?", [*params, stage]
        rows = self.conn.execute(
            f"SELECT {', '.join(_columns(StageRunRow))} FROM stage_runs WHERE {where} "
            "ORDER BY finished_at DESC LIMIT ?",
            (*params, limit),
        )
        return [_decode(StageRunRow, row) for row in rows]

    # -- units --------------------------------------------------------------

    def units(self, branch_id: str, stage: str) -> dict[str, UnitRow]:
        """Every unit row of one (branch, stage) with a single SELECT, by unit_key."""

        rows = self.conn.execute(
            f"SELECT {', '.join(_columns(UnitRow))} FROM units "
            "WHERE branch_id = ? AND stage_name = ?",
            (branch_id, stage),
        )
        return {row["unit_key"]: _decode(UnitRow, row) for row in rows}

    def get_unit(self, branch_id: str, stage: str, unit_key: str) -> UnitRow | None:
        found = self.conn.execute(
            f"SELECT {', '.join(_columns(UnitRow))} FROM units "
            "WHERE branch_id = ? AND stage_name = ? AND unit_key = ?",
            (branch_id, stage, unit_key),
        ).fetchone()
        return None if found is None else _decode(UnitRow, found)

    def upsert_units(self, rows: Iterable[UnitRow]) -> int:
        """Insert or replace unit rows (used for running/ok/failed transitions)."""

        now = now_iso()
        stamped = []
        for row in rows:
            row.updated_at = now
            stamped.append(row)
        return self._upsert("units", stamped, ["branch_id", "stage_name", "unit_key"])

    def set_status(self, branch_id: str, stage: str, unit_keys: Iterable[str], status: str) -> int:
        now = now_iso()
        with transaction(self.conn):
            cursor = self.conn.executemany(
                "UPDATE units SET status = ?, updated_at = ? "
                "WHERE branch_id = ? AND stage_name = ? AND unit_key = ?",
                [(status, now, branch_id, stage, key) for key in unit_keys],
            )
        return cursor.rowcount

    def mark_orphaned(self, branch_id: str, stage: str, unit_keys: Iterable[str]) -> int:
        return self.set_status(branch_id, stage, unit_keys, "orphaned")

    def delete_units(
        self, branch_id: str, stage: str | None = None, unit_keys: Iterable[str] | None = None
    ) -> int:
        with transaction(self.conn):
            if stage is None:
                cursor = self.conn.execute("DELETE FROM units WHERE branch_id = ?", (branch_id,))
            elif unit_keys is None:
                cursor = self.conn.execute(
                    "DELETE FROM units WHERE branch_id = ? AND stage_name = ?", (branch_id, stage)
                )
            else:
                cursor = self.conn.executemany(
                    "DELETE FROM units WHERE branch_id = ? AND stage_name = ? AND unit_key = ?",
                    [(branch_id, stage, key) for key in unit_keys],
                )
        return cursor.rowcount

    def count_units_in_storage(self, storage: str, *, branch_id: str | None = None) -> int:
        sql, params = "SELECT COUNT(*) FROM units WHERE storage_name = ?", [storage]
        if branch_id is not None:
            sql, params = f"{sql} AND branch_id = ?", [storage, branch_id]
        return self.conn.execute(sql, params).fetchone()[0]

    def delete_units_in_storage(self, storage: str, *, branch_id: str | None = None) -> int:
        sql, params = "DELETE FROM units WHERE storage_name = ?", [storage]
        if branch_id is not None:
            sql, params = f"{sql} AND branch_id = ?", [storage, branch_id]
        with transaction(self.conn):
            return self.conn.execute(sql, params).rowcount

    def units_with_status(self, status: str, *, branch_id: str | None = None) -> list[UnitRow]:
        sql = f"SELECT {', '.join(_columns(UnitRow))} FROM units WHERE status = ?"
        params: tuple[Any, ...] = (status,)
        if branch_id is not None:
            sql, params = f"{sql} AND branch_id = ?", (status, branch_id)
        return [_decode(UnitRow, row) for row in self.conn.execute(sql, params)]

    def unit_counts(self, branch_id: str) -> dict[str, dict[str, int]]:
        """``{stage: {status: count}}`` for one branch."""

        counts: dict[str, dict[str, int]] = {}
        rows = self.conn.execute(
            "SELECT stage_name, status, COUNT(*) FROM units WHERE branch_id = ? "
            "GROUP BY stage_name, status",
            (branch_id,),
        )
        for stage, status, count in rows:
            counts.setdefault(stage, {})[status] = count
        return counts

    def reuse_candidates(
        self, stage: str, unit_key: str, input_key: str, *, exclude_branch_id: str
    ) -> list[tuple[str, UnitRow]]:
        """``ok`` units of other branches with the same (stage, unit_key, input_key).

        Returns ``(branch_name, row)`` pairs; the caller applies the source
        rule and checks storage and artifact integrity before hardlinking.
        """

        columns = ", ".join(f"u.{c}" for c in _columns(UnitRow))
        rows = self.conn.execute(
            f"SELECT b.name AS branch_name, {columns} FROM units u "
            "JOIN branches b ON b.branch_id = u.branch_id "
            "WHERE u.stage_name = ? AND u.unit_key = ? AND u.input_key = ? "
            "AND u.status = 'ok' AND u.branch_id != ? ORDER BY u.updated_at DESC",
            (stage, unit_key, input_key, exclude_branch_id),
        )
        return [(row["branch_name"], _decode(UnitRow, row)) for row in rows]

    def set_reused_source_fp(
        self,
        branch_id: str,
        stage: str,
        matches: Iterable[tuple[str, str]],
        source_fp: str,
    ) -> int:
        """``varve reuse``: exempt ``ok`` units whose ``(unit_key, input_key)`` still match."""

        now = now_iso()
        with transaction(self.conn):
            cursor = self.conn.executemany(
                "UPDATE units SET reused_source_fp = ?, updated_at = ? "
                "WHERE branch_id = ? AND stage_name = ? AND unit_key = ? AND input_key = ? "
                "AND status = 'ok'",
                [(source_fp, now, branch_id, stage, key, input_key) for key, input_key in matches],
            )
        return cursor.rowcount

    def link_candidates(
        self, stage: str, unit_key: str, *, exclude_branch_id: str
    ) -> list[tuple[str, UnitRow]]:
        """``ok`` units of other branches with the same (stage, unit_key), newest first.

        The caller recomputes its key under each candidate's ``config_access``
        before comparing ``input_key`` (cross-branch hardlink reuse).
        """

        columns = ", ".join(f"u.{c}" for c in _columns(UnitRow))
        rows = self.conn.execute(
            f"SELECT b.name AS branch_name, {columns} FROM units u "
            "JOIN branches b ON b.branch_id = u.branch_id "
            "WHERE u.stage_name = ? AND u.unit_key = ? AND u.status = 'ok' "
            "AND u.branch_id != ? ORDER BY u.updated_at DESC",
            (stage, unit_key, exclude_branch_id),
        )
        return [(row["branch_name"], _decode(UnitRow, row)) for row in rows]

    def probe_rows(self, branch_id: str, stage: str) -> dict[str, ProbeRow]:
        """The columns a probe needs for every unit of one (branch, stage), one SELECT.

        JSON columns stay raw strings; decode them lazily.
        """

        rows = self.conn.execute(
            "SELECT unit_key, status, storage_name, input_key, config_access_json, source_fp, "
            "reused_source_fp, artifact_relpath, artifact_fp, artifact_manifest_json, "
            "linked_from, error_summary FROM units WHERE branch_id = ? AND stage_name = ?",
            (branch_id, stage),
        )
        return {row[0]: ProbeRow(*row) for row in rows}

    def key_components(self, branch_id: str, stage: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT unit_key, key_components_json FROM units "
            "WHERE branch_id = ? AND stage_name = ?",
            (branch_id, stage),
        )
        return {row[0]: json.loads(row[1]) for row in rows}

    def set_meta(self, values: Mapping[str, str]) -> None:
        write_meta(self.conn, values)

    def meta(self) -> dict[str, str]:
        return read_meta(self.conn)

    # -- file sets ----------------------------------------------------------

    def put_file_sets(self, rows: Iterable[FileSetRow]) -> None:
        now = now_iso()
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT OR IGNORE INTO file_sets"
                "(fileset_hash, file_count, total_bytes, entries_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (row.fileset_hash, row.file_count, row.total_bytes, _dumps(row.entries), now)
                    for row in rows
                ],
            )

    def get_file_set(self, fileset_hash: str) -> FileSetRow | None:
        found = self.conn.execute(
            "SELECT fileset_hash, file_count, total_bytes, entries_json FROM file_sets "
            "WHERE fileset_hash = ?",
            (fileset_hash,),
        ).fetchone()
        if found is None:
            return None
        return FileSetRow(found[0], found[1], found[2], json.loads(found[3]))

    # -- stat cache ---------------------------------------------------------

    def load_stat_cache(self) -> dict[str, CachedStat]:
        rows = self.conn.execute(
            "SELECT norm_path, inode, size, mtime_ns, content_hash, ast_hash FROM stat_cache"
        )
        return {row[0]: CachedStat(row[1], row[2], row[3], row[4], row[5]) for row in rows}

    def put_stat_cache(self, rows: Mapping[str, CachedStat]) -> None:
        now = now_iso()
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT INTO stat_cache"
                "(norm_path, inode, size, mtime_ns, content_hash, ast_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(norm_path) DO UPDATE SET "
                "inode = excluded.inode, size = excluded.size, mtime_ns = excluded.mtime_ns, "
                "content_hash = excluded.content_hash, ast_hash = excluded.ast_hash, "
                "updated_at = excluded.updated_at",
                [
                    (path, row.inode, row.size, row.mtime_ns, row.content_hash, row.ast_hash, now)
                    for path, row in rows.items()
                ],
            )
