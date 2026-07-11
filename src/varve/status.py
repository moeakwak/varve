"""Structured, read-only pipeline status."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from varve.engine.runner import probe_pipeline
from varve.engine.state import Status
from varve.keying.dependencies import SourceDependencies
from varve.matrix import PipelineGraph, build_graph
from varve.models import FileFingerprint
from varve.pipeline import Pipeline

SourceChange = Literal["changed", "added", "removed"]


@dataclass(frozen=True)
class KeyInputs:
    config: dict[str, Any]
    files: dict[str, list[FileFingerprint]]
    values: dict[str, Any]
    upstreams: dict[str, dict[str, str]]


@dataclass(frozen=True)
class StageStatus:
    name: str
    kind: str
    needs: tuple[str, ...]
    status: Status
    reason: str
    duration: float | None
    decision_key: str | None
    stored_key: str | None
    key_inputs: KeyInputs | None
    source_dependencies: SourceDependencies
    source_changes: dict[str, SourceChange]
    unavailable_reason: str | None

    @property
    def direct_count(self) -> int:
        return len(set(self.source_dependencies.direct))

    @property
    def total_count(self) -> int:
        return len(reachable_identities(self.source_dependencies))

    @property
    def broad_count(self) -> int:
        reachable = reachable_identities(self.source_dependencies)
        return sum(
            self.source_dependencies.nodes[identity].scope is not None for identity in reachable
        )


@dataclass(frozen=True)
class PipelineStatus:
    pipeline: str
    branch: str
    output_root: Path
    stages: tuple[StageStatus, ...]


def reachable_identities(source: SourceDependencies) -> frozenset[str]:
    children: dict[str, list[str]] = defaultdict(list)
    for edge in source.edges:
        children[edge.parent].append(edge.child)
    seen: set[str] = set()
    stack = list(source.direct)
    while stack:
        identity = stack.pop()
        if identity in seen:
            continue
        seen.add(identity)
        stack.extend(children.get(identity, ()))
    return frozenset(seen)


def source_component_changes(
    old: Mapping[str, str],
    new: Mapping[str, str],
) -> dict[str, SourceChange]:
    changes: dict[str, SourceChange] = {}
    for name in sorted(set(old) | set(new)):
        if name not in old:
            changes[name] = "added"
        elif name not in new:
            changes[name] = "removed"
        elif old[name] != new[name]:
            changes[name] = "changed"
    return changes


def collect_pipeline_status(
    pipeline: type[Pipeline],
    config: Any,
    *,
    args: Any,
    out: Path,
    branch: str,
    stage: str | None = None,
    graph: PipelineGraph | None = None,
) -> PipelineStatus:
    """Collect decision keys and dependency descriptions without executing stages."""

    graph = graph or build_graph(pipeline)
    selected_names = None if stage is None else set(graph.names_for(stage))
    probes = probe_pipeline(pipeline, config, args=args, out=out, graph=graph)
    selected_probes = (
        probes
        if selected_names is None
        else tuple(probe for probe in probes if probe.stage in selected_names)
    )
    stages: list[StageStatus] = []
    for probe in selected_probes:
        spec = graph.stages[probe.stage]
        components = probe.components
        key_inputs = (
            None
            if components is None
            else KeyInputs(
                config=components.config,
                files=components.files,
                values=components.values,
                upstreams=components.upstreams,
            )
        )
        previous = probe.previous
        source_changes = (
            source_component_changes(previous.key_components.source, components.source)
            if probe.decision.status == "stale" and previous is not None and components is not None
            else {}
        )
        stages.append(
            StageStatus(
                name=probe.stage,
                kind=spec.kind,
                needs=spec.needs,
                status=probe.decision.status,
                reason=probe.decision.reason,
                duration=(
                    None
                    if probe.decision.status == "no-cache" or previous is None
                    else previous.elapsed
                ),
                decision_key=probe.decision_key,
                stored_key=previous.content_key if previous is not None else None,
                key_inputs=key_inputs,
                source_dependencies=probe.source_dependencies,
                source_changes=source_changes,
                unavailable_reason=probe.unavailable_reason,
            )
        )
    return PipelineStatus(
        pipeline=pipeline.__name__,
        branch=branch,
        output_root=out,
        stages=tuple(stages),
    )
