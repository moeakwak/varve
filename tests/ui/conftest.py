from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from varve.keying.keys import FileSetRow
from varve.store.db import connect, write_meta
from varve.store.layout import state_db_path, unit_log_path
from varve.store.repo import BranchRow, Repo, RunRow, StageRunRow, UnitRow

CORPUS_GRAPH = {
    "stages": [
        {"name": "prepare", "deps": [], "axes": [], "storage": "default", "version": None},
        {
            "name": "shard",
            "deps": [{"stage": "prepare", "kind": "data"}],
            "axes": [{"name": "shard", "kind": "static"}],
            "storage": "default",
            "version": "2",
        },
        {"name": "merge", "deps": ["shard", {"stage": "prepare", "kind": "needs"}]},
    ],
    "refs": [],
}
EVAL_GRAPH = {
    "stages": [{"name": "score", "deps": [{"stage": "ref:corpus:merge", "kind": "data"}]}],
    "refs": [
        {
            "attr": "corpus",
            "target": "corpus",
            "stage": "merge",
            "branches": ["main"],
            "align_by": None,
        }
    ],
}


def _components(fileset: str | None = None, version: str | None = None) -> dict:
    inputs = {"corpus": {"fileset": fileset}} if fileset else {}
    return {
        "config": {"hash": "c" * 64, "summary": {"fields": ["lang"]}},
        "upstream": {"hash": "u" * 64, "summary": {"prepare": "a" * 16}},
        "inputs": {"hash": "i" * 64, "summary": inputs},
        "values": {"hash": "v" * 64, "summary": {}},
        "version": {"hash": "n" * 64, "summary": version},
    }


def _unit(branch: str, stage: str, key: str, status: str, **extra) -> UnitRow:
    defaults: dict[str, Any] = dict(
        branch_id=branch,
        stage_name=stage,
        unit_key=key,
        storage_name="default",
        input_key="k" * 64,
        key_components=_components(),
        source_fp="s" * 64,
        source_detail={
            "file": "pipe.py",
            "mode": "closure",
            "fallback_reason": None,
            "symbols": {"Pipe.shard": "1" * 16, "helper": "2" * 16},
            "external": {},
        },
        status=status,
    )
    defaults.update(extra)
    return UnitRow(**defaults)


def _run(run_id: str, started: str, status: str, **extra) -> RunRow:
    return RunRow(
        run_id=run_id,
        branch_id="main",
        started_at=started,
        status=status,
        cli_argv=["run"],
        host="box",
        **extra,
    )


def _stage_run(run_id: str, stage: str, finished: str, executed: int, total: int, **extra):
    values: dict[str, Any] = dict(
        run_id=run_id,
        branch_id="main",
        stage_name=stage,
        status="ok",
        executed_units=executed,
        hit_units=total - executed,
        failed_units=0,
        total_units=total,
        workers=4,
        wall_ms=1000,
        finished_at=finished,
    )
    values.update(extra)
    return StageRunRow(**values)


def build_corpus(root: Path) -> Path:
    """Pipeline ``demo.corpus``: two full/partial runs, one running run, mixed units."""

    path = state_db_path(root)
    conn = connect(path)
    repo = Repo(conn)
    write_meta(
        conn,
        {
            "pipeline_name": "demo.corpus",
            "module": "demo.corpus",
            "qualname": "Corpus",
            "root": str(root),
            "graph_json": json.dumps(CORPUS_GRAPH),
        },
    )
    repo.upsert_storages({"default": root / "out"})
    for name in ("main", "dev"):
        repo.upsert_branch(BranchRow(name, name, config={"lang": "en"}, active_axes={}))
    repo.insert_run(
        _run(
            "r1",
            "2026-11-01T00:00:00",
            "ok",
            git_commit="abc1234",
            git_dirty=False,
            total_wall_ms=12000,
            ended_at="2026-11-01T00:00:12",
        )
    )
    repo.insert_run(
        _run(
            "r2",
            "2026-11-02T00:00:00",
            "failed",
            git_commit="def5678",
            git_dirty=True,
            total_wall_ms=3000,
            ended_at="2026-11-02T00:00:03",
        )
    )
    repo.insert_run(_run("r3", "2026-11-03T00:00:00", "running", git_commit="def5678"))
    for row in (
        _stage_run("r1", "prepare", "2026-11-01T00:00:01", 1, 1, wall_ms=1000),
        _stage_run(
            "r1",
            "shard",
            "2026-11-01T00:00:10",
            10,
            10,
            wall_ms=9000,
            unit_p50_ms=3000,
            unit_p95_ms=4000,
            unit_max_ms=4100,
            peak_rss_mb=512.0,
            metrics_summary={"items": 900},
        ),
        _stage_run("r1", "merge", "2026-11-01T00:00:12", 1, 1, wall_ms=2000),
        _stage_run("r2", "prepare", "2026-11-02T00:00:00", 0, 1, wall_ms=10),
        _stage_run(
            "r2",
            "shard",
            "2026-11-02T00:00:03",
            2,
            10,
            wall_ms=2000,
            failed_units=3,
            status="failed",
            unit_p50_ms=900,
            unit_p95_ms=1000,
            metrics_summary={"items": 200},
        ),
    ):
        repo.put_stage_run(row)
    repo.put_file_sets([FileSetRow("f" * 64, 40, 4096, [["a.txt", 1, 1]])])
    units = [
        _unit(
            "main",
            "prepare",
            "",
            "ok",
            last_run_id="r1",
            duration_ms=900,
            artifact_bytes=10,
            artifact_files=1,
            artifact_relpath=".internal/main/prepare",
            key_components=_components("f" * 64),
        ),
    ]
    for i in range(6):
        units.append(
            _unit(
                "main",
                "shard",
                f"shard={i}",
                "ok",
                last_run_id="r2",
                duration_ms=1000 + i,
                artifact_bytes=100,
                artifact_files=2,
                metrics={"items": 100, "note": "x"},
                key_components=_components(version="2"),
                reused_source_fp="r" * 64 if i == 1 else None,
            )
        )
    units[3].linked_from = "dev"
    for i, (etype, summary) in enumerate(
        [("ValueError", "bad row"), ("ValueError", "bad row"), ("KeyError", "'x'")], start=6
    ):
        units.append(
            _unit(
                "main",
                "shard",
                f"shard={i}",
                "failed",
                last_run_id="r3",
                error_type=etype,
                error_summary=summary,
                error_traceback=f"Traceback (most recent call last):\n{etype}: {summary}",
            )
        )
    units.append(_unit("main", "shard", "shard=9", "running", last_run_id="r3"))
    units.append(
        _unit("main", "shard", "shard=10", "orphaned", last_run_id="r1", artifact_bytes=999)
    )
    units.append(_unit("dev", "prepare", "", "ok", duration_ms=5))
    repo.upsert_units(units)
    conn.close()
    log = unit_log_path(root, "r3", "shard", "shard=6")
    log.parent.mkdir(parents=True)
    log.write_text("hello from shard 6\n")
    return path


def build_eval(root: Path) -> Path:
    """Pipeline ``demo.eval`` referencing ``corpus`` (suffix match); no runs."""

    path = state_db_path(root)
    conn = connect(path)
    write_meta(conn, {"pipeline_name": "demo.eval", "graph_json": json.dumps(EVAL_GRAPH)})
    Repo(conn).upsert_branch(BranchRow("main", "main", config={}, active_axes={}))
    conn.close()
    return path


def build_bare(root: Path) -> Path:
    """No pipeline_name, no graph_json: ids fall back to a path hash, order is inferred."""

    path = state_db_path(root)
    conn = connect(path)
    repo = Repo(conn)
    repo.upsert_storages({"default": root / "out"})
    repo.upsert_branch(BranchRow("main", "main", config={}, active_axes={}))
    repo.insert_run(_run("b1", "2026-11-01T00:00:00", "ok", total_wall_ms=10))
    # Inserted out of order on purpose: finish order in the run defines the stage order.
    repo.put_stage_run(_stage_run("b1", "late", "2026-11-01T00:00:09", 1, 1))
    repo.put_stage_run(_stage_run("b1", "early", "2026-11-01T00:00:01", 1, 1))
    repo.upsert_units(
        [
            _unit("main", "late", "", "ok", last_run_id="b1"),
            _unit("main", "early", "", "ok", last_run_id="b1"),
            _unit("main", "lonely", "", "ok"),
        ]
    )
    conn.close()
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {
        "corpus": build_corpus(tmp_path / "corpus"),
        "eval": build_eval(tmp_path / "eval"),
        "bare": build_bare(tmp_path / "bare"),
    }
