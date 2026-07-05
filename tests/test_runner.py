from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import KeySpec, Pipeline, batch_stage, stage
from varve.engine.runner import evaluate_state, run, selected_stages
from varve.store.store import Store


class Config(BaseModel):
    token: str = "a"
    batch_size: int = 2


class Args(BaseModel):
    fail_after: int | None = None


class ToyPipeline(Pipeline):
    Config = Config
    Args = Args

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.token, encoding="utf-8")

    @batch_stage(needs="sample")
    async def transform(self, ctx):
        items = list(range(4))
        async for index, item in ctx.resume(items):
            path = ctx.out / "transform" / f"part-{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"{item}:{ctx.input('sample').read_text(encoding='utf-8')}", encoding="utf-8"
            )
            yield path
            if ctx.args.fail_after is not None and index >= ctx.args.fail_after:
                raise RuntimeError("planned failure")

    @stage(needs="transform", produces="summary.txt")
    def summarize(self, ctx):
        parts = ctx.inputs("transform")
        text = ",".join(path.read_text(encoding="utf-8") for path in parts)
        (ctx.out / "summary.txt").write_text(text, encoding="utf-8")


class MultiOutputBatchPipeline(Pipeline):
    Config = Config
    Args = Args

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @batch_stage()
    async def split(self, ctx):
        async for index, item in ctx.resume(["zero", "one"]):
            left = ctx.out / f"{index}-left.txt"
            right = ctx.out / f"{index}-right.txt"
            left.write_text(f"{item}:left", encoding="utf-8")
            right.write_text(f"{item}:right", encoding="utf-8")
            yield [left, right]


class CwdRelativeBatchPipeline(Pipeline):
    Config = Config
    Args = Args

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @batch_stage()
    async def transform(self, ctx):
        async for index, _item in ctx.resume(["zero"]):
            path = ctx.out / "transform" / f"part-{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("payload", encoding="utf-8")
            yield path


class OutsideProducesPipeline(Pipeline):
    Config = Config
    Args = Args

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @stage(produces="../outside.txt")
    def sample(self, ctx):
        outside = ctx.out.parent / "outside.txt"
        outside.write_text("payload", encoding="utf-8")


class MissingNeedsInputPipeline(Pipeline):
    Config = Config
    Args = Args

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")

    @stage(produces="summary.txt")
    def summarize(self, ctx):
        source = ctx.input("sample")
        (ctx.out / "summary.txt").write_text(source.read_text(encoding="utf-8"))


class NakedYieldBatchPipeline(Pipeline):
    Config = Config
    Args = Args

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @batch_stage()
    async def transform(self, ctx):
        path = ctx.out / "part.txt"
        path.write_text("payload", encoding="utf-8")
        yield path
        if ctx.args.fail_after is not None:
            raise RuntimeError("planned failure")


def _out(base: Path) -> Path:
    return base / "main"


def test_selected_stages() -> None:
    assert selected_stages(ToyPipeline, upto="transform") == {"sample", "transform"}
    assert selected_stages(ToyPipeline, downstream="transform") == {"transform", "summarize"}


def test_runner_hit_stale_and_artifact_missing(tmp_path: Path) -> None:
    config = Config()
    first = run(ToyPipeline, config, cli_out=tmp_path)
    assert [outcome.status for outcome in first] == ["no-cache", "no-cache", "no-cache"]
    second = run(ToyPipeline, config, cli_out=tmp_path)
    assert [outcome.status for outcome in second] == ["hit", "hit", "hit"]

    changed = run(ToyPipeline, Config(token="b"), cli_out=tmp_path)
    assert [outcome.status for outcome in changed] == ["stale", "stale", "stale"]

    (_out(tmp_path) / "summary.txt").unlink()
    repaired = run(ToyPipeline, Config(token="b"), cli_out=tmp_path)
    assert repaired[-1].status == "artifact-missing"


def test_evaluate_state_does_not_initialize_store(tmp_path: Path) -> None:
    outcomes = evaluate_state(ToyPipeline, Config(), cli_out=tmp_path)
    assert [outcome.status for outcome in outcomes] == ["no-cache", "no-cache", "no-cache"]
    assert not (_out(tmp_path) / ".varve").exists()


def test_run_writes_importable_main_module_to_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MainPipeline(ToyPipeline):
        pass

    class Spec:
        name = "pkg.demo.__main__"

    module = type("Module", (), {"__spec__": Spec()})()
    MainPipeline.__module__ = "__main__"
    monkeypatch.setitem(sys.modules, "__main__", module)

    run(MainPipeline, Config(), cli_out=tmp_path, upto="sample")

    manifest = Store(_out(tmp_path)).read_manifest()
    assert manifest is not None
    assert manifest.pipeline == "MainPipeline"
    assert manifest.module == "pkg.demo.__main__"


def test_run_persists_stage_elapsed(
    tmp_path: Path,
) -> None:
    run(ToyPipeline, Config(), cli_out=tmp_path, upto="sample")

    record = Store(_out(tmp_path)).read_success("sample")
    assert record is not None
    assert record.elapsed is not None
    assert record.elapsed >= 0


def test_evaluate_state_propagates_current_upstream_keys(tmp_path: Path) -> None:
    run(ToyPipeline, Config(token="a"), cli_out=tmp_path)
    dry = evaluate_state(ToyPipeline, Config(token="b"), cli_out=tmp_path)
    actual = run(ToyPipeline, Config(token="b"), cli_out=tmp_path)
    assert [outcome.status for outcome in dry] == [outcome.status for outcome in actual]


def test_batch_resume_after_failure(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="planned failure"):
        run(
            ToyPipeline,
            Config(),
            args=Args(fail_after=1),
            cli_out=tmp_path,
            upto="transform",
        )
    resumed = run(ToyPipeline, Config(), cli_out=tmp_path, upto="transform")
    assert resumed[-1].status == "resume"
    assert len(list((_out(tmp_path) / "transform").glob("part-*.txt"))) == 4

    record = Store(_out(tmp_path)).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [output.index for output in record.outputs] == [0, 1, 2, 3]

    summary = run(ToyPipeline, Config(), cli_out=tmp_path, downstream="summarize")
    assert summary[-1].status == "no-cache"
    assert (_out(tmp_path) / "summary.txt").read_text(encoding="utf-8") == "0:a,1:a,2:a,3:a"


def test_completed_batch_artifact_missing_then_config_change_is_stale(tmp_path: Path) -> None:
    run(ToyPipeline, Config(), cli_out=tmp_path, upto="transform")
    (_out(tmp_path) / "transform" / "part-1.txt").unlink()
    repaired = run(ToyPipeline, Config(), cli_out=tmp_path, upto="transform")
    assert repaired[-1].status == "artifact-missing"
    record = Store(_out(tmp_path)).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [output.index for output in record.outputs] == [0, 1, 2, 3]

    (_out(tmp_path) / "transform" / "part-1.txt").unlink()
    changed_partition = run(ToyPipeline, Config(batch_size=3), cli_out=tmp_path, upto="transform")
    assert changed_partition[-1].status == "stale"
    assert (_out(tmp_path) / "transform" / "part-1.txt").exists()


def test_batch_stage_warns_when_yielding_without_ctx_resume(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="yielded without iterating ctx.resume"):
        run(NakedYieldBatchPipeline, Config(), cli_out=tmp_path)


def test_batch_stage_warns_on_failure_after_naked_yield(tmp_path: Path) -> None:
    class FailingNakedYieldBatchPipeline(Pipeline):
        Config = Config
        Args = Args

        @classmethod
        def default_output_root(cls, config: Config) -> Path:
            return Path("varve-test-output")

        @batch_stage()
        async def transform(self, ctx):
            path = ctx.out / "part.txt"
            path.write_text("payload", encoding="utf-8")
            yield path
            raise RuntimeError("planned failure")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(RuntimeError, match="planned failure"):
            run(FailingNakedYieldBatchPipeline, Config(), cli_out=tmp_path)

    assert any("yielded without iterating ctx.resume" in str(item.message) for item in caught)


def test_failed_naked_batch_yield_does_not_resume_partial(tmp_path: Path) -> None:
    for _ in range(2):
        with pytest.warns(UserWarning, match="yielded without iterating ctx.resume"):
            with pytest.raises(RuntimeError, match="planned failure"):
                run(
                    NakedYieldBatchPipeline,
                    Config(),
                    args=Args(fail_after=0),
                    cli_out=tmp_path,
                )

    with pytest.warns(UserWarning, match="yielded without iterating ctx.resume"):
        result = run(NakedYieldBatchPipeline, Config(), cli_out=tmp_path)

    assert result[-1].status == "dirty"
    record = Store(_out(tmp_path)).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [(output.index, output.path) for output in record.outputs] == [(0, "part.txt")]
    assert Store(_out(tmp_path)).read_partial("transform", record.content_key) is None


def test_force_reruns_all_batch_items_after_partial(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="planned failure"):
        run(
            ToyPipeline,
            Config(),
            args=Args(fail_after=1),
            cli_out=tmp_path,
            upto="transform",
        )
    forced = run(ToyPipeline, Config(), cli_out=tmp_path, upto="transform", force=True)
    assert forced[-1].reason == "forced"
    assert len(list((_out(tmp_path) / "transform").glob("part-*.txt"))) == 4
    record = Store(_out(tmp_path)).read_success("transform")
    assert record is not None
    assert record.outputs is not None
    assert [output.index for output in record.outputs] == [0, 1, 2, 3]


def test_batch_multi_output_index_requires_all_paths(tmp_path: Path) -> None:
    first = run(MultiOutputBatchPipeline, Config(), cli_out=tmp_path)
    assert first[-1].status == "no-cache"
    second = run(MultiOutputBatchPipeline, Config(), cli_out=tmp_path)
    assert second[-1].status == "hit"

    (_out(tmp_path) / "1-right.txt").unlink()
    repaired = run(MultiOutputBatchPipeline, Config(), cli_out=tmp_path)
    assert repaired[-1].status == "artifact-missing"
    assert (_out(tmp_path) / "1-right.txt").exists()

    record = Store(_out(tmp_path)).read_success("split")
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
        run(CwdRelativeBatchPipeline, Config(), cli_out=Path("out"))


def test_single_produces_rejects_output_outside_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside the output root"):
        run(OutsideProducesPipeline, Config(), cli_out=tmp_path)


def test_ctx_input_requires_declared_need_during_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="declare it in needs"):
        run(MissingNeedsInputPipeline, Config(), cli_out=tmp_path)


class FileKeyConfig(BaseModel):
    pass


class FileKeyArgs(BaseModel):
    src: Path


class FileKeyPipeline(Pipeline):
    Config = FileKeyConfig
    Args = FileKeyArgs

    @classmethod
    def default_output_root(cls, config: FileKeyConfig) -> Path:
        return Path("varve-test-output")

    @stage(
        produces="copy.txt",
        key=KeySpec(files={"src": lambda ctx: ctx.args.src}),
    )
    def copy(self, ctx):
        (ctx.out / "copy.txt").write_text(
            ctx.args.src.read_text(encoding="utf-8"), encoding="utf-8"
        )


def test_hit_refreshes_touched_but_unchanged_file_fingerprint(tmp_path: Path) -> None:
    src = tmp_path / "input.txt"
    src.write_text("payload", encoding="utf-8")
    out = tmp_path / "work"
    config = FileKeyConfig()
    args = FileKeyArgs(src=src)

    first = run(FileKeyPipeline, config, args=args, cli_out=out)
    assert [outcome.status for outcome in first] == ["no-cache"]

    cached_mtime = _cached_src_mtime(_out(out))

    # Touch the file without changing its content: bump mtime, same bytes.
    new_mtime = cached_mtime + 100.0
    os.utime(src, (new_mtime, new_mtime))

    second = run(FileKeyPipeline, config, args=args, cli_out=out)
    assert [outcome.status for outcome in second] == ["hit"]

    refreshed_mtime = _cached_src_mtime(_out(out))
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
