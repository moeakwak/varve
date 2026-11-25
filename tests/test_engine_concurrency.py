"""Backends, sliding windows, exclusive, max_failures, --failed-only, logs, bookkeeping."""

from __future__ import annotations

import json
import sqlite3

from varve.engine import RunOptions
from varve.store import layout
from varve.store.db import connect
from varve.store.repo import Repo

SOURCE = """
import asyncio
import json
import os
import time
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

class Config(BaseModel):
    flag: str = "on"
    other: int = 0

class Args(BaseModel):
    workers: int = 3
    fail: list[str] = []

ITEM = Axis("item", tuple(f"i{n}" for n in range(6)))
GROUP = Axis("group", ("heavy", "light"))

def stamp(ctx, start):
    (ctx.out / "t.json").write_text(json.dumps([start, time.time(), os.getpid()]))

class Demo(Pipeline):
    Config = Config
    Args = Args

    @stage(foreach=ITEM, workers=lambda ctx: ctx.args.workers)
    async def aio(self, ctx: Ctx[Config, Args], item: str) -> None:
        start = time.time()
        await asyncio.sleep(0.05)
        print(f"aio says {item}")
        ctx.metric(items=2)
        stamp(ctx, start)

    @stage(foreach=ITEM, workers=3)
    def proc(self, ctx: Ctx[Config, Args], item: str) -> None:
        start = time.time()
        time.sleep(0.1)
        print(f"proc says {item}", ctx.config.flag)
        for _ in ctx.progress(range(3), desc="inner"):
            pass
        ctx.metric(items=1)
        stamp(ctx, start)

    @stage(foreach=(GROUP, ITEM), workers=4, backend="thread", exclusive="group=heavy")
    def excl(self, ctx: Ctx[Config, Args], group: str, item: str) -> None:
        start = time.time()
        time.sleep(0.02)
        stamp(ctx, start)

    @stage(foreach=ITEM, max_failures=2)
    def flaky(self, ctx: Ctx[Config, Args], item: str) -> None:
        if item in ctx.args.fail:
            raise RuntimeError(f"boom {item}")
        stamp(ctx, time.time())
"""


def _intervals(ws, stage: str) -> dict[str, tuple[float, float, int]]:
    base = ws.root / "out/.internal/main" / stage
    return {
        str(path.parent.relative_to(base)): tuple(json.loads(path.read_text()))
        for path in base.rglob("t.json")
    }


def _overlap(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _repo(ws) -> Repo:
    return Repo(connect(layout.state_db_path(ws.root), readonly=True))


def test_async_backend_runs_concurrently_and_captures_logs(ws) -> None:
    Demo = ws.write(SOURCE)
    result = ws.run(Demo, options=RunOptions(only="aio", tui=False))
    assert result.status == "ok"
    spans = list(_intervals(ws, "aio").values())
    assert any(_overlap(a, b) for i, a in enumerate(spans) for b in spans[i + 1 :])
    log = layout.unit_log_path(ws.root, result.run_id, "aio", "item=i3")
    assert log.read_text() == "aio says i3\n"
    repo = _repo(ws)
    [stage_run] = repo.stage_runs("main", stage="aio")
    assert stage_run.metrics_summary == {"items": 12}
    assert stage_run.workers == 3 and stage_run.executed_units == 6
    assert stage_run.unit_p50_ms is not None and stage_run.unit_max_ms is not None
    assert stage_run.unit_max_ms >= stage_run.unit_p50_ms
    run = repo.get_run(result.run_id)
    assert run is not None and run.status == "ok" and run.total_wall_ms is not None
    assert {row.last_run_id for row in repo.units("main", "aio").values()} == {result.run_id}
    repo.conn.close()


def test_process_backend_parallel_with_config_access_and_logs(ws) -> None:
    Demo = ws.write(SOURCE)
    events = []
    result = ws.run(Demo, options=RunOptions(only="proc", tui=False), on_event=events.append)
    assert result.status == "ok", result.stages
    spans = _intervals(ws, "proc")
    assert len({pid for _, _, pid in spans.values()}) > 1
    values = list(spans.values())
    assert any(_overlap(a, b) for i, a in enumerate(values) for b in values[i + 1 :])
    log = layout.unit_log_path(ws.root, result.run_id, "proc", "item=i0")
    assert log.read_text() == "proc says i0 on\n"
    repo = _repo(ws)
    rows = repo.units("main", "proc")
    assert {tuple(row.config_access or ["<all>"]) for row in rows.values()} == {("flag",)}
    assert all(row.peak_rss_mb for row in rows.values())
    repo.conn.close()
    assert any(type(event).__name__ == "UnitProgress" for event in events)
    # a field the children never read does not invalidate them
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"other": 3}}}))
    assert ws.run(Demo, options=RunOptions(only="proc", tui=False)).stages["proc"].executed == 0


def test_exclusive_units_never_overlap(ws) -> None:
    Demo = ws.write(SOURCE)
    assert ws.run(Demo, options=RunOptions(only="excl", tui=False)).status == "ok"
    spans = _intervals(ws, "excl")
    heavy = [span for key, span in spans.items() if key.startswith("group=heavy")]
    light = [span for key, span in spans.items() if key.startswith("group=light")]
    assert len(heavy) == 6 and len(light) == 6
    for span in heavy:
        others = [other for other in heavy + light if other is not span]
        assert not any(_overlap(span, other) for other in others)
    assert any(_overlap(a, b) for i, a in enumerate(light) for b in light[i + 1 :])


def test_max_failures_then_failed_only(ws, capsys) -> None:
    Demo = ws.write(SOURCE)
    first = ws.run(
        Demo, options=RunOptions(only="flaky", tui=False), args={"fail": ["i0", "i1", "i2"]}
    )
    outcome = first.stages["flaky"]
    assert first.status == "failed" and first.exit_code == 1
    assert (outcome.executed, outcome.failed) == (2, 2)
    assert "max_failures=2" in (outcome.detail or "")
    repo = _repo(ws)
    failed = repo.get_unit("main", "flaky", "item=i0")
    assert failed is not None and failed.status == "failed"
    assert failed.error_type == "RuntimeError" and failed.error_summary == "boom i0"
    assert "RuntimeError: boom i0" in (failed.error_traceback or "")
    repo.conn.close()

    second = ws.run(Demo, options=RunOptions(only="flaky", failed_only=True, tui=False))
    outcome = second.stages["flaky"]
    assert (outcome.executed, outcome.failed, outcome.status) == (2, 0, "partial")
    assert second.status == "ok"
    third = ws.run(Demo, options=RunOptions(only="flaky", tui=False))
    assert third.stages["flaky"].executed == 4
    assert ws.cli(Demo, "inspect", "flaky@item=i0") == 0
    assert "flaky@item=i0" in capsys.readouterr().out


def test_failed_unit_status_and_cli_exit_code(ws, capsys) -> None:
    Demo = ws.write(SOURCE)
    code = ws.cli(Demo, "run", "--only", "flaky", "--no-tui", "--fail", '["i4"]')
    assert code == 1
    assert ws.cli(Demo, "status", "flaky", "--fail", '["i4"]') == 0
    out = capsys.readouterr().out
    assert "1 failed" in out and "boom i4" in out
    conn = sqlite3.connect(layout.state_db_path(ws.root))
    statuses = dict(conn.execute("SELECT stage_name, status FROM stage_runs").fetchall())
    conn.close()
    assert statuses == {"flaky": "failed"}
