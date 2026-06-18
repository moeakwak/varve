from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Experiment, KeySpec, batch_stage, stage
from varve.ledger import Ledger
from varve.runner import run, selected_stages


class Config(BaseModel):
    out: Path
    token: str = "a"
    batch_size: int = 2
    fail_after: int | None = None


class ToyExperiment(Experiment):
    Config = Config

    @stage(produces="sample.txt", key=["token"])
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.token, encoding="utf-8")

    @batch_stage(needs="sample", key=["token"], partition_key=["batch_size"])
    async def transform(self, ctx):
        items = list(range(4))
        async for index, item in ctx.resume(items):
            path = ctx.out / "transform" / f"part-{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{item}:{ctx.input('sample').read_text(encoding='utf-8')}", encoding="utf-8")
            yield path
            if ctx.config.fail_after is not None and index >= ctx.config.fail_after:
                raise RuntimeError("planned failure")

    @stage(needs="transform", produces="summary.txt")
    def summarize(self, ctx):
        parts = ctx.input("transform")
        assert isinstance(parts, list)
        text = ",".join(path.read_text(encoding="utf-8") for path in parts)
        (ctx.out / "summary.txt").write_text(text, encoding="utf-8")


class MultiOutputBatchExperiment(Experiment):
    Config = Config

    @batch_stage(partition_key=["batch_size"])
    async def split(self, ctx):
        async for index, item in ctx.resume(["zero", "one"]):
            left = ctx.out / f"{index}-left.txt"
            right = ctx.out / f"{index}-right.txt"
            left.write_text(f"{item}:left", encoding="utf-8")
            right.write_text(f"{item}:right", encoding="utf-8")
            yield [left, right]


def test_selected_stages() -> None:
    assert selected_stages(ToyExperiment, target="transform") == {"sample", "transform"}
    assert selected_stages(ToyExperiment, only="transform") == {"transform"}
    assert selected_stages(ToyExperiment, downstream="transform") == {"transform", "summarize"}


def test_runner_hit_stale_and_artifact_missing(tmp_path: Path) -> None:
    config = Config(out=tmp_path)
    first = run(ToyExperiment, config)
    assert [outcome.status for outcome in first] == ["no-cache", "no-cache", "no-cache"]
    second = run(ToyExperiment, config)
    assert [outcome.status for outcome in second] == ["hit", "hit", "hit"]

    changed = run(ToyExperiment, Config(out=tmp_path, token="b"))
    assert [outcome.status for outcome in changed] == ["stale", "stale", "stale"]

    (tmp_path / "summary.txt").unlink()
    repaired = run(ToyExperiment, Config(out=tmp_path, token="b"))
    assert repaired[-1].status == "artifact-missing"


def test_runner_dry_does_not_initialize_ledger(tmp_path: Path) -> None:
    outcomes = run(ToyExperiment, Config(out=tmp_path), dry=True)
    assert [outcome.status for outcome in outcomes] == ["no-cache", "no-cache", "no-cache"]
    assert not (tmp_path / ".varve").exists()


def test_runner_dry_propagates_current_upstream_keys(tmp_path: Path) -> None:
    run(ToyExperiment, Config(out=tmp_path, token="a"))
    dry = run(ToyExperiment, Config(out=tmp_path, token="b"), dry=True)
    actual = run(ToyExperiment, Config(out=tmp_path, token="b"))
    assert [outcome.status for outcome in dry] == [outcome.status for outcome in actual]


def test_batch_resume_after_failure(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="planned failure"):
        run(ToyExperiment, Config(out=tmp_path, fail_after=1), target="transform")
    resumed = run(ToyExperiment, Config(out=tmp_path), target="transform")
    assert resumed[-1].status == "resume"
    assert len(list((tmp_path / "transform").glob("part-*.txt"))) == 4

    record = Ledger(tmp_path).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [output.index for output in record.outputs] == [0, 1, 2, 3]

    summary = run(ToyExperiment, Config(out=tmp_path), only="summarize")
    assert summary[-1].status == "no-cache"
    assert (tmp_path / "summary.txt").read_text(encoding="utf-8") == "0:a,1:a,2:a,3:a"


def test_completed_batch_artifact_missing_and_unrecoverable(tmp_path: Path) -> None:
    run(ToyExperiment, Config(out=tmp_path), target="transform")
    (tmp_path / "transform" / "part-1.txt").unlink()
    repaired = run(ToyExperiment, Config(out=tmp_path), target="transform")
    assert repaired[-1].status == "artifact-missing"
    record = Ledger(tmp_path).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [output.index for output in record.outputs] == [0, 1, 2, 3]

    (tmp_path / "transform" / "part-1.txt").unlink()
    with pytest.raises(RuntimeError, match="partition changed"):
        run(ToyExperiment, Config(out=tmp_path, batch_size=3), target="transform")


def test_force_reruns_all_batch_items_after_partial(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="planned failure"):
        run(ToyExperiment, Config(out=tmp_path, fail_after=1), target="transform")
    forced = run(ToyExperiment, Config(out=tmp_path), target="transform", force=True)
    assert forced[-1].reason == "forced"
    assert len(list((tmp_path / "transform").glob("part-*.txt"))) == 4
    record = Ledger(tmp_path).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [output.index for output in record.outputs] == [0, 1, 2, 3]


def test_batch_multi_output_index_requires_all_paths(tmp_path: Path) -> None:
    first = run(MultiOutputBatchExperiment, Config(out=tmp_path))
    assert first[-1].status == "no-cache"
    second = run(MultiOutputBatchExperiment, Config(out=tmp_path))
    assert second[-1].status == "hit"

    (tmp_path / "1-right.txt").unlink()
    repaired = run(MultiOutputBatchExperiment, Config(out=tmp_path))
    assert repaired[-1].status == "artifact-missing"
    assert (tmp_path / "1-right.txt").exists()

    record = Ledger(tmp_path).read_success("split")
    assert record is not None
    assert record.outputs is not None
    assert [(output.index, output.path) for output in record.outputs] == [
        (0, "0-left.txt"),
        (0, "0-right.txt"),
        (1, "1-left.txt"),
        (1, "1-right.txt"),
    ]


class FileKeyConfig(BaseModel):
    out: Path
    src: Path


class FileKeyExperiment(Experiment):
    Config = FileKeyConfig

    @stage(
        produces="copy.txt",
        key=KeySpec(files={"src": lambda ctx: ctx.config.src}),
    )
    def copy(self, ctx):
        (ctx.out / "copy.txt").write_text(
            ctx.config.src.read_text(encoding="utf-8"), encoding="utf-8"
        )


def test_hit_refreshes_touched_but_unchanged_file_fingerprint(tmp_path: Path) -> None:
    src = tmp_path / "input.txt"
    src.write_text("payload", encoding="utf-8")
    config = FileKeyConfig(out=tmp_path / "work", src=src)

    first = run(FileKeyExperiment, config)
    assert [outcome.status for outcome in first] == ["no-cache"]

    cached_mtime = _cached_src_mtime(config.out)

    # Touch the file without changing its content: bump mtime, same bytes.
    new_mtime = cached_mtime + 100.0
    os.utime(src, (new_mtime, new_mtime))

    second = run(FileKeyExperiment, config)
    assert [outcome.status for outcome in second] == ["hit"]

    refreshed_mtime = _cached_src_mtime(config.out)
    assert refreshed_mtime == new_mtime
    assert refreshed_mtime != cached_mtime


def _cached_src_mtime(out: Path) -> float:
    record = Ledger(out).read_success("copy")
    assert record is not None
    return record.key_components.files["src"][0].mtime


def test_batch_stage_rejects_produces() -> None:
    with pytest.raises(ValueError, match="batch_stage does not accept produces"):

        @batch_stage(produces="out.txt")
        async def bad(self, ctx):  # pragma: no cover - decorator raises first
            yield ctx.out / "out.txt"
