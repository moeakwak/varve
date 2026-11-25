"""Read-only queries behind the web UI.

Everything here opens ``state.db`` files with ``mode=ro`` URIs and
``PRAGMA query_only``; no function writes. Counting, pagination, error
grouping, metric sums and history series are computed in SQL.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varve.store.db import connect
from varve.store.layout import unit_log_path

#: Source of state databases: a fixed list or a callable re-evaluated on refresh.
DbSource = Iterable[Path] | Callable[[], Iterable[Path]]

SEGMENTS = ("config", "upstream", "inputs", "values", "version")
UNIT_STATUSES = ("running", "ok", "failed", "orphaned")
EMPTY_UNIT_KEY = "_"  # URL token for the unit of a stage without axes
LOG_TAIL_BYTES = 64 * 1024
TRACEBACK_TAIL_CHARS = 16_000
MAX_PAGE_SIZE = 500

_UNIT_LIST_COLUMNS = (
    "unit_key, status, duration_ms, peak_rss_mb, artifact_files, artifact_bytes, "
    "error_type, error_summary, last_run_id, updated_at, "
    "reused_source_fp IS NOT NULL AS reused, {linked} AS linked_from"
)
_SORTS = {
    "key": "rowid",
    "duration": "duration_ms IS NULL, duration_ms DESC, rowid",
    "updated": "updated_at DESC, rowid",
}


class NotFoundError(LookupError):
    """A pipeline, branch, stage, unit, or run does not exist."""


# -- connections and registry -------------------------------------------------


@contextmanager
def open_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``state.db`` read-only (``mode=ro``) for one request."""

    conn = connect(path, readonly=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 2000")
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class PipelineDb:
    id: str
    path: Path
    meta: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def root(self) -> Path:
        return self.path.parent.parent

    @property
    def name(self) -> str:
        return self.meta.get("pipeline_name") or str(self.root)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "module": self.meta.get("module"),
            "qualname": self.meta.get("qualname"),
            "root": str(self.root),
            "db_path": str(self.path),
            "error": self.error,
        }


def _path_hash(path: Path, n: int) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:n]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


def assign_ids(paths: list[Path], metas: list[dict[str, str]]) -> list[str]:
    """Stable ids: slug of ``meta.pipeline_name``, else ``db-<path hash>``.

    Duplicate names (worktrees, ``--root`` test copies) get ``~<path hash>``
    appended, except for the first in path order.
    """

    ids = [""] * len(paths)
    seen: set[str] = set()
    for index in sorted(range(len(paths)), key=lambda i: str(paths[i])):
        path = paths[index]
        base = _slug(metas[index].get("pipeline_name", "")) or f"db-{_path_hash(path, 12)}"
        ids[index] = base if base not in seen else f"{base}~{_path_hash(path, 6)}"
        seen.add(base)
    return ids


def read_identity(path: Path) -> tuple[dict[str, str], str | None]:
    """``(meta, error)`` of one database; never raises."""

    try:
        with open_ro(path) as conn:
            meta = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}
        return meta, None
    except Exception as exc:  # noqa: BLE001 - reported per pipeline in the UI
        return {}, f"{type(exc).__name__}: {exc}"


class Registry:
    """Maps pipeline ids to state databases; rediscovers on :meth:`refresh`."""

    def __init__(self, source: DbSource) -> None:
        if callable(source):
            self._source: Callable[[], Iterable[Path]] = source
        else:
            fixed = [Path(p) for p in source]
            self._source = lambda: fixed
        self._lock = threading.Lock()
        self._pipelines: dict[str, PipelineDb] = {}
        self._loaded = False

    def refresh(self) -> list[PipelineDb]:
        paths: list[Path] = []
        seen: set[Path] = set()
        for raw in self._source():
            path = Path(raw).absolute()
            if path not in seen:
                seen.add(path)
                paths.append(path)
        paths.sort(key=str)
        entries = [(path, *read_identity(path)) for path in paths]
        ids = assign_ids(paths, [meta for _, meta, _ in entries])
        pipelines = {
            ident: PipelineDb(ident, path, meta, error)
            for ident, (path, meta, error) in zip(ids, entries)
        }
        with self._lock:
            self._pipelines = pipelines
            self._loaded = True
        return list(pipelines.values())

    def all(self) -> list[PipelineDb]:
        if not self._loaded:
            return self.refresh()
        return list(self._pipelines.values())

    def get(self, ident: str) -> PipelineDb:
        found = self._pipelines.get(ident) if self._loaded else None
        if found is None:
            self.refresh()
            found = self._pipelines.get(ident)
        if found is None:
            raise NotFoundError(f"unknown pipeline {ident!r}")
        return found


# -- helpers --------------------------------------------------------------------


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key.endswith("_json"):
            out[key[: -len("_json")]] = _loads(value)
        elif key in ("git_dirty", "is_temp", "manual", "reused") and value is not None:
            out[key] = bool(value)
        else:
            out[key] = value
    return out


def _branch(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    found = conn.execute("SELECT * FROM branches WHERE name = ?", (name,)).fetchone()
    if found is None:
        raise NotFoundError(f"unknown branch {name!r}")
    return _row(found)


def _linked(conn: sqlite3.Connection) -> str:
    """``units.linked_from`` (hardlink reuse source branch), ``NULL`` on older databases."""

    columns = {row[1] for row in conn.execute("PRAGMA table_info(units)")}
    return "linked_from" if "linked_from" in columns else "NULL"


def load_graph(meta: dict[str, str]) -> dict[str, Any] | None:
    """The logical stage graph recorded by the engine in ``meta.graph_json``."""

    raw = meta.get("graph_json")
    if not raw:
        return None
    try:
        graph = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(graph, dict) or not isinstance(graph.get("stages"), list):
        return None
    return graph


def _deps(stage: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize ``deps`` given as names or ``{stage, kind}`` objects."""

    out: list[dict[str, str]] = []
    for dep in stage.get("deps") or []:
        if isinstance(dep, str):
            out.append({"stage": dep, "kind": "data"})
        elif isinstance(dep, dict) and isinstance(dep.get("stage"), str):
            out.append({"stage": dep["stage"], "kind": str(dep.get("kind") or "data")})
    return out


def _run_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else _row(row)


def _tail(path: Path, limit: int) -> tuple[str, bool, int]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
        data = handle.read()
    return data.decode("utf-8", errors="replace"), size > limit, size


# -- /api/workspace -------------------------------------------------------------


def workspace(registry: Registry) -> dict[str, Any]:
    pipelines = registry.refresh()
    items = []
    graphs: dict[str, dict[str, Any] | None] = {}
    for pipe in pipelines:
        entry = pipe.describe()
        entry["branches"] = []
        entry["stage_count"] = None
        graphs[pipe.id] = load_graph(pipe.meta)
        if pipe.error is None:
            try:
                with open_ro(pipe.path) as conn:
                    entry["branches"] = _workspace_branches(conn)
                    entry["stage_count"] = conn.execute(
                        "SELECT COUNT(DISTINCT stage_name) FROM units"
                    ).fetchone()[0]
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"{type(exc).__name__}: {exc}"
        graph = graphs[pipe.id]
        if graph is not None:
            entry["stage_count"] = len(graph["stages"])
        entry["has_graph"] = graph is not None
        items.append(entry)
    return {"pipelines": items, "edges": _ref_edges(pipelines, graphs)}


def _workspace_branches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    branches: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT branch_id, name, is_temp, manual, updated_at FROM branches ORDER BY name"
    ):
        branches[row["branch_id"]] = {
            **_row(row),
            "counts": {},
            "total_units": 0,
            "artifact_bytes": 0,
            "last_run": None,
            "last_duration_ms": None,
            "active": None,
        }
    for row in conn.execute(
        "SELECT branch_id, status, COUNT(*) AS n, COALESCE(SUM(artifact_bytes), 0) AS bytes "
        "FROM units GROUP BY branch_id, status"
    ):
        entry = branches.get(row["branch_id"])
        if entry is None:
            continue
        entry["counts"][row["status"]] = row["n"]
        if row["status"] != "orphaned":
            entry["total_units"] += row["n"]
            entry["artifact_bytes"] += row["bytes"]
    for row in conn.execute(
        "SELECT run_id, branch_id, started_at, ended_at, status, total_wall_ms FROM ("
        "  SELECT r.*, ROW_NUMBER() OVER (PARTITION BY branch_id ORDER BY started_at DESC) AS rn "
        "  FROM runs r) WHERE rn = 1"
    ):
        if row["branch_id"] in branches:
            branches[row["branch_id"]]["last_run"] = _row(row)
    for row in conn.execute(
        "SELECT branch_id, total_wall_ms FROM ("
        "  SELECT branch_id, total_wall_ms, "
        "  ROW_NUMBER() OVER (PARTITION BY branch_id ORDER BY started_at DESC) AS rn "
        "  FROM runs WHERE total_wall_ms IS NOT NULL) WHERE rn = 1"
    ):
        if row["branch_id"] in branches:
            branches[row["branch_id"]]["last_duration_ms"] = row["total_wall_ms"]
    for row in _active_runs(conn):
        if row["branch_id"] in branches:
            branches[row["branch_id"]]["active"] = _row(row)
    return list(branches.values())


def _active_runs(conn: sqlite3.Connection, branch_id: str | None = None) -> list[sqlite3.Row]:
    """Runs in status ``running`` with units finished in that run and running now."""

    sql = (
        "SELECT r.run_id, r.branch_id, r.started_at, "
        "(SELECT COUNT(*) FROM units u WHERE u.last_run_id = r.run_id "
        " AND u.status IN ('ok', 'failed')) AS done, "
        "(SELECT COUNT(*) FROM units u WHERE u.branch_id = r.branch_id "
        " AND u.status = 'running') AS running "
        "FROM runs r WHERE r.status = 'running'"
    )
    params: tuple[Any, ...] = ()
    if branch_id is not None:
        sql, params = f"{sql} AND r.branch_id = ?", (branch_id,)
    return list(conn.execute(f"{sql} ORDER BY r.started_at DESC", params))


def _match_target(target: str, names: dict[str, str]) -> str | None:
    """Pipeline id whose name equals ``target`` or ends with ``.target`` (unique)."""

    exact = [ident for ident, name in names.items() if name == target]
    if len(exact) == 1:
        return exact[0]
    suffix = [ident for ident, name in names.items() if name.endswith(f".{target}")]
    if len(suffix) == 1:
        return suffix[0]
    reverse = [ident for ident, name in names.items() if target.endswith(f".{name}")]
    return reverse[0] if len(reverse) == 1 else None


def _ref_edges(
    pipelines: list[PipelineDb], graphs: dict[str, dict[str, Any] | None]
) -> list[dict[str, Any]]:
    names = {pipe.id: pipe.name for pipe in pipelines if pipe.meta.get("pipeline_name")}
    edges = []
    for pipe in pipelines:
        graph = graphs.get(pipe.id)
        for ref in (graph or {}).get("refs") or []:
            if not isinstance(ref, dict) or not isinstance(ref.get("target"), str):
                continue
            edges.append(
                {
                    "source": _match_target(ref["target"], names),
                    "source_name": ref["target"],
                    "source_stage": ref.get("stage"),
                    "branches": ref.get("branches") or [],
                    "target": pipe.id,
                    "attr": ref.get("attr"),
                }
            )
    return edges


# -- /api/pipelines/{id}/branches/{b} -------------------------------------------


def branch_detail(pipe: PipelineDb, branch_name: str) -> dict[str, Any]:
    with open_ro(pipe.path) as conn:
        meta = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}
        branch = _branch(conn, branch_name)
        bid = branch["branch_id"]
        counts: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT stage_name, status, COUNT(*) FROM units WHERE branch_id = ? "
            "GROUP BY stage_name, status",
            (bid,),
        ):
            counts.setdefault(row[0], {})[row[1]] = row[2]
        linked = _linked(conn)
        sums = {
            row["stage_name"]: _row(row)
            for row in conn.execute(
                "SELECT stage_name, "
                "COALESCE(SUM(CASE WHEN status != 'orphaned' THEN artifact_bytes END), 0) "
                "  AS artifact_bytes, "
                "SUM(reused_source_fp IS NOT NULL) AS reused_units, "
                f"SUM({linked} IS NOT NULL) AS linked_units, "
                f"GROUP_CONCAT(DISTINCT {linked}) AS linked_from, "
                "MAX(updated_at) AS updated_at, MIN(rowid) AS first_rowid "
                "FROM units WHERE branch_id = ? GROUP BY stage_name",
                (bid,),
            )
        }
        last_stage_runs = {
            row["stage_name"]: _row(row)
            for row in conn.execute(
                "SELECT * FROM (SELECT s.*, ROW_NUMBER() OVER "
                "(PARTITION BY stage_name ORDER BY finished_at DESC) AS rn "
                "FROM stage_runs s WHERE branch_id = ?) WHERE rn = 1",
                (bid,),
            )
        }
        backbone = [
            row[0]
            for row in conn.execute(
                "SELECT stage_name FROM stage_runs WHERE run_id = ("
                "  SELECT run_id FROM stage_runs WHERE branch_id = ? GROUP BY run_id "
                "  ORDER BY COUNT(*) DESC, MAX(finished_at) DESC LIMIT 1) "
                "ORDER BY finished_at",
                (bid,),
            )
        ]
        active = _active_runs(conn, bid)
        last_run = conn.execute(
            "SELECT * FROM runs WHERE branch_id = ? ORDER BY started_at DESC LIMIT 1", (bid,)
        ).fetchone()
        storages = {
            row[0]: row[1] for row in conn.execute("SELECT storage_name, root_path FROM storages")
        }

    graph = load_graph(meta)
    known = set(counts) | set(last_stage_runs)
    stages: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    if graph is not None:
        for spec in graph["stages"]:
            if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                stages.append({**spec, "deps": _deps(spec), "declared": True})
        declared = {stage["name"] for stage in stages}
        for name in sorted(known - declared):  # stages removed from the code
            stages.append({"name": name, "deps": [], "declared": False})
        names = {stage["name"] for stage in stages}
        for stage in stages:
            for dep in stage["deps"]:
                if dep["stage"] in names:
                    edges.append({"from": dep["stage"], "to": stage["name"], "kind": dep["kind"]})
    else:
        # Within one run stages finish in topological order (stage barrier).
        order = list(dict.fromkeys(backbone))
        rest = sorted(known - set(order), key=lambda s: sums.get(s, {}).get("first_rowid") or 0)
        stages = [{"name": name, "deps": [], "declared": None} for name in order + rest]

    running_units = 0
    for stage in stages:
        name = stage["name"]
        stage_counts = counts.get(name, {})
        total = sum(n for status, n in stage_counts.items() if status != "orphaned")
        info = sums.get(name, {})
        stage.update(
            counts=stage_counts,
            total=total,
            ok=stage_counts.get("ok", 0),
            failed=stage_counts.get("failed", 0),
            running=stage_counts.get("running", 0),
            orphaned=stage_counts.get("orphaned", 0),
            artifact_bytes=info.get("artifact_bytes", 0),
            reused_units=info.get("reused_units") or 0,
            linked_units=info.get("linked_units") or 0,
            linked_from=sorted(filter(None, (info.get("linked_from") or "").split(","))),
            updated_at=info.get("updated_at"),
            last=last_stage_runs.get(name),
            state=_stage_state(stage_counts, total),
        )
        stage.pop("first_rowid", None)
        running_units += stage["running"]

    return {
        "pipeline": pipe.describe(),
        "branch": branch,
        "graph_source": "meta" if graph is not None else "inferred",
        "stages": stages,
        "edges": edges,
        "refs": (graph or {}).get("refs") or [],
        "running_units": running_units,
        "active_run": _row(active[0]) if active else None,
        "last_run": _run_summary(last_run),
        "storages": storages,
    }


def _stage_state(counts: dict[str, int], total: int) -> str:
    if counts.get("running"):
        return "running"
    if counts.get("failed"):
        return "failed"
    if total == 0:
        return "empty"
    if counts.get("ok", 0) == total:
        return "ok"
    return "partial"


# -- /api/.../stages/{s}/units --------------------------------------------------


def stage_units(
    pipe: PipelineDb,
    branch_name: str,
    stage: str,
    *,
    status: str | None = None,
    error_type: str | None = None,
    error_summary: str | None = None,
    q: str | None = None,
    sort: str = "key",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(max(1, page_size), MAX_PAGE_SIZE)
    order = _SORTS.get(sort, _SORTS["key"])
    with open_ro(pipe.path) as conn:
        bid = _branch(conn, branch_name)["branch_id"]
        base = "branch_id = ? AND stage_name = ?"
        base_params: list[Any] = [bid, stage]
        counts = {
            row[0]: row[1]
            for row in conn.execute(
                f"SELECT status, COUNT(*) FROM units WHERE {base} GROUP BY status", base_params
            )
        }
        stage_seen = bool(counts) or (
            conn.execute(
                "SELECT 1 FROM stage_runs WHERE branch_id = ? AND stage_name = ? LIMIT 1",
                base_params,
            ).fetchone()
            is not None
        )
        if not stage_seen and stage not in _graph_stage_names(conn):
            raise NotFoundError(f"unknown stage {stage!r}")

        where, params = base, list(base_params)
        if status:
            where, params = f"{where} AND status = ?", [*params, status]
        if error_type is not None:
            where, params = f"{where} AND COALESCE(error_type, '') = ?", [*params, error_type]
        if error_summary is not None:
            where = f"{where} AND COALESCE(error_summary, '') = ?"
            params = [*params, error_summary]
        if q:
            where, params = f"{where} AND instr(unit_key, ?) > 0", [*params, q]
        total = conn.execute(f"SELECT COUNT(*) FROM units WHERE {where}", params).fetchone()[0]
        units = [
            _row(row)
            for row in conn.execute(
                f"SELECT {_UNIT_LIST_COLUMNS.format(linked=_linked(conn))} FROM units "
                f"WHERE {where} ORDER BY {order} "
                "LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            )
        ]
        error_groups = [
            _row(row)
            for row in conn.execute(
                "SELECT error_type, error_summary, COUNT(*) AS count, "
                "unit_key AS sample_unit_key, last_run_id AS sample_run_id, "
                "substr(error_traceback, -?) AS sample_traceback, MIN(rowid) AS _first "
                f"FROM units WHERE {base} AND status = 'failed' "
                "GROUP BY error_type, error_summary ORDER BY count DESC, _first LIMIT 50",
                [TRACEBACK_TAIL_CHARS, *base_params],
            )
        ]
        for group in error_groups:
            group.pop("_first", None)
        metrics = [
            _row(row)
            for row in conn.execute(
                "SELECT j.key AS name, COUNT(*) AS units, SUM(j.value) AS sum, "
                "MIN(j.value) AS min, MAX(j.value) AS max, AVG(j.value) AS mean "
                "FROM units u, json_each(u.metrics_json) j "
                "WHERE u.branch_id = ? AND u.stage_name = ? AND u.metrics_json IS NOT NULL "
                "AND u.status != 'orphaned' AND j.type IN ('integer', 'real') "
                "GROUP BY j.key ORDER BY j.key",
                base_params,
            )
        ]
        summary = _row(
            conn.execute(
                "SELECT COUNT(duration_ms) AS timed_units, AVG(duration_ms) AS mean_ms, "
                "MAX(duration_ms) AS max_ms, MAX(peak_rss_mb) AS peak_rss_mb, "
                "COALESCE(SUM(artifact_files), 0) AS artifact_files, "
                "COALESCE(SUM(artifact_bytes), 0) AS artifact_bytes "
                f"FROM units WHERE {base} AND status != 'orphaned'",
                base_params,
            ).fetchone()
        )
    return {
        "stage": stage,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, -(-total // page_size)),
        "counts": counts,
        "error_groups": error_groups,
        "metrics": metrics,
        "summary": summary,
        "units": units,
    }


def _graph_stage_names(conn: sqlite3.Connection) -> set[str]:
    found = conn.execute("SELECT value FROM meta WHERE key = 'graph_json'").fetchone()
    graph = load_graph({"graph_json": found[0]}) if found else None
    stages = (graph or {}).get("stages", [])
    return {s["name"] for s in stages if isinstance(s, dict) and isinstance(s.get("name"), str)}


# -- /api/.../units/{unit_key} --------------------------------------------------


def unit_detail(pipe: PipelineDb, branch_name: str, stage: str, unit_key: str) -> dict[str, Any]:
    key = "" if unit_key == EMPTY_UNIT_KEY else unit_key
    with open_ro(pipe.path) as conn:
        branch = _branch(conn, branch_name)
        found = conn.execute(
            "SELECT *, reused_source_fp IS NOT NULL AS reused "
            "FROM units WHERE branch_id = ? AND stage_name = ? AND unit_key = ?",
            (branch["branch_id"], stage, key),
        ).fetchone()
        if found is None:
            raise NotFoundError(f"unknown unit {key!r} in stage {stage!r}")
        unit = _row(found)
        unit.setdefault("linked_from", None)
        components = unit.get("key_components") or {}
        hashes = sorted(set(_fileset_refs(components)))
        file_sets = {
            row[0]: {"file_count": row[1], "total_bytes": row[2]}
            for row in conn.execute(
                "SELECT fileset_hash, file_count, total_bytes FROM file_sets "
                f"WHERE fileset_hash IN ({', '.join('?' for _ in hashes)})",
                hashes,
            )
        }
        storage = conn.execute(
            "SELECT root_path FROM storages WHERE storage_name = ?", (unit["storage_name"],)
        ).fetchone()
        run = None
        if unit.get("last_run_id"):
            run = _run_summary(
                conn.execute(
                    "SELECT run_id, started_at, ended_at, status, git_commit, git_dirty "
                    "FROM runs WHERE run_id = ?",
                    (unit["last_run_id"],),
                ).fetchone()
            )

    segments = []
    if isinstance(components, dict):
        for name in [*SEGMENTS, *sorted(set(components) - set(SEGMENTS))]:
            part = components.get(name)
            if isinstance(part, dict):
                segments.append(
                    {"name": name, "hash": part.get("hash"), "summary": part.get("summary")}
                )
            elif name in components:
                segments.append({"name": name, "hash": None, "summary": part})
    artifact_path = None
    if storage is not None and unit.get("artifact_relpath"):
        artifact_path = str(Path(storage[0]) / unit["artifact_relpath"])
    labels = []
    version = next((s for s in segments if s["name"] == "version"), None)
    if version and version["summary"] not in (None, "", {}):
        labels.append({"kind": "version", "text": f"version={version['summary']}"})
    if unit.get("reused"):
        labels.append({"kind": "reused", "text": "reused"})
    if unit.get("linked_from"):
        labels.append({"kind": "linked", "text": f"linked@{unit['linked_from']}"})
    return {
        "pipeline": pipe.describe(),
        "branch": branch_name,
        "unit": unit,
        "segments": segments,
        "file_sets": file_sets,
        "labels": labels,
        "artifact_path": artifact_path,
        "run": run,
        "log": _unit_log(pipe, unit),
    }


def _fileset_refs(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("fileset", "fileset_hash") and isinstance(item, str):
                yield item
            else:
                yield from _fileset_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _fileset_refs(item)


def _unit_log(pipe: PipelineDb, unit: dict[str, Any]) -> dict[str, Any] | None:
    run_id = unit.get("last_run_id")
    if not run_id:
        return None
    path = unit_log_path(pipe.root, run_id, unit["stage_name"], unit["unit_key"])
    if not path.is_file():
        return {"available": False, "path": str(path)}
    try:
        text, truncated, size = _tail(path, LOG_TAIL_BYTES)
    except OSError as exc:
        return {"available": False, "path": str(path), "error": str(exc)}
    return {
        "available": True,
        "path": str(path),
        "size": size,
        "truncated": truncated,
        "text": text,
    }


# -- /api/.../history ------------------------------------------------------------

_HISTORY_SQL = """
SELECT r.run_id, r.started_at, r.ended_at, r.status, r.git_commit, r.git_dirty, r.host,
       r.cli_argv_json, r.total_wall_ms,
       COUNT(s.stage_name) AS stage_count,
       SUM(s.executed_units) AS executed, SUM(s.hit_units) AS hit,
       SUM(s.failed_units) AS failed, SUM(s.total_units) AS total,
       MAX(s.workers) AS workers,
       CASE WHEN :stage IS NULL THEN COALESCE(r.total_wall_ms, SUM(s.wall_ms))
            ELSE SUM(s.wall_ms) END AS wall_ms,
       SUM(s.wall_ms) AS stage_wall_ms,
       CASE WHEN :stage IS NOT NULL THEN MAX(s.unit_p50_ms) END AS unit_p50_ms,
       CASE WHEN :stage IS NOT NULL THEN MAX(s.unit_p95_ms) END AS unit_p95_ms,
       MAX(s.unit_max_ms) AS unit_max_ms,
       MAX(s.peak_rss_mb) AS peak_rss_mb,
       SUM(CAST(json_extract(s.metrics_summary_json, '$.items') AS REAL)) AS items,
       CASE
         WHEN COUNT(s.stage_name) = 0 THEN 'none'
         WHEN SUM(s.total_units) > 0 AND SUM(s.executed_units) = SUM(s.total_units) THEN 'full'
         ELSE 'partial'
       END AS kind
FROM runs r
LEFT JOIN stage_runs s ON s.run_id = r.run_id AND (:stage IS NULL OR s.stage_name = :stage)
WHERE r.branch_id = :branch_id
GROUP BY r.run_id
HAVING :stage IS NULL OR COUNT(s.stage_name) > 0
ORDER BY r.started_at DESC
LIMIT :limit
"""


def history(
    pipe: PipelineDb, branch_name: str, *, stage: str | None = None, limit: int = 50
) -> dict[str, Any]:
    limit = min(max(1, limit), 1000)
    with open_ro(pipe.path) as conn:
        bid = _branch(conn, branch_name)["branch_id"]
        rows = conn.execute(
            f"SELECT h.*, "
            "CASE WHEN h.items IS NOT NULL AND h.stage_wall_ms > 0 "
            "     THEN h.items * 1000.0 / h.stage_wall_ms END AS items_per_s, "
            "CASE WHEN h.stage_wall_ms > 0 AND h.executed > 0 "
            "     THEN h.executed * 1000.0 / h.stage_wall_ms END AS units_per_s "
            f"FROM ({_HISTORY_SQL}) h",
            {"stage": stage, "branch_id": bid, "limit": limit},
        )
        runs = [_row(row) for row in rows]
        stages = [
            row[0]
            for row in conn.execute(
                "SELECT stage_name FROM stage_runs WHERE branch_id = ? "
                "GROUP BY stage_name ORDER BY MIN(finished_at)",
                (bid,),
            )
        ]
    for run in runs:
        run["argv"] = run.pop("cli_argv", None)
    has_items = any(run["items_per_s"] is not None for run in runs)
    return {
        "branch": branch_name,
        "stage": stage,
        "limit": limit,
        "stages": stages,
        "throughput_unit": "items/s" if has_items else "units/s",
        "runs": runs,
    }


# -- /api/runs/{run_id} -------------------------------------------------------------


def run_detail(registry: Registry, run_id: str, pipeline: str | None = None) -> dict[str, Any]:
    candidates = [registry.get(pipeline)] if pipeline else registry.all()
    for pipe in candidates:
        if pipe.error is not None:
            continue
        try:
            with open_ro(pipe.path) as conn:
                run = conn.execute(
                    "SELECT r.*, b.name AS branch FROM runs r "
                    "LEFT JOIN branches b ON b.branch_id = r.branch_id WHERE r.run_id = ?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    continue
                stages = [
                    _row(row)
                    for row in conn.execute(
                        "SELECT *, "
                        "CAST(json_extract(metrics_summary_json, '$.items') AS REAL) AS items, "
                        "CASE WHEN wall_ms > 0 AND json_extract(metrics_summary_json, '$.items') "
                        "  IS NOT NULL THEN json_extract(metrics_summary_json, '$.items') "
                        "  * 1000.0 / wall_ms END AS items_per_s, "
                        "CASE WHEN wall_ms > 0 AND executed_units > 0 "
                        "  THEN executed_units * 1000.0 / wall_ms END AS units_per_s, "
                        "CASE WHEN total_units > 0 AND executed_units = total_units "
                        "  THEN 'full' ELSE 'partial' END AS kind "
                        "FROM stage_runs WHERE run_id = ? ORDER BY finished_at",
                        (run_id,),
                    )
                ]
                totals = _row(
                    conn.execute(
                        "SELECT COALESCE(SUM(executed_units), 0) AS executed, "
                        "COALESCE(SUM(hit_units), 0) AS hit, "
                        "COALESCE(SUM(failed_units), 0) AS failed, "
                        "COALESCE(SUM(total_units), 0) AS total, "
                        "COALESCE(SUM(wall_ms), 0) AS stage_wall_ms, "
                        "MAX(peak_rss_mb) AS peak_rss_mb "
                        "FROM stage_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                )
        except Exception:  # noqa: BLE001 - an unreadable db cannot hold the run
            continue
        detail = _row(run)
        detail["argv"] = detail.pop("cli_argv", None)
        return {"pipeline": pipe.describe(), "run": detail, "stages": stages, "totals": totals}
    raise NotFoundError(f"unknown run {run_id!r}")
