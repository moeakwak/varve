"""Resource preflight and scopes (stage / worker / unit), zero cold start, picklability."""

from __future__ import annotations

import json

import pytest

from varve.engine import RunOptions

SOURCE = """
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, Resource, stage

class Config(BaseModel):
    trace: str = ""

ITEM = Axis("item", ("a", "b", "c", "d"))

def mark(ctx, kind):
    (Path(ctx.config.trace) / f"{kind}-{os.getpid()}-{uuid.uuid4().hex}").touch()

def session(kind):
    @contextmanager
    def open_(ctx):
        mark(ctx, kind)
        yield {"kind": kind, "pid": os.getpid()}
    return open_

@contextmanager
def lock_session(ctx):
    yield threading.Lock()

StageRes = Resource("stage_res", open=session("stage"), preflight=lambda ctx: mark(ctx, "preflight"))
WorkerRes = Resource("worker_res", open=session("worker"), scope="worker")
UnitRes = Resource("unit_res", open=session("unit"), scope="unit")
LockRes = Resource("lock_res", open=lock_session)

@contextmanager
def broken_session(ctx):
    raise RuntimeError("cannot start engine")
    yield

BrokenRes = Resource("broken_res", open=broken_session, scope="worker")

class Demo(Pipeline):
    Config = Config

    @stage(foreach=ITEM, workers=2, resources=[StageRes, WorkerRes, UnitRes])
    def uses(self, ctx: Ctx[Config], item: str, stage_res: dict, worker_res: dict, unit_res: dict) -> None:
        payload = {"stage": stage_res, "worker": worker_res, "unit": unit_res, "pid": os.getpid()}
        (ctx.out / "s.json").write_text(__import__("json").dumps(payload))

    @stage(foreach=ITEM, resources=[StageRes, WorkerRes, UnitRes], workers=2, backend="thread")
    def threaded(self, ctx: Ctx[Config], item: str, stage_res: dict, worker_res: dict, unit_res: dict) -> None:
        (ctx.out / "ok").write_text(stage_res["kind"] + worker_res["kind"] + unit_res["kind"])

    @stage(foreach=ITEM, workers=2, resources=[BrokenRes])
    def crash(self, ctx: Ctx[Config], item: str, broken_res) -> None:
        (ctx.out / "never").write_text("x")

    @stage(foreach=ITEM, workers=2, resources=[LockRes])
    def bad(self, ctx: Ctx[Config], item: str, lock_res) -> None:
        (ctx.out / "never").write_text("x")
"""


def _counts(trace) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in trace.iterdir():
        kind = path.name.split("-")[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def test_process_backend_scopes_and_zero_cold_start(ws) -> None:
    trace = ws.base / "trace"
    trace.mkdir()
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"trace": str(trace)}}}))
    Demo = ws.write(SOURCE)
    result = ws.run(Demo, options=RunOptions(only="uses", tui=False))
    assert result.status == "ok", result.stages
    counts = _counts(trace)
    assert counts["preflight"] == 1 and counts["stage"] == 1 and counts["unit"] == 4
    assert 1 <= counts["worker"] <= 2
    payloads = [
        json.loads(path.read_text())
        for path in (ws.root / "out/.internal/main/uses").rglob("s.json")
    ]
    main_pids = {payload["stage"]["pid"] for payload in payloads}
    assert len(main_pids) == 1 and main_pids != {payload["pid"] for payload in payloads}
    assert all(payload["worker"]["pid"] == payload["pid"] for payload in payloads)
    assert all(payload["unit"]["pid"] == payload["pid"] for payload in payloads)

    before = _counts(trace)
    again = ws.run(Demo, options=RunOptions(only="uses", tui=False))
    assert again.stages["uses"].executed == 0
    assert _counts(trace) == before


def test_thread_backend_worker_scope_behaves_like_stage(ws) -> None:
    trace = ws.base / "trace"
    trace.mkdir()
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"trace": str(trace)}}}))
    Demo = ws.write(SOURCE)
    assert ws.run(Demo, options=RunOptions(only="threaded", tui=False)).status == "ok"
    counts = _counts(trace)
    assert counts == {"preflight": 1, "stage": 1, "worker": 1, "unit": 4}


def test_unpicklable_stage_resource_on_process_backend_fails_clearly(ws) -> None:
    trace = ws.base / "trace"
    trace.mkdir()
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"trace": str(trace)}}}))
    Demo = ws.write(SOURCE)
    result = ws.run(Demo, options=RunOptions(only="bad", tui=False))
    outcome = result.stages["bad"]
    assert result.status == "failed" and outcome.status == "failed"
    assert "lock_res" in (outcome.detail or "")
    assert "cannot be pickled for backend='process'" in (outcome.detail or "")
    assert outcome.executed == 0


def test_worker_start_failure_fails_the_stage(ws) -> None:
    trace = ws.base / "trace"
    trace.mkdir()
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"trace": str(trace)}}}))
    Demo = ws.write(SOURCE)
    result = ws.run(Demo, options=RunOptions(only="crash", tui=False))
    outcome = result.stages["crash"]
    assert outcome.status == "failed"
    assert "process-pool worker crashed" in (outcome.detail or "")


# -- review regressions: cell-level resources and Config reads in callbacks ----

CELL_DEPENDS = """
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Dependencies, Pipeline, Resource, stage

class Config(BaseModel):
    base: str = ""

DS = Axis("dataset", ("a", "b"))
ENGINE = Resource(
    "engine",
    depends=lambda ctx: Dependencies(
        inputs={"model": Path(ctx.config.base) / f"{ctx.cell.dataset}.bin"},
        values={"tag": ctx.cell.dataset},
    ),
)

class Demo(Pipeline):
    Config = Config

    @stage(foreach=DS, resources=[ENGINE])
    def work(self, ctx: Ctx[Config], dataset: str, engine) -> None:
        (ctx.out / "x.txt").write_text((Path(ctx.config.base) / f"{dataset}.bin").read_text())
"""


def test_resource_with_callable_depends_keys_each_cell(ws) -> None:
    (ws.base / "a.bin").write_text("A1")
    (ws.base / "b.bin").write_text("B1")
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"base": str(ws.base)}}}))
    Demo = ws.write(CELL_DEPENDS)
    assert ws.run(Demo).status == "ok"
    (ws.base / "b.bin").write_text("B2-changed")
    again = ws.run(Demo)
    assert again.stages["work"].executed == 1


PER_CELL = """
from contextlib import contextmanager
from pydantic import BaseModel
from varve import Axis, Ctx, Dependencies, Pipeline, Resource, stage

class Config(BaseModel):
    pass

R = Axis("renderer", ("mathjax", "katex"))

def make(name, scope):
    @contextmanager
    def open_(rc):
        yield name
    return Resource("engine", depends=Dependencies(values={"engine": name}), open=open_, scope=scope)

class Demo(Pipeline):
    Config = Config

    @stage(foreach=R, resources=lambda ctx: [make(ctx.cell.renderer, "stage")])
    def inline(self, ctx: Ctx[Config], renderer: str, engine) -> None:
        (ctx.out / "x.txt").write_text(engine)

    @stage(foreach=R, workers=2, resources=lambda ctx: [make(ctx.cell.renderer, "worker")])
    def pooled(self, ctx: Ctx[Config], renderer: str, engine) -> None:
        (ctx.out / "x.txt").write_text(engine)
"""


def test_same_named_resources_of_different_cells_get_their_own_sessions(ws) -> None:
    Demo = ws.write(PER_CELL)
    assert ws.run(Demo).status == "ok"
    for name in ("inline", "pooled"):
        out = ws.root / "out/.internal/main" / name
        got = {p.parent.name: p.read_text() for p in out.rglob("x.txt")}
        assert got == {"renderer=mathjax": "mathjax", "renderer=katex": "katex"}, name


def test_two_resources_with_one_name_in_a_cell_are_an_error(ws) -> None:
    Demo = ws.write(
        PER_CELL.replace(
            '[make(ctx.cell.renderer, "stage")]',
            '[make(ctx.cell.renderer, "stage"), make("other", "stage")]',
        )
    )
    result = ws.run(Demo, options=RunOptions(only="inline", tui=False))
    assert result.stages["inline"].status == "failed"
    assert "two different resources are named 'engine'" in (result.stages["inline"].detail or "")


OPEN_READS = """
from contextlib import contextmanager
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, Resource, stage

class Config(BaseModel):
    renderer: str = "mathjax"
    gate: str = "on"
    other: int = 0

ITEM = Axis("item", ("a", "b"))

@contextmanager
def open_r(rc):
    yield rc.config.renderer

def check(rc):
    assert rc.config.gate

class Demo(Pipeline):
    Config = Config

    @stage(foreach=ITEM, resources=[Resource("rend", open=open_r, preflight=check, scope="SCOPE")],
           workers=WORKERS)
    def work(self, ctx: Ctx[Config], item: str, rend) -> None:
        (ctx.out / "x.txt").write_text(rend + str(ctx.config.other))
"""


@pytest.mark.parametrize(
    ("scope", "workers"), [("stage", 1), ("unit", 1), ("worker", 1), ("worker", 2), ("stage", 2)]
)
def test_config_reads_in_resource_callbacks_enter_the_key(ws, scope: str, workers: int) -> None:
    def config(**fields) -> None:
        (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": fields}}))

    config()
    Demo = ws.write(OPEN_READS.replace("SCOPE", scope).replace("WORKERS", str(workers)))
    assert ws.run(Demo).status == "ok"
    config(renderer="katex")
    assert ws.run(Demo).stages["work"].executed == 2  # read in open
    out = ws.root / "out/.internal/main/work"
    assert {p.read_text() for p in out.rglob("x.txt")} == {"katex0"}
    config(renderer="katex", gate="yes")
    assert ws.run(Demo).stages["work"].executed == 2  # read in preflight
    config(renderer="katex", gate="yes", other=0)
    assert ws.run(Demo).stages["work"].executed == 0


def test_workers_callable_reads_do_not_enter_the_key(ws) -> None:
    source = OPEN_READS.replace("SCOPE", "stage").replace(
        "WORKERS", "lambda rc: 1 if rc.config.other >= 0 else 2"
    )
    source = source.replace("str(ctx.config.other)", '""')
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {}}}))
    Demo = ws.write(source)
    assert ws.run(Demo).status == "ok"
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"other": 5}}}))
    assert ws.run(Demo).stages["work"].executed == 0


INTERRUPTED = """
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, Resource, stage

class Config(BaseModel):
    trace: str = ""

ITEM = Axis("item", tuple(f"i{n}" for n in range(6)))

def mark(ctx, kind):
    (Path(ctx.config.trace) / f"{kind}-{os.getpid()}-{uuid.uuid4().hex}").touch()

def session(kind):
    @contextmanager
    def open_(ctx):
        mark(ctx, f"open.{kind}")
        try:
            yield kind
        finally:
            mark(ctx, f"close.{kind}")
    return open_

SHARED = Resource("shared", open=session("stage"))
WORKER = Resource("worker", open=session("worker"), scope="worker")
PER_UNIT = Resource("per_unit", open=session("unit"), scope="unit")

def body(ctx, item):
    mark(ctx, "started")
    time.sleep(0.4)
    (ctx.out / "v.txt").write_text(item)

class Demo(Pipeline):
    Config = Config

    @stage(foreach=ITEM, workers=2, backend="thread", resources=[SHARED, WORKER, PER_UNIT])
    def threaded(self, ctx: Ctx[Config], item: str, shared, worker, per_unit) -> None:
        body(ctx, item)

    @stage(foreach=ITEM, workers=2, backend="process", resources=[SHARED, WORKER, PER_UNIT])
    def pooled(self, ctx: Ctx[Config], item: str, shared, worker, per_unit) -> None:
        body(ctx, item)

if __name__ == "__main__":
    raise SystemExit(Demo.cli())
"""


@pytest.mark.parametrize("stage", ["threaded", "pooled"])
def test_first_sigint_closes_every_session_and_commits_active_units(ws, stage: str) -> None:
    import os
    import signal
    import subprocess
    import sys
    import time

    from varve.store import layout
    from varve.store.db import connect
    from varve.store.repo import Repo

    trace = ws.base / "trace"
    trace.mkdir()
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"trace": str(trace)}}}))
    ws.write(INTERRUPTED)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.Popen(
        [sys.executable, str(ws.path), "run", "--no-tui", "--only", stage, "--root", str(ws.root)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20
    while _counts(trace).get("started", 0) < 2:
        assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
        time.sleep(0.02)
    proc.send_signal(signal.SIGINT)
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 130, stderr
    counts = _counts(trace)
    started = counts["started"]
    assert 2 <= started < 6
    assert counts["open.stage"] == counts["close.stage"] == 1
    assert counts["open.unit"] == counts["close.unit"] == started
    assert counts["open.worker"] == counts["close.worker"] >= 1
    repo = Repo(connect(layout.state_db_path(ws.root), readonly=True))
    rows = repo.units("main", stage)
    assert len(rows) == started and {row.status for row in rows.values()} == {"ok"}
    repo.conn.close()
