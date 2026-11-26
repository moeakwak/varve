from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from varve.keying.fingerprint import CachedStat
from varve.keying.keys import FileSetRow
from varve.store import db, layout
from varve.store.lock import FileLock, LockBusyError, branch_lock, pipeline_lock
from varve.store.repo import BranchRow, Repo, RunRow, StageRunRow, UnitRow


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    conn = db.connect(layout.state_db_path(tmp_path))
    repo = Repo(conn)
    repo.upsert_storages({"default": tmp_path / "out", "interim": tmp_path / "interim"})
    repo.upsert_branch(BranchRow("main", "main", {"seed": 1}, {"dataset": ["mw"]}))
    repo.upsert_branch(BranchRow("katex", "katex", {"seed": 1}, {}, manual=True))
    repo.insert_run(RunRow("r1", "main", db.now_iso(), "running", ["run"], "host"))
    repo.insert_run(RunRow("r2", "katex", db.now_iso(), "running", ["run"], "host"))
    return repo


def unit(branch: str = "main", key: str = "dataset=mw", **overrides: object) -> UnitRow:
    row = UnitRow(
        branch_id=branch,
        stage_name="work",
        unit_key=key,
        storage_name="default",
        input_key="ik-1",
        key_components={"config": {"hash": "h", "summary": {}}},
        source_fp="s1",
        source_detail={"mode": "closure", "symbols": {"Demo.work": "x"}},
        status="ok",
        artifact_relpath=f".internal/{branch}/work/{key}",
        artifact_fp="afp",
        artifact_manifest=[["part.parquet", 4, 123, "sha"]],
        artifact_files=1,
        artifact_bytes=4,
        metrics={"items": 3},
        last_run_id="r1" if branch == "main" else "r2",
    )
    for name, value in overrides.items():
        setattr(row, name, value)
    return row


def test_schema_wal_and_meta(tmp_path: Path, repo: Repo) -> None:
    conn = repo.conn
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {
        "meta",
        "storages",
        "branches",
        "runs",
        "stage_runs",
        "file_sets",
        "units",
        "stat_cache",
    }
    db.write_meta(conn, {"pipeline_name": "exp.demo", "module": "exp.demo.run", "root": "/r"})
    assert db.read_meta(conn) == {
        "schema_version": db.SCHEMA_VERSION,
        "pipeline_name": "exp.demo",
        "module": "exp.demo.run",
        "root": "/r",
    }
    db.connect(layout.state_db_path(tmp_path)).close()  # reopening an existing db is fine


def test_schema_mismatch_and_foreign_db_are_rejected(tmp_path: Path, repo: Repo) -> None:
    repo.conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    with pytest.raises(db.SchemaError, match="no migration"):
        db.connect(layout.state_db_path(tmp_path))
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x (a)").connection.close()
    with pytest.raises(db.SchemaError, match="not a varve"):
        db.connect(other)


def test_read_only_open(tmp_path: Path, repo: Repo) -> None:
    repo.upsert_units([unit()])
    with pytest.raises(FileNotFoundError):
        db.connect(tmp_path / "missing.db", readonly=True)
    reader = Repo(db.connect(layout.state_db_path(tmp_path), readonly=True))
    assert reader.get_unit("main", "work", "dataset=mw") is not None
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.upsert_units([unit(key="dataset=other")])
    # A reader sees commits of a concurrent writer (WAL).
    repo.upsert_units([unit(key="dataset=im2latex")])
    assert set(reader.units("main", "work")) == {"dataset=mw", "dataset=im2latex"}


def test_branches_and_storages_round_trip(repo: Repo) -> None:
    main = repo.get_branch("main")
    assert (
        main is not None and main.config == {"seed": 1} and main.active_axes == {"dataset": ["mw"]}
    )
    assert main.is_temp is False and main.created_at
    created = main.created_at
    repo.upsert_branch(BranchRow("ignored-id", "main", {"seed": 2}, {}))
    again = repo.get_branch("main")
    assert again is not None
    assert again.branch_id == "main" and again.created_at == created and again.config == {"seed": 2}
    assert [b.name for b in repo.list_branches()] == ["katex", "main"]
    assert repo.storages()["interim"].name == "interim"


def test_unit_round_trip_and_status_transitions(repo: Repo) -> None:
    repo.upsert_units([unit(status="running", artifact_fp=None)])
    repo.upsert_units([unit(key="dataset=im2latex")])
    rows = repo.units("main", "work")
    assert set(rows) == {"dataset=mw", "dataset=im2latex"}
    assert rows["dataset=mw"].status == "running" and rows["dataset=mw"].artifact_fp is None
    ok = rows["dataset=im2latex"]
    assert ok.artifact_manifest == [["part.parquet", 4, 123, "sha"]]
    assert ok.key_components["config"]["hash"] == "h" and ok.metrics == {"items": 3}
    assert ok.config_access is None and ok.updated_at

    failed = unit(
        status="failed", error_type="ValueError", error_summary="bad", error_traceback="tb"
    )
    repo.upsert_units([failed])
    row = repo.get_unit("main", "work", "dataset=mw")
    assert row is not None and row.status == "failed" and row.error_type == "ValueError"

    assert repo.mark_orphaned("main", "work", ["dataset=im2latex"]) == 1
    assert [u.unit_key for u in repo.units_with_status("orphaned")] == ["dataset=im2latex"]
    assert repo.unit_counts("main") == {"work": {"failed": 1, "orphaned": 1}}
    assert repo.delete_units("main", "work", ["dataset=im2latex"]) == 1
    assert repo.delete_units("main") == 1


def test_cross_branch_candidates_and_reuse(repo: Repo) -> None:
    repo.upsert_units([unit(), unit(branch="katex"), unit(branch="katex", key="dataset=b")])
    candidates = repo.reuse_candidates("work", "dataset=mw", "ik-1", exclude_branch_id="main")
    assert [(name, row.branch_id) for name, row in candidates] == [("katex", "katex")]
    assert repo.reuse_candidates("work", "dataset=mw", "ik-2", exclude_branch_id="main") == []

    updated = repo.set_reused_source_fp(
        "main", "work", [("dataset=mw", "ik-1"), ("dataset=zz", "ik-1")], "s2"
    )
    assert updated == 1
    row = repo.get_unit("main", "work", "dataset=mw")
    assert row is not None and row.reused_source_fp == "s2"
    assert repo.set_reused_source_fp("main", "work", [("dataset=mw", "stale")], "s3") == 0


def test_runs_and_stage_runs(repo: Repo) -> None:
    repo.put_stage_run(
        StageRunRow(
            "r1",
            "main",
            "work",
            "ok",
            3,
            1,
            0,
            4,
            2,
            1500,
            db.now_iso(),
            unit_p50_ms=400,
            metrics_summary={"items": 30},
        )
    )
    repo.finish_run("r1", status="ok", total_wall_ms=1600)
    run = repo.get_run("r1")
    assert run is not None and run.status == "ok" and run.ended_at and run.cli_argv == ["run"]
    (stage_run,) = repo.stage_runs("main", stage="work")
    assert stage_run.metrics_summary == {"items": 30} and stage_run.unit_p95_ms is None
    assert [r.run_id for r in repo.runs("main")] == ["r1"]
    repo.delete_branch("katex")
    assert repo.get_branch("katex") is None and repo.get_run("r2") is None


def test_file_sets_dedup_and_stat_cache(repo: Repo) -> None:
    row = FileSetRow("f" * 64, 2, 10, [["content", [["a", "x"], ["b", "y"]]]])
    repo.put_file_sets([row, row])
    repo.put_file_sets([row])
    assert repo.conn.execute("SELECT COUNT(*) FROM file_sets").fetchone()[0] == 1
    assert repo.get_file_set("f" * 64) == row
    assert repo.get_file_set("missing") is None

    repo.put_stat_cache({"/a.py": CachedStat(1, 2, 3, "c", "ast")})
    repo.put_stat_cache({"/a.py": CachedStat(1, 2, 4, "c2", None)})
    assert repo.load_stat_cache() == {"/a.py": CachedStat(1, 2, 4, "c2", None)}


def test_transaction_batches_and_rolls_back(repo: Repo) -> None:
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.upsert_units([unit()])
            raise RuntimeError("boom")
    assert repo.units("main", "work") == {}
    with repo.transaction():
        repo.upsert_units([unit()])
        repo.mark_orphaned("main", "work", ["dataset=mw"])
    assert repo.units("main", "work")["dataset=mw"].status == "orphaned"


def test_lock_semantics(tmp_path: Path) -> None:
    with pipeline_lock(tmp_path, exclusive=False), pipeline_lock(tmp_path, exclusive=False):
        with pytest.raises(LockBusyError, match="exclusive"):
            pipeline_lock(tmp_path, exclusive=True).acquire()
    with pipeline_lock(tmp_path, exclusive=True):
        with pytest.raises(LockBusyError, match="shared"):
            pipeline_lock(tmp_path, exclusive=False).acquire()
    with branch_lock(tmp_path, "main") as held:
        assert held.held
        with branch_lock(tmp_path, "katex"):
            pass
        with pytest.raises(LockBusyError):
            branch_lock(tmp_path, "main").acquire()
    assert layout.branch_lock_path(tmp_path, "main").exists()  # lock files persist
    lock = FileLock(tmp_path / "x.lock", exclusive=True)
    with lock:
        with pytest.raises(RuntimeError, match="already held"):
            lock.acquire()
    assert not lock.held


def _connect_after_barrier(path: str, barrier) -> None:
    barrier.wait()
    db.connect(Path(path)).close()


def test_concurrent_first_connects_do_not_race(tmp_path: Path) -> None:
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    for attempt in range(8):
        path = tmp_path / f"s{attempt}" / "state.db"
        barrier = context.Barrier(3)
        processes = [
            context.Process(target=_connect_after_barrier, args=(str(path), barrier))
            for _ in range(3)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)
        assert [process.exitcode for process in processes] == [0, 0, 0]


def test_reader_waits_for_a_store_that_is_being_created(tmp_path: Path) -> None:
    import threading
    import time

    path = layout.state_db_path(tmp_path)
    path.parent.mkdir(parents=True)
    found: list[object] = []

    def read() -> None:
        try:
            conn = db.connect(path, readonly=True)
            found.append(db.read_meta(conn)["schema_version"])
            conn.close()
        except Exception as error:  # noqa: BLE001 - asserted below
            found.append(error)

    with db._init_lock(path):
        path.touch()  # a writable open created the file and is still creating the schema
        reader = threading.Thread(target=read)
        reader.start()
        time.sleep(0.2)
        assert reader.is_alive() and not found
        conn = sqlite3.connect(path, isolation_level=None)
        db._check_schema(conn, path, readonly=False)
        conn.close()
    reader.join(timeout=10)
    assert found == [db.SCHEMA_VERSION]
    # A blank file nobody is initializing is still rejected.
    blank = tmp_path / "blank.db"
    blank.touch()
    with pytest.raises(db.SchemaError):
        db.connect(blank, readonly=True)
