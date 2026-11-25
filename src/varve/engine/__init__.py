"""Execution engine: probe, scheduling, unit lifecycle, and in-process services."""

from varve.engine.refs import NoRefResolver, RefBinding, RefResolutionError, RefResolver
from varve.engine.run import RunOptions, RunResult, StageOutcome
from varve.engine.service import (
    InspectResult,
    ReuseResult,
    clean_pipeline,
    inspect_pipeline,
    plan_clean,
    probe_pipeline,
    reuse_stage,
    run_pipeline,
)
from varve.engine.session import WorkspaceSettings

__all__ = [
    "InspectResult",
    "NoRefResolver",
    "RefBinding",
    "RefResolutionError",
    "RefResolver",
    "ReuseResult",
    "RunOptions",
    "RunResult",
    "StageOutcome",
    "WorkspaceSettings",
    "clean_pipeline",
    "inspect_pipeline",
    "plan_clean",
    "probe_pipeline",
    "reuse_stage",
    "run_pipeline",
]
