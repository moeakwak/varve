"""Resolved command targets shared by varve frontends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from varve.branch_config import ResolvedBranch
from varve.matrix import PipelineGraph, build_graph
from varve.pipeline import Pipeline


@dataclass(frozen=True)
class ResolvedCommandContext:
    """One fully resolved pipeline branch and its operational arguments."""

    pipeline: type[Pipeline]
    resolved: ResolvedBranch
    args: Any
    output_root: Path
    graph: PipelineGraph


def resolved_command_context(
    pipeline: type[Pipeline],
    resolved: ResolvedBranch,
    args: Any,
    *,
    graph: PipelineGraph | None = None,
) -> ResolvedCommandContext:
    """Construct a context without probing or mutating the selected store."""

    output_root = pipeline.output_root(
        resolved.config,
        cli_out=resolved.output_base,
        branch=resolved.branch,
        is_temporary=resolved.is_temporary,
    )
    return ResolvedCommandContext(
        pipeline=pipeline,
        resolved=resolved,
        args=args,
        output_root=output_root,
        graph=graph or build_graph(pipeline, resolved.axes),
    )
