"""publish=: single, list with globs, templated mapping, multi-unit lists, temp branches, cross-fs."""

from __future__ import annotations

import pytest

from varve.engine import RunOptions
from varve.engine import publish as publish_module

SOURCE = """
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

class Config(BaseModel):
    text: str = "v1"

DATASET = Axis("dataset", ("a", "b"))

class Demo(Pipeline):
    Config = Config

    @stage(produces="result.txt", publish=True)
    def single(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "result.txt").write_text(ctx.config.text)

    @stage(publish=["summary.md", "images/*.png"])
    def listing(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "summary.md").write_text("# summary")
        (ctx.out / "images").mkdir()
        for name in ("one", "two"):
            (ctx.out / "images" / f"{name}.png").write_bytes(b"png")
        (ctx.out / "images" / "skip.txt").write_text("no")

    @stage(foreach=DATASET, publish={"summary.md": "{dataset}/summary.md"})
    def mapped(self, ctx: Ctx[Config], dataset: str) -> None:
        (ctx.out / "summary.md").write_text(dataset)

    @stage(foreach=DATASET, publish=["report.txt"])
    def multi(self, ctx: Ctx[Config], dataset: str) -> None:
        (ctx.out / "report.txt").write_text("report " + dataset)
"""


def test_publish_forms_and_restore(ws) -> None:
    Demo = ws.write(SOURCE)
    assert ws.run(Demo).status == "ok"
    pub = ws.root / "out" / "main"
    assert (pub / "result.txt").read_text() == "v1"
    assert (pub / "summary.md").read_text() == "# summary"
    assert sorted(p.name for p in (pub / "images").iterdir()) == ["one.png", "two.png"]
    assert (pub / "a" / "summary.md").read_text() == "a"
    assert (pub / "dataset=b" / "report.txt").read_text() == "report b"
    managed = ws.root / "out/.internal/main/single/result.txt"
    assert (pub / "result.txt").stat().st_ino == managed.stat().st_ino

    (pub / "result.txt").unlink()
    again = ws.run(Demo)
    assert again.stages["single"].executed == 0
    assert (pub / "result.txt").read_text() == "v1"


def test_publish_replaces_on_rerun_and_temp_branch_location(ws) -> None:
    Demo = ws.write(SOURCE)
    ws.run(Demo)
    result = ws.run(Demo, overrides={"text": "v2"}, options=RunOptions(only="single", tui=False))
    assert result.status == "ok"
    assert result.branch.startswith("main_tmp_")
    assert (ws.root / "out/.tmp" / result.branch / "result.txt").read_text() == "v2"
    assert (ws.root / "out/main/result.txt").read_text() == "v1"


def test_publish_across_filesystems_is_an_error(ws, monkeypatch: pytest.MonkeyPatch) -> None:
    Demo = ws.write(SOURCE)
    real = publish_module.device_of

    def fake(path):
        return real(path) + (1 if "main" in path.parts[-1:] else 0)

    monkeypatch.setattr(publish_module, "device_of", fake)
    result = ws.run(Demo, options=RunOptions(only="single", tui=False))
    outcome = result.stages["single"]
    assert result.status == "failed" and outcome.status == "failed"
    assert "different filesystem" in (outcome.detail or "")
    assert "storage=" in (outcome.detail or "")


TEMPLATED_CLASH = """
from varve import Axis, Ctx, Pipeline, stage

DATASET = Axis("dataset", ("a", "b"))

class Demo(Pipeline):
    @stage(foreach=DATASET, publish={"x.txt": "{dataset}/out.txt"})
    def one(self, ctx: Ctx, dataset: str) -> None:
        (ctx.out / "x.txt").write_text("one")

    @stage(foreach=DATASET, publish={"y.txt": "{dataset}/out.txt"})
    def two(self, ctx: Ctx, dataset: str) -> None:
        (ctx.out / "y.txt").write_text("two")
"""


def test_templated_publish_targets_colliding_across_stages_fail_at_expansion(ws) -> None:
    Demo = ws.write(TEMPLATED_CLASH)
    result = ws.run(Demo)
    assert result.stages["one"].status == "ok"
    outcome = result.stages["two"]
    assert outcome.status == "failed"
    assert "publish target 'a/out.txt' is claimed by both one:dataset=a:x.txt and" in (
        outcome.detail or ""
    )
    assert (ws.root / "out/main/a/out.txt").read_text() == "one"


GLOB = """
from pydantic import BaseModel
from varve import Ctx, Pipeline, stage

class Config(BaseModel):
    names: list[str] = ["one", "two"]

class Demo(Pipeline):
    Config = Config

    @stage(publish=["images/*.png"])
    def plots(self, ctx: Ctx[Config]) -> None:
        for name in ctx.config.names:
            path = ctx.out / "images" / name / "plot.png"
            path.parent.mkdir(parents=True)
            path.write_bytes(name.encode())
"""


def test_glob_publish_removes_files_no_longer_produced(ws) -> None:
    import json

    Demo = ws.write(GLOB.replace("images/*.png", "images/**/*.png"))
    assert ws.run(Demo).status == "ok"
    images = ws.root / "out/main/images"
    (images / "mine.png").write_bytes(b"user file")  # not published by varve
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"names": ["two"]}}}))
    Demo = ws.write(GLOB.replace("images/*.png", "images/**/*.png"))
    assert ws.run(Demo).status == "ok"
    assert sorted(p.relative_to(images).as_posix() for p in images.rglob("*.png")) == [
        "mine.png",
        "two/plot.png",
    ]
    assert not (images / "one").exists()


MERGED = """
import json
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

class Config(BaseModel):
    suffix: str = ""

DATASET = Axis("dataset", ("a", "b"))

class Demo(Pipeline):
    Config = Config

    @stage(
        foreach=DATASET,
        produces=["parquets/manifest.json"],
        publish={"parquets/**/*.parquet": "sources/"},
    )
    def export(self, ctx: Ctx[Config], dataset: str) -> None:
        (ctx.out / "parquets").mkdir()
        name = f"{dataset}{ctx.config.suffix}.parquet"
        (ctx.out / "parquets" / name).write_text(dataset)
        (ctx.out / "parquets" / "manifest.json").write_text(json.dumps([name]))

    @stage(produces="sources/manifest.json", publish=["sources/manifest.json"])
    def finalize(self, ctx: Ctx[Config], export: dict[str, Path]) -> None:
        names = [json.loads((p / "parquets/manifest.json").read_text())[0] for p in export.values()]
        (ctx.out / "sources").mkdir()
        (ctx.out / "sources" / "manifest.json").write_text(json.dumps(names))
"""


def test_directory_targets_merge_units_into_one_directory(ws) -> None:
    import json

    Demo = ws.write(MERGED)
    assert ws.run(Demo).status == "ok"
    sources = ws.root / "out/main/sources"
    assert sorted(p.name for p in sources.iterdir()) == ["a.parquet", "b.parquet", "manifest.json"]
    assert json.loads((sources / "manifest.json").read_text()) == ["a.parquet", "b.parquet"]
    managed = ws.root / "out/.internal/main/export/dataset=a/parquets/a.parquet"
    assert (sources / "a.parquet").stat().st_ino == managed.stat().st_ino
    # Renamed glob matches replace the files the same unit published before.
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": {"suffix": ".v2"}}}))
    Demo = ws.write(MERGED)
    assert ws.run(Demo).status == "ok"
    assert sorted(p.name for p in sources.iterdir()) == [
        "a.v2.parquet",
        "b.v2.parquet",
        "manifest.json",
    ]


def test_directory_target_collisions_fail_the_stage(ws) -> None:
    # A literal source below a directory target keeps its name, so both units
    # claim manifests/manifest.json.
    entry = '"parquets/**/*.parquet": "sources/"'
    Demo = ws.write(MERGED.replace(entry, f'{entry}, "parquets/manifest.json": "manifests/"'))
    result = ws.run(Demo)
    assert result.stages["export"].status == "failed"
    assert "manifests/manifest.json" in (result.stages["export"].detail or "")
