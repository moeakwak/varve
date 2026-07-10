"""Structured, read-only pipeline key details."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from varve.engine.runner import probe_pipeline
from varve.engine.state import Status
from varve.keying.dependencies import SourceDependencies
from varve.models import FileFingerprint
from varve.pipeline import Pipeline


@dataclass(frozen=True)
class KeyInputsDetails:
    config: dict[str, Any]
    files: dict[str, list[FileFingerprint]]
    values: dict[str, Any]
    upstreams: dict[str, dict[str, str]]


@dataclass(frozen=True)
class StageDetails:
    name: str
    kind: str
    needs: tuple[str, ...]
    status: Status
    reason: str
    decision_key: str | None
    stored_key: str | None
    key_inputs: KeyInputsDetails | None
    source_dependencies: SourceDependencies
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
class PipelineDetails:
    pipeline: str
    branch: str
    output_root: Path
    stages: tuple[StageDetails, ...]


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


def collect_pipeline_details(
    pipeline: type[Pipeline],
    config: Any,
    *,
    args: Any,
    out: Path,
    branch: str,
    stage: str | None = None,
) -> PipelineDetails:
    """Collect decision keys and dependency descriptions without executing stages."""

    if stage is not None and stage not in pipeline.stages():
        raise ValueError(f"Unknown varve stage: {stage}")
    probes = probe_pipeline(pipeline, config, args=args, out=out)
    selected_probes = (
        probes if stage is None else tuple(probe for probe in probes if probe.stage == stage)
    )
    stages: list[StageDetails] = []
    for probe in selected_probes:
        spec = pipeline.stages()[probe.stage]
        components = probe.components
        key_inputs = (
            None
            if components is None
            else KeyInputsDetails(
                config=components.config,
                files=components.files,
                values=components.values,
                upstreams=components.upstreams,
            )
        )
        stages.append(
            StageDetails(
                name=probe.stage,
                kind=spec.kind,
                needs=spec.needs,
                status=probe.decision.status,
                reason=probe.decision.reason,
                decision_key=probe.decision_key,
                stored_key=(probe.previous.content_key if probe.previous is not None else None),
                key_inputs=key_inputs,
                source_dependencies=probe.source_dependencies,
                unavailable_reason=probe.unavailable_reason,
            )
        )
    return PipelineDetails(
        pipeline=pipeline.__name__,
        branch=branch,
        output_root=out,
        stages=tuple(stages),
    )
