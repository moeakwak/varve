"""Branch resolution, the generated CLI commands, and invocation styles."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from varve.branch_config import BranchRequest
from varve.engine import RunOptions, probe_pipeline
from varve.engine.probe import probe
from varve.engine.session import open_session
from varve.store import layout

SOURCE = """
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

class Config(BaseModel):
    count: int = 4
    label: str = "x"

class Args(BaseModel):
    verbose_output: bool = False

DATASET = Axis("dataset", ("a", "b", "c"))
SHARD = Axis("shard", resolver=lambda ctx: list(range(ctx.config.count)))

class Demo(Pipeline):
    \"\"\"Demo pipeline for CLI tests.\"\"\"

    Config = Config
    Args = Args

    @stage(foreach=DATASET, produces="d.txt")
    def per_dataset(self, ctx: Ctx[Config, Args], dataset: str) -> None:
        (ctx.out / "d.txt").write_text(dataset + ctx.config.label)

    @stage(foreach=SHARD, produces="s.txt")
    def shards(self, ctx: Ctx[Config, Args], shard: int) -> None:
        if ctx.args.verbose_output:
            print("shard", shard)
        (ctx.out / "s.txt").write_text(str(shard))

    @stage(produces="all.txt", publish=True)
    def merge(self, ctx: Ctx[Config, Args], per_dataset: dict, shards: dict) -> None:
        (ctx.out / "all.txt").write_text(f"{len(per_dataset)}:{len(shards)}")

if __name__ == "__main__":
    raise SystemExit(Demo.cli())
"""


def _branches(ws) -> dict:
    repo = ws.repo()
    try:
        return {row.branch_id: row for row in repo.list_branches()}
    finally:
        repo.conn.close()


def test_slice_and_sample_need_a_temporary_branch(ws, capsys) -> None:
    Demo = ws.write(SOURCE)
    assert ws.cli(Demo, "run", "--no-tui", "--slice", "dataset=a") == 1
    assert "add --temp or pass a new --branch" in capsys.readouterr().err
    assert ws.cli(Demo, "run", "--no-tui", "--slice", "dataset=a,c", "--temp") == 0
    branches = _branches(ws)
    [temp] = [row for row in branches.values() if row.is_temp]
    assert temp.branch_id.startswith("main_tmp_")
    assert temp.active_axes == {"dataset": ["a", "c"]}
    internal = ws.root / "out/.internal" / temp.branch_id / "per_dataset"
    assert sorted(p.name for p in internal.iterdir()) == ["dataset=a", "dataset=c"]
    assert (ws.root / "out/.tmp" / temp.branch_id / "all.txt").read_text() == "2:4"

    assert (
        ws.cli(Demo, "run", "--no-tui", "--sample", "shard=2", "--seed", "3", "--branch", "pilot")
        == 0
    )
    pilot = _branches(ws)["pilot"]
    assert pilot.is_temp and pilot.active_axes == {"shard": {"sample": 2, "seed": 3}}
    assert len(list((ws.root / "out/.internal/pilot/shards").iterdir())) == 2
    # a stored temporary branch is addressable by name alone
    view = probe_pipeline(Demo, root=ws.root, branch="pilot")
    assert view.stage("shards").total == 2 and view.stage("shards").status == "hit"
    # ... and reusing its name with another snapshot is refused
    assert ws.cli(Demo, "run", "--no-tui", "--sample", "shard=3", "--branch", "pilot") == 1
    assert "different active axes" in capsys.readouterr().err


def test_override_creates_a_stable_temporary_branch(ws) -> None:
    Demo = ws.write(SOURCE)
    first = ws.run(Demo, overrides='{"label": "y"}')
    second = ws.run(Demo, overrides={"label": "y"})
    assert first.branch == second.branch and first.branch.startswith("main_tmp_")
    assert second.stages["per_dataset"].executed == 0
    assert _branches(ws)[first.branch].config["label"] == "y"
    with pytest.raises(ValueError, match="Unknown branch"):
        probe_pipeline(Demo, root=ws.root, branch="nope")


def test_dynamic_slice_and_unknown_ids(ws, capsys) -> None:
    Demo = ws.write(SOURCE)
    assert (
        ws.cli(Demo, "run", "--no-tui", "--branch", "s", "--slice", "shard=1,3", "--only", "shards")
        == 0
    )
    assert sorted(p.name for p in (ws.root / "out/.internal/s/shards").iterdir()) == [
        "shard=1",
        "shard=3",
    ]
    ids = ws.base / "ids.txt"
    ids.write_text("# pilot\n0\n9\n")
    assert ws.cli(Demo, "run", "--no-tui", "--branch", "t", "--slice", f"shard=@{ids}") == 1
    assert "not produced by the axis resolver" in capsys.readouterr().err


def test_status_plan_ls_inspect_outputs(ws, capsys) -> None:
    Demo = ws.write(SOURCE)
    assert ws.cli(Demo, "run", "--no-tui", "--verbose-output") == 0
    capsys.readouterr()
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"label": "z"}}}))

    assert ws.cli(Demo, "status") == 0
    out = capsys.readouterr().out
    assert "per_dataset" in out and "0/3 hit" in out and "config-changed 3" in out
    assert "shards" in out and "hit (4)" in out

    assert ws.cli(Demo, "status", "per_dataset@dataset=b") == 0
    out = capsys.readouterr().out
    assert "dataset=b" in out and "dataset=a" not in out.split("per_dataset", 2)[-1]

    assert ws.cli(Demo, "plan") == 0
    out = capsys.readouterr().out
    assert "per_dataset" in out and "merge" in out and "0/3" in out

    assert ws.cli(Demo, "ls") == 0
    out = capsys.readouterr().out
    assert "per_dataset" in out and "shard*" in out and "main" in out and "persistent" in out

    assert ws.cli(Demo, "inspect", "per_dataset@dataset=a", "--sources", "--log") == 0
    out = capsys.readouterr().out
    assert "config-changed" in out and "label" in out
    assert "Demo.per_dataset" in out and "No captured log" in out

    assert ws.cli(Demo, "inspect", "per_dataset") == 0
    assert "3 units" in capsys.readouterr().out


def test_clean_branch_storage_and_whole(ws, capsys, monkeypatch) -> None:
    Demo = ws.write(SOURCE)
    ws.run(Demo)
    ws.run(Demo, branch="tmpb", temp=True)
    assert ws.cli(Demo, "clean", "--branch", "tmpb", "--yes") == 0
    assert "tmpb" not in _branches(ws)
    assert not (ws.root / "out/.internal/tmpb").exists()
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert ws.cli(Demo, "clean", "--storage", "default") == 1
    assert (ws.root / "out/.internal/main").exists()
    assert ws.cli(Demo, "clean", "--storage", "default", "--yes") == 0
    assert not (ws.root / "out/.internal").exists()
    view = probe_pipeline(Demo, root=ws.root)
    assert {stage.status for stage in view.stages} == {"needs-run"}
    assert ws.cli(Demo, "clean", "--yes") == 0
    assert not layout.state_db_path(ws.root).exists()
    assert not (ws.root / "out/main").exists()
    capsys.readouterr()


def test_cli_runs_as_script_and_as_module(ws) -> None:
    ws.write(SOURCE)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    script = subprocess.run(
        [sys.executable, str(ws.path), "run", "--no-tui", "--root", str(ws.root)],
        env=env,
        capture_output=True,
        text=True,
        cwd=ws.base,
        timeout=60,
    )
    assert script.returncode == 0, script.stderr
    module = subprocess.run(
        [sys.executable, "-m", ws.module, "--root", str(ws.root), "status"],
        env=env,
        capture_output=True,
        text=True,
        cwd=ws.base,
        timeout=60,
    )
    assert module.returncode == 0, module.stderr
    assert "hit (4)" in module.stdout
    help_text = subprocess.run(
        [sys.executable, "-m", ws.module, "--help"],
        env=env,
        capture_output=True,
        text=True,
        cwd=ws.base,
        timeout=60,
    )
    assert f"python -m {ws.module}" in help_text.stdout


def test_process_backend_from_a_script_run(ws) -> None:
    source = SOURCE.replace(
        '@stage(foreach=SHARD, produces="s.txt")',
        '@stage(foreach=SHARD, produces="s.txt", workers=2)',
    )
    ws.write(source)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p != str(ws.src))}
    run = subprocess.run(
        [sys.executable, str(ws.path), "run", "--no-tui", "--root", str(ws.root), "-v"],
        env=env,
        capture_output=True,
        text=True,
        cwd=ws.base,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert "backend=process" in run.stderr


@pytest.mark.slow
def test_probe_5000_units_under_200ms(ws) -> None:
    source = """
    from pathlib import Path
    from varve import Axis, Ctx, Pipeline, stage

    SHARD = Axis("shard", resolver=lambda ctx: list(range(5000)))

    class Demo(Pipeline):
        @stage(foreach=SHARD, produces="part.txt")
        def work(self, ctx: Ctx, shard: int) -> None:
            (ctx.out / "part.txt").write_text(str(shard))
    """
    Demo = ws.write(source)
    assert ws.run(Demo, options=RunOptions(tui=False)).status == "ok"
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        session = open_session(Demo, BranchRequest(), root=ws.root)
        try:
            plans = probe(session)
        finally:
            session.close()
        timings.append(time.perf_counter() - started)
        assert plans["work"].counts() == {"hit": 5000, "needs-run": 0, "failed": 0}
    print(f"5000-unit probe: best {min(timings) * 1000:.1f} ms")
    # The 200 ms target holds on a developer machine; shared CI runners are a few times
    # slower and noisy, so CI only guards against superlinear regressions.
    budget = 1.0 if os.environ.get("CI") else 0.2
    assert min(timings) < budget


def test_varve_yaml_axes_must_name_known_axes_and_ids(ws, capsys) -> None:
    Demo = ws.write(SOURCE)
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"axes": {"datasets": ["a"]}}}))
    assert ws.cli(Demo, "status") == 1
    assert "axes: names unknown axis 'datasets'" in capsys.readouterr().err
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"axes": {"dataset": ["a", "z"]}}}))
    assert ws.cli(Demo, "run", "--no-tui") == 1
    assert "ids ['z'] are not values of axis 'dataset'" in capsys.readouterr().err
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"axes": {"dataset": ["a", "c"]}}}))
    assert ws.cli(Demo, "run", "--no-tui") == 0


@pytest.mark.parametrize("command", [["status", "--expand"], ["plan"], ["ls"], ["run", "--no-tui"]])
def test_closed_stdout_pipe_is_not_an_error(ws, command: list[str]) -> None:
    ws.write(SOURCE)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.Popen(
        [sys.executable, str(ws.path), "--root", str(ws.root), *command],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ws.base,
    )
    assert proc.stdout is not None
    proc.stdout.close()  # the reader is gone before anything is written (`| head -0`)
    _, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 0, stderr.decode()
    assert b"Traceback" not in stderr and b"BrokenPipe" not in stderr
    if command[0] == "run":
        repo = ws.repo()
        [run] = repo.runs("main")
        assert run.status == "ok"
        repo.conn.close()


MAIN_SOURCE = """
import enum
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"

class Config(BaseModel):
    scale: int = 3
    tint: Color = Color.RED
    unused: int = 0

class Args(BaseModel):
    suffix: str = "!"

COLOR = Axis("color", [Color.RED, Color.BLUE])

class Demo(Pipeline):
    Config = Config
    Args = Args

    @stage(foreach=COLOR, workers=2, produces="x.txt")
    def work(self, ctx: Ctx[Config, Args], color: Color) -> None:
        assert isinstance(ctx.config, Config) and isinstance(color, Color)
        text = f"{color.value}:{ctx.config.scale}:{ctx.config.tint.value}{ctx.args.suffix}"
        (ctx.out / "x.txt").write_text(text)

if __name__ == "__main__":
    raise SystemExit(Demo.cli())
"""


@pytest.mark.parametrize("style", ["package", "script"])
def test_process_backend_with_classes_in_main(ws, style: str) -> None:
    # spawn does not re-run a package's __main__ in workers, so Config/Args/Enum
    # instances pickled as `__main__.X` must still resolve there.
    package = ws.base / "pkgs" / f"{ws.module}_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(MAIN_SOURCE)
    target = ["-m", package.name] if style == "package" else [str(package / "__main__.py")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p != str(ws.src))}
    run = subprocess.run(
        [sys.executable, *target, "run", "--no-tui", "--root", str(ws.root), "-v"],
        env=env,
        capture_output=True,
        text=True,
        cwd=package.parent,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert "backend=process" in run.stderr
    final = ws.root / "out/.internal/main/work"
    assert (final / "color=red/x.txt").read_text() == "red:3:red!"
    assert (final / "color=blue/x.txt").read_text() == "blue:3:red!"
    repo = ws.repo()
    rows = repo.units("main", "work")
    assert {tuple(row.config_access or ["<all>"]) for row in rows.values()} == {("scale", "tint")}
    repo.conn.close()


MANY = """
from varve import Axis, Ctx, Pipeline, stage

STATIC = Axis("static", [str(i) for i in range(3000)])
DYNAMIC = Axis("dynamic", resolver=lambda ctx: [str(i) for i in range(3000)])

class Demo(Pipeline):
    @stage(foreach=STATIC)
    def wide(self, ctx: Ctx, static: str) -> None:
        pass

    @stage(foreach=DYNAMIC)
    def deep(self, ctx: Ctx, dynamic: str) -> None:
        pass
"""


def test_large_axis_restrictions(ws) -> None:
    ids = [str(i) for i in range(0, 3000, 2)]
    branch = {"axes": {"static": ids, "dynamic": ids}}
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {}, "half": branch}))
    Demo = ws.write(MANY)
    view = probe_pipeline(Demo, root=ws.root, branch="half", expand=True)
    assert [len(view.stage(name).units) for name in ("wide", "deep")] == [1500, 1500]
    assert view.stage("deep").units[-1].unit_key == "dynamic=2998"


def test_clean_storage_of_an_unknown_branch_deletes_no_rows(ws, capsys) -> None:
    from varve.engine import clean_pipeline

    Demo = ws.write(SOURCE)
    ws.run(Demo)
    ws.run(Demo, branch="tmpb", temp=True)
    plan = clean_pipeline(Demo, root=ws.root, storage="default", branch="nosuch")
    assert plan.rows == 0
    repo = ws.repo()
    try:
        assert repo.unit_counts("main") and repo.unit_counts("tmpb")
    finally:
        repo.conn.close()
    assert (ws.root / "out/.internal/main").exists()
    capsys.readouterr()


def test_slice_plus_sample_branch_restores_its_slice_from_the_row(ws, capsys) -> None:
    Demo = ws.write(SOURCE)
    argv = ("--slice", "dataset=a,b", "--sample", "dataset=1", "--seed", "3", "--branch", "pick")
    assert ws.cli(Demo, "run", "--no-tui", *argv) == 0
    pick = _branches(ws)["pick"]
    assert pick.active_axes == {"dataset": {"ids": ["a", "b"], "sample": 1, "seed": 3}}
    ran = sorted(p.name for p in (ws.root / "out/.internal/pick/per_dataset").iterdir())
    assert len(ran) == 1 and ran[0] in {"dataset=a", "dataset=b"}
    # Addressed by name alone, the branch samples from its slice, not the full domain.
    view = probe_pipeline(Demo, root=ws.root, branch="pick", expand=True)
    stage = view.stage("per_dataset")
    assert stage.status == "hit" and [unit.unit_key for unit in stage.units] == ran
    # Rerunning the same command matches the stored snapshot.
    assert ws.cli(Demo, "run", "--no-tui", *argv) == 0
    capsys.readouterr()
