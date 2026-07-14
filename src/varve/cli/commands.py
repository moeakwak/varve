"""Shared single-pipeline command services for both CLI frontends."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

from varve.cli.clean import clean
from varve.cli.review import render_source_review
from varve.cli.run import render_run_outcomes
from varve.cli.status import render_status, status_view
from varve.command import ResolvedCommandContext
from varve.engine.review import ReviewAction, SourceReviewResult
from varve.engine.run_display import StageOutcome
from varve.engine.runner import ReviewRequiredError, record_source_review, run
from varve.matrix import PipelineGraph
from varve.status import collect_pipeline_status
from varve.style import make_console


def render_plan(
    graph: PipelineGraph,
    *,
    upto: str | None,
    downstream: str | None,
    only: str | None,
) -> int:
    selected = graph.selected(upto=upto, downstream=downstream, only=only)
    print(" -> ".join(name for name in graph.topo_order() if name in selected))
    return 0


def execute_review(
    context: ResolvedCommandContext,
    *,
    decision: ReviewAction,
    targets: tuple[str, ...] = (),
    **options: Any,
) -> SourceReviewResult:
    return record_source_review(
        context.pipeline,
        context.resolved.config,
        decision=decision,
        args=context.args,
        targets=targets,
        cli_out=context.resolved.output_base,
        branch=context.resolved.branch,
        is_temporary=context.resolved.is_temporary,
        axes=context.resolved.axes,
        graph=context.graph,
        **options,
    )


def execute_run(
    context: ResolvedCommandContext,
    **options: Any,
) -> list[StageOutcome]:
    return run(
        context.pipeline,
        context.resolved.config,
        args=context.args,
        cli_out=context.resolved.output_base,
        branch=context.resolved.branch,
        is_temporary=context.resolved.is_temporary,
        temporary_config=context.resolved.temporary_config,
        axes=context.resolved.axes,
        temporary_axes=context.resolved.temporary_axes,
        graph=context.graph,
        **options,
    )


def dispatch_command(
    context: ResolvedCommandContext,
    namespace: Any,
    *,
    confirm,
    review_targets: tuple[str, ...] = (),
    target_module: str | None = None,
) -> int:
    """Execute a parsed single-pipeline command from either CLI frontend."""

    if namespace.command == "status":
        console = make_console()
        loading = (
            console.status("Evaluating pipeline status…", spinner="dots")
            if console.is_terminal
            else nullcontext()
        )
        with loading:
            status = collect_pipeline_status(
                context,
                selector=namespace.stage,
                rehash=namespace.rehash,
            )
        render_status(
            console,
            status,
            view=status_view(status, expand=namespace.expand),
            target_module=target_module,
        )
        return 0
    if namespace.command == "run":
        display_mode = "expand" if namespace.expand else "compact" if namespace.compact else "auto"
        try:
            outcomes = execute_run(
                context,
                upto=namespace.upto,
                downstream=namespace.downstream,
                only=namespace.only,
                force=namespace.force,
                rehash=namespace.rehash,
                display_mode=display_mode,
                slices=tuple(getattr(namespace, "slice", ())),
            )
        except ReviewRequiredError as error:
            print(str(error), file=sys.stderr)
            return 2
        render_run_outcomes(make_console(), outcomes)
        return 0
    if namespace.command in {"reuse", "invalidate"}:
        result = execute_review(context, decision=namespace.command, targets=review_targets)
        render_source_review(make_console(), result)
        return 0
    if namespace.command == "clean":
        clean(
            context.pipeline,
            context.resolved.config,
            cli_out=context.resolved.output_base,
            branch=context.resolved.branch,
            is_temporary=context.resolved.is_temporary,
            target=namespace.downstream,
            yes=namespace.yes,
            allowed_roots=(
                None
                if isinstance(context.resolved.config, SimpleNamespace)
                else context.pipeline.clean_roots(context.resolved.config)
            ),
            confirm=confirm,
            axes=context.resolved.axes,
            graph=context.graph,
        )
        return 0
    raise ValueError(f"Unknown command: {namespace.command}")
