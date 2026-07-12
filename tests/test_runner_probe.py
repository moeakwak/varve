"""Tests for reusable read-only runner probes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Axis, Dependencies, Pipeline, batch_stage, matrix, stage
from varve.engine import runner as runner_module
from varve.engine.runner import _KeyingSession, evaluate_state, probe_pipeline, run
from varve.keying.fingerprint import FingerprintSession, file_fingerprint
from varve.keying.keys import input_key
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

    @stage(needs="upstream")
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


class FileInputConfig(Config):
    source: Path


class ExternalValidationSnapshotPipeline(Pipeline):
    Config = FileInputConfig

    @stage(produces="upstream.txt")
    def upstream(self, ctx):
        (ctx.out / "upstream.txt").write_text("stable", encoding="utf-8")

    @stage(needs="upstream", produces="mutated.txt")
    def mutate(self, ctx):
        ctx.config.source.write_text(ctx.config.profile, encoding="utf-8")
        (ctx.out / "mutated.txt").write_text(ctx.config.profile, encoding="utf-8")

    @stage(
        needs="mutate",
        produces="consumer.txt",
        depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}),
    )
    def consumer(self, ctx):
        (ctx.out / "consumer.txt").write_text(
            ctx.config.source.read_text(encoding="utf-8"), encoding="utf-8"
        )


class ScopedExternalValidationPipeline(Pipeline):
    Config = FileInputConfig

    @stage(depends=Dependencies(inputs={"missing": lambda ctx: ctx.config.source}))
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


class FileMutationConfig(Config):
    source: Path
    trigger: Path


def _replace_source_when_triggered(ctx) -> None:
    if not ctx.config.trigger.exists():
        return
    previous_mtime = ctx.config.source.stat().st_mtime
    ctx.config.source.write_text("B", encoding="utf-8")
    os.utime(ctx.config.source, (previous_mtime + 1, previous_mtime + 1))


class MatrixFileMutationPipeline(Pipeline):
    Config = FileMutationConfig

    @matrix(MUTATION_ROLE)
    @stage(
        produces="artifact.txt",
        depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}),
    )
    def shared(self, ctx, *, role: str):
        if role == "mutator":
            _replace_source_when_triggered(ctx)
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "artifact.txt").write_text(
            ctx.config.source.read_text(encoding="utf-8"), encoding="utf-8"
        )


class BatchFileMutationPipeline(Pipeline):
    Config = FileMutationConfig

    @batch_stage(depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}))
    async def mutate(self, ctx):
        async for _, _ in ctx.resume([None], progress=False):
            _replace_source_when_triggered(ctx)
            artifact = ctx.out / "mutated.txt"
            artifact.write_text(ctx.config.source.read_text(encoding="utf-8"), encoding="utf-8")
            yield artifact

    @stage(
        needs="mutate",
        produces="consumer.txt",
        depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}),
    )
    def consumer(self, ctx):
        (ctx.out / "consumer.txt").write_text(
            ctx.config.source.read_text(encoding="utf-8"), encoding="utf-8"
        )


class MatrixFileHitPipeline(Pipeline):
    Config = FileInputConfig

    @matrix(HIT_CELL)
    @stage(
        produces="artifact.txt",
        depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}),
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
    assert probe.decision_key == input_key(probe.components)


def test_probe_without_upstream_record_keeps_source_fingerprint(tmp_path: Path) -> None:
    probes = probe_pipeline(
        DownstreamPipeline,
        Config(),
        args=DownstreamPipeline.Args(),
        out=tmp_path / "main",
    )

    downstream = next(item for item in probes if item.stage == "downstream")
    assert downstream.decision.status == "needs-run"
    assert downstream.decision.reason == "no-cache"
    assert downstream.decision_key is None
    assert downstream.components is None
    assert downstream.source_fingerprint.files
    assert downstream.unavailable_reason == "upstream upstream has no success record"


def test_status_missing_upstream_is_needs_run(tmp_path: Path) -> None:
    outcomes = evaluate_state(
        MissingUpstreamStrictUsesPipeline,
        Config(),
        downstream="downstream",
        cli_out=tmp_path,
    )
    assert [(outcome.stage, outcome.status) for outcome in outcomes] == [
        ("downstream", "needs-run")
    ]


def test_probe_shares_matrix_template_source_fingerprint(tmp_path: Path) -> None:
    probes = probe_pipeline(
        MatrixProbePipeline,
        Config(),
        args=MatrixProbePipeline.Args(),
        out=tmp_path / "main",
    )

    assert len(probes) == 4
    assert probes[0].source_fingerprint is probes[1].source_fingerprint
    assert probes[1].source_fingerprint is probes[2].source_fingerprint
    assert probes[2].source_fingerprint is not probes[3].source_fingerprint


def test_external_validation_does_not_freeze_execution_file_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("old", encoding="utf-8")
    config = FileInputConfig(profile="old", source=source)
    run(ExternalValidationSnapshotPipeline, config, cli_out=tmp_path)
    first = Store(tmp_path / "main").read_success("consumer")
    assert first is not None

    run(
        ExternalValidationSnapshotPipeline,
        FileInputConfig(profile="new", source=source),
        cli_out=tmp_path,
        downstream="mutate",
    )

    current = file_fingerprint(source)
    second = Store(tmp_path / "main").read_success("consumer")
    assert second is not None
    assert second.key_components.inputs["source"][0].content_hash == current.content_hash
    assert (
        second.key_components.inputs["source"][0].content_hash
        != first.key_components.inputs["source"][0].content_hash
    )


def test_external_validation_probes_only_the_external_ancestor_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "does-not-exist.txt"
    config = FileInputConfig(source=missing)
    run(
        ScopedExternalValidationPipeline,
        config,
        cli_out=tmp_path,
        upto="external",
    )

    probed: list[str] = []
    original = runner_module._probe_stage

    def counted(*call_args, **call_kwargs):
        result = original(*call_args, **call_kwargs)
        probed.append(result.stage)
        return result

    monkeypatch.setattr(runner_module, "_probe_stage", counted)

    outcomes = run(
        ScopedExternalValidationPipeline,
        config,
        cli_out=tmp_path,
        only="target",
    )

    assert probed == ["ancestor", "external", "ancestor", "external", "target"]
    assert [(outcome.stage, outcome.status) for outcome in outcomes] == [("target", "needs-run")]

    probes = probe_pipeline(
        ScopedExternalValidationPipeline,
        config,
        args=ScopedExternalValidationPipeline.Args(),
        out=tmp_path / "main",
    )
    assert probes[0].stage == "unrelated"
    assert probes[0].decision.status == "error"


@pytest.mark.parametrize("ancestor_state", ["dirty", "stale", "missing-record", "missing-artifact"])
def test_external_validation_rejects_a_non_current_recursive_ancestor(
    tmp_path: Path,
    ancestor_state: str,
) -> None:
    source = tmp_path / "unrelated-missing.txt"
    run(
        ScopedExternalValidationPipeline,
        FileInputConfig(profile="old", source=source),
        cli_out=tmp_path,
        upto="external",
    )
    out = tmp_path / "main"
    store = Store(out)
    config = FileInputConfig(profile="old", source=source)
    if ancestor_state == "dirty":
        record = store.read_success("ancestor")
        assert record is not None
        store.write_attempt(
            "ancestor",
            AttemptMarker(
                input_key=record.input_key,
                source_fingerprint=record.executed_source_fingerprint.fingerprint,
                started_at="test",
                touched_existing=True,
            ),
        )
    elif ancestor_state == "stale":
        config = FileInputConfig(profile="new", source=source)
    elif ancestor_state == "missing-record":
        (store.root / "stages" / "ancestor.json").unlink()
    else:
        assert ancestor_state == "missing-artifact"
        (out / "ancestor.txt").unlink()

    with pytest.raises(ValueError):
        run(
            ScopedExternalValidationPipeline,
            config,
            cli_out=tmp_path,
            only="target",
        )


def test_scoped_probe_propagates_current_ancestor_decision_keys(tmp_path: Path) -> None:
    source = tmp_path / "unrelated-missing.txt"
    run(
        ScopedExternalValidationPipeline,
        FileInputConfig(profile="old", source=source),
        cli_out=tmp_path,
        upto="external",
    )

    probes = probe_pipeline(
        ScopedExternalValidationPipeline,
        FileInputConfig(profile="new", source=source),
        args=ScopedExternalValidationPipeline.Args(),
        out=tmp_path / "main",
        _stage_names={"ancestor", "external"},
    )

    assert [(probe.stage, probe.decision.status) for probe in probes] == [
        ("ancestor", "needs-run"),
        ("external", "hit"),
    ]
    assert probes[1].decision.reason == "hit"


def test_executed_matrix_cell_refreshes_file_inputs_for_later_cells(tmp_path: Path) -> None:
    source = tmp_path / "shared.txt"
    trigger = tmp_path / "mutate"
    source.write_text("A", encoding="utf-8")
    config = FileMutationConfig(source=source, trigger=trigger)

    first = run(MatrixFileMutationPipeline, config, cli_out=tmp_path)
    assert [outcome.status for outcome in first] == ["needs-run", "needs-run"]

    mutator_artifact = tmp_path / "main" / ".matrix" / "shared" / "role=mutator" / "artifact.txt"
    mutator_artifact.unlink()
    trigger.touch()

    second = run(MatrixFileMutationPipeline, config, cli_out=tmp_path)

    assert [outcome.status for outcome in second] == ["needs-run", "needs-run"]
    consumer_artifact = tmp_path / "main" / ".matrix" / "shared" / "role=consumer" / "artifact.txt"
    assert consumer_artifact.read_text(encoding="utf-8") == "B"


def test_executed_batch_refreshes_file_inputs_for_later_stages(tmp_path: Path) -> None:
    source = tmp_path / "shared.txt"
    trigger = tmp_path / "mutate"
    source.write_text("A", encoding="utf-8")
    config = FileMutationConfig(source=source, trigger=trigger)

    first = run(BatchFileMutationPipeline, config, cli_out=tmp_path)
    assert [outcome.status for outcome in first] == ["needs-run", "needs-run"]

    (tmp_path / "main" / "mutated.txt").unlink()
    trigger.touch()

    second = run(BatchFileMutationPipeline, config, cli_out=tmp_path)

    assert [outcome.status for outcome in second] == ["needs-run", "needs-run"]
    assert (tmp_path / "main" / "consumer.txt").read_text(encoding="utf-8") == "B"


def test_all_hit_matrix_cells_share_one_fingerprint_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "shared.txt"
    source.write_text("stable", encoding="utf-8")
    config = FileInputConfig(source=source)
    run(MatrixFileHitPipeline, config, cli_out=tmp_path)

    sessions: list[FingerprintSession] = []
    original = FingerprintSession.fingerprint

    def counted(self, path, cached=None, *, cached_by_path=None, force_rehash=False):
        sessions.append(self)
        return original(
            self,
            path,
            cached,
            cached_by_path=cached_by_path,
            force_rehash=force_rehash,
        )

    monkeypatch.setattr(FingerprintSession, "fingerprint", counted)

    outcomes = run(MatrixFileHitPipeline, config, cli_out=tmp_path)

    assert len(outcomes) == 97
    assert {outcome.status for outcome in outcomes} == {"hit"}
    assert len(sessions) >= 97
    assert len({id(session) for session in sessions}) == 2


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


def test_probe_reports_old_store_schema_without_rewriting_manifest(tmp_path: Path) -> None:
    store = Store(tmp_path / "main")
    store.root.mkdir(parents=True)
    manifest_path = store.root / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "pipeline": "ProbePipeline",
                "module": __name__,
                "temporary_config": None,
            }
        ),
        encoding="utf-8",
    )
    before = manifest_path.read_bytes()

    probe = probe_pipeline(
        ProbePipeline,
        Config(),
        args=ProbePipeline.Args(),
        out=tmp_path / "main",
    )[0]

    assert probe.decision.status == "needs-run"
    assert probe.decision.reason == "schema-migration"
    assert probe.unavailable_reason == "store schema 4 must be rebuilt as schema 5"
    assert manifest_path.read_bytes() == before


def test_preflight_evaluation_error_does_not_create_attempt_or_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("value", encoding="utf-8")
    config = FileInputConfig(source=source)
    run(MatrixFileHitPipeline, config, cli_out=tmp_path)
    source.unlink()

    with pytest.raises(ValueError, match="Cannot evaluate selected stages"):
        run(MatrixFileHitPipeline, config, cli_out=tmp_path)

    store = Store(tmp_path / "main")
    for stage_name in MatrixFileHitPipeline.graph().stages:
        assert store.read_attempt(stage_name) is None
        assert store.read_failure(stage_name) is None


def test_refresh_observations_discards_source_snapshot() -> None:
    session = _KeyingSession()
    source_session = session.sources

    session.refresh_observations()

    assert session.sources is not source_session
