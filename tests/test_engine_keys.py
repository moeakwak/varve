"""Config projection, source rules, version=, reuse, and cross-branch hardlink reuse."""

from __future__ import annotations

import json

from varve.engine import inspect_pipeline, probe_pipeline, reuse_stage
from varve.store import layout
from varve.store.db import connect
from varve.store.repo import Repo

SOURCE = """
from pydantic import BaseModel
from varve import Ctx, Pipeline, stage

def helper_a():
    return "A1"

def helper_b():
    return "B1"

class Config(BaseModel):
    a_field: str = "x"
    unused: int = 0

class Demo(Pipeline):
    Config = Config

    @stage(produces="a.txt")
    def a(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "a.txt").write_text(helper_a() + ctx.config.a_field)

    @stage(produces="b.txt")
    def b(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "b.txt").write_text(helper_b())

    @stage(produces="v.txt", version=1)
    def v(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "v.txt").write_text(helper_b())
"""


def _statuses(Demo, ws, **kwargs):
    view = probe_pipeline(Demo, root=ws.root, expand=True, **kwargs)
    return {
        stage.name: (stage.status, stage.units[0].reason, stage.units[0].labels)
        for stage in view.stages
    }


def _yaml(ws, branches: dict) -> None:
    (ws.src / "varve.yaml").write_text(json.dumps(branches))


def test_helper_change_reruns_only_its_stage_and_version_marks_code_modified(ws) -> None:
    Demo = ws.write(SOURCE)
    assert ws.run(Demo).status == "ok"
    Demo = ws.write(SOURCE.replace('return "B1"', 'return "B2"'))
    statuses = _statuses(Demo, ws)
    assert statuses["a"] == ("hit", None, ())
    assert statuses["b"] == ("needs-run", "source-changed", ())
    assert statuses["v"] == ("hit", None, ("code-modified",))
    result = ws.run(Demo)
    assert {name: o.executed for name, o in result.stages.items()} == {"a": 0, "b": 1, "v": 0}


def test_config_projection_only_read_fields_matter(ws) -> None:
    Demo = ws.write(SOURCE)
    ws.run(Demo)
    _yaml(ws, {"main": {"config": {"unused": 5}}})
    assert {s[0] for s in _statuses(Demo, ws).values()} == {"hit"}
    _yaml(ws, {"main": {"config": {"unused": 5, "a_field": "y"}}})
    statuses = _statuses(Demo, ws)
    assert statuses["a"][:2] == ("needs-run", "config-changed")
    assert statuses["b"][0] == "hit"
    result = ws.run(Demo)
    assert result.stages["a"].executed == 1 and result.stages["b"].executed == 0
    repo = Repo(connect(layout.state_db_path(ws.root), readonly=True))
    unit_a, unit_b = repo.get_unit("main", "a", ""), repo.get_unit("main", "b", "")
    assert unit_a is not None and unit_a.config_access == ["a_field"]
    assert unit_b is not None and unit_b.config_access == []
    repo.conn.close()


def test_reuse_accepts_a_source_change_until_the_next_change(ws, capsys) -> None:
    Demo = ws.write(SOURCE)
    ws.run(Demo)
    Demo = ws.write(SOURCE.replace('return "A1"', 'return "A1"  # tweak\n    pass'))
    assert _statuses(Demo, ws)["a"][:2] == ("needs-run", "source-changed")
    assert ws.cli(Demo, "reuse", "a") == 0
    assert "Reused 1 unit(s)" in capsys.readouterr().out
    assert _statuses(Demo, ws)["a"] == ("hit", None, ("reused",))
    assert ws.run(Demo).stages["a"].executed == 0
    Demo = ws.write(SOURCE.replace('return "A1"', 'return "A3"'))
    assert _statuses(Demo, ws)["a"][:2] == ("needs-run", "source-changed")
    result = reuse_stage(Demo, "b", root=ws.root)
    assert result.reused == 0 and result.already == 1


def test_cross_branch_hardlink_reuse(ws, capsys) -> None:
    _yaml(ws, {"main": {"config": {}}, "other": {"config": {"unused": 7}}})
    Demo = ws.write(SOURCE)
    ws.run(Demo)
    result = ws.run(Demo, branch="other")
    assert {name: (o.executed, o.linked) for name, o in result.stages.items()} == {
        "a": (0, 1),
        "b": (0, 1),
        "v": (0, 1),
    }
    statuses = _statuses(Demo, ws, branch="other")
    assert statuses["a"] == ("hit", None, ("linked@main",))
    main_file = ws.root / "out/.internal/main/a/a.txt"
    other_file = ws.root / "out/.internal/other/a/a.txt"
    assert main_file.stat().st_ino == other_file.stat().st_ino
    assert ws.cli(Demo, "status", "--branch", "other") == 0
    assert "linked@main" in capsys.readouterr().out
    # a branch that differs in a field `a` reads executes `a` but links `b`
    _yaml(ws, {"main": {"config": {}}, "third": {"config": {"a_field": "z"}}})
    third = ws.run(Demo, branch="third")
    assert (third.stages["a"].executed, third.stages["b"].linked) == (1, 1)


# -- review regressions ---------------------------------------------------------

VERSIONED = """
from pydantic import BaseModel
from varve import Ctx, Pipeline, stage

class Config(BaseModel):
    a: int = 0
    b: int = 0

class Demo(Pipeline):
    Config = Config

    @stage(version=1, produces=PRODUCES)
    def work(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "x.txt").write_text(str(ctx.config.a)READ_B)
        (ctx.out / "y.txt").write_text("y")
"""


def test_version_mode_keeps_the_access_set_but_produces_enters_the_key(ws) -> None:
    _yaml(ws, {"main": {"config": {}}})
    Demo = ws.write(VERSIONED.replace("PRODUCES", '"x.txt"').replace("READ_B", ""))
    assert ws.run(Demo).status == "ok"
    # Documented: with version= a stage that starts reading a new Config field keeps
    # its recorded access set until `version` is bumped (or the unit reruns).
    Demo = ws.write(
        VERSIONED.replace("PRODUCES", '"x.txt"').replace("READ_B", " + str(ctx.config.b)")
    )
    assert ws.run(Demo).stages["work"].executed == 0
    _yaml(ws, {"main": {"config": {"b": 5}}})
    assert ws.run(Demo).stages["work"].executed == 0
    # Changing produces= changes the artifact contract and invalidates even here.
    Demo = ws.write(
        VERSIONED.replace("PRODUCES", '["x.txt", "y.txt"]').replace(
            "READ_B", " + str(ctx.config.b)"
        )
    )
    assert _statuses(Demo, ws)["work"][1] == "version-changed"
    assert ws.run(Demo).stages["work"].executed == 1
    Demo = ws.write(
        VERSIONED.replace("PRODUCES", '["y.txt", "x.txt"]').replace(
            "READ_B", " + str(ctx.config.b)"
        )
    )
    assert ws.run(Demo).stages["work"].executed == 0  # order does not matter


def test_inspect_shows_a_produces_only_change_in_the_version_segment(ws) -> None:
    Demo = ws.write(
        VERSIONED.replace("version=1, ", "").replace("PRODUCES", '"x.txt"').replace("READ_B", "")
    )
    assert ws.run(Demo).status == "ok"
    Demo = ws.write(
        VERSIONED.replace("version=1, ", "")
        .replace("PRODUCES", '["x.txt", "y.txt"]')
        .replace("READ_B", "")
    )
    view = inspect_pipeline(Demo, "work", root=ws.root).view
    assert view is not None
    changed = {diff.segment: diff.details for diff in view.segments if diff.changed}
    assert changed == {
        "version": ("version=None, produces='x.txt' -> version=None, produces=['x.txt', 'y.txt']",)
    }


NAN = """
import math
from varve import Axis, Ctx, Dependencies, Pipeline, stage

def values():
    return [1.0, VALUE]

DYN = Axis("x", resolver=lambda: {str(i): v for i, v in enumerate(values())})

class Demo(Pipeline):
    @stage(depends=Dependencies(values={"w": DEPENDS}))
    def dep(self, ctx: Ctx) -> None:
        (ctx.out / "x.txt").write_text("x")

    @stage(foreach=DYN)
    def dyn(self, ctx: Ctx, x: float) -> None:
        (ctx.out / "x.txt").write_text(str(x))
"""


def test_nan_in_key_values_fails_the_stage_not_the_run(ws) -> None:
    Demo = ws.write(NAN.replace("VALUE", "2.0").replace("DEPENDS", "math.nan"))
    result = ws.run(Demo)
    assert result.status == "failed"
    assert result.stages["dep"].status == "failed"
    assert "NaN and Infinity are not allowed" in (result.stages["dep"].detail or "")
    assert result.stages["dyn"].status == "ok"
    Demo = ws.write(NAN.replace("VALUE", "math.inf").replace("DEPENDS", "1.5"))
    result = ws.run(Demo)
    assert result.stages["dep"].status == "ok"
    assert result.stages["dyn"].status == "failed"
    assert "NaN and Infinity are not allowed" in (result.stages["dyn"].detail or "")


CLASS_MEMBERS = """
from contextlib import contextmanager
from varve import Ctx, Pipeline, Resource, stage

@contextmanager
def open_r(rc):
    yield "OPENED"

R = Resource("rend", open=open_r)

class Demo(Pipeline):
    defaults = dict(resources=[R])
    SCALE = 1

    @stage()
    def work(self, ctx: Ctx, rend) -> None:
        (ctx.out / "x.txt").write_text(rend + str(type(self).SCALE))
"""


def test_defaults_resources_and_class_attributes_enter_the_source_fp(ws) -> None:
    Demo = ws.write(CLASS_MEMBERS)
    assert ws.run(Demo).status == "ok"
    Demo = ws.write(CLASS_MEMBERS.replace('yield "OPENED"', 'yield "OPENED2"'))
    assert ws.run(Demo).stages["work"].executed == 1
    Demo = ws.write(
        CLASS_MEMBERS.replace('yield "OPENED"', 'yield "OPENED2"').replace("SCALE = 1", "SCALE = 2")
    )
    assert ws.run(Demo).stages["work"].executed == 1
    Demo = ws.write(
        CLASS_MEMBERS.replace('yield "OPENED"', 'yield "OPENED2"').replace("SCALE = 1", "SCALE = 2")
        + "\nDemo.SCALE = 3\n"
    )
    assert ws.run(Demo).stages["work"].executed == 1


def test_status_lists_every_needs_run_reason(ws) -> None:
    Demo = ws.write(SOURCE)
    assert ws.run(Demo).status == "ok"
    _yaml(ws, {"main": {"config": {"a_field": "y"}}})
    Demo = ws.write(SOURCE.replace('return "A1"', 'return "A2"'))
    view = probe_pipeline(Demo, root=ws.root, expand=True)
    stage = view.stage("a")
    assert stage.units[0].reason == "config-changed, source-changed"
    assert stage.reasons == {"config-changed": 1, "source-changed": 1}


def test_single_branch_runs_skip_the_cross_branch_link_index(ws, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("link_index queried with a single branch")

    monkeypatch.setattr(Repo, "link_index", fail)
    Demo = ws.write(SOURCE)
    assert ws.run(Demo).status == "ok"
    Demo = ws.write(SOURCE.replace('return "B1"', 'return "B2"'))
    assert ws.run(Demo).stages["b"].executed == 1


COUNTED = """
from pathlib import Path
from varve import Axis, Ctx, Dependencies, Pipeline, stage

ITEM = Axis("item", ["a", "b", "c"])
CALLS = Path(__file__).with_suffix(".calls")

def depends(ctx):
    with CALLS.open("a") as handle:
        handle.write(ctx.cell["item"] + "\\n")
    return Dependencies(values={"item": ctx.cell["item"]})

class Demo(Pipeline):
    @stage(foreach=ITEM, depends=depends)
    def work(self, ctx: Ctx, item: str) -> None:
        pass
"""


def test_inspect_with_coordinates_evaluates_only_the_matching_cell(ws) -> None:
    Demo = ws.write(COUNTED)
    calls = ws.path.with_suffix(".calls")
    result = inspect_pipeline(Demo, "work@item=b", root=ws.root)
    assert [unit[0] for unit in result.units] == ["item=b"]
    assert calls.read_text().split() == ["b"]


def test_a_failed_unit_keys_on_the_whole_config(ws) -> None:
    Demo = ws.write(SOURCE)
    assert ws.run(Demo).status == "ok"
    # The edited stage reads a new field and fails on its current value.
    failing = SOURCE.replace(
        '        (ctx.out / "a.txt").write_text(helper_a() + ctx.config.a_field)',
        "        if ctx.config.unused == 0:\n"
        '            raise ValueError("unused must be set")\n'
        '        (ctx.out / "a.txt").write_text(helper_a() + ctx.config.a_field)',
    )
    Demo = ws.write(failing)
    assert ws.run(Demo).stages["a"].failed == 1
    assert _statuses(Demo, ws)["a"][:2] == ("failed", "unused must be set")
    # Fixing the newly read field is a config change, not a stale failure.
    _yaml(ws, {"main": {"config": {"unused": 5}}})
    assert _statuses(Demo, ws)["a"][:2] == ("needs-run", "config-changed")
    assert ws.run(Demo).stages["a"].executed == 1
    assert _statuses(Demo, ws)["a"][0] == "hit"


def test_reuse_skips_units_whose_artifact_is_missing(ws) -> None:
    Demo = ws.write(SOURCE)
    ws.run(Demo)
    (ws.root / "out/.internal/main/a/a.txt").unlink()
    Demo = ws.write(SOURCE.replace('return "A1"', 'return "A1"  # tweak\n    pass'))
    result = reuse_stage(Demo, "a", root=ws.root)
    assert result.reused == 0 and result.not_reusable == {"artifact-missing": 1}
    assert _statuses(Demo, ws)["a"][:2] == ("needs-run", "source-changed")
