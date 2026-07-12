from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Pipeline, stage
from varve.engine.runner import probe_pipeline, run
from varve.store.store import Store


class Config(BaseModel):
    revision: int = 1
    payload: str = "same"


class ArtifactPipeline(Pipeline):
    Config = Config

    @stage(produces="upstream.txt")
    def upstream(self, ctx):
        _ = ctx.config.revision
        (ctx.out / "upstream.txt").write_text(ctx.config.payload, encoding="utf-8")

    @stage(needs="upstream", produces="downstream.txt")
    def downstream(self, ctx):
        (ctx.out / "downstream.txt").write_text(
            ctx.input("upstream").read_text(encoding="utf-8"), encoding="utf-8"
        )


class OrderedConfig(BaseModel):
    reverse: bool = False


class OrderedOutputsPipeline(Pipeline):
    Config = OrderedConfig

    @stage(produces=lambda ctx: ["b.txt", "a.txt"] if ctx.config.reverse else ["a.txt", "b.txt"])
    def upstream(self, ctx):
        (ctx.out / "a.txt").write_text("a", encoding="utf-8")
        (ctx.out / "b.txt").write_text("b", encoding="utf-8")

    @stage(needs="upstream", produces="order.txt")
    def downstream(self, ctx):
        (ctx.out / "order.txt").write_text(
            ",".join(path.name for path in ctx.inputs("upstream")),
            encoding="utf-8",
        )


def test_downstream_depends_on_upstream_artifact_content(tmp_path: Path) -> None:
    first = run(ArtifactPipeline, Config(), cli_out=tmp_path)
    assert [item.status for item in first] == ["needs-run", "needs-run"]

    same_output = run(ArtifactPipeline, Config(revision=2), cli_out=tmp_path)
    assert [item.status for item in same_output] == ["needs-run", "hit"]

    changed_output = run(ArtifactPipeline, Config(revision=3, payload="changed"), cli_out=tmp_path)
    assert [item.status for item in changed_output] == ["needs-run", "needs-run"]


def test_downstream_key_preserves_single_output_handle_order(tmp_path: Path) -> None:
    first = run(OrderedOutputsPipeline, OrderedConfig(), cli_out=tmp_path)
    assert [item.status for item in first] == ["needs-run", "needs-run"]

    reordered = run(
        OrderedOutputsPipeline,
        OrderedConfig(reverse=True),
        cli_out=tmp_path,
    )

    assert [item.status for item in reordered] == ["needs-run", "needs-run"]
    assert (tmp_path / "main" / "order.txt").read_text(encoding="utf-8") == "b.txt,a.txt"


def test_terminal_artifact_external_change_is_detected(tmp_path: Path) -> None:
    run(ArtifactPipeline, Config(), cli_out=tmp_path)
    output_root = tmp_path / "main"
    (output_root / "downstream.txt").write_text("tampered", encoding="utf-8")

    probe = probe_pipeline(
        ArtifactPipeline,
        Config(),
        args=ArtifactPipeline.Args(),
        out=output_root,
    )[-1]
    assert probe.decision.status == "needs-run"
    assert probe.decision.reason == "artifact-changed"


def test_external_upstream_artifact_change_propagates_to_downstream_input(
    tmp_path: Path,
) -> None:
    run(ArtifactPipeline, Config(), cli_out=tmp_path)
    output_root = tmp_path / "main"
    (output_root / "upstream.txt").write_text("changed", encoding="utf-8")

    probes = probe_pipeline(
        ArtifactPipeline,
        Config(),
        args=ArtifactPipeline.Args(),
        out=output_root,
    )

    assert probes[0].decision.reason == "artifact-changed"
    assert probes[1].decision.status == "needs-run"
    assert probes[1].decision.reason == "upstream 'upstream' changed"


def test_run_rehash_detects_artifact_change_hidden_by_restored_stat(tmp_path: Path) -> None:
    run(ArtifactPipeline, Config(), cli_out=tmp_path)
    artifact = tmp_path / "main" / "downstream.txt"
    original_stat = artifact.stat()
    artifact.write_text("evil", encoding="utf-8")
    os.utime(artifact, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    cached = run(ArtifactPipeline, Config(), cli_out=tmp_path)
    assert cached[-1].status == "hit"
    assert artifact.read_text(encoding="utf-8") == "evil"

    checked = run(ArtifactPipeline, Config(), cli_out=tmp_path, rehash=True)
    assert checked[-1].status == "needs-run"
    assert checked[-1].reason == "artifact-changed"
    assert artifact.read_text(encoding="utf-8") == "same"


class FailingPipeline(Pipeline):
    Config = Config

    @stage(produces="never.txt")
    def fail(self, ctx):
        raise RuntimeError("planned")


class InvalidArtifactPipeline(Pipeline):
    Config = Config

    @stage(produces="artifact.txt")
    def build(self, ctx):
        target = ctx.out / "target.txt"
        target.write_text("value", encoding="utf-8")
        (ctx.out / "artifact.txt").symlink_to(target)


def test_stage_exception_persists_failure_record(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="planned"):
        run(FailingPipeline, Config(), cli_out=tmp_path)

    store = Store(tmp_path / "main")
    failure = store.read_failure("fail")
    assert failure is not None
    assert failure.exception_type == "RuntimeError"
    assert failure.message == "planned"
    probe = probe_pipeline(
        FailingPipeline,
        Config(),
        args=FailingPipeline.Args(),
        out=tmp_path / "main",
    )[0]
    assert probe.decision.status == "failed"
    assert probe.failure == failure


def test_artifact_fingerprint_error_is_not_recorded_as_stage_failure(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Symlinks are not supported in managed artifacts"):
        run(InvalidArtifactPipeline, Config(), cli_out=tmp_path)

    store = Store(tmp_path / "main")
    assert store.read_failure("build") is None
    assert store.read_attempt("build") is not None
    probe = probe_pipeline(
        InvalidArtifactPipeline,
        Config(),
        args=InvalidArtifactPipeline.Args(),
        out=tmp_path / "main",
    )[0]
    assert probe.decision.status == "error"
    assert "Symlinks are not supported in managed artifacts" in probe.decision.reason
