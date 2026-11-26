"""Typed operations over ``state.db``.

Row dataclasses hold decoded JSON columns (``*_json`` columns become Python
values on fields without the suffix). Every write runs in a transaction;
wrap several calls in :meth:`Repo.transaction` to batch them into one.

Unit rows are normalized on write: ``source_detail`` is stored once per
``source_fp`` in ``sources``, and key-component summaries longer than
``PART_INLINE_LIMIT`` are stored once in ``key_parts``, keyed by segment and
segment hash (the segment entry in ``key_components_json`` then has no
``summary``). Reads through :class:`Repo` (and :func:`expand_components`)
restore both.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
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


PART_INLINE_LIMIT = 128
"""Key-component summaries whose JSON is longer are stored in ``key_parts``."""

_SQL_VARIABLES = 500


def part_key(segment: str, segment_hash: str) -> str:
    """``key_parts`` key of a segment summary.

    A segment's summary is a function of its hash (input summaries carry no
    absolute paths), so equal hashes share one part.
    """

    return f"{segment}:{segment_hash}"


def collapse_components(components: Mapping[str, Any], parts: dict[str, str]) -> str:
    """``key_components_json`` text without long summaries (added to ``parts``).

    A segment whose summary is not inline reads it from ``key_parts`` under
    :func:`part_key` of its ``hash``.
    """

    compact: dict[str, Any] = {}
    for segment, part in components.items():
        if isinstance(part, Mapping) and "summary" in part and isinstance(part.get("hash"), str):
            text = json.dumps(part["summary"], separators=(",", ":"))
            if len(text) > PART_INLINE_LIMIT:
                parts[part_key(segment, part["hash"])] = text
                part = {key: value for key, value in part.items() if key != "summary"}
        compact[segment] = part
    collapsed = _dumps(compact)
    assert collapsed is not None
    return collapsed


def _missing_summaries(doc: Any) -> Iterator[str]:
    if isinstance(doc, dict):
        for segment, part in doc.items():
            if (
                isinstance(part, dict)
                and "summary" not in part
                and isinstance(part.get("hash"), str)
            ):
                yield part_key(segment, part["hash"])


def load_parts(conn: sqlite3.Connection, keys: Iterable[str]) -> dict[str, Any]:
    """Decoded ``key_parts`` by key (missing keys are absent)."""

    wanted = sorted(set(keys))
    found: dict[str, Any] = {}
    for start in range(0, len(wanted), _SQL_VARIABLES):
        chunk = wanted[start : start + _SQL_VARIABLES]
        rows = conn.execute(
            "SELECT part_key, part_json FROM key_parts "
            f"WHERE part_key IN ({', '.join('?' for _ in chunk)})",
            chunk,
        )
        found.update((row[0], json.loads(row[1])) for row in rows)
    return found


def expand_components(
    conn: sqlite3.Connection, docs: Sequence[Any], cache: dict[str, Any] | None = None
) -> None:
    """Restore the summaries of decoded ``key_components`` documents in place.

    ``cache`` keeps decoded parts across calls (parts never change). A missing
    part expands to a ``None`` summary.
    """

    cache = {} if cache is None else cache
    keys = {key for doc in docs for key in _missing_summaries(doc)}
    if not keys:
        return
    missing = keys - cache.keys()
    if missing:
        cache.update(load_parts(conn, missing))
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for segment, part in doc.items():
            if (
                isinstance(part, dict)
                and "summary" not in part
                and isinstance(part.get("hash"), str)
            ):
                expanded = {
                    "hash": part["hash"],
                    "summary": cache.get(part_key(segment, part["hash"])),
                }
                expanded.update((key, value) for key, value in part.items() if key != "hash")
                doc[segment] = expanded


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
class LinkCandidate:
    """Light columns of another branch's ``ok`` unit (cross-branch link lookup)."""

    branch_name: str
    branch_id: str
    storage_name: str
    input_key: str
    config_access_json: str | None
    source_fp: str
    reused_source_fp: str | None
    has_manifest: bool


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


# Unit rows: `source_detail` is joined from `sources`; key components are expanded.
_UNIT_SELECT = ", ".join(
    "s.detail_json AS source_detail_json" if column == "source_detail_json" else f"u.{column}"
    for column in _columns(UnitRow)
)
_UNIT_FROM = "units u LEFT JOIN sources s ON s.source_fp = u.source_fp"
_UNIT_WRITE = [item.name for item in fields(UnitRow) if item.name != "source_detail"]
_UNIT_WRITE_COLUMNS = [_column(name) for name in _UNIT_WRITE]
_UNIT_KEY = ("branch_id", "stage_name", "unit_key")
_UNIT_UPSERT = (
    f"INSERT INTO units ({', '.join(_UNIT_WRITE_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _UNIT_WRITE_COLUMNS)}) "
    f"ON CONFLICT({', '.join(_UNIT_KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _UNIT_WRITE_COLUMNS if c not in _UNIT_KEY)
)


def _unit_values(row: UnitRow, parts: dict[str, str]) -> tuple[Any, ...]:
    values: list[Any] = []
    for name in _UNIT_WRITE:
        value = getattr(row, name)
        if name == "key_components":
            value = collapse_components(value, parts)
        elif name in _JSON_FIELDS:
            value = _dumps(value)
        values.append(value)
    return tuple(values)


class Repo:
    """Typed queries and commands over one open ``state.db`` connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._parts: dict[str, Any] = {}

    def _unit_rows(self, rows: Iterable[sqlite3.Row]) -> list[UnitRow]:
        decoded: list[UnitRow] = []
        for row in rows:
            unit = _decode(UnitRow, row)
            if unit.source_detail is None:
                unit.source_detail = {}
            decoded.append(unit)
        expand_components(self.conn, [unit.key_components for unit in decoded], self._parts)
        return decoded

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
            f"SELECT {_UNIT_SELECT} FROM {_UNIT_FROM} WHERE u.branch_id = ? AND u.stage_name = ?",
            (branch_id, stage),
        )
        return {unit.unit_key: unit for unit in self._unit_rows(rows)}

    def get_unit(self, branch_id: str, stage: str, unit_key: str) -> UnitRow | None:
        found = self.conn.execute(
            f"SELECT {_UNIT_SELECT} FROM {_UNIT_FROM} "
            "WHERE u.branch_id = ? AND u.stage_name = ? AND u.unit_key = ?",
            (branch_id, stage, unit_key),
        ).fetchall()
        return self._unit_rows(found)[0] if found else None

    def upsert_units(self, rows: Iterable[UnitRow]) -> int:
        """Insert or replace unit rows (used for running/ok/failed transitions).

        Source details and long key-component summaries are written to
        ``sources`` and ``key_parts`` in the same transaction.
        """

        now = now_iso()
        parts: dict[str, str] = {}
        details: dict[str, Any] = {}
        values = []
        for row in rows:
            row.updated_at = now
            details[row.source_fp] = row.source_detail
            values.append(_unit_values(row, parts))
        if not values:
            return 0
        with transaction(self.conn):
            if parts:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO key_parts(part_key, part_json) VALUES (?, ?)",
                    list(parts.items()),
                )
            self.conn.executemany(
                "INSERT INTO sources(source_fp, detail_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(source_fp) DO UPDATE SET detail_json = excluded.detail_json, "
                "updated_at = excluded.updated_at WHERE detail_json != excluded.detail_json",
                [(fp, _dumps(detail or {}), now) for fp, detail in details.items()],
            )
            self.conn.executemany(_UNIT_UPSERT, values)
        return len(values)

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
        sql = f"SELECT {_UNIT_SELECT} FROM {_UNIT_FROM} WHERE u.status = ?"
        params: tuple[Any, ...] = (status,)
        if branch_id is not None:
            sql, params = f"{sql} AND u.branch_id = ?", (status, branch_id)
        return self._unit_rows(self.conn.execute(sql, params))

    def unit_keys_with_status(
        self, status: str, *, branch_id: str | None = None
    ) -> list[tuple[str, str, str, str]]:
        """``(branch_id, stage_name, unit_key, storage_name)`` of rows with ``status``."""

        sql = "SELECT branch_id, stage_name, unit_key, storage_name FROM units WHERE status = ?"
        params: tuple[Any, ...] = (status,)
        if branch_id is not None:
            sql, params = f"{sql} AND branch_id = ?", (status, branch_id)
        return [tuple(row) for row in self.conn.execute(sql, params)]  # type: ignore[misc]

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

    def branch_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM branches").fetchone()[0]

    def link_candidates(
        self, stage: str, unit_key: str, *, exclude_branch_id: str
    ) -> list[tuple[str, UnitRow]]:
        """``ok`` units of other branches with the same (stage, unit_key), newest first.

        The caller recomputes its key under each candidate's ``config_access``
        before comparing ``input_key`` (cross-branch hardlink reuse).
        """

        rows = self.conn.execute(
            f"SELECT b.name AS branch_name, {_UNIT_SELECT} FROM {_UNIT_FROM} "
            "JOIN branches b ON b.branch_id = u.branch_id "
            "WHERE u.stage_name = ? AND u.unit_key = ? AND u.status = 'ok' "
            "AND u.branch_id != ? ORDER BY u.updated_at DESC",
            (stage, unit_key, exclude_branch_id),
        ).fetchall()
        units = self._unit_rows(rows)
        return [(row["branch_name"], unit) for row, unit in zip(rows, units, strict=True)]

    def link_index(self, stage: str, *, exclude_branch_id: str) -> dict[str, list[LinkCandidate]]:
        """Light columns of every ``ok`` unit of ``stage`` in other branches, by unit_key.

        Ordered newest first per unit; full rows are loaded with :meth:`get_unit`
        only for candidates whose key matches.
        """

        rows = self.conn.execute(
            "SELECT u.unit_key, b.name, u.branch_id, u.storage_name, u.input_key, "
            "u.config_access_json, u.source_fp, u.reused_source_fp, "
            "u.artifact_manifest_json IS NOT NULL "
            "FROM units u JOIN branches b ON b.branch_id = u.branch_id "
            "WHERE u.stage_name = ? AND u.status = 'ok' AND u.branch_id != ? "
            "ORDER BY u.updated_at DESC",
            (stage, exclude_branch_id),
        )
        index: dict[str, list[LinkCandidate]] = {}
        for row in rows:
            index.setdefault(row[0], []).append(
                LinkCandidate(row[1], row[2], row[3], row[4], row[5], row[6], row[7], bool(row[8]))
            )
        return index

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

    def source_details(self, fingerprints: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = sorted(set(fingerprints))
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(wanted), _SQL_VARIABLES):
            chunk = wanted[start : start + _SQL_VARIABLES]
            rows = self.conn.execute(
                "SELECT source_fp, detail_json FROM sources "
                f"WHERE source_fp IN ({', '.join('?' for _ in chunk)})",
                chunk,
            )
            found.update((row[0], json.loads(row[1])) for row in rows)
        return found

    # -- garbage collection -------------------------------------------------

    def collect_garbage(self) -> dict[str, int]:
        """Delete key parts, source details, and file sets that no unit row references.

        Safe while other processes write: every writer inserts these rows in the
        same transaction as the unit rows that reference them.
        """

        with transaction(self.conn):
            parts = self.conn.execute(
                "DELETE FROM key_parts WHERE part_key NOT IN ("
                "SELECT j.key || ':' || json_extract(j.value, '$.hash') "
                "FROM units u, json_each(u.key_components_json) j "
                "WHERE j.type = 'object' AND json_type(j.value, '$.summary') IS NULL "
                "AND json_extract(j.value, '$.hash') IS NOT NULL)"
            ).rowcount
            sources = self.conn.execute(
                "DELETE FROM sources WHERE source_fp NOT IN (SELECT source_fp FROM units)"
            ).rowcount
            file_sets = self.conn.execute(
                "DELETE FROM file_sets WHERE fileset_hash NOT IN ("
                "SELECT json_extract(i.value, '$.fileset') FROM ("
                "  SELECT json_extract(u.key_components_json, '$.inputs.summary') AS doc "
                "  FROM units u"
                "  UNION ALL"
                "  SELECT p.part_json AS doc FROM key_parts p WHERE p.part_key LIKE 'inputs:%'"
                ") d, json_each(d.doc) i "
                "WHERE d.doc IS NOT NULL AND i.type = 'object' "
                "AND json_extract(i.value, '$.fileset') IS NOT NULL)"
            ).rowcount
        return {"key_parts": parts, "sources": sources, "file_sets": file_sets}

    def prune_stat_cache(self) -> int:
        """Delete stat-cache rows whose file is gone or changed (they can never hit again)."""

        stale: list[tuple[str]] = []
        rows = self.conn.execute("SELECT norm_path, inode, size, mtime_ns FROM stat_cache")
        for path, inode, size, mtime_ns in rows.fetchall():
            try:
                st = os.stat(path)
            except OSError:
                stale.append((path,))
                continue
            if (st.st_ino, st.st_size, st.st_mtime_ns) != (inode, size, mtime_ns):
                stale.append((path,))
        if stale:
            with transaction(self.conn):
                self.conn.executemany("DELETE FROM stat_cache WHERE norm_path = ?", stale)
        return len(stale)

    # -- stat cache ---------------------------------------------------------

    def load_stat_cache(self) -> dict[str, CachedStat]:
        rows = self.conn.execute(
            "SELECT norm_path, inode, size, mtime_ns, content_hash, ast_hash FROM stat_cache"
        )
        return {row[0]: CachedStat(row[1], row[2], row[3], row[4], row[5]) for row in rows}

    def put_stat_cache(self, rows: Mapping[str, CachedStat]) -> None:
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT INTO stat_cache"
                "(norm_path, inode, size, mtime_ns, content_hash, ast_hash) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(norm_path) DO UPDATE SET "
                "inode = excluded.inode, size = excluded.size, mtime_ns = excluded.mtime_ns, "
                "content_hash = excluded.content_hash, ast_hash = excluded.ast_hash",
                [
                    (path, row.inode, row.size, row.mtime_ns, row.content_hash, row.ast_hash)
                    for path, row in rows.items()
                ],
            )
