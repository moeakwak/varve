from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import varve
from varve import Ctx, Pipeline, Ref, stage
from varve.axis import Cell
from varve.store import layout


class Config(BaseModel):
    prefix: str = "hello"


class Args(BaseModel):
    workers: int = 2


class Demo(Pipeline):
    Config = Config
    Args = Args
    upstream_labels = Ref("audit.labels", stage="export")

    @stage(produces="items.txt")
    def prepare(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "items.txt").write_text("alpha\n")

    @stage(produces="result.txt", publish=True)
    def render(self, ctx: Ctx[Config, Args], prepare: Path) -> None:
        pass


def test_public_surface_is_exactly_seven_names() -> None:
    assert sorted(varve.__all__) == sorted(
        ["Axis", "Ctx", "Dependencies", "Pipeline", "Ref", "Resource", "stage"]
    )


def test_import_does_not_load_cli_or_engine() -> None:
    code = "import sys, varve; varve.Pipeline; print(any(m.startswith('varve.cli') for m in sys.modules))"
    output = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "False"


def test_pipeline_collects_stages_refs_and_roots() -> None:
    assert list(Demo.stages()) == ["prepare", "render"]
    assert Demo.refs() == {"upstream_labels": Demo.upstream_labels}
    assert Demo.graph() is Demo.graph()  # cached
    assert Demo.default_root(Config()) == Path(__file__).parent.absolute()
    assert Demo.module_file() == Path(__file__).absolute()
    assert Demo.import_module_name() == __name__
    assert Demo.storage_roots(Config()) == {}
    assert Demo.publish_root(Config()) is None
    assert Pipeline.Config().model_dump() == {}


def test_storage_and_publish_root_resolution(tmp_path: Path) -> None:
    roots = layout.resolve_storage_roots(
        {"output": "data/output", "interim": tmp_path / "fast"},
        tmp_path,
        workspace={"interim": "/ws/interim", "bulk": "/ws/bulk"},
        pipeline_name="exp.demo",
    )
    assert roots == {
        "interim": tmp_path / "fast",  # the pipeline declaration wins
        "bulk": Path("/ws/bulk/exp.demo"),
        "output": tmp_path / "data" / "output",
        "default": tmp_path / "out",
    }
    assert layout.resolve_publish_root(None, roots, tmp_path) == tmp_path / "out"
    assert layout.resolve_publish_root("pub", roots, tmp_path) == tmp_path / "pub"


def test_layout_paths(tmp_path: Path) -> None:
    pairs = [("dataset", "mw 2024"), ("batch", "7")]
    key = "dataset=mw 2024,batch=7"
    assert layout.parse_unit_key(key) == pairs
    assert layout.parse_unit_key("") == []
    assert layout.unit_relpath(pairs) == "dataset=mw%202024/batch=7"
    assert layout.internal_dir(tmp_path, "main", "work", "a=1") == (
        tmp_path / ".internal" / "main" / "work" / "a=1"
    )
    assert layout.internal_dir(tmp_path, "main", "work", "") == tmp_path / ".internal/main/work"
    assert layout.internal_relpath("main", "work", "") == ".internal/main/work"
    assert layout.staging_dir(tmp_path, "r1", "work", key).parent == tmp_path / ".staging/r1/work"
    assert layout.publish_dir(tmp_path, "tmp1", temporary=True) == tmp_path / ".tmp" / "tmp1"
    assert (
        layout.unit_log_path(tmp_path, "r1", "work", key).name == "dataset=mw%202024%2Cbatch=7.log"
    )
    assert layout.unit_log_path(tmp_path, "r1", "work", "").name == "_.log"


class RecordingSink:
    def __init__(self) -> None:
        self.progress_calls: list[tuple[int | None, str | None]] = []

    def progress(
        self, ctx: Ctx[Any, Any], iterable: Iterable[Any], total: int | None, desc: str | None
    ) -> Iterator[Any]:
        self.progress_calls.append((total, desc))
        yield from iterable


def test_ctx_data_and_sink(tmp_path: Path) -> None:
    sink = RecordingSink()
    ctx: Ctx[Config, Args] = Ctx(
        out=tmp_path,
        config=Config(),
        args=Args(),
        branch="main",
        stage="render",
        unit_key="dataset=mw",
        coords={"dataset": "mw"},
        cell=Cell({"dataset": "mw"}),
        upstream={"prepare": tmp_path / "items.txt"},
        sink=sink,
    )
    assert ctx.config.prefix == "hello" and ctx.args.workers == 2
    assert ctx.cell.dataset == "mw" and ctx.coords["dataset"] == "mw"
    assert ctx.upstream("prepare") == tmp_path / "items.txt"
    ctx.metric(items=3, rows=10)
    ctx.metric(items=4)
    assert ctx.metrics == {"items": 4, "rows": 10}
    assert list(ctx.progress([1, 2, 3], desc="rows")) == [1, 2, 3]
    assert list(ctx.progress(iter([1]))) == [1]
    assert sink.progress_calls == [(3, "rows"), (None, None)]
    try:
        ctx.upstream("missing")
    except KeyError as error:
        assert "declared: prepare" in str(error)
    else:
        raise AssertionError("expected KeyError")


def test_ctx_default_sink_passes_through(tmp_path: Path) -> None:
    ctx = Ctx(out=tmp_path, config={}, args=None, branch="main", stage="s")
    assert list(ctx.progress(range(3))) == [0, 1, 2]
    ctx.metric(items=1)
    assert ctx.metrics == {"items": 1} and repr(ctx) == "Ctx(s, branch='main')"
