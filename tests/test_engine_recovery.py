"""Startup recovery, SIGINT handling, bookkeeping rows and graph metadata."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

from varve.branch_config import BranchRequest
from varve.engine import RunOptions
from varve.engine.housekeeping import recover
from varve.engine.session import open_session
from varve.store import layout
from varve.store.db import connect, read_meta
from varve.store.repo import Repo, RunRow, StageRunRow

SOURCE = """
import time
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

class Config(BaseModel):
    marker_dir: str = ""
    sleep: float = 0.0

ITEM = Axis("item", ("a", "b", "c"))

class Demo(Pipeline):
    Config = Config

    @stage(foreach=ITEM, produces="v.txt")
    def work(self, ctx: Ctx[Config], item: str) -> None:
        if ctx.config.marker_dir:
            (Path(ctx.config.marker_dir) / f"started-{item}").touch()
        time.sleep(ctx.config.sleep)
        (ctx.out / "v.txt").write_text(item)
        ctx.metric(items=3)

    @stage(produces="all.txt")
    def merge(self, ctx: Ctx[Config], work: dict) -> None:
        (ctx.out / "all.txt").write_text(",".join(p.read_text() for p in work.values()))

if __name__ == "__main__":
    raise SystemExit(Demo.cli())
"""


def _repo(ws, readonly: bool = True) -> Repo:
    return Repo(connect(layout.state_db_path(ws.root), readonly=readonly))


def test_recovery_restores_old_dirs_and_removes_staging(ws) -> None:
    Demo = ws.write(SOURCE)
    first = ws.run(Demo)
    final = ws.root / "out/.internal/main/work/item=a"
    backup = final.with_name("item=a.old-crashed")
    os.rename(final, backup)
    staging = ws.root / "out/.staging/crashed/work/0123456789abcdef"
    staging.mkdir(parents=True)
    (staging / "partial").write_text("x")
    repo = _repo(ws, readonly=False)
    repo.set_status("main", "work", ["item=a"], "running")
    repo.insert_run(RunRow("crashed", "main", "2026-01-01T00:00:00", "running", [], "host"))
    repo.put_stage_run(StageRunRow("crashed", "main", "work", "running", 0, 0, 0, 3, 1, 0, "x"))
    repo.conn.close()

    session = open_session(Demo, BranchRequest(), root=ws.root, readonly=False)
    try:
        notes = recover(session, first.run_id + "-new")
    finally:
        session.close()
    assert final.is_dir() and not backup.exists()
    assert (final / "v.txt").read_text() == "a"
    assert not (ws.root / "out/.staging/crashed").exists()
    assert any("restored work@item=a" in note for note in notes)
    repo = _repo(ws)
    run = repo.get_run("crashed")
    assert run is not None and run.status == "interrupted"
    crashed = [row for row in repo.stage_runs("main") if row.run_id == "crashed"]
    assert [row.status for row in crashed] == ["interrupted"]
    repo.conn.close()

    result = ws.run(Demo, options=RunOptions(tui=False))
    assert result.stages["work"].executed == 1  # the interrupted unit reruns
    assert result.stages["merge"].executed == 0  # same bytes: downstream stays hit


def test_running_row_reads_as_interrupted(ws, capsys) -> None:
    Demo = ws.write(SOURCE)
    ws.run(Demo)
    repo = _repo(ws, readonly=False)
    repo.set_status("main", "work", ["item=b"], "running")
    repo.conn.close()
    assert ws.cli(Demo, "status", "work", "--expand") == 0
    assert "interrupted" in capsys.readouterr().out


def test_bookkeeping_rows_and_graph_meta(ws) -> None:
    Demo = ws.write(SOURCE)
    events = []
    result = ws.run(Demo, on_event=events.append)
    repo = _repo(ws)
    meta = read_meta(repo.conn)
    graph = json.loads(meta["graph_json"])
    assert [stage["name"] for stage in graph["stages"]] == ["work", "merge"]
    assert graph["stages"][1]["deps"] == [{"stage": "work", "kind": "data"}]
    assert graph["stages"][0]["axes"] == [
        {"name": "item", "kind": "static", "ids": ["a", "b", "c"]}
    ]
    assert graph["stages"][0]["workers_static"] == 1
    assert meta["module"] == ws.module and meta["qualname"] == "Demo"
    runs = repo.runs("main")
    assert [run.status for run in runs] == ["ok"]
    stage_runs = {row.stage_name: row for row in repo.stage_runs("main")}
    assert stage_runs["work"].total_units == 3 and stage_runs["work"].status == "ok"
    assert stage_runs["work"].metrics_summary == {"items": 9}
    assert stage_runs["merge"].hit_units == 0 and stage_runs["merge"].executed_units == 1
    branch = repo.get_branch("main")
    assert branch is not None and branch.config == {"marker_dir": "", "sleep": 0.0}
    assert repo.storages() == {"default": ws.root / "out"}
    repo.conn.close()
    assert type(events[0]).__name__ == "RunStarted" and type(events[-1]).__name__ == "RunFinished"
    assert result.exit_code == 0


def test_stage_run_row_is_running_while_the_stage_executes(ws) -> None:
    Demo = ws.write(SOURCE)
    seen = {}

    def on_event(event) -> None:
        if type(event).__name__ == "UnitStarted" and not seen:
            conn = connect(layout.state_db_path(ws.root), readonly=True)
            seen["row"] = tuple(
                conn.execute(
                    "SELECT status, total_units FROM stage_runs WHERE stage_name = 'work'"
                ).fetchone()
            )
            seen["run"] = conn.execute("SELECT status FROM runs").fetchone()[0]
            conn.close()

    ws.run(Demo, on_event=on_event)
    assert seen == {"row": ("running", 3), "run": "running"}


def test_first_sigint_finishes_active_unit_and_stops(ws) -> None:
    markers = ws.base / "markers"
    markers.mkdir()
    (ws.src / "varve.yaml").write_text(
        json.dumps({"main": {"config": {"marker_dir": str(markers), "sleep": 0.6}}})
    )
    ws.write(SOURCE)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.Popen(
        [sys.executable, str(ws.path), "run", "--no-tui", "--root", str(ws.root)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20
    while not (markers / "started-a").exists():
        assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
        time.sleep(0.02)
    proc.send_signal(signal.SIGINT)
    _, stderr = proc.communicate(timeout=20)
    assert proc.returncode == 130, stderr
    repo = _repo(ws)
    units = repo.units("main", "work")
    assert {key: row.status for key, row in units.items()} == {"item=a": "ok"}
    [run] = repo.runs("main")
    assert run.status == "interrupted"
    repo.conn.close()
    assert not (markers / "started-b").exists()
    assert "interrupt" in stderr


# -- review regressions ---------------------------------------------------------


def test_failed_promote_puts_the_previous_unit_dir_back(tmp_path, monkeypatch) -> None:
    import pytest

    from varve.engine import unit as unit_module

    final = tmp_path / "final"
    final.mkdir()
    (final / "v.txt").write_text("old")
    staging = tmp_path / "staging"
    staging.mkdir()
    real_rename = os.rename

    def rename(src, dst) -> None:
        if str(src) == str(staging):
            raise OSError("disk trouble")
        real_rename(src, dst)

    monkeypatch.setattr(unit_module.os, "rename", rename)
    with pytest.raises(OSError, match="disk trouble"):
        unit_module.promote(staging, final, "run1")
    assert (final / "v.txt").read_text() == "old"
    assert not unit_module.old_dir(final, "run1").exists()


def test_link_promote_failure_falls_back_to_executing(ws, monkeypatch) -> None:
    from varve.engine import commit as commit_module

    (ws.src / "varve.yaml").write_text(
        json.dumps({"main": {"config": {}}, "other": {"config": {"sleep": 0.0}}})
    )
    Demo = ws.write(SOURCE)
    assert ws.run(Demo).status == "ok"
    real_promote = commit_module.promote

    def promote(staging, final, run_id) -> None:
        if sys._getframe(1).f_code.co_name == "link":
            raise OSError("cross-branch promote failed")
        real_promote(staging, final, run_id)

    monkeypatch.setattr(commit_module, "promote", promote)
    result = ws.run(Demo, branch="other")
    assert result.status == "ok"
    assert (result.stages["work"].executed, result.stages["work"].linked) == (3, 0)


def test_unexpected_commit_errors_fail_the_unit_not_the_run(ws, monkeypatch) -> None:
    from varve.engine.commit import Committer

    def boom(*args, **kwargs) -> None:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(Committer, "commit_ok", boom)
    Demo = ws.write(SOURCE)
    result = ws.run(Demo)
    assert result.status == "failed"
    assert result.stages["work"].failed == 3
    repo = _repo(ws)
    rows = repo.units("main", "work")
    assert {row.status for row in rows.values()} == {"failed"}
    assert all(
        "commit failed: store exploded" in (row.error_summary or "") for row in rows.values()
    )
    repo.conn.close()


RUNNING_MARK = """
import sqlite3
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage
from varve.store import layout

class Config(BaseModel):
    root: str = ""

ITEM = Axis("item", ("a", "b"))

class Demo(Pipeline):
    Config = Config

    @stage(foreach=ITEM)
    def work(self, ctx: Ctx[Config], item: str) -> None:
        db = layout.state_db_path(Path(ctx.config.root))
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT status FROM units WHERE stage_name = 'work' AND unit_key = ?", (ctx.unit_key,)
        ).fetchone()
        conn.close()
        (ctx.out / "seen.txt").write_text(row[0] if row else "missing")
"""


def test_running_marker_is_committed_before_a_rerun_unit_body_starts(ws) -> None:
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"root": str(ws.root)}}}))
    Demo = ws.write(RUNNING_MARK)
    assert ws.run(Demo).status == "ok"
    for item in ("a", "b"):
        seen = ws.root / f"out/.internal/main/work/item={item}/seen.txt"
        # New units' marks are batched (without a row they probe as `new` anyway).
        assert seen.read_text() in ("missing", "running")
    assert ws.run(Demo, options=RunOptions(force=True, tui=False)).status == "ok"
    for item in ("a", "b"):
        seen = ws.root / f"out/.internal/main/work/item={item}/seen.txt"
        assert seen.read_text() == "running"


def test_sigint_during_an_event_publish_does_not_deadlock(ws) -> None:
    Demo = ws.write(SOURCE)
    events = []

    def on_event(event) -> None:
        events.append(event)
        if type(event).__name__ == "UnitStarted" and len(events) < 100:
            os.kill(os.getpid(), signal.SIGINT)  # handled while the bus lock is held

    result = ws.run(Demo, on_event=on_event)
    assert result.status == "interrupted"
    notices = [e.text for e in events if type(e).__name__ == "Notice"]
    assert any("interrupt: finishing active units" in text for text in notices)
    assert result.stages["work"].executed == 1


def test_cross_branch_link_rechecks_a_source_replaced_meanwhile(ws, monkeypatch) -> None:
    from varve.engine import commit as commit_module
    from varve.keying.fingerprint import verify_manifest

    (ws.src / "varve.yaml").write_text(json.dumps({"main": {}, "other": {}}))
    Demo = ws.write(SOURCE)
    assert ws.run(Demo).status == "ok"
    real_link_tree = commit_module.link_tree

    def link_tree(source, staging) -> None:
        # `main` replaces the unit dir right now (branches run concurrently).
        fresh = source.with_name(source.name + ".fresh")
        fresh.mkdir()
        (fresh / "v.txt").write_text("replaced")
        os.rename(source, source.with_name(source.name + ".gone"))
        os.rename(fresh, source)
        real_link_tree(source, staging)

    monkeypatch.setattr(commit_module, "link_tree", link_tree)
    result = ws.run(Demo, branch="other")
    assert result.status == "ok"
    assert (result.stages["work"].linked, result.stages["work"].executed) == (0, 3)
    repo = _repo(ws)
    for row in repo.units("other", "work").values():
        assert row.linked_from is None and row.artifact_manifest is not None
        unit_dir = (ws.root / "out" / (row.artifact_relpath or "")).parent
        assert verify_manifest(unit_dir, row.artifact_manifest, rehash=True)
    repo.conn.close()


def _popen_cli(ws, *argv: str) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    return subprocess.Popen(
        [sys.executable, str(ws.path), "run", "--no-tui", "--root", str(ws.root), *argv],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _wait_for(path, proc) -> None:
    deadline = time.monotonic() + 20
    while not path.exists():
        assert time.monotonic() < deadline and proc.poll() is None
        time.sleep(0.02)


def test_kill_9_mid_run_is_recovered_by_the_next_run(ws) -> None:
    from varve.keying.fingerprint import verify_manifest

    Demo = ws.write(SOURCE)
    assert ws.run(Demo).status == "ok"
    markers = ws.base / "markers"
    markers.mkdir()
    config = {"marker_dir": str(markers), "sleep": 0.3}
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": config}}))
    proc = _popen_cli(ws)  # a config change: every unit dir is replaced via `.old-*`
    _wait_for(markers / "started-b", proc)
    proc.kill()
    proc.wait()

    result = ws.run(Demo)
    assert result.status == "ok"
    assert result.stages["work"].executed >= 2  # b (killed mid-unit) and c rerun
    repo = _repo(ws)
    assert [run.status for run in repo.runs("main")] == ["ok", "interrupted", "ok"]
    for stage in ("work", "merge"):
        for row in repo.units("main", stage).values():
            assert row.status == "ok" and row.artifact_manifest is not None
            unit_dir = (ws.root / "out" / (row.artifact_relpath or "")).parent
            assert verify_manifest(unit_dir, row.artifact_manifest, rehash=True)
    repo.conn.close()
    assert not list((ws.root / "out/.internal").rglob("*.old-*"))
    assert not any((ws.root / "out/.staging").iterdir())
    assert ws.run(Demo).stages["work"].executed == 0


PROCESS_WORKERS = """
import time
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

class Config(BaseModel):
    marker_dir: str = ""

ITEM = Axis("item", tuple(f"i{n}" for n in range(8)))

class Demo(Pipeline):
    Config = Config

    @stage(foreach=ITEM, workers=2)
    def work(self, ctx: Ctx[Config], item: str) -> None:
        (Path(ctx.config.marker_dir) / f"started-{item}").touch()
        time.sleep(0.5)
        (ctx.out / "v.txt").write_text(item)

if __name__ == "__main__":
    raise SystemExit(Demo.cli())
"""


def _children(pid: int) -> list[int]:
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = open(f"/proc/{entry}/stat").read()
            except OSError:
                continue
            if int(stat.rpartition(")")[2].split()[1]) == pid:
                found.append(int(entry))
    return found


def _alive(pid: int) -> bool:
    try:
        stat = open(f"/proc/{pid}/stat").read()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def test_process_workers_exit_when_the_run_process_is_killed(ws) -> None:
    import pytest

    if not os.path.exists("/proc/self/stat"):
        pytest.skip("needs /proc")
    markers = ws.base / "markers"
    markers.mkdir()
    (ws.src / "varve.yaml").write_text(
        json.dumps({"main": {"config": {"marker_dir": str(markers)}}})
    )
    ws.write(PROCESS_WORKERS)
    proc = _popen_cli(ws)
    _wait_for(markers / "started-i1", proc)
    children = _children(proc.pid)
    assert children
    proc.kill()
    proc.wait()
    deadline = time.monotonic() + 10
    while any(_alive(pid) for pid in children):
        assert time.monotonic() < deadline, "process-pool workers outlived the run"
        time.sleep(0.05)


def test_run_records_the_git_commit_of_the_pipeline_checkout(ws, tmp_path, monkeypatch) -> None:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(ws.src)]
    Demo = ws.write(SOURCE)
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # not a git checkout
    result = ws.run(Demo)
    repo = _repo(ws)
    run = repo.get_run(result.run_id)
    repo.conn.close()
    assert run is not None and run.git_commit == head and run.git_dirty is False
