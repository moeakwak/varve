from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Experiment, KeySpec, batch_stage, stage
from varve.engine.runner import run, selected_stages
from varve.models import BatchRecord, PartialMeta
from varve.store.store import Store


class Config(BaseModel):
    token: str = "a"
    batch_size: int = 2
    fail_after: int | None = None


class ToyExperiment(Experiment):
    Config = Config

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @stage(produces="sample.txt", key=["token"])
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.token, encoding="utf-8")

    @batch_stage(needs="sample", key=["token"], partition_key=["batch_size"])
    async def transform(self, ctx):
        items = list(range(4))
        async for index, item in ctx.resume(items):
            path = ctx.out / "transform" / f"part-{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"{item}:{ctx.input('sample').read_text(encoding='utf-8')}", encoding="utf-8"
            )
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

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @batch_stage(partition_key=["batch_size"])
    async def split(self, ctx):
        async for index, item in ctx.resume(["zero", "one"]):
            left = ctx.out / f"{index}-left.txt"
            right = ctx.out / f"{index}-right.txt"
            left.write_text(f"{item}:left", encoding="utf-8")
            right.write_text(f"{item}:right", encoding="utf-8")
            yield [left, right]


class CwdRelativeBatchExperiment(Experiment):
    Config = Config

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @batch_stage(partition_key=["batch_size"])
    async def transform(self, ctx):
        async for index, _item in ctx.resume(["zero"]):
            path = ctx.out / "transform" / f"part-{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("payload", encoding="utf-8")
            yield path


def test_selected_stages() -> None:
    assert selected_stages(ToyExperiment, target="transform") == {"sample", "transform"}
    assert selected_stages(ToyExperiment, only="transform") == {"transform"}
    assert selected_stages(ToyExperiment, downstream="transform") == {"transform", "summarize"}


def test_runner_hit_stale_and_artifact_missing(tmp_path: Path) -> None:
    config = Config()
    first = run(ToyExperiment, config, cli_out=tmp_path)
    assert [outcome.status for outcome in first] == ["no-cache", "no-cache", "no-cache"]
    second = run(ToyExperiment, config, cli_out=tmp_path)
    assert [outcome.status for outcome in second] == ["hit", "hit", "hit"]

    changed = run(ToyExperiment, Config(token="b"), cli_out=tmp_path)
    assert [outcome.status for outcome in changed] == ["stale", "stale", "stale"]

    (tmp_path / "summary.txt").unlink()
    repaired = run(ToyExperiment, Config(token="b"), cli_out=tmp_path)
    assert repaired[-1].status == "artifact-missing"


def test_runner_dry_does_not_initialize_store(tmp_path: Path) -> None:
    outcomes = run(ToyExperiment, Config(), cli_out=tmp_path, dry=True)
    assert [outcome.status for outcome in outcomes] == ["no-cache", "no-cache", "no-cache"]
    assert not (tmp_path / ".varve").exists()


def test_runner_dry_propagates_current_upstream_keys(tmp_path: Path) -> None:
    run(ToyExperiment, Config(token="a"), cli_out=tmp_path)
    dry = run(ToyExperiment, Config(token="b"), cli_out=tmp_path, dry=True)
    actual = run(ToyExperiment, Config(token="b"), cli_out=tmp_path)
    assert [outcome.status for outcome in dry] == [outcome.status for outcome in actual]


def test_batch_resume_after_failure(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="planned failure"):
        run(ToyExperiment, Config(fail_after=1), cli_out=tmp_path, target="transform")
    resumed = run(ToyExperiment, Config(), cli_out=tmp_path, target="transform")
    assert resumed[-1].status == "resume"
    assert len(list((tmp_path / "transform").glob("part-*.txt"))) == 4

    record = Store(tmp_path).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [output.index for output in record.outputs] == [0, 1, 2, 3]

    summary = run(ToyExperiment, Config(), cli_out=tmp_path, only="summarize")
    assert summary[-1].status == "no-cache"
    assert (tmp_path / "summary.txt").read_text(encoding="utf-8") == "0:a,1:a,2:a,3:a"


def test_no_cache_batch_ignores_mismatched_partial_outputs(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="planned failure"):
        run(ToyExperiment, Config(fail_after=0), cli_out=tmp_path, target="transform")

    partial_root = tmp_path / ".varve" / "partial" / "transform"
    current_run_key = next(path.name for path in partial_root.iterdir())
    stale_output = tmp_path / "stale.txt"
    stale_output.write_text("stale", encoding="utf-8")

    store = Store(tmp_path)
    store.write_partial_meta(
        "transform",
        current_run_key,
        PartialMeta(
            content_key="sha256:mismatch",
            partition_values={"batch_size": 999},
            started_at="old",
        ),
    )
    store.write_batch(
        "transform",
        current_run_key,
        BatchRecord(index=99, yielded=["stale.txt"], committed_at="old"),
    )

    rerun = run(ToyExperiment, Config(), cli_out=tmp_path, target="transform")
    assert rerun[-1].status == "no-cache"

    record = Store(tmp_path).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [(output.index, output.path) for output in record.outputs] == [
        (0, "transform/part-0.txt"),
        (1, "transform/part-1.txt"),
        (2, "transform/part-2.txt"),
        (3, "transform/part-3.txt"),
    ]


def test_no_cache_failure_does_not_resume_mismatched_partial_outputs(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="planned failure"):
        run(ToyExperiment, Config(fail_after=0), cli_out=tmp_path, target="transform")

    partial_root = tmp_path / ".varve" / "partial" / "transform"
    current_run_key = next(path.name for path in partial_root.iterdir())
    stale_output = tmp_path / "stale.txt"
    stale_output.write_text("stale", encoding="utf-8")

    store = Store(tmp_path)
    store.write_partial_meta(
        "transform",
        current_run_key,
        PartialMeta(
            content_key="sha256:mismatch",
            partition_values={"batch_size": 999},
            started_at="old",
        ),
    )
    store.write_batch(
        "transform",
        current_run_key,
        BatchRecord(index=99, yielded=["stale.txt"], committed_at="old"),
    )

    with pytest.raises(RuntimeError, match="planned failure"):
        run(ToyExperiment, Config(fail_after=0), cli_out=tmp_path, target="transform")

    resumed = run(ToyExperiment, Config(), cli_out=tmp_path, target="transform")
    assert resumed[-1].status == "resume"

    record = Store(tmp_path).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [(output.index, output.path) for output in record.outputs] == [
        (0, "transform/part-0.txt"),
        (1, "transform/part-1.txt"),
        (2, "transform/part-2.txt"),
        (3, "transform/part-3.txt"),
    ]


def test_completed_batch_artifact_missing_and_unrecoverable(tmp_path: Path) -> None:
    run(ToyExperiment, Config(), cli_out=tmp_path, target="transform")
    (tmp_path / "transform" / "part-1.txt").unlink()
    repaired = run(ToyExperiment, Config(), cli_out=tmp_path, target="transform")
    assert repaired[-1].status == "artifact-missing"
    record = Store(tmp_path).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [output.index for output in record.outputs] == [0, 1, 2, 3]

    (tmp_path / "transform" / "part-1.txt").unlink()
    with pytest.raises(RuntimeError, match="partition changed"):
        run(ToyExperiment, Config(batch_size=3), cli_out=tmp_path, target="transform")


def test_force_reruns_all_batch_items_after_partial(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="planned failure"):
        run(ToyExperiment, Config(fail_after=1), cli_out=tmp_path, target="transform")
    forced = run(ToyExperiment, Config(), cli_out=tmp_path, target="transform", force=True)
    assert forced[-1].reason == "forced"
    assert len(list((tmp_path / "transform").glob("part-*.txt"))) == 4
    record = Store(tmp_path).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [output.index for output in record.outputs] == [0, 1, 2, 3]


def test_batch_multi_output_index_requires_all_paths(tmp_path: Path) -> None:
    first = run(MultiOutputBatchExperiment, Config(), cli_out=tmp_path)
    assert first[-1].status == "no-cache"
    second = run(MultiOutputBatchExperiment, Config(), cli_out=tmp_path)
    assert second[-1].status == "hit"

    (tmp_path / "1-right.txt").unlink()
    repaired = run(MultiOutputBatchExperiment, Config(), cli_out=tmp_path)
    assert repaired[-1].status == "artifact-missing"
    assert (tmp_path / "1-right.txt").exists()

    record = Store(tmp_path).read_success("split")
    assert record is not None
    assert record.outputs is not None
    assert [(output.index, output.path) for output in record.outputs] == [
        (0, "0-left.txt"),
        (0, "0-right.txt"),
        (1, "1-left.txt"),
        (1, "1-right.txt"),
    ]


def test_batch_rejects_cwd_relative_output_with_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="relative to the output root"):
        run(CwdRelativeBatchExperiment, Config(), cli_out=Path("out"))


class FileKeyConfig(BaseModel):
    src: Path


class FileKeyExperiment(Experiment):
    Config = FileKeyConfig

    @classmethod
    def default_output_root(cls, config: FileKeyConfig) -> Path:
        return Path("varve-test-output")

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
    out = tmp_path / "work"
    config = FileKeyConfig(src=src)

    first = run(FileKeyExperiment, config, cli_out=out)
    assert [outcome.status for outcome in first] == ["no-cache"]

    cached_mtime = _cached_src_mtime(out)

    # Touch the file without changing its content: bump mtime, same bytes.
    new_mtime = cached_mtime + 100.0
    os.utime(src, (new_mtime, new_mtime))

    second = run(FileKeyExperiment, config, cli_out=out)
    assert [outcome.status for outcome in second] == ["hit"]

    refreshed_mtime = _cached_src_mtime(out)
    assert refreshed_mtime == new_mtime
    assert refreshed_mtime != cached_mtime


def _cached_src_mtime(out: Path) -> float:
    record = Store(out).read_success("copy")
    assert record is not None
    return record.key_components.files["src"][0].mtime


def test_batch_stage_rejects_produces() -> None:
    with pytest.raises(ValueError, match="batch_stage does not accept produces"):

        @batch_stage(produces="out.txt")
        async def bad(self, ctx):  # pragma: no cover - decorator raises first
            yield ctx.out / "out.txt"
