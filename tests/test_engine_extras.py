"""TUI mode capture, live view rendering, log rotation, --rehash, worker_env, the Ref seam."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from rich.console import Console

from varve.axis import Axis
from varve.engine import (
    NoRefResolver,
    RefBinding,
    RefResolutionError,
    RunOptions,
    WorkspaceSettings,
    probe_pipeline,
)
from varve.engine.events import (
    RunStarted,
    StageFinished,
    StagePlanned,
    UnitFinished,
    UnitProgress,
    UnitStarted,
)
from varve.engine.housekeeping import rotate_logs
from varve.engine.tui import LiveState
from varve.ref import Ref
from varve.store import layout

SOURCE = """
import os
import subprocess
import sys
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

ITEM = Axis("item", ("a", "b"))

class Demo(Pipeline):
    @stage(foreach=ITEM, produces="v.txt")
    def talk(self, ctx: Ctx, item: str) -> None:
        print(f"python {item}")
        subprocess.run([sys.executable, "-c", f"print('subprocess {item}')"], check=True)
        (ctx.out / "v.txt").write_text(item)

    @stage(foreach=ITEM, workers=2, backend="thread", produces="v.txt")
    def threads(self, ctx: Ctx, item: str) -> None:
        print(f"thread {item}")
        (ctx.out / "v.txt").write_text(item)

    @stage(foreach=ITEM, workers=2, produces="env.txt")
    def env(self, ctx: Ctx, item: str) -> None:
        (ctx.out / "env.txt").write_text(os.environ.get("VARVE_TEST_ENV", "unset"))
"""


def test_tui_mode_routes_unit_output_and_fd_output(ws) -> None:
    Demo = ws.write(SOURCE)
    result = ws.run(Demo, options=RunOptions(only="talk", tui=True))
    assert result.status == "ok"
    log = layout.unit_log_path(ws.root, result.run_id, "talk", "item=a")
    assert log.read_text() == "python a\n"
    process_log = layout.logs_dir(ws.root, result.run_id) / "process.log"
    assert "subprocess a" in process_log.read_text()


def test_thread_backend_captures_per_unit_logs(ws) -> None:
    Demo = ws.write(SOURCE)
    result = ws.run(Demo, options=RunOptions(only="threads", tui=False))
    for item in ("a", "b"):
        log = layout.unit_log_path(ws.root, result.run_id, "threads", f"item={item}")
        assert log.read_text() == f"thread {item}\n"


def test_worker_env_reaches_process_workers(ws) -> None:
    Demo = ws.write(SOURCE)
    workspace = WorkspaceSettings(worker_env={"VARVE_TEST_ENV": "set-by-workspace"})
    ws.run(Demo, options=RunOptions(only="env", tui=False), workspace=workspace)
    value = (ws.root / "out/.internal/main/env/item=a/env.txt").read_text()
    assert value == "set-by-workspace"
    assert "VARVE_TEST_ENV" not in os.environ


def test_workspace_storage_bases(ws) -> None:
    Demo = ws.write(SOURCE)
    base = ws.base / "bulk"
    workspace = WorkspaceSettings(storage={"default": base}, pipeline_name="demo")
    ws.run(Demo, options=RunOptions(only="threads", tui=False), workspace=workspace)
    assert (base / "demo/.internal/main/threads/item=a/v.txt").exists()


def test_rehash_detects_same_size_same_mtime_edits(ws) -> None:
    Demo = ws.write(SOURCE)
    ws.run(Demo, options=RunOptions(only="threads", tui=False))
    managed = ws.root / "out/.internal/main/threads/item=a/v.txt"
    st = managed.stat()
    managed.chmod(0o644)
    managed.write_text("z")
    os.utime(managed, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert probe_pipeline(Demo, root=ws.root, stages=["threads"]).stage("threads").status == "hit"
    view = probe_pipeline(Demo, root=ws.root, stages=["threads"], rehash=True, expand=True)
    reasons = {unit.unit_key: unit.reason for unit in view.stage("threads").units}
    assert reasons == {"item=a": "artifact-changed", "item=b": None}
    result = ws.run(Demo, options=RunOptions(only="threads", rehash=True, tui=False))
    assert result.stages["threads"].executed == 1
    assert managed.read_text() == "a"


def test_rotate_logs_keeps_the_newest_runs(tmp_path: Path) -> None:
    base = layout.logs_dir(tmp_path)
    for index in range(25):
        (base / f"20260101T0000{index:02d}-abc").mkdir(parents=True)
    rotate_logs(tmp_path, keep=20)
    remaining = sorted(path.name for path in base.iterdir())
    assert len(remaining) == 20 and remaining[0] == "20260101T000005-abc"


def test_log_rotation_keeps_the_logs_of_runs_still_running(ws) -> None:
    from varve.store.db import connect, now_iso
    from varve.store.repo import BranchRow, Repo, RunRow

    Demo = ws.write(SOURCE)
    assert ws.run(Demo, options=RunOptions(only="threads", tui=False)).status == "ok"
    # A long run of another branch started before 25 newer runs and is still writing logs.
    repo = Repo(connect(layout.state_db_path(ws.root)))
    repo.upsert_branch(BranchRow("other", "other", {}, {}))
    repo.insert_run(RunRow("20000101T000000-aaaaaa", "other", now_iso(), "running", [], "h"))
    repo.conn.close()
    base = layout.logs_dir(ws.root)
    (base / "20000101T000000-aaaaaa" / "work").mkdir(parents=True)
    for index in range(25):
        (base / f"20000101T0001{index:02d}-bbbbbb").mkdir()
    assert ws.run(Demo, options=RunOptions(only="threads", tui=False)).status == "ok"
    assert (base / "20000101T000000-aaaaaa").is_dir()
    assert not (base / "20000101T000100-bbbbbb").exists()


def test_live_state_renders_stages_units_and_failures() -> None:
    state = LiveState()
    state.apply(RunStarted("demo", "main", "run1", ("prepare", "work", "later")))
    state.apply(StagePlanned("prepare", "ready", total=3, hit=3))
    state.apply(StageFinished("prepare", "ok", 0, 3, 0, 5))
    state.apply(
        StagePlanned("work", "ready", total=4, hit=1, to_run=3, workers=2, backend="process")
    )
    state.apply(UnitStarted("work", "shard=1", at=0.0))
    state.apply(UnitProgress("work", "shard=1", 5, 10, "rows"))
    state.apply(UnitStarted("work", "shard=2"))
    state.apply(UnitFinished("work", "shard=2", False, 12, "RuntimeError: boom"))
    state.apply(StagePlanned("later", "blocked", detail="awaiting 'work'"))
    buffer = io.StringIO()
    Console(file=buffer, width=120, color_system=None).print(state.render())
    text = buffer.getvalue()
    assert "demo" in text and "@ main" in text and "1/3 units" in text
    assert "hit 3" in text and "running 1/3" in text
    assert "work@shard=1" in text and "5/10" in text
    assert "✕ work@shard=2: RuntimeError: boom" in text
    assert "blocked" in text and "awaiting 'work'" in text


REF_SOURCE = """
from pathlib import Path
from varve import Ctx, Pipeline, Ref, stage

class Demo(Pipeline):
    upstream_data = Ref("other.pipeline", stage="export")

    @stage(produces="copy.txt")
    def consume(self, ctx: Ctx, upstream_data: Path) -> None:
        (ctx.out / "copy.txt").write_text(upstream_data.read_text())
"""


class FakeRefs:
    def __init__(self, path: Path, key: str) -> None:
        self.path, self.key = path, key
        self.calls: list[tuple[str, tuple[tuple[str, ...], ...]]] = []

    def bind(
        self, name: str, ref: Ref, axes: Sequence[Axis], coords: Sequence[tuple[str, ...]]
    ) -> list[RefBinding]:
        self.calls.append((name, tuple(coords)))
        return [RefBinding(self.path, self.key) for _ in coords]


def test_ref_seam_injects_bindings_and_keys_on_them(ws) -> None:
    Demo = ws.write(REF_SOURCE)
    data = ws.base / "export.txt"
    data.write_text("from upstream")
    with pytest.raises(RefResolutionError, match="Ref resolution not available"):
        probe_pipeline(Demo, root=ws.root, refs=NoRefResolver())
    refs = FakeRefs(data, "fp-1")
    assert ws.run(Demo, refs=refs).status == "ok"
    assert (ws.root / "out/.internal/main/consume/copy.txt").read_text() == "from upstream"
    assert refs.calls[0] == ("upstream_data", ((),))
    assert probe_pipeline(Demo, root=ws.root, refs=refs).stage("consume").status == "hit"
    view = probe_pipeline(Demo, root=ws.root, refs=FakeRefs(data, "fp-2"), expand=True)
    assert view.stage("consume").units[0].reason == "upstream-changed"
    graph = json.loads(
        __import__("sqlite3")
        .connect(layout.state_db_path(ws.root))
        .execute("SELECT value FROM meta WHERE key = 'graph_json'")
        .fetchone()[0]
    )
    assert graph["refs"] == [
        {
            "attr": "upstream_data",
            "target": "other.pipeline",
            "stage": "export",
            "branches": ["main"],
            "selector": None,
            "align_by": None,
        }
    ]
    assert graph["stages"][0]["refs"] == [{"attr": "upstream_data", "kind": "data"}]


# -- review regressions ---------------------------------------------------------

ENV_AT_IMPORT = """
import os
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

AT_IMPORT = os.environ.get("VARVE_TEST_ENV", "unset")

class Config(BaseModel):
    pass

ITEM = Axis("item", ("a", "b"))

class Demo(Pipeline):
    Config = Config

    @stage(foreach=ITEM, workers=2)
    def env(self, ctx: Ctx[Config], item: str) -> None:
        (ctx.out / "env.txt").write_text(AT_IMPORT)
"""


def test_worker_env_is_set_before_workers_import_user_modules(ws) -> None:
    Demo = ws.write(ENV_AT_IMPORT)
    workspace = WorkspaceSettings(worker_env={"VARVE_TEST_ENV": "from-birth"})
    assert ws.run(Demo, workspace=workspace).status == "ok"
    for item in ("a", "b"):
        value = (ws.root / f"out/.internal/main/env/item={item}/env.txt").read_text()
        assert value == "from-birth"
    assert "VARVE_TEST_ENV" not in os.environ


STREAMS = """
import sys
from varve import Axis, Ctx, Pipeline, stage

ITEM = Axis("item", ("a", "b"))

class Demo(Pipeline):
    @stage(foreach=ITEM, workers=2, backend="thread")
    def binary(self, ctx: Ctx, item: str) -> None:
        print(f"text {item}")
        sys.stdout.buffer.write(f"bytes {item}\\n".encode())
        sys.stdout.flush()
        assert sys.stdout.encoding and sys.stdout.errors is not None
        assert not sys.stdout.isatty() and sys.stdout.fileno() >= 0
        (ctx.out / "ok").write_text("x")

    @stage()
    def plain(self, ctx: Ctx) -> None:
        (ctx.out / "stream.txt").write_text(type(sys.stdout).__name__)
"""


def test_routed_streams_have_a_binary_buffer_and_are_only_installed_when_capturing(ws) -> None:
    Demo = ws.write(STREAMS)
    result = ws.run(Demo)
    assert result.status == "ok", result.stages
    for item in ("a", "b"):
        log = layout.unit_log_path(ws.root, result.run_id, "binary", f"item={item}")
        assert log.read_text() == f"text {item}\nbytes {item}\n"
    assert (ws.root / "out/.internal/main/plain/stream.txt").read_text() != "RoutedStream"


ROOTS = """
from pathlib import Path
from pydantic import BaseModel
from varve import Ctx, Pipeline, stage

class Config(BaseModel):
    out: str = ""

class Demo(Pipeline):
    Config = Config

    @classmethod
    def storage_roots(cls, config):
        return {"default": Path(config.out)}

    @stage()
    def work(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "x.txt").write_text("x")
"""


def test_storage_roots_must_not_depend_on_branch_config(ws) -> None:
    first, second = ws.base / "first", ws.base / "second"
    branches = {"main": {"config": {"out": str(first)}}, "b": {"config": {"out": str(second)}}}
    (ws.src / "varve.yaml").write_text(json.dumps(branches))
    Demo = ws.write(ROOTS)
    assert ws.run(Demo).status == "ok"
    with pytest.raises(ValueError, match="storage root for 'default' changed from"):
        ws.run(Demo, branch="b")
    assert not second.exists()
    assert ws.cli(Demo, "run", "--branch", "b", "--no-tui") == 1


def test_clean_uses_the_recorded_storage_roots(ws) -> None:
    first, second = ws.base / "first", ws.base / "second"
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"out": str(first)}}}))
    Demo = ws.write(ROOTS)
    assert ws.run(Demo).status == "ok"
    assert (first / ".internal/main/work/x.txt").exists()
    # The computed root moves; clean still removes what the store recorded.
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"out": str(second)}}}))
    assert ws.cli(Demo, "clean", "--yes") == 0
    assert not (first / ".internal").exists()
