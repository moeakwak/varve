"""Resolve discovered targets and load their canonical exact status."""

from __future__ import annotations

import importlib
from typing import Any

from varve.branch_config import ResolvedBranch, resolve_branch
from varve.command import ResolvedCommandContext, resolved_command_context
from varve.dashboard.models import ErrorPhase, PipelineEntry, PipelineState, StateError
from varve.engine.runner import _KeyingSession
from varve.matrix import PipelineGraph, build_graph
from varve.pipeline import Pipeline
from varve.status import collect_pipeline_status


def load_state(entry: PipelineEntry, session: _KeyingSession | None = None) -> PipelineState:
    """Load one branch through the shared exact status collector."""

    if entry.manifest_error:
        return _error(entry, "manifest", entry.manifest_error)
    if entry.pipeline_name is None:
        return _error(entry, "manifest", "Manifest is missing pipeline")
    if entry.module is None:
        return _error(entry, "manifest", "Manifest is missing module")

    try:
        pipeline = import_entry_pipeline(entry)
    except Exception as error:  # noqa: BLE001 - overview continues after entry failures.
        return _error(entry, "import", str(error))
    try:
        context = resolve_entry_context(entry, pipeline, pipeline.Args())
    except Exception as error:  # noqa: BLE001 - expose branch-resolution diagnostics.
        return _error(entry, "resolve", str(error))
    try:
        status = collect_pipeline_status(context, session=session)
    except Exception as error:  # noqa: BLE001 - overview continues after evaluation failures.
        return _error(entry, "evaluate", str(error))
    return PipelineState(entry=entry, pipeline_status=status)


def resolve_module_entry(
    entries: list[PipelineEntry],
    module: str,
    *,
    branch: str = "main",
) -> PipelineEntry:
    """Resolve one exact manifest MODULE and branch without importing candidates."""

    module_entries = [entry for entry in entries if entry.module == module]
    candidates = [entry for entry in module_entries if entry.branch == branch]
    if not candidates:
        available = sorted({entry.module for entry in entries if entry.module is not None})
        if module_entries:
            branches = sorted({entry.branch for entry in module_entries})
            raise ValueError(
                f"Unknown branch {branch!r} for module {module!r}. "
                f"Available branches: {', '.join(branches) or '(none)'}"
            )
        raise ValueError(
            f"Unknown module: {module}. Available modules: {', '.join(available) or '(none)'}"
        )
    if len(candidates) != 1:
        raise ValueError(_ambiguity(module, branch, candidates))
    entry = candidates[0]
    if entry.pipeline_name is None or entry.manifest_error is not None:
        raise ValueError(_ambiguity(module, branch, candidates))
    return entry


def resolve_structure_pipeline(
    entries: list[PipelineEntry],
    module: str,
) -> tuple[type[Pipeline], tuple[PipelineEntry, ...]]:
    """Resolve one branch-independent MODULE, deduplicating identical classes."""

    candidates = [entry for entry in entries if entry.module == module]
    if not candidates:
        available = sorted({entry.module for entry in entries if entry.module is not None})
        raise ValueError(
            f"Unknown module: {module}. Available modules: {', '.join(available) or '(none)'}"
        )
    class_names = {entry.pipeline_name for entry in candidates if entry.pipeline_name is not None}
    if len(class_names) != 1 or any(entry.manifest_error for entry in candidates):
        raise ValueError(_ambiguity(module, "all branches", candidates))
    representative = candidates[0]
    return import_entry_pipeline(representative), tuple(candidates)


def import_entry_pipeline(entry: PipelineEntry) -> type[Pipeline]:
    if entry.manifest_error:
        raise ValueError(entry.manifest_error)
    if entry.pipeline_name is None:
        raise ValueError("Manifest is missing pipeline")
    if entry.module is None:
        raise ValueError("Manifest is missing module")
    module = importlib.import_module(entry.module)
    value = getattr(module, entry.pipeline_name)
    if not isinstance(value, type) or not issubclass(value, Pipeline):
        raise TypeError(f"{entry.module}.{entry.pipeline_name} is not a varve Pipeline")
    return value


def resolve_entry_context(
    entry: PipelineEntry,
    pipeline: type[Pipeline],
    args: Any,
) -> ResolvedCommandContext:
    """Restore a discovered store's exact output identity as a shared context."""

    resolved, graph = resolve_entry_target(entry, pipeline)
    return resolved_command_context(
        pipeline,
        resolved,
        args,
        graph=graph,
    )


def resolve_entry_target(
    entry: PipelineEntry,
    pipeline: type[Pipeline],
) -> tuple[ResolvedBranch, PipelineGraph]:
    """Resolve only the branch, exact output identity, and graph."""

    resolved = resolve_branch(
        pipeline,
        branch=entry.branch,
        override_json=None,
        cli_out=(
            entry.output_root.parent.parent
            if entry.output_root.parent.name == ".tmp"
            else entry.output_root.parent
        ),
    )
    output_root = pipeline.output_root(
        resolved.config,
        cli_out=resolved.output_base,
        branch=resolved.branch,
        is_temporary=resolved.is_temporary,
    )
    if output_root.resolve() != entry.output_root.resolve():
        raise ValueError(
            f"Resolved output root {output_root} does not match manifest anchor {entry.output_root}"
        )
    return resolved, build_graph(pipeline, resolved.axes)


def _ambiguity(module: str, branch: str, candidates: list[PipelineEntry]) -> str:
    details = "; ".join(
        f"class={entry.pipeline_name or '(unknown)'}, branch={entry.branch}, "
        f"output={entry.output_root}"
        for entry in candidates
    )
    return f"Ambiguous module {module!r} ({branch}): {details}"


def _error(entry: PipelineEntry, phase: ErrorPhase, message: str) -> PipelineState:
    return PipelineState(entry=entry, error=StateError(phase=phase, message=message))
