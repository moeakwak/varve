"""End-to-end: the five expansion shapes, dynamic-axis reruns, orphans, fan-in, produces."""

from __future__ import annotations

import json
import os
import stat

from varve.engine import clean_pipeline, probe_pipeline

SHAPES = """
import json
from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Ctx, Pipeline, stage

class Config(BaseModel):
    source_dir: str = ""
    batches: list[str] = ["x", "y", "z"]
    suffix: str = "!"

DATASET = Axis("dataset", ("a", "b"))
TARS = Axis(
    "tar",
    resolver=lambda ctx: {p.stem: p for p in sorted(Path(ctx.config.source_dir).glob("*.tar"))},
    input_fingerprint="stat",
)
BATCH = Axis(
    "batch",
    resolver=lambda ctx, prepare: {int(p.stem[1:]): p for p in sorted(prepare.glob("b*.txt"))},
)
PART = Axis(
    "part",
    resolver=lambda ctx, dataset, split: sorted(p.stem for p in split.glob("*.txt")),
)

class Demo(Pipeline):
    Config = Config

    @stage(produces="items.txt")
    def items(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "items.txt").write_text("alpha\\nbeta\\n")

    @stage(foreach=DATASET, produces=["data.txt"])
    def per_dataset(self, ctx: Ctx[Config], dataset: str, items: Path) -> None:
        (ctx.out / "data.txt").write_text(dataset + ":" + items.read_text())

    @stage(foreach=TARS, produces="size.txt")
    def extract(self, ctx: Ctx[Config], tar: Path) -> None:
        (ctx.out / "size.txt").write_text(str(len(tar.read_bytes())))

    @stage()
    def prepare(self, ctx: Ctx[Config]) -> None:
        for index, text in enumerate(ctx.config.batches):
            (ctx.out / f"b{index}.txt").write_text(text)

    @stage(foreach=BATCH, produces="out.txt")
    def process(self, ctx: Ctx[Config], batch: Path) -> None:
        (ctx.out / "out.txt").write_text(batch.read_text().upper())

    @stage(produces="merged.json")
    def merge(self, ctx: Ctx[Config], process: dict[int, Path]) -> None:
        payload = {str(k): [type(k).__name__, v.read_text()] for k, v in process.items()}
        (ctx.out / "merged.json").write_text(json.dumps(payload))

    @stage(foreach=DATASET)
    def split(self, ctx: Ctx[Config], dataset: str) -> None:
        for name in ("p1", "p2") if dataset == "a" else ("p1",):
            (ctx.out / f"{name}.txt").write_text(dataset + name)

    @stage(foreach=(DATASET, PART), produces="row.txt")
    def mixed(self, ctx: Ctx[Config], dataset: str, part: str, split: Path) -> None:
        (ctx.out / "row.txt").write_text((split / f"{part}.txt").read_text() + ctx.coords["part"])

    @stage(produces="summary.txt")
    def summary(self, ctx: Ctx[Config], per_dataset: dict[str, Path], mixed: dict) -> None:
        keys = sorted(f"{k}" for k in mixed)
        (ctx.out / "summary.txt").write_text(",".join(per_dataset) + "|" + ";".join(keys))
"""


def _config(ws, **values) -> None:
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"config": values}}))


def test_five_shapes_end_to_end(ws) -> None:
    tars = ws.base / "tars"
    tars.mkdir()
    for name in ("t1", "t2"):
        (tars / f"{name}.tar").write_bytes(b"x" * (3 if name == "t1" else 5))
    _config(ws, source_dir=str(tars))
    Demo = ws.write(SHAPES)

    result = ws.run(Demo)
    assert result.status == "ok", result.stages
    executed = {name: o.executed for name, o in result.stages.items()}
    assert executed == {
        "items": 1,
        "per_dataset": 2,
        "extract": 2,
        "prepare": 1,
        "process": 3,
        "merge": 1,
        "split": 2,
        "mixed": 3,
        "summary": 1,
    }
    internal = ws.root / "out" / ".internal" / "main"
    merged = json.loads((internal / "merge" / "merged.json").read_text())
    assert merged == {"0": ["int", "X"], "1": ["int", "Y"], "2": ["int", "Z"]}
    assert (internal / "mixed" / "dataset=a" / "part=p2" / "row.txt").read_text() == "ap2p2"
    assert (
        internal / "summary" / "summary.txt"
    ).read_text() == "a,b|('a', 'p1');('a', 'p2');('b', 'p1')"
    assert (internal / "extract" / "tar=t2" / "size.txt").read_text() == "5"

    again = ws.run(Demo)
    assert {o.executed for o in again.stages.values()} == {0}
    view = probe_pipeline(Demo, root=ws.root)
    assert {stage.status for stage in view.stages} == {"hit"}


def test_upstream_driven_axis_reruns_only_changed_batches(ws) -> None:
    _config(ws, source_dir=str(ws.base), batches=["x", "y", "z"])
    Demo = ws.write(SHAPES)
    assert ws.run(Demo, options=None).status == "ok"

    _config(ws, source_dir=str(ws.base), batches=["x", "CHANGED", "z"])
    view = probe_pipeline(Demo, root=ws.root, stages=["process", "merge"])
    assert view.stage("process").status == "awaiting"
    assert view.stage("process").awaiting == "prepare"
    assert view.stage("merge").awaiting == "prepare"

    result = ws.run(Demo)
    assert result.stages["prepare"].executed == 1
    assert result.stages["process"].executed == 1
    repo = ws.repo()
    cached = repo.load_stat_cache()
    repo.conn.close()
    assert any(path.endswith("/b1.txt") for path in cached)  # batch inputs are content-hashed
    assert result.stages["process"].hit == 2
    assert result.stages["merge"].executed == 1


def test_shrink_marks_orphans_and_clean_orphans_removes_them(ws) -> None:
    _config(ws, source_dir=str(ws.base), batches=["x", "y", "z"])
    Demo = ws.write(SHAPES)
    ws.run(Demo)
    _config(ws, source_dir=str(ws.base), batches=["x", "y"])
    result = ws.run(Demo)
    assert result.stages["process"].orphaned == 1
    merged = json.loads((ws.root / "out/.internal/main/merge/merged.json").read_text())
    assert list(merged) == ["0", "1"]

    repo = ws.repo()
    assert [row.unit_key for row in repo.units_with_status("orphaned")] == ["batch=2"]
    repo.conn.close()
    orphan_dir = ws.root / "out/.internal/main/process/batch=2"
    assert orphan_dir.is_dir()
    assert ws.cli(Demo, "clean", "--orphans", "--yes") == 0
    assert not orphan_dir.exists()
    repo = ws.repo()
    assert repo.units_with_status("orphaned") == []
    repo.conn.close()
    assert clean_pipeline(Demo, root=ws.root, orphans=True).rows == 0


def test_produces_single_file_vs_dir_and_read_only_files(ws) -> None:
    _config(ws, source_dir=str(ws.base))
    Demo = ws.write(SHAPES)
    ws.run(Demo, options=None)
    repo = ws.repo()
    items = repo.get_unit("main", "items", "")
    per_dataset = repo.get_unit("main", "per_dataset", "dataset=a")
    repo.conn.close()
    assert items is not None and per_dataset is not None
    assert items.artifact_relpath == ".internal/main/items/items.txt"
    assert per_dataset.artifact_relpath == ".internal/main/per_dataset/dataset=a"
    managed = ws.root / "out" / items.artifact_relpath
    assert stat.S_IMODE(managed.stat().st_mode) == 0o444
    assert stat.S_IMODE(managed.parent.stat().st_mode) == 0o755
    assert not os.access(managed, os.W_OK) or os.geteuid() == 0
    # deleting a managed file is detected as artifact-missing and repaired
    managed.unlink()
    view = probe_pipeline(Demo, root=ws.root, stages=["items"], expand=True)
    assert view.stage("items").units[0].reason == "artifact-missing"
    result = ws.run(Demo)
    assert result.stages["items"].executed == 1
    # identical bytes: downstream stays hit
    assert result.stages["per_dataset"].executed == 0


def test_static_branch_axes_restrict_cells(ws) -> None:
    (ws.src / "varve.yaml").write_text(
        json.dumps({"main": {"config": {"source_dir": str(ws.base)}, "axes": {"dataset": ["b"]}}})
    )
    Demo = ws.write(SHAPES)
    result = ws.run(Demo, options=None)
    assert result.stages["per_dataset"].total == 1
    assert (ws.root / "out/.internal/main/per_dataset/dataset=b").is_dir()
    assert not (ws.root / "out/.internal/main/per_dataset/dataset=a").exists()


EMPTY_UNITS = """
from varve import Axis, Ctx, Pipeline, stage

ITEM = Axis("item", ("a", "b"))

class Demo(Pipeline):
    @stage(foreach=ITEM)
    def maybe(self, ctx: Ctx, item: str) -> None:
        if item == "a":
            (ctx.out / "x.txt").write_text(item)
"""


def test_a_removed_or_replaced_unit_dir_with_an_empty_manifest_is_missing(ws) -> None:
    import shutil

    Demo = ws.write(EMPTY_UNITS)
    assert ws.run(Demo).status == "ok"
    empty = ws.root / "out/.internal/main/maybe/item=b"
    assert empty.is_dir() and not any(empty.iterdir())
    empty.rmdir()
    for rehash in (False, True):
        view = probe_pipeline(Demo, root=ws.root, expand=True, rehash=rehash)
        reasons = {unit.unit_key: unit.reason for unit in view.stage("maybe").units}
        assert reasons["item=b"] in {"artifact-missing", "artifact-changed"}
        assert reasons["item=a"] is None
    assert ws.run(Demo).stages["maybe"].executed == 1
    assert empty.is_dir()
    # A symlink in place of the unit directory is not the managed directory.
    full = ws.root / "out/.internal/main/maybe/item=a"
    os.chmod(full.parent, 0o755)
    moved = ws.base / "moved"
    shutil.move(str(full), moved)
    full.symlink_to(moved, target_is_directory=True)
    view = probe_pipeline(Demo, root=ws.root, expand=True)
    reasons = {unit.unit_key: unit.reason for unit in view.stage("maybe").units}
    assert reasons["item=a"] == "artifact-missing"


STATIC_ONLY = """
from varve import Axis, Ctx, Pipeline, stage

ITEM = Axis("item", ("a", "b", "c"))

class Demo(Pipeline):
    @stage(foreach=ITEM, produces="v.txt")
    def work(self, ctx: Ctx, item: str) -> None:
        (ctx.out / "v.txt").write_text(item)
"""


def test_undeclared_static_ids_and_old_foreach_rows_become_orphans(ws) -> None:
    def statuses() -> dict[str, str]:
        repo = ws.repo()
        try:
            return {row.unit_key: row.status for row in repo.units("main", "work").values()}
        finally:
            repo.conn.close()

    Demo = ws.write(STATIC_ONLY)
    assert ws.run(Demo).status == "ok"
    # Outside the branch's active subset only: kept as they are.
    (ws.src / "varve.yaml").write_text(json.dumps({"main": {"axes": {"item": ["a"]}}}))
    Demo = ws.write(STATIC_ONLY)
    assert ws.run(Demo).stages["work"].orphaned == 0
    assert set(statuses().values()) == {"ok"}
    (ws.src / "varve.yaml").unlink()
    # "c" is no longer declared: its row is an orphan.
    Demo = ws.write(STATIC_ONLY.replace('("a", "b", "c")', '("a", "b")'))
    assert ws.run(Demo).stages["work"].orphaned == 1
    assert statuses() == {"item=a": "ok", "item=b": "ok", "item=c": "orphaned"}
    # Rows written under another foreach declaration are orphans too.
    Demo = ws.write(
        STATIC_ONLY.replace('Axis("item"', 'Axis("name"')
        .replace("item: str", "name: str")
        .replace("write_text(item)", "write_text(name)")
    )
    result = ws.run(Demo)
    assert result.stages["work"].orphaned == 2 and result.stages["work"].executed == 3
    assert clean_pipeline(Demo, root=ws.root, orphans=True).rows == 3
    assert set(statuses()) == {"name=a", "name=b", "name=c"}
    assert not (ws.root / "out/.internal/main/work/item=a").exists()
