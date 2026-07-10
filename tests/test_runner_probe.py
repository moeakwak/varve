"""Tests for reusable read-only runner probes."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Pipeline, stage
from varve.engine.runner import evaluate_state, probe_pipeline, run
from varve.keying.keys import content_key


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


def test_details_probe_missing_upstream_still_validates_strict_uses(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        probe_pipeline(
            MissingUpstreamStrictUsesPipeline,
            Config(),
            args=MissingUpstreamStrictUsesPipeline.Args(),
            out=tmp_path / "main",
        )
