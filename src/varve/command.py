"""Resolved command targets shared by varve frontends."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from varve.branch_config import ResolvedBranch
from varve.matrix import PipelineGraph, build_graph
from varve.pipeline import Pipeline


class ResolvedCommandContext(NamedTuple):
    """One fully resolved pipeline branch and its operational arguments."""

    pipeline: type[Pipeline]
    resolved: ResolvedBranch
    args: Any
    graph: PipelineGraph

    @property
    def output_root(self) -> Path:
        return self.pipeline.output_root(
            self.resolved.config,
            cli_out=self.resolved.output_base,
            branch=self.resolved.branch,
            is_temporary=self.resolved.is_temporary,
        )

    def runner_kwargs(self, *, temporary_state: bool = False) -> dict[str, Any]:
        options = {
            "pipeline": self.pipeline,
            "config": self.resolved.config,
            "args": self.args,
            "cli_out": self.resolved.output_base,
            "branch": self.resolved.branch,
            "is_temporary": self.resolved.is_temporary,
            "axes": self.resolved.axes,
            "graph": self.graph,
        }
        if temporary_state:
            options.update(
                temporary_config=self.resolved.temporary_config,
                temporary_axes=self.resolved.temporary_axes,
            )
        return options


def resolved_command_context(
    pipeline: type[Pipeline],
    resolved: ResolvedBranch,
    args: Any,
    *,
    graph: PipelineGraph | None = None,
) -> ResolvedCommandContext:
    """Construct a context without probing or mutating the selected store."""

    return ResolvedCommandContext(
        pipeline=pipeline,
        resolved=resolved,
        args=args,
        graph=graph or build_graph(pipeline, resolved.axes),
    )
