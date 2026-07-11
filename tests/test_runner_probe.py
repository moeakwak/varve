"""Tests for reusable read-only runner probes."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Axis, KeySpec, Pipeline, batch_stage, matrix, stage
from varve.engine import runner as runner_module
from varve.engine.runner import evaluate_state, probe_pipeline, run
from varve.keying.dependencies import SourceDependencies
from varve.keying.fingerprint import FingerprintSession, file_fingerprint
from varve.keying.keys import content_key
from varve.models import AttemptMarker
from varve.store.store import Store


class Config(BaseModel):
    profile: str = "a"
    limit: int = 1


class ProbePipeline(Pipeline):
    Config = Config

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.profile, encoding="utf-8")


def downstream_helper(value: str) -> str:
    return value.upper()


class DownstreamPipeline(Pipeline):
    Config = Config

    @stage(produces="upstream.txt")
    def upstream(self, ctx):
        (ctx.out / "upstream.txt").write_text(ctx.config.profile, encoding="utf-8")

    @stage(needs="upstream", produces="downstream.txt")
    def downstream(self, ctx):
        value = downstream_helper(ctx.config.profile)
        (ctx.out / "downstream.txt").write_text(value, encoding="utf-8")


class MissingUpstreamStrictUsesPipeline(Pipeline):
    Config = Config

    @stage()
    def upstream(self, ctx):  # pragma: no cover - inspected only
        return ctx.config

    @stage(needs="upstream", uses=[len])
    def downstream(self, ctx):  # pragma: no cover - inspected only
        return len(str(ctx.config))


CELL = Axis("cell", ["a", "b", "c"])


class MatrixProbePipeline(Pipeline):
    Config = Config

    @matrix(CELL)
    @stage()
    def shared(self, ctx, *, cell: str):  # pragma: no cover - inspected only
        return ctx.config.profile, cell

    @stage()
    def distinct(self, ctx):  # pragma: no cover - inspected only
        return ctx.config.limit


class FileInputArgs(BaseModel):
    source: Path


class ExternalValidationSnapshotPipeline(Pipeline):
    Config = Config
    Args = FileInputArgs

    @stage(produces="upstream.txt")
    def upstream(self, ctx):
        (ctx.out / "upstream.txt").write_text("stable", encoding="utf-8")

    @stage(needs="upstream", produces="mutated.txt")
    def mutate(self, ctx):
        ctx.args.source.write_text(ctx.config.profile, encoding="utf-8")
        (ctx.out / "mutated.txt").write_text(ctx.config.profile, encoding="utf-8")

    @stage(
        needs="mutate",
        produces="consumer.txt",
        key=KeySpec(files={"source": lambda ctx: ctx.args.source}),
    )
    def consumer(self, ctx):
        (ctx.out / "consumer.txt").write_text(
            ctx.args.source.read_text(encoding="utf-8"), encoding="utf-8"
        )


class ScopedExternalValidationPipeline(Pipeline):
    Config = Config
    Args = FileInputArgs

    @stage(key=KeySpec(files={"missing": lambda ctx: ctx.args.source}))
    def unrelated(self, ctx):  # pragma: no cover - must never be inspected by scoped runs
        pass

    @stage(produces="ancestor.txt")
    def ancestor(self, ctx):
        (ctx.out / "ancestor.txt").write_text(ctx.config.profile, encoding="utf-8")

    @stage(needs="ancestor", produces="external.txt")
    def external(self, ctx):
        (ctx.out / "external.txt").write_text(
            ctx.input("ancestor").read_text(encoding="utf-8"), encoding="utf-8"
        )

    @stage(needs="external", produces="target.txt")
    def target(self, ctx):
        (ctx.out / "target.txt").write_text(
            ctx.input("external").read_text(encoding="utf-8"), encoding="utf-8"
        )


MUTATION_ROLE = Axis("role", ["mutator", "consumer"])
HIT_CELL = Axis("cell", [str(index) for index in range(97)])


class FileMutationArgs(BaseModel):
    source: Path
    trigger: Path


def _replace_source_when_triggered(ctx) -> None:
    if not ctx.args.trigger.exists():
        return
    previous_mtime = ctx.args.source.stat().st_mtime
    ctx.args.source.write_text("B", encoding="utf-8")
    os.utime(ctx.args.source, (previous_mtime + 1, previous_mtime + 1))


class MatrixFileMutationPipeline(Pipeline):
    Config = Config
    Args = FileMutationArgs

    @matrix(MUTATION_ROLE)
    @stage(
        produces="artifact.txt",
        key=KeySpec(files={"source": lambda ctx: ctx.args.source}),
    )
    def shared(self, ctx, *, role: str):
        if role == "mutator":
            _replace_source_when_triggered(ctx)
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "artifact.txt").write_text(
            ctx.args.source.read_text(encoding="utf-8"), encoding="utf-8"
        )


class BatchFileMutationPipeline(Pipeline):
    Config = Config
    Args = FileMutationArgs

    @batch_stage(key=KeySpec(files={"source": lambda ctx: ctx.args.source}))
    async def mutate(self, ctx):
        async for _, _ in ctx.resume([None], progress=False):
            _replace_source_when_triggered(ctx)
            artifact = ctx.out / "mutated.txt"
            artifact.write_text(ctx.args.source.read_text(encoding="utf-8"), encoding="utf-8")
            yield artifact

    @stage(
        needs="mutate",
        produces="consumer.txt",
        key=KeySpec(files={"source": lambda ctx: ctx.args.source}),
    )
    def consumer(self, ctx):
        (ctx.out / "consumer.txt").write_text(
            ctx.args.source.read_text(encoding="utf-8"), encoding="utf-8"
        )


class MatrixFileHitPipeline(Pipeline):
    Config = Config
    Args = FileInputArgs

    @matrix(HIT_CELL)
    @stage(
        produces="artifact.txt",
        key=KeySpec(files={"source": lambda ctx: ctx.args.source}),
    )
    def shared(self, ctx, *, cell: str):
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "artifact.txt").write_text(cell, encoding="utf-8")


def test_probe_uses_previous_config_access_for_decision_key(tmp_path: Path) -> None:
    run(ProbePipeline, Config(profile="a", limit=1), cli_out=tmp_path)

    probe = probe_pipeline(
        ProbePipeline,
        Config(profile="a", limit=99),
        args=ProbePipeline.Args(),
        out=tmp_path / "main",
    )[0]

    assert probe.components is not None
    assert probe.components.config_access == ["profile"]
    assert probe.decision_key == content_key(probe.components)


def test_probe_without_upstream_record_keeps_source_dependencies(tmp_path: Path) -> None:
    probes = probe_pipeline(
        DownstreamPipeline,
        Config(),
        args=DownstreamPipeline.Args(),
        out=tmp_path / "main",
    )

    downstream = next(item for item in probes if item.stage == "downstream")
    assert downstream.decision.status == "no-cache"
    assert downstream.decision_key is None
    assert downstream.components is None
    assert downstream.source_dependencies.direct
    assert downstream.unavailable_reason == "upstream upstream has no success record"


def test_status_missing_upstream_short_circuits_strict_uses(tmp_path: Path) -> None:
    outcomes = evaluate_state(
        MissingUpstreamStrictUsesPipeline,
        Config(),
        downstream="downstream",
        cli_out=tmp_path,
    )
    assert [(outcome.stage, outcome.status) for outcome in outcomes] == [("downstream", "no-cache")]


def test_status_probe_missing_upstream_still_validates_strict_uses(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        probe_pipeline(
            MissingUpstreamStrictUsesPipeline,
            Config(),
            args=MissingUpstreamStrictUsesPipeline.Args(),
            out=tmp_path / "main",
        )


def test_probe_shares_matrix_template_source_discovery_but_not_distinct_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    original = runner_module.compute_source_dependencies

    def counted(stage_spec, *, auto_uses_packages=None):
        calls.append(stage_spec.func)
        return original(stage_spec, auto_uses_packages=auto_uses_packages)

    monkeypatch.setattr(runner_module, "compute_source_dependencies", counted)

    probes = probe_pipeline(
        MatrixProbePipeline,
        Config(),
        args=MatrixProbePipeline.Args(),
        out=tmp_path / "main",
    )

    assert len(probes) == 4
    assert calls.count(MatrixProbePipeline.shared) == 1
    assert calls.count(MatrixProbePipeline.distinct) == 1


def test_source_discovery_cache_isolates_every_discovery_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def counted(stage_spec, *, auto_uses_packages=None):
        calls.append(
            (
                stage_spec.func,
                stage_spec.uses,
                stage_spec.auto_uses,
                auto_uses_packages,
            )
        )
        return SourceDependencies(components={}, nodes={}, edges=(), direct=())

    monkeypatch.setattr(runner_module, "compute_source_dependencies", counted)
    template = MatrixProbePipeline.stages()["shared"]
    session = runner_module._KeyingSession()

    first = session.source_dependencies(template, auto_uses_packages=("package",))
    assert session.source_dependencies(template, auto_uses_packages=("package",)) is first
    first_fingerprints = session.fingerprints
    session.refresh_fingerprints()
    assert session.fingerprints is not first_fingerprints
    assert session.source_dependencies(template, auto_uses_packages=("package",)) is first
    session.source_dependencies(
        replace(template, func=MatrixProbePipeline.distinct), auto_uses_packages=("package",)
    )
    session.source_dependencies(replace(template, uses=(len,)), auto_uses_packages=("package",))
    session.source_dependencies(replace(template, auto_uses=False), auto_uses_packages=("package",))
    session.source_dependencies(template, auto_uses_packages=("other",))

    assert len(calls) == 5


def test_external_validation_does_not_freeze_execution_file_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("old", encoding="utf-8")
    args = FileInputArgs(source=source)
    run(ExternalValidationSnapshotPipeline, Config(profile="old"), args=args, cli_out=tmp_path)
    first = Store(tmp_path / "main").read_success("consumer")
    assert first is not None

    run(
        ExternalValidationSnapshotPipeline,
        Config(profile="new"),
        args=args,
        cli_out=tmp_path,
        downstream="mutate",
    )

    current = file_fingerprint(source)
    second = Store(tmp_path / "main").read_success("consumer")
    assert second is not None
    assert second.key_components.files["source"][0].sha256 == current.sha256
    assert (
        second.key_components.files["source"][0].sha256
        != first.key_components.files["source"][0].sha256
    )


def test_external_validation_probes_only_the_external_ancestor_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "does-not-exist.txt"
    args = FileInputArgs(source=missing)
    run(
        ScopedExternalValidationPipeline,
        Config(),
        args=args,
        cli_out=tmp_path,
        upto="external",
    )

    probed: list[str] = []
    discovered: list[str] = []
    original = runner_module._probe_stage
    original_discovery = runner_module.compute_source_dependencies

    def counted(*call_args, **call_kwargs):
        result = original(*call_args, **call_kwargs)
        probed.append(result.stage)
        return result

    def counted_discovery(stage_spec, *, auto_uses_packages=None):
        discovered.append(stage_spec.name)
        return original_discovery(stage_spec, auto_uses_packages=auto_uses_packages)

    monkeypatch.setattr(runner_module, "_probe_stage", counted)
    monkeypatch.setattr(runner_module, "compute_source_dependencies", counted_discovery)

    outcomes = run(
        ScopedExternalValidationPipeline,
        Config(),
        args=args,
        cli_out=tmp_path,
        only="target",
    )

    assert probed == ["ancestor", "external"]
    assert discovered == ["ancestor", "external", "target"]
    assert [(outcome.stage, outcome.status) for outcome in outcomes] == [("target", "no-cache")]

    with pytest.raises(FileNotFoundError):
        probe_pipeline(
            ScopedExternalValidationPipeline,
            Config(),
            args=args,
            out=tmp_path / "main",
        )


@pytest.mark.parametrize(
    ("ancestor_state", "expected_status"),
    [
        ("dirty", "dirty"),
        ("stale", "stale"),
        ("missing-record", "no-cache"),
        ("missing-artifact", "artifact-missing"),
    ],
)
def test_external_validation_rejects_a_non_current_recursive_ancestor(
    tmp_path: Path,
    ancestor_state: str,
    expected_status: str,
) -> None:
    args = FileInputArgs(source=tmp_path / "unrelated-missing.txt")
    run(
        ScopedExternalValidationPipeline,
        Config(profile="old"),
        args=args,
        cli_out=tmp_path,
        upto="external",
    )
    out = tmp_path / "main"
    store = Store(out)
    config = Config(profile="old")
    if ancestor_state == "dirty":
        record = store.read_success("ancestor")
        assert record is not None
        store.write_attempt(
            "ancestor",
            AttemptMarker(
                content_key=record.content_key,
                started_at="test",
                touched_existing=True,
            ),
        )
    elif ancestor_state == "stale":
        config = Config(profile="new")
    elif ancestor_state == "missing-record":
        (store.root / "stages" / "ancestor.json").unlink()
    else:
        assert ancestor_state == "missing-artifact"
        (out / "ancestor.txt").unlink()

    with pytest.raises(
        ValueError,
        match=rf"Upstream stage is not current: ancestor \({expected_status}:",
    ):
        run(
            ScopedExternalValidationPipeline,
            config,
            args=args,
            cli_out=tmp_path,
            only="target",
        )


def test_scoped_probe_propagates_current_ancestor_decision_keys(tmp_path: Path) -> None:
    args = FileInputArgs(source=tmp_path / "unrelated-missing.txt")
    run(
        ScopedExternalValidationPipeline,
        Config(profile="old"),
        args=args,
        cli_out=tmp_path,
        upto="external",
    )

    probes = probe_pipeline(
        ScopedExternalValidationPipeline,
        Config(profile="new"),
        args=args,
        out=tmp_path / "main",
        _stage_names={"ancestor", "external"},
    )

    assert [(probe.stage, probe.decision.status) for probe in probes] == [
        ("ancestor", "stale"),
        ("external", "stale"),
    ]
    assert probes[1].decision.reason == "upstream 'ancestor' changed"


def test_executed_matrix_cell_refreshes_file_inputs_for_later_cells(tmp_path: Path) -> None:
    source = tmp_path / "shared.txt"
    trigger = tmp_path / "mutate"
    source.write_text("A", encoding="utf-8")
    args = FileMutationArgs(source=source, trigger=trigger)

    first = run(MatrixFileMutationPipeline, Config(), args=args, cli_out=tmp_path)
    assert [outcome.status for outcome in first] == ["no-cache", "no-cache"]

    mutator_artifact = tmp_path / "main" / ".matrix" / "shared" / "role=mutator" / "artifact.txt"
    mutator_artifact.unlink()
    trigger.touch()

    second = run(MatrixFileMutationPipeline, Config(), args=args, cli_out=tmp_path)

    assert [outcome.status for outcome in second] == ["artifact-missing", "stale"]
    consumer_artifact = tmp_path / "main" / ".matrix" / "shared" / "role=consumer" / "artifact.txt"
    assert consumer_artifact.read_text(encoding="utf-8") == "B"


def test_executed_batch_refreshes_file_inputs_for_later_stages(tmp_path: Path) -> None:
    source = tmp_path / "shared.txt"
    trigger = tmp_path / "mutate"
    source.write_text("A", encoding="utf-8")
    args = FileMutationArgs(source=source, trigger=trigger)

    first = run(BatchFileMutationPipeline, Config(), args=args, cli_out=tmp_path)
    assert [outcome.status for outcome in first] == ["no-cache", "no-cache"]

    (tmp_path / "main" / "mutated.txt").unlink()
    trigger.touch()

    second = run(BatchFileMutationPipeline, Config(), args=args, cli_out=tmp_path)

    assert [outcome.status for outcome in second] == ["artifact-missing", "stale"]
    assert (tmp_path / "main" / "consumer.txt").read_text(encoding="utf-8") == "B"


def test_all_hit_matrix_cells_share_one_fingerprint_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "shared.txt"
    source.write_text("stable", encoding="utf-8")
    args = FileInputArgs(source=source)
    run(MatrixFileHitPipeline, Config(), args=args, cli_out=tmp_path)

    sessions: list[FingerprintSession] = []
    original = FingerprintSession.fingerprint

    def counted(self, path, cached=None, *, cached_by_path=None):
        sessions.append(self)
        return original(self, path, cached, cached_by_path=cached_by_path)

    monkeypatch.setattr(FingerprintSession, "fingerprint", counted)

    outcomes = run(MatrixFileHitPipeline, Config(), args=args, cli_out=tmp_path)

    assert len(outcomes) == 97
    assert {outcome.status for outcome in outcomes} == {"hit"}
    assert len(sessions) == 97
    assert all(session is sessions[0] for session in sessions)


def test_probe_reads_each_success_record_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run(MatrixProbePipeline, Config(), args=MatrixProbePipeline.Args(), cli_out=tmp_path)
    reads: dict[str, int] = {}
    original = Store.read_success

    def counted(self, stage):
        reads[stage] = reads.get(stage, 0) + 1
        return original(self, stage)

    monkeypatch.setattr(Store, "read_success", counted)
    probes = probe_pipeline(
        MatrixProbePipeline,
        Config(),
        args=MatrixProbePipeline.Args(),
        out=tmp_path / "main",
    )

    assert len(probes) == 4
    assert reads == {stage: 1 for stage in MatrixProbePipeline.graph().topo_order()}
