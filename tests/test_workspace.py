"""Cross-pipeline Refs, varve.toml workspaces, discovery, and the top-level CLI."""

from __future__ import annotations

import importlib
import itertools
import os
import sqlite3
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from varve.engine import RefResolutionError, RunOptions, probe_pipeline, run_pipeline
from varve.engine.refs import bound_refs
from varve.pipeline import Pipeline
from varve.store import layout
from varve.store.db import connect
from varve.store.repo import Repo
from varve.workspace import cli
from varve.workspace.config import ROOT_BASE_ENV, WorkspaceConfigError, load_workspace
from varve.workspace.discovery import discover
from varve.workspace.macro import MacroError, Workspace, build_plan
from varve.workspace.refs import WorkspaceRefResolver

_counter = itertools.count()

UP = """
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

class Config(BaseModel):
    tag: str = "v1"

DATASET = Axis("dataset", ("a", "b", "c"))

class Up(Pipeline):
    Config = Config

    @stage(foreach=DATASET, produces="data.txt")
    def export(self, ctx: Ctx[Config], dataset: str) -> None:
        (ctx.out / "data.txt").write_text(f"{dataset}:{ctx.config.tag}")

    @stage(produces="all.txt")
    def combined(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "all.txt").write_text(f"all:{ctx.config.tag}")
"""

UP_YAML = """
main: {}
alt:
  config: {tag: alt}
heavy:
  manual: true
  config: {tag: heavy}
"""


def down_source(
    prefix: str, ref: str, axis: str = 'Axis("dataset", ("a", "b"))', **extra: Any
) -> str:
    foreach = "foreach=AXIS, " if axis else ""
    param = ", " + axis.split('"')[1] + ": str" if axis else ""
    return f"""
import json
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, Ref, stage

class Args(BaseModel):
    scale: int = 1

AXIS = {axis or "None"}

def show(value):
    if isinstance(value, dict):
        return {{str(k): show(v) for k, v in value.items()}}
    return Path(value).read_text()

class Down(Pipeline):
    Args = Args
    src = {ref}

    @stage({foreach}produces="out.json")
    def use(self, ctx: Ctx{param}, src) -> None:
        (ctx.out / "out.json").write_text(json.dumps(show(src)))
"""


class Space:
    """A temp workspace: packages ``<prefix>.<name>`` with ``run.py`` + ``__main__.py``."""

    def __init__(self, base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.base = base
        self.prefix = f"vw{os.getpid()}_{next(_counter)}"
        self.pkg = base / self.prefix
        self.pkg.mkdir()
        (self.pkg / "__init__.py").write_text("")
        self._tick = 1_700_000_000
        sys.path.insert(0, str(base))
        monkeypatch.chdir(base)
        monkeypatch.delenv(ROOT_BASE_ENV, raising=False)
        self.monkeypatch = monkeypatch

    def module(self, name: str) -> str:
        return f"{self.prefix}.{name}"

    def add(self, name: str, source: str, cls: str, yaml: str | None = None) -> None:
        directory = self.pkg / name
        directory.mkdir(exist_ok=True)
        (directory / "__init__.py").write_text("")
        (directory / "__main__.py").write_text(
            f"from .run import {cls}\n\nif __name__ == '__main__':\n    raise SystemExit({cls}.cli())\n"
        )
        path = directory / "run.py"
        path.write_text(textwrap.dedent(source))
        self._tick += 10
        os.utime(path, (self._tick, self._tick))
        if yaml is not None:
            (directory / "varve.yaml").write_text(textwrap.dedent(yaml))
        sys.modules.pop(f"{self.module(name)}.__main__", None)
        module = f"{self.module(name)}.run"
        if module in sys.modules:
            importlib.reload(sys.modules[module])

    def load(self, name: str, cls: str) -> type[Pipeline]:
        return getattr(importlib.import_module(f"{self.module(name)}.run"), cls)

    def toml(self, *names: str, extra: str = "") -> None:
        entries = ", ".join(f'"{self.module(name)}"' for name in names)
        (self.base / "varve.toml").write_text(f"[workspace]\npipelines = [{entries}]\n{extra}")

    def run(self, pipeline: type[Pipeline], **kwargs: Any):
        kwargs.setdefault("options", RunOptions(tui=False))
        return run_pipeline(pipeline, **kwargs)

    def close(self) -> None:
        for name in list(sys.modules):
            if name == self.prefix or name.startswith(self.prefix + "."):
                del sys.modules[name]
        if str(self.base) in sys.path:
            sys.path.remove(str(self.base))


@pytest.fixture
def space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Space]:
    workspace = Space(tmp_path, monkeypatch)
    try:
        yield workspace
    finally:
        workspace.close()


def outputs(pipeline: type[Pipeline], root: Path, stage: str = "use") -> dict[str, Any]:
    import json

    base = root / "out/.internal/main" / stage
    return {
        str(path.parent.relative_to(base)): json.loads(path.read_text())
        for path in sorted(base.rglob("out.json"))
    }


def setup_pair(space: Space, ref: str, axis: str = 'Axis("dataset", ("a", "b"))'):
    space.add("up", UP, "Up", UP_YAML)
    space.add("down", down_source(space.prefix, ref, axis), "Down")
    space.toml("up", "down")
    return space.load("up", "Up"), space.load("down", "Down")


# -- Ref semantics ---------------------------------------------------------------


def test_ref_aligns_one_to_one_by_axis_name_on_a_superset(space: Space) -> None:
    up, down = setup_pair(space, f'Ref("{space.module("up")}", stage="export")')
    assert space.run(up).status == "ok"
    assert space.run(down).status == "ok"
    root = space.pkg / "down"
    assert outputs(down, root) == {"dataset=a": "a:v1", "dataset=b": "b:v1"}
    view = probe_pipeline(down)
    assert view.stage("use").status == "hit"
    (info,) = view.refs
    assert (info.attr, info.target, info.stage, info.branch) == (
        "src",
        space.module("up"),
        "export",
        "main",
    )
    assert info.units == 3 and info.last_commit_at is not None


def test_ref_key_follows_upstream_bytes(space: Space) -> None:
    up, down = setup_pair(space, f'Ref("{space.module("up")}", stage="export")')
    space.run(up)
    space.run(down)
    # Same bytes after a forced rerun: downstream stays hit.
    assert space.run(up, options=RunOptions(tui=False, force=True)).status == "ok"
    assert probe_pipeline(down).stage("use").status == "hit"
    # Different bytes for one dataset: only that unit needs a run.
    space.add(
        "up", UP.replace('f"{dataset}:{ctx.config.tag}"', 'f"{dataset}!{ctx.config.tag}"'), "Up"
    )
    up = space.load("up", "Up")
    assert space.run(up).status == "ok"
    stage = probe_pipeline(down, expand=True).stage("use")
    assert {unit.reason for unit in stage.units} == {"upstream-changed"}
    assert space.run(down).status == "ok"
    assert outputs(down, space.pkg / "down") == {"dataset=a": "a!v1", "dataset=b": "b!v1"}


def test_ref_missing_coordinate_is_a_probe_error(space: Space) -> None:
    up, down = setup_pair(
        space, f'Ref("{space.module("up")}", stage="export")', 'Axis("dataset", ("a", "z"))'
    )
    space.run(up)
    with pytest.raises(RefResolutionError, match=r"no committed \(ok\) unit for dataset=z"):
        probe_pipeline(down)


def test_ref_missing_upstream_prints_the_upstream_command(space: Space) -> None:
    _, down = setup_pair(space, f'Ref("{space.module("up")}", stage="export", branch="alt")')
    with pytest.raises(RefResolutionError) as error:
        probe_pipeline(down)
    assert f"python -m {space.module('up')} run --branch alt" in str(error.value)
    result = space.run(down)
    assert result.status == "failed"
    assert "python -m" in (result.stages["use"].detail or "")


def test_ref_fan_in_with_empty_align_by(space: Space) -> None:
    up, down = setup_pair(space, f'Ref("{space.module("up")}", stage="export", align_by=())')
    space.run(up)
    space.run(down)
    expected = {"a": "a:v1", "b": "b:v1", "c": "c:v1"}
    assert outputs(down, space.pkg / "down") == {"dataset=a": expected, "dataset=b": expected}


def test_ref_renamed_axis_and_selector_filter(space: Space) -> None:
    up, down = setup_pair(
        space,
        f'Ref("{space.module("up")}", stage="export", align_by={{"ds": "dataset"}})',
        'Axis("ds", ("c",))',
    )
    space.run(up)
    space.run(down)
    assert outputs(down, space.pkg / "down") == {"ds=c": "c:v1"}

    space.add(
        "down",
        down_source(
            space.prefix, f'Ref("{space.module("up")}", stage="export", selector="dataset=b")', ""
        ),
        "Down",
    )
    down = space.load("down", "Down")
    space.run(down)
    assert outputs(down, space.pkg / "down") == {".": {"b": "b:v1"}}


def test_multi_branch_ref_is_an_outer_dict(space: Space) -> None:
    up, down = setup_pair(
        space,
        f'Ref("{space.module("up")}", stage="combined", branch=("main", "alt"))',
        "",
    )
    space.run(up)
    space.run(up, branch="alt")
    space.run(down)
    assert outputs(down, space.pkg / "down") == {".": {"main": "all:v1", "alt": "all:alt"}}


def test_class_target_ref(space: Space) -> None:
    space.add("up", UP, "Up", UP_YAML)
    space.add(
        "down",
        down_source(space.prefix, 'Ref(Up, stage="export")').replace(
            "class Down", f"from {space.module('up')}.run import Up\n\nclass Down"
        ),
        "Down",
    )
    up, down = space.load("up", "Up"), space.load("down", "Down")
    space.run(up)
    assert space.run(down).status == "ok"
    assert outputs(down, space.pkg / "down")["dataset=b"] == "b:v1"


def test_ref_resolve_reads_committed_artifacts_outside_a_pipeline(
    space: Space, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from varve import Ref

    up, _ = setup_pair(space, f'Ref("{space.module("up")}", stage="export")')
    name = space.module("up")
    with pytest.raises(RefResolutionError, match="run it first"):
        Ref(name, stage="export").resolve()
    space.run(up)
    space.run(up, branch="alt")

    export = Ref(name, stage="export").resolve()
    assert {key: path.read_text() for key, path in export.items()} == {
        "a": "a:v1",
        "b": "b:v1",
        "c": "c:v1",
    }
    combined = Ref(name, stage="combined").resolve()
    assert isinstance(combined, Path) and combined.read_text() == "all:v1"
    assert list(Ref(name, stage="export", selector="dataset=b").resolve()) == ["b"]
    both = Ref(up, stage="combined", branch=("main", "alt")).resolve()
    assert {branch: path.read_text() for branch, path in both.items()} == {
        "main": "all:v1",
        "alt": "all:alt",
    }

    # VARVE_ROOT_BASE relocates the upstream for class and string targets alike.
    base = tmp_path / "scratch"
    monkeypatch.setenv(ROOT_BASE_ENV, str(base))
    with pytest.raises(RefResolutionError, match="no store"):
        Ref(up, stage="combined").resolve()
    space.run(up)
    assert Ref(up, stage="combined").resolve().is_relative_to(base)
    assert Ref(name, stage="combined").resolve().is_relative_to(base)


def test_root_base_relocates_whole_chains(
    space: Space, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    setup_pair(space, f'Ref("{space.module("up")}", stage="export")')
    base = tmp_path / "scratch"
    monkeypatch.setenv(ROOT_BASE_ENV, str(base))
    assert cli.main(["run", "--no-tui"]) == 0
    for name in ("up", "down"):
        assert (base / space.module(name) / ".varve/state.db").is_file()
        assert not (space.pkg / name / ".varve").exists()
        assert not (space.pkg / name / "out").exists()
    down_root = base / space.module("down")
    assert outputs(space.load("down", "Down"), down_root) == {
        "dataset=a": "a:v1",
        "dataset=b": "b:v1",
    }
    capsys.readouterr()
    assert cli.main(["ls"]) == 0
    listing = capsys.readouterr().out
    assert "VARVE_ROOT_BASE" in listing and "uninitialized" not in listing
    # The generated CLI honours the same relocation (and reads the Ref there).
    assert space.load("down", "Down").cli(["status"]) == 0
    assert "hit (2)" in capsys.readouterr().out
    (info,) = probe_pipeline(space.load("down", "Down")).refs
    assert info.root == base / space.module("up")


def test_root_base_moves_named_storages_below_the_root(
    space: Space, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    space.add(
        "up",
        UP.replace(
            "class Up(Pipeline):", "class Up(Pipeline):\n    defaults = dict(storage='bulk')"
        )
        + "\n    @classmethod\n    def storage_roots(cls, config):\n"
        f"        return {{'bulk': {str(tmp_path / 'real-bulk')!r}}}\n",
        "Up",
    )
    space.toml("up")
    base = tmp_path / "scratch"
    monkeypatch.setenv(ROOT_BASE_ENV, str(base))
    assert space.run(space.load("up", "Up")).status == "ok"
    assert not (tmp_path / "real-bulk").exists()
    assert (base / space.module("up") / "storages/bulk/.internal/main/export").is_dir()


# -- varve.toml and discovery ----------------------------------------------------


def test_varve_toml_parsing(space: Space, tmp_path: Path) -> None:
    space.toml(
        "up",
        extra='[storage]\ninterim = "data/interim"\n[defaults]\nworker_env = { OMP_NUM_THREADS = "1" }\n',
    )
    nested = tmp_path / "a/b"
    nested.mkdir(parents=True)
    settings = load_workspace([nested])
    assert settings.pipelines == (space.module("up"),)
    assert settings.storage == {"interim": tmp_path / "data/interim"}
    assert settings.worker_env == {"OMP_NUM_THREADS": "1"}
    assert settings.workspace_root == tmp_path
    (tmp_path / "varve.toml").write_text("[workspace]\npipelinez = []\n")
    with pytest.raises(WorkspaceConfigError, match="pipelinez"):
        load_workspace([tmp_path])


def test_ls_is_zero_import(space: Space, capsys) -> None:
    space.add("up", UP, "Up", UP_YAML)
    space.add("boom", "raise RuntimeError('imported!')\n", "Boom")
    space.toml("up", "boom")
    space.run(space.load("up", "Up"))
    before = set(sys.modules)
    assert cli.main(["ls"]) == 0
    assert not {name for name in set(sys.modules) - before if name.startswith(space.prefix)}
    assert f"{space.module('boom')}.run" not in sys.modules
    out = capsys.readouterr().out
    assert space.module("boom") in out and "uninitialized" in out
    assert space.module("up") in out and "alt" not in out  # only main has been run
    entries = discover(load_workspace())
    assert [entry.name for entry in entries] == [space.module("up"), space.module("boom")]


def test_ui_subcommand_serves_workspace_dbs(space: Space, monkeypatch: pytest.MonkeyPatch) -> None:
    import varve.ui

    space.add("up", UP, "Up", UP_YAML)
    space.toml("up")
    space.run(space.load("up", "Up"))
    calls: list[dict[str, Any]] = []

    def fake_serve(source, *, host, port):
        calls.append({"paths": list(source()), "host": host, "port": port})

    # Patch the module dict so the lazy ``__getattr__`` (which imports Starlette) never runs.
    monkeypatch.setitem(vars(varve.ui), "serve", fake_serve)
    assert cli.main(["ui", "--port", "9999"]) == 0
    assert calls == [
        {
            "paths": [layout.state_db_path(space.pkg / "up").absolute()],
            "host": "127.0.0.1",
            "port": 9999,
        }
    ]


def test_ui_without_the_extra_prints_a_hint(
    space: Space, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import varve.ui

    monkeypatch.delitem(sys.modules, "varve.ui.app", raising=False)
    for name in ("app", "serve", "create_app"):
        monkeypatch.delitem(vars(varve.ui), name, raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    assert cli.main(["ui"]) == 1
    assert "varve[ui]" in capsys.readouterr().err


# -- macro plan and run ----------------------------------------------------------


def macro_space(space: Space) -> None:
    space.add("up", UP, "Up", UP_YAML)
    space.add(
        "down",
        down_source(
            space.prefix,
            f'Ref("{space.module("up")}", stage="combined", branch=("main", "alt"))',
            "",
        ),
        "Down",
    )
    space.add(
        "solo",
        "from varve import Ctx, Pipeline, stage\n\nclass Solo(Pipeline):\n"
        "    @stage()\n    def only(self, ctx: Ctx) -> None:\n        (ctx.out / 'x').write_text('x')\n",
        "Solo",
    )


def labels(plan) -> list[str]:
    return [node.label for node in plan.nodes]


def test_macro_order_auto_inclusion_and_manual(space: Space) -> None:
    macro_space(space)
    space.toml("down", "solo", "up")
    workspace = Workspace(load_workspace())
    m = space.module
    plan = build_plan(workspace)
    assert labels(plan) == [
        f"{m('solo')}@main",
        f"{m('up')}@main",
        f"{m('up')}@alt",
        f"{m('down')}@main",
    ]
    assert plan.selected == labels(plan)
    assert plan.upstream[f"{m('down')}@main"] == [f"{m('up')}@main", f"{m('up')}@alt"]

    plan = build_plan(workspace, all_branches=True)
    assert f"{m('up')}@heavy" in labels(plan)

    plan = build_plan(workspace, ["down"])
    assert labels(plan) == [f"{m('up')}@main", f"{m('up')}@alt", f"{m('down')}@main"]
    assert plan.why[f"{m('up')}@alt"] == f"required by {m('down')}@main"

    plan = build_plan(workspace, ["up@heavy"])
    assert labels(plan) == [f"{m('up')}@heavy"]
    plan = build_plan(workspace, branch="alt")
    assert labels(plan) == [f"{m('up')}@alt"]


def test_macro_selection_and_set_routing(space: Space) -> None:
    macro_space(space)
    space.toml("down", "solo", "up")
    workspace = Workspace(load_workspace())
    m = space.module
    plan = build_plan(workspace, only=["up"])
    assert plan.selected == [f"{m('up')}@main", f"{m('up')}@alt"]
    plan = build_plan(workspace, skip=["up@alt", "solo"])
    assert plan.selected == [f"{m('up')}@main", f"{m('down')}@main"]
    plan = build_plan(workspace, start="up@alt")
    assert plan.selected == [f"{m('up')}@alt", f"{m('down')}@main"]

    plan = build_plan(workspace, sets={"scale": "3"})
    assert plan.sets == {f"{m('down')}@main": {"scale": 3}}
    with pytest.raises(MacroError, match="no selected pipeline declares an Args field 'scal'"):
        build_plan(workspace, sets={"scal": "3"})
    with pytest.raises(MacroError, match="scale"):
        build_plan(workspace, ["up"], sets={"scale": "3"})
    with pytest.raises(MacroError, match="invalid"):
        build_plan(workspace, sets={"scale": "many"})


def test_macro_cycle_is_an_error(space: Space) -> None:
    for name, other in (("left", "right"), ("right", "left")):
        space.add(
            name,
            down_source(space.prefix, f'Ref("{space.module(other)}", stage="use")', ""),
            "Down",
        )
    space.toml("left", "right")
    with pytest.raises(MacroError, match="cycle"):
        build_plan(Workspace(load_workspace()))


def test_macro_run_dry_run_and_resume(space: Space, capsys) -> None:
    macro_space(space)
    space.toml("down", "solo", "up")
    m = space.module
    assert cli.main(["run", "--dry-run", "--set", "scale=2"]) == 0
    out = capsys.readouterr().out
    assert "scale=2" in out and f"{m('down')}@main" in out
    assert not any(space.pkg.rglob(".varve"))

    assert cli.main(["run", "--no-tui", "--set", "scale=2"]) == 0
    out = capsys.readouterr().out
    assert "Summary" in out
    down = space.load("down", "Down")
    assert outputs(down, space.pkg / "down") == {".": {"main": "all:v1", "alt": "all:alt"}}
    view = probe_pipeline(down)
    assert view.stage("use").status == "hit"

    # A failing upstream stops the run and prints a resume command.
    space.add(
        "up",
        UP.replace('(ctx.out / "all.txt")', 'raise RuntimeError("boom"); (ctx.out / "all.txt")'),
        "Up",
    )
    assert cli.main(["run", "--no-tui", "down"]) == 1
    out = capsys.readouterr().out
    assert f"varve run --no-tui down --from {m('up')}@main" in out
    assert "not-run" in out


def test_status_and_plan_commands(space: Space, capsys, monkeypatch) -> None:
    macro_space(space)
    space.toml("down", "solo", "up")
    assert cli.main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "waits for" in out
    assert cli.main(["run", "--no-tui", "down"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "down"]) == 0
    out = capsys.readouterr().out
    assert "Single-pipeline probing only checks" in out and "combined@alt" in out
    assert cli.main(["plan", "down"]) == 0
    out = capsys.readouterr().out
    ref = f"@{space.module('up')}:combined@main,alt"
    assert "Plan" in out and "External inputs (Refs)" in out and ref in out.replace("\n", "")
    down = space.load("down", "Down")
    monkeypatch.setenv("COLUMNS", "200")
    assert down.cli(["ls"]) == 0
    assert ref in capsys.readouterr().out
    assert cli.main(["inspect", "down", "--stage", "use"]) == 0
    assert "Refs" in capsys.readouterr().out
    assert cli.main(["status", "nope"]) == 1


def test_resolver_reads_upstream_without_importing_it(space: Space) -> None:
    up, down = setup_pair(space, f'Ref("{space.module("up")}", stage="export")')
    space.run(up)
    for name in list(sys.modules):
        if name.startswith(space.module("up")):
            del sys.modules[name]
    resolver = WorkspaceRefResolver(load_workspace())
    view = probe_pipeline(down, refs=resolver)
    assert view.stage("use").status == "needs-run"
    assert not any(name.startswith(space.module("up")) for name in sys.modules)
    assert [info.units for info in bound_refs(resolver)] == [3]
    conn = sqlite3.connect(layout.state_db_path(space.pkg / "up"))
    assert conn.execute("SELECT COUNT(*) FROM units WHERE status='ok'").fetchone()[0] == 4


def test_workspace_storage_reaches_single_pipeline_commands(space: Space, tmp_path: Path) -> None:
    space.add(
        "up",
        UP.replace(
            "class Up(Pipeline):", "class Up(Pipeline):\n    defaults = dict(storage='bulk')"
        ),
        "Up",
    )
    space.toml("up", extra='[storage]\nbulk = "bulkdata"\n')
    up = space.load("up", "Up")
    assert up.cli(["run", "--no-tui"]) == 0
    assert (tmp_path / "bulkdata" / space.module("up") / ".internal/main/export").is_dir()


# -- review regressions: Ref fan-in sets and inline Refs in macro plans ----------

PARTIAL_UP = UP.replace(
    '        (ctx.out / "data.txt").write_text(f"{dataset}:{ctx.config.tag}")',
    '        if dataset == "c" and ctx.config.tag == "v1":\n'
    '            raise RuntimeError("boom")\n'
    '        (ctx.out / "data.txt").write_text(f"{dataset}:{ctx.config.tag}")',
)


def test_ref_fan_in_with_a_failed_upstream_unit_is_an_error(space: Space) -> None:
    space.add("up", PARTIAL_UP, "Up", UP_YAML)
    ref = f'Ref("{space.module("up")}", stage="export", align_by=())'
    space.add("down", down_source(space.prefix, ref), "Down")
    space.toml("up", "down")
    up, down = space.load("up", "Up"), space.load("down", "Down")
    assert space.run(up).status == "failed"
    with pytest.raises(RefResolutionError, match=r"fan-in is incomplete: 1 unit\(s\) are not ok"):
        probe_pipeline(down)
    result = space.run(down)
    assert result.stages["use"].status == "failed"
    assert "dataset=c failed" in (result.stages["use"].detail or "")
    assert f"python -m {space.module('up')} run" in (result.stages["use"].detail or "")


def test_ref_fan_in_with_a_running_upstream_unit_is_an_error(space: Space) -> None:
    up, down = setup_pair(space, f'Ref("{space.module("up")}", stage="export", align_by=())')
    assert space.run(up).status == "ok"
    conn = sqlite3.connect(layout.state_db_path(space.pkg / "up"))
    conn.execute("UPDATE units SET status = 'running' WHERE unit_key = 'dataset=b'")
    conn.commit()
    conn.close()
    with pytest.raises(RefResolutionError, match="dataset=b running"):
        probe_pipeline(down)


def test_ref_fan_in_excludes_units_outside_the_upstream_active_domain(space: Space) -> None:
    up, down = setup_pair(space, f'Ref("{space.module("up")}", stage="export", align_by=())')
    assert space.run(up).status == "ok"
    # The upstream axis loses "c": its old ok row is outside the domain now.
    space.add("up", UP.replace('("a", "b", "c")', '("a", "b")'), "Up", UP_YAML)
    up = space.load("up", "Up")
    assert space.run(up).status == "ok"
    assert space.run(down).status == "ok"
    expected = {"a": "a:v1", "b": "b:v1"}
    assert outputs(down, space.pkg / "down") == {"dataset=a": expected, "dataset=b": expected}
    # A varve.yaml axes: restriction narrows the set the same way.
    space.add("up", UP, "Up", "main:\n  axes: {dataset: [b, c]}\n")
    up = space.load("up", "Up")
    assert space.run(up).status == "ok"
    assert space.run(down).status == "ok"
    expected = {"b": "b:v1", "c": "c:v1"}
    assert outputs(down, space.pkg / "down") == {"dataset=a": expected, "dataset=b": expected}


def test_macro_plan_follows_inline_needs_refs(space: Space) -> None:
    space.add("up", UP, "Up", UP_YAML)
    space.add(
        "down",
        f"""
from varve import Ctx, Pipeline, Ref, stage

class Down(Pipeline):
    @stage(needs=Ref("{space.module("up")}", stage="combined", branch="alt"))
    def use(self, ctx: Ctx) -> None:
        (ctx.out / "x").write_text("x")
""",
        "Down",
    )
    space.toml("down")
    plan = build_plan(Workspace(load_workspace()), ["down"])
    m = space.module
    assert labels(plan) == [f"{m('up')}@alt", f"{m('down')}@main"]


def test_ref_fan_in_follows_declared_axis_order_not_commit_order(space: Space) -> None:
    up, down = setup_pair(space, f'Ref("{space.module("up")}", stage="export", align_by=())')
    # Commit "b" and "c" first, then "a": store rows are in b, c, a order.
    space.add("up", UP, "Up", "main:\n  axes: {dataset: [b, c]}\n")
    assert space.run(space.load("up", "Up")).status == "ok"
    space.add("up", UP, "Up", UP_YAML)
    up = space.load("up", "Up")
    assert space.run(up).status == "ok"
    assert space.run(down).status == "ok"
    value = outputs(down, space.pkg / "down")["dataset=a"]
    assert list(value) == ["a", "b", "c"]
    repo = Repo(connect(layout.state_db_path(space.pkg / "down"), readonly=True))
    try:
        components = repo.units("main", "use")["dataset=a"].key_components
    finally:
        repo.conn.close()
    entry = components["upstream"]["summary"]["src"]
    assert [unit_key for unit_key, _ in entry] == ["dataset=a", "dataset=b", "dataset=c"]


def test_set_null_is_none_for_optional_fields_like_the_generated_cli() -> None:
    from varve.workspace.macro import _coerce

    assert _coerce(str | None, "null", "tag", "p@main") is None
    assert _coerce(int | None, "null", "n", "p@main") is None
    assert _coerce(str, "null", "tag", "p@main") == "null"
    assert _coerce(str | None, "x", "tag", "p@main") == "x"


def test_ls_table_fits_a_narrow_terminal() -> None:
    import io

    from rich.console import Console

    from varve.store.repo import BranchSummary, RunRow
    from varve.workspace.config import WorkspaceSettings
    from varve.workspace.discovery import Entry

    entry = Entry(
        name="metric_eval.benchmark_misjudgment.threshold_sensitivity",
        module="studies.exp.metric_eval.benchmark_misjudgment.threshold_sensitivity.run",
        root=Path("/very/long/path/to/studies/exp/metric_eval/benchmark_misjudgment/threshold"),
        db_path=Path("/x/.varve/state.db"),
        branches=[
            BranchSummary(
                "main_tmp_0123456789abcdef",
                True,
                False,
                {"ok": 1234},
                RunRow("r", "main_tmp_0123456789abcdef", "", "ok", [], "host", total_wall_ms=5),
            )
        ],
    )
    buffer = io.StringIO()
    console = Console(file=buffer, width=100, force_terminal=False)
    cli.render_workspace(console, WorkspaceSettings(), [entry])
    out = buffer.getvalue()
    assert all(len(line) <= 100 for line in out.splitlines())
    assert "Units" in out and "Last run" in out and "Root" in out and "1234 ok" in out
