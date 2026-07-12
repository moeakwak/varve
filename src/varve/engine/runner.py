"""Stage execution runner."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from varve.context import Ctx, StageDisplay
from varve.decorators import ProducesItem, ProducesSpec
from varve.dependencies import merge_dependencies
from varve.engine.run_display import (
    RunDisplayMode,
    RunReporter,
    StageOutcome,
    build_run_display_plan,
)
from varve.engine.state import Decision, decide_batch, decide_single
from varve.keying.config_access import ConfigAccess, RecordingConfig, project_config
from varve.keying.fingerprint import (
    FingerprintSession,
    artifact_fingerprint,
    artifacts_root_fingerprint,
)
from varve.keying.keys import (
    compute_key_components,
    config_data,
    input_key,
)
from varve.keying.source import SourceFingerprintSession
from varve.matrix import Cell, PipelineGraph, build_graph, cell_output_path
from varve.models import (
    SCHEMA_VERSION,
    ArtifactFingerprint,
    AttemptMarker,
    BatchRecord,
    FailureRecord,
    KeyComponents,
    OutputHandle,
    ProducedPath,
    ReviewRecord,
    SourceFingerprint,
    SuccessRecord,
)
from varve.pipeline import Pipeline
from varve.store.lock import OutputLock
from varve.store.store import Store


@dataclass(frozen=True)
class StageProbe:
    stage: str
    decision: Decision
    decision_key: str | None
    components: KeyComponents | None
    previous: SuccessRecord | None
    source_fingerprint: SourceFingerprint
    source_review: str
    failure: FailureRecord | None = None
    unavailable_reason: str | None = None


@dataclass
class _KeyingSession:
    """Command-scoped source, filesystem, and success-record snapshots."""

    fingerprints: FingerprintSession = field(default_factory=FingerprintSession)
    sources: SourceFingerprintSession = field(default_factory=SourceFingerprintSession)
    records: dict[tuple[Path, str], SuccessRecord | object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fingerprints.force_rehash:
            self.sources.force_rehash = True

    def fresh_observations(self) -> _KeyingSession:
        """Share static source inspection while starting fresh mutable observations."""

        return _KeyingSession(
            fingerprints=FingerprintSession(force_rehash=self.fingerprints.force_rehash),
            sources=self.sources,
        )

    def refresh_fingerprints(self) -> None:
        """Discard filesystem observations after a successful stage."""

        self.fingerprints = FingerprintSession(force_rehash=self.fingerprints.force_rehash)

    def refresh_observations(self) -> None:
        """Discard source, filesystem, and record observations after possible side effects."""

        self.fingerprints = FingerprintSession(force_rehash=self.fingerprints.force_rehash)
        self.sources = SourceFingerprintSession(force_rehash=self.sources.force_rehash)
        self.records.clear()

    def read_success(self, store: Store, stage: str) -> SuccessRecord | None:
        key = (store.root, stage)
        cached = self.records.get(key, _RECORD_UNOBSERVED)
        if cached is _RECORD_UNOBSERVED:
            record = store.read_success(stage)
            self.records[key] = _RECORD_MISSING if record is None else record
            return record
        return None if cached is _RECORD_MISSING else cached  # type: ignore[return-value]

    def write_success(self, store: Store, record: SuccessRecord) -> None:
        store.write_success(record)
        self.records[(store.root, record.stage)] = record

    def discard_success(self, store: Store, stage: str) -> None:
        self.records.pop((store.root, stage), None)

    def source_fingerprint(
        self,
        pipeline_type: type[Pipeline],
        stage_spec,
        store: Store,
    ) -> SourceFingerprint:
        cached: list[SourceFingerprint] = []
        review = store.read_review(stage_spec.name)
        if review is not None:
            cached.append(review.source_observation)
        previous = self.read_success(store, stage_spec.name)
        if previous is not None:
            cached.append(previous.executed_source_fingerprint)
        return self.sources.fingerprint(
            pipeline_type,
            stage_spec,
            cached=tuple(cached),
        )


_RECORD_UNOBSERVED = object()
_RECORD_MISSING = object()


class ReviewRequiredError(Exception):
    """Raised before execution when selected stages have pending source reviews."""

    def __init__(self, stages: list[str]) -> None:
        self.stages = stages
        super().__init__(
            "Source review required for: " + ", ".join(stages) + ". Run accept or reject first."
        )


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _record_stage_failure(
    store: Store,
    pipeline: type[Pipeline],
    stage_name: str,
    error: Exception,
) -> None:
    attempt = store.read_attempt(stage_name)
    if attempt is None:
        return
    store.write_failure(
        stage_name,
        FailureRecord(
            pipeline=pipeline.__name__,
            stage=stage_name,
            input_key=attempt.input_key,
            source_fingerprint=attempt.source_fingerprint,
            exception_type=type(error).__name__,
            message=str(error),
            failed_at=_now(),
        ),
    )


def _stage_display(stage_spec) -> StageDisplay:
    return StageDisplay(
        base_name=stage_spec.base_name or stage_spec.name,
        cell_values=tuple(axis.id_of(value) for axis, value in stage_spec.cell),
    )


def _relative_to_out(
    path: Path,
    out: Path,
    *,
    description: str = "Yielded varve output",
) -> str:
    resolved = path.resolve()
    out_resolved = out.resolve()
    try:
        return str(resolved.relative_to(out_resolved))
    except ValueError as error:
        raise ValueError(
            f"{description} must live inside the output root: {resolved} "
            f"is not under {out_resolved}"
        ) from error


def _cwd_relative_path_hint(path: Path, out: Path) -> str | None:
    if path.is_absolute() or not path.exists():
        return None
    resolved = path.resolve()
    out_resolved = out.resolve()
    try:
        out_relative = resolved.relative_to(out_resolved)
    except ValueError:
        return None
    return (
        "Relative batch output paths are interpreted relative to the output root, "
        f"not the current working directory: yielded {path!s}, which exists at {resolved}. "
        f"Yield {out_relative!s} or {resolved!s} instead."
    )


def _refresh_fingerprint_cache(
    *,
    store: Store,
    previous: SuccessRecord | None,
    components: KeyComponents,
    keying_session: _KeyingSession,
) -> None:
    """Rewrite a hit stage's success record when input fingerprints drifted.

    A content-key hit guarantees identical file sha256 digests, since the key
    only folds in digests. But a file may have been touched (new mtime/size)
    without its content changing. The freshly computed `components` already
    carry the refreshed fingerprints; persisting them avoids re-hashing the
    same unchanged bytes on every subsequent run while leaving the input key
    untouched. Only the size/mtime metadata moves.
    """

    if previous is None:
        return
    if previous.key_components.inputs == components.inputs:
        return
    refreshed = previous.model_copy(
        update={
            "key_components": previous.key_components.model_copy(
                update={"inputs": components.inputs}
            )
        }
    )
    keying_session.write_success(store, refreshed)


def _produced_paths(
    produces: ProducesSpec,
    ctx: Ctx[Any, Any],
) -> list[ProducedPath]:
    if produces is None:
        return []
    raw = produces(ctx) if callable(produces) else produces
    paths: list[ProducesItem] = [raw] if isinstance(raw, str | Path) else list(raw)
    result = []
    for item in paths:
        declared = Path(item)
        path = declared if declared.is_absolute() else ctx.cell_out / declared
        if not path.exists():
            raise FileNotFoundError(f"Declared varve output does not exist: {path}")
        if ctx.cell:
            _relative_to_out(path, ctx.cell_out, description="Declared varve output")
        relative = _relative_to_out(path, ctx.out, description="Declared varve output")
        result.append(
            ProducedPath(
                path=relative,
                kind="dir" if path.is_dir() else "file",
                artifact=artifact_fingerprint(path, ctx.out, force_rehash=True),
            )
        )
    return result


def _validate_static_produces_location(produces: ProducesSpec, ctx: Ctx[Any, Any]) -> None:
    if produces is None or callable(produces):
        return
    paths: list[ProducesItem] = [produces] if isinstance(produces, str | Path) else list(produces)
    for item in paths:
        declared = Path(item)
        path = declared if declared.is_absolute() else ctx.cell_out / declared
        if ctx.cell:
            _relative_to_out(
                path,
                ctx.cell_out,
                description="Declared matrix stage output",
            )


def _success_outputs_exist(record: SuccessRecord, out: Path) -> bool:
    if record.kind == "single":
        assert record.produces is not None
        return all((out / item.path).exists() for item in record.produces)
    assert record.outputs is not None
    return all((out / item.path).exists() for item in record.outputs)


def _current_artifacts(
    record: SuccessRecord,
    out: Path,
    session: FingerprintSession,
) -> tuple[list[ArtifactFingerprint], str]:
    if record.kind == "single":
        produced = record.produces or []
        recorded = [item.artifact for item in produced]
        positions = [(ordinal,) for ordinal in range(len(produced))]
    else:
        outputs = record.outputs or []
        recorded = [item.artifact for item in outputs]
        ordinals: dict[int, int] = {}
        positions = []
        for item in outputs:
            ordinal = ordinals.get(item.index, 0)
            positions.append((item.index, ordinal))
            ordinals[item.index] = ordinal + 1
    current = [
        artifact_fingerprint(out / item.root, out, cached=item, session=session)
        for item in recorded
    ]
    return current, artifacts_root_fingerprint(current, positions=positions)


def _source_review(
    store: Store,
    stage_name: str,
    previous: SuccessRecord | None,
    current: SourceFingerprint,
) -> str:
    if previous is None or previous.executed_source_fingerprint.fingerprint == current.fingerprint:
        return "confirmed"
    review = store.read_review(stage_name)
    if review is None or review.source_fingerprint != current.fingerprint:
        return "pending"
    return "accepted" if review.decision == "accept" else "rerun-required"


def _stage_sets(
    graph: PipelineGraph,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    stages = graph.stages
    ancestors = {name: set(spec.needs) for name, spec in stages.items()}
    descendants = {name: set() for name in stages}
    for name, spec in stages.items():
        for upstream in spec.needs:
            descendants[upstream].add(name)
    return ancestors, descendants


def _closure(seed: str, graph: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [seed]
    while stack:
        item = stack.pop()
        if item in seen:
            continue
        seen.add(item)
        stack.extend(graph[item])
    return seen


def selected_stages(
    pipeline_or_graph: type[Pipeline] | PipelineGraph,
    *,
    upto: str | None = None,
    downstream: str | None = None,
    only: str | None = None,
    slices: tuple[str, ...] | list[str] = (),
) -> set[str]:
    graph = (
        pipeline_or_graph
        if isinstance(pipeline_or_graph, PipelineGraph)
        else build_graph(pipeline_or_graph)
    )
    return graph.selected(upto=upto, downstream=downstream, only=only, slices=slices)


def _upstream_keys(
    stage_spec,
    store: Store,
    keying_session: _KeyingSession,
    known_upstream_fingerprints: dict[str, str] | None = None,
) -> dict[str, str]:
    keys: dict[str, str] = {}
    for name in stage_spec.needs:
        if known_upstream_fingerprints is not None and name in known_upstream_fingerprints:
            keys[name] = known_upstream_fingerprints[name]
            continue
        record = keying_session.read_success(store, name)
        if record is None:
            raise ValueError(f"Upstream stage has no success record: {name}")
        if _success_outputs_exist(record, store.output_root):
            _, keys[name] = _current_artifacts(
                record, store.output_root, keying_session.fingerprints
            )
        else:
            keys[name] = record.artifact_fingerprint
    return keys


def _validate_external_upstreams(
    pipeline_type: type[Pipeline],
    graph: PipelineGraph,
    selected: set[str],
    store: Store,
    out: Path,
    config: Any,
    args: Any,
    keying_session: _KeyingSession,
) -> None:
    validation_session = keying_session.fresh_observations()
    stages = graph.stages
    external = {
        upstream
        for stage_name in selected
        for upstream in stages[stage_name].needs
        if upstream not in selected
    }
    if not external:
        return
    ancestors, _ = _stage_sets(graph)
    validation_stages: set[str] = set()
    for upstream in external:
        validation_stages.update(_closure(upstream, ancestors))
    for stage_name in graph.topo_order():
        if stage_name not in external:
            continue
        attempt = store.read_attempt(stage_name)
        record = validation_session.read_success(store, stage_name)
        if attempt is not None:
            raise ValueError(f"Upstream stage has an unfinished attempt: {stage_name}")
        if record is None:
            raise ValueError(f"Upstream stage has not been built: {stage_name}")
        if not _success_outputs_exist(record, out):
            raise ValueError(f"Upstream stage artifacts are missing: {stage_name}")
    probes = probe_pipeline(
        pipeline_type,
        config,
        args=args,
        out=out,
        graph=graph,
        _keying_session=validation_session,
        _stage_names=validation_stages,
    )
    for probe in probes:
        decision = probe.decision
        if decision.status != "hit":
            raise ValueError(
                f"Upstream stage is not current: {probe.stage} "
                f"({decision.status}: {decision.reason})"
            )


def _batch_outputs_from_records(
    *,
    previous: SuccessRecord | None,
    partial: dict[int, BatchRecord] | None,
    out: Path,
    force: bool,
) -> dict[int, list[str]]:
    if force:
        return {}
    outputs: dict[int, list[str]] = {}
    if previous is not None and previous.outputs is not None:
        grouped: dict[int, list[str]] = {}
        for item in previous.outputs:
            grouped.setdefault(item.index, []).append(item.path)
        for index, paths in grouped.items():
            if all((out / path).exists() for path in paths):
                outputs[index] = list(paths)
    if partial is not None:
        for index, batch in partial.items():
            if all((out / path).exists() for path in batch.yielded):
                outputs[index] = list(batch.yielded)
    return outputs


def _batch_artifact_positions(outputs: list[OutputHandle]) -> list[tuple[int, int]]:
    ordinals: dict[int, int] = {}
    positions = []
    for output in outputs:
        ordinal = ordinals.get(output.index, 0)
        positions.append((output.index, ordinal))
        ordinals[output.index] = ordinal + 1
    return positions


def _validated_partial(
    partial: dict[int, BatchRecord] | None,
    out: Path,
    session: FingerprintSession,
) -> dict[int, BatchRecord] | None:
    if partial is None:
        return None
    valid: dict[int, BatchRecord] = {}
    for index, batch in partial.items():
        if len(batch.yielded) != len(batch.artifacts):
            continue
        try:
            current = [
                artifact_fingerprint(out / path, out, cached=cached, session=session)
                for path, cached in zip(batch.yielded, batch.artifacts)
            ]
        except FileNotFoundError:
            continue
        if all(
            left.fingerprint == right.fingerprint for left, right in zip(current, batch.artifacts)
        ):
            valid[index] = batch
    return valid or None


def _merge_config_access(
    previous: SuccessRecord | None,
    source: SourceFingerprint,
    recorded: list[str] | None,
) -> list[str] | None:
    """Combine a run's recorded config access with the previous record's.

    `recorded` is `None` when the run touched the whole config (conservative).
    On unchanged source we union with the previous set so a resume that skips
    batches, or a data-dependent branch not taken this run, never drops a real
    dependency; a source change resets the basis so fields the code no longer
    reads do not linger.
    """

    if recorded is None:
        return None
    if previous is None:
        return recorded
    prev_access = previous.key_components.config_access
    if prev_access is None:
        return None
    if previous.executed_source_fingerprint.fingerprint != source.fingerprint:
        return recorded
    return sorted(set(prev_access) | set(recorded))


def _commit_components(
    probe: KeyComponents,
    config: Any,
    committed_access: list[str] | None,
) -> KeyComponents:
    """Reproject a probe's components onto the config fields actually read."""

    return probe.model_copy(
        update={
            "config": project_config(config_data(config), committed_access),
            "config_access": committed_access,
        }
    )


def _probe_stage(
    pipeline_type: type[Pipeline],
    graph: PipelineGraph,
    stage_name: str,
    *,
    config: Any,
    args: Any,
    out: Path,
    store: Store,
    known_upstream_fingerprints: dict[str, str],
    source_fingerprint: SourceFingerprint,
    keying_session: _KeyingSession,
) -> StageProbe:
    stage_spec = graph.stages[stage_name]
    previous = keying_session.read_success(store, stage_name)
    review = _source_review(store, stage_name, previous, source_fingerprint)
    upstream_keys = _upstream_keys(stage_spec, store, keying_session, known_upstream_fingerprints)
    cached_inputs = previous.key_components.inputs if previous is not None else None
    previous_access = (
        previous.key_components.config_access
        if previous is not None and review != "rerun-required"
        else None
    )
    ctx_for_key = Ctx(
        config=config,
        args=args,
        out=out,
        store=store,
        stage_name=stage_name,
        stage_display=_stage_display(stage_spec),
        declared_needs=frozenset(stage_spec.logical_needs),
        cell=Cell(stage_spec.cell),
        cell_out=cell_output_path(out, stage_spec),
        need_cells=stage_spec.need_cells,
    )
    components = compute_key_components(
        stage_spec,
        ctx_for_key,
        upstream_keys,
        cached_inputs,
        config_access=previous_access,
        dependencies=merge_dependencies(pipeline_type.depends, stage_spec.depends),
        fingerprint_session=keying_session.fingerprints,
    )
    decision_key = input_key(components)
    attempt = store.read_attempt(stage_name)
    failure = store.read_failure(stage_name)
    if previous is None and attempt is not None and failure is None and stage_spec.kind == "single":
        try:
            _produced_paths(stage_spec.produces, ctx_for_key)
        except FileNotFoundError:
            pass
    artifacts_match = True
    if previous is not None and _success_outputs_exist(previous, out):
        _, current_artifact_root = _current_artifacts(previous, out, keying_session.fingerprints)
        artifacts_match = current_artifact_root == previous.artifact_fingerprint
    if stage_spec.kind == "single":
        produces = previous.produces if previous is not None else []
        assert produces is not None
        decision = decide_single(
            current_key=decision_key,
            current_components=components,
            success=previous,
            attempt=attempt,
            produces_exist=all((out / item.path).exists() for item in produces),
            artifacts_match=artifacts_match,
            failure=failure,
        )
    else:
        partial = _validated_partial(
            store.read_partial(stage_name, decision_key), out, keying_session.fingerprints
        )
        decision = decide_batch(
            current_key=decision_key,
            current_components=components,
            success=previous,
            partial=partial,
            attempt=attempt,
            output_exists=lambda path: (out / path).exists(),
            artifacts_match=artifacts_match,
            failure=failure,
        )
    if review == "rerun-required":
        decision = Decision("needs-run", "source-change")
    return StageProbe(
        stage=stage_name,
        decision=decision,
        decision_key=decision_key,
        components=components,
        previous=previous,
        source_fingerprint=source_fingerprint,
        source_review=review,
        failure=failure,
    )


def probe_pipeline(
    pipeline_type: type[Pipeline],
    config: Any,
    *,
    args: Any,
    out: Path,
    axes: dict[str, tuple[str, ...]] | None = None,
    graph: PipelineGraph | None = None,
    force_rehash: bool = False,
    _keying_session: _KeyingSession | None = None,
    _stage_names: set[str] | None = None,
) -> tuple[StageProbe, ...]:
    """Probe all or an internal ancestor-closed stage set without writing state."""

    store = Store(out)
    known_upstream_fingerprints: dict[str, str] = {}
    probes: list[StageProbe] = []
    graph = graph or build_graph(pipeline_type, axes)
    keying_session = _keying_session or _KeyingSession(
        fingerprints=FingerprintSession(force_rehash=force_rehash)
    )
    topo_order = graph.topo_order()
    if _stage_names is not None:
        unknown = _stage_names.difference(graph.stages)
        if unknown:
            raise ValueError(f"Unknown varve stages to probe: {sorted(unknown)!r}")
        topo_order = [name for name in topo_order if name in _stage_names]
    manifest = store.read_manifest()
    schema_migration = manifest is not None and manifest.schema_version != SCHEMA_VERSION
    for stage_name in topo_order:
        stage_spec = graph.stages[stage_name]
        try:
            source_fingerprint = keying_session.source_fingerprint(pipeline_type, stage_spec, store)
        except Exception as error:  # noqa: BLE001 - status must retain evaluation errors.
            probes.append(
                StageProbe(
                    stage=stage_name,
                    decision=Decision("error", str(error)),
                    decision_key=None,
                    components=None,
                    previous=keying_session.read_success(store, stage_name),
                    source_fingerprint=SourceFingerprint(fingerprint="error", files=[]),
                    source_review="confirmed",
                    unavailable_reason=str(error),
                )
            )
            continue
        if schema_migration:
            assert manifest is not None
            probes.append(
                StageProbe(
                    stage=stage_name,
                    decision=Decision("needs-run", "schema-migration"),
                    decision_key=None,
                    components=None,
                    previous=None,
                    source_fingerprint=source_fingerprint,
                    source_review="confirmed",
                    unavailable_reason=(
                        f"store schema {manifest.schema_version} must be rebuilt as "
                        f"schema {SCHEMA_VERSION}"
                    ),
                )
            )
            continue
        missing_upstream = next(
            (name for name in stage_spec.needs if keying_session.read_success(store, name) is None),
            None,
        )
        if missing_upstream is not None:
            previous = keying_session.read_success(store, stage_name)
            probes.append(
                StageProbe(
                    stage=stage_name,
                    decision=Decision("needs-run", "no-cache"),
                    decision_key=None,
                    components=None,
                    previous=previous,
                    source_fingerprint=source_fingerprint,
                    source_review=_source_review(store, stage_name, previous, source_fingerprint),
                    unavailable_reason=f"upstream {missing_upstream} has no success record",
                )
            )
            continue
        previous = keying_session.read_success(store, stage_name)
        review = _source_review(store, stage_name, previous, source_fingerprint)
        try:
            probe = _probe_stage(
                pipeline_type,
                graph,
                stage_name,
                config=config,
                args=args,
                out=out,
                store=store,
                known_upstream_fingerprints=known_upstream_fingerprints,
                source_fingerprint=source_fingerprint,
                keying_session=keying_session,
            )
        except Exception as error:  # noqa: BLE001 - status must retain evaluation errors.
            probe = StageProbe(
                stage=stage_name,
                decision=Decision("error", str(error)),
                decision_key=None,
                components=None,
                previous=previous,
                source_fingerprint=source_fingerprint,
                source_review=review,
                failure=store.read_failure(stage_name),
                unavailable_reason=str(error),
            )
        probes.append(probe)
        if probe.previous is not None and _success_outputs_exist(probe.previous, out):
            _, known_upstream_fingerprints[stage_name] = _current_artifacts(
                probe.previous, out, keying_session.fingerprints
            )
    return tuple(probes)


async def _execute_stage(instance, stage_spec, ctx: Ctx) -> None:
    coordinates = {axis.name: value for axis, value in stage_spec.cell}
    result = stage_spec.func(instance, ctx, **coordinates)
    if inspect.isawaitable(result):
        await result


async def _execute_batch(instance, stage_spec, ctx: Ctx):
    coordinates = {axis.name: value for axis, value in stage_spec.cell}
    generator = stage_spec.func(instance, ctx, **coordinates)
    if not hasattr(generator, "__aiter__"):
        raise TypeError(f"Batch stage must return an async iterator: {stage_spec.name}")
    async for yielded in generator:
        if isinstance(yielded, list | tuple):
            yield ctx._current_batch_index, [Path(item) for item in yielded]
        else:
            yield ctx._current_batch_index, [Path(yielded)]


async def _drive(
    pipeline_type: type[Pipeline],
    graph: PipelineGraph,
    config,
    *,
    args,
    out: Path,
    upto: str | None,
    downstream: str | None,
    only: str | None,
    force: bool,
    execute: bool,
    display_mode: RunDisplayMode,
    reporter: RunReporter | None = None,
    slices: tuple[str, ...] = (),
    keying_session: _KeyingSession | None = None,
    record_callback: Callable[[str, SuccessRecord | None], None] | None = None,
) -> list[StageOutcome]:
    store = Store(out)
    keying_session = keying_session or _KeyingSession()
    selected = selected_stages(
        graph,
        upto=upto,
        downstream=downstream,
        only=only,
        slices=slices,
    )
    if reporter is None:
        display_plan = build_run_display_plan(graph, selected, store, mode=display_mode)
        reporter = RunReporter(display_plan, logging.getLogger("varve"))
    else:
        display_plan = reporter.plan
    if execute:
        _validate_external_upstreams(
            pipeline_type,
            graph,
            selected,
            store,
            out,
            config,
            args,
            keying_session,
        )
        ancestors, _ = _stage_sets(graph)
        preflight_names = set(selected)
        for selected_name in selected:
            preflight_names.update(_closure(selected_name, ancestors))
        preflight = probe_pipeline(
            pipeline_type,
            config,
            args=args,
            out=out,
            graph=graph,
            _keying_session=keying_session.fresh_observations(),
            _stage_names=preflight_names,
        )
        pending = [probe.stage for probe in preflight if probe.source_review == "pending"]
        errors = [probe for probe in preflight if probe.decision.status == "error"]
        if errors:
            details = "; ".join(f"{probe.stage}: {probe.decision.reason}" for probe in errors)
            raise ValueError(f"Cannot evaluate selected stages: {details}")
        if pending:
            raise ReviewRequiredError(pending)

    instance = pipeline_type()
    outcomes: list[StageOutcome] = []
    known_upstream_fingerprints: dict[str, str] = {}
    known_success: dict[str, bool] = {}
    reporter.log_plan(graph.topo_order())

    for stage_name in graph.topo_order():
        if stage_name not in selected:
            continue
        stage_spec = graph.stages[stage_name]
        reporter.start(stage_name)
        if not execute:
            for name in stage_spec.needs:
                if name not in known_success:
                    known_success[name] = keying_session.read_success(store, name) is not None
            missing_upstream = any(not known_success[name] for name in stage_spec.needs)
            if missing_upstream:
                previous = keying_session.read_success(store, stage_name)
                known_success[stage_name] = previous is not None
                if record_callback is not None:
                    record_callback(stage_name, previous)
                    keying_session.discard_success(store, stage_name)
                outcome = display_plan.outcome(stage_name, "needs-run", "no-cache", None)
                outcomes.append(outcome)
                reporter.record(outcome)
                continue
            source_fingerprint = keying_session.source_fingerprint(pipeline_type, stage_spec, store)
            probe = _probe_stage(
                pipeline_type,
                graph,
                stage_name,
                config=config,
                args=args,
                out=out,
                store=store,
                known_upstream_fingerprints=known_upstream_fingerprints,
                source_fingerprint=source_fingerprint,
                keying_session=keying_session,
            )
            assert probe.decision_key is not None
            if probe.previous is not None and _success_outputs_exist(probe.previous, out):
                _, known_upstream_fingerprints[stage_name] = _current_artifacts(
                    probe.previous, out, keying_session.fingerprints
                )
            known_success[stage_name] = probe.previous is not None
            if record_callback is not None:
                record_callback(stage_name, probe.previous)
                keying_session.discard_success(store, stage_name)
            reporter.lifecycle(stage_name, probe.decision.status, probe.decision.reason)
            reporter.input_key(stage_name, probe.decision_key)
            outcome = display_plan.outcome(
                stage_name, probe.decision.status, probe.decision.reason, None
            )
            outcomes.append(outcome)
            reporter.record(outcome)
            continue
        source_fingerprint = keying_session.source_fingerprint(pipeline_type, stage_spec, store)
        upstream_keys = _upstream_keys(stage_spec, store, keying_session)
        previous = keying_session.read_success(store, stage_name)
        review = _source_review(store, stage_name, previous, source_fingerprint)
        cached_inputs = previous.key_components.inputs if previous is not None else None
        previous_access = (
            previous.key_components.config_access
            if previous is not None and review != "rerun-required"
            else None
        )
        declared_needs = frozenset(stage_spec.logical_needs)
        ctx_for_key = Ctx(
            config=config,
            args=args,
            out=out,
            store=store,
            stage_name=stage_name,
            stage_display=_stage_display(stage_spec),
            declared_needs=declared_needs,
            cell=Cell(stage_spec.cell),
            cell_out=cell_output_path(out, stage_spec),
            need_cells=stage_spec.need_cells,
        )
        # Probe key: project config onto the fields the previous run read (whole
        # config when there is no prior record). The committed key below is
        # reprojected onto this run's actual reads.
        components = compute_key_components(
            stage_spec,
            ctx_for_key,
            upstream_keys,
            cached_inputs,
            config_access=previous_access,
            dependencies=merge_dependencies(pipeline_type.depends, stage_spec.depends),
            fingerprint_session=keying_session.fingerprints,
        )
        current_key = input_key(components)
        attempt = store.read_attempt(stage_name)
        artifacts_match = True
        if previous is not None and _success_outputs_exist(previous, out):
            _, current_artifact_root = _current_artifacts(
                previous, out, keying_session.fingerprints
            )
            artifacts_match = current_artifact_root == previous.artifact_fingerprint
        failure = store.read_failure(stage_name)

        partial: dict[int, BatchRecord] | None = None
        partial_adoption: tuple[str, str, dict[int, BatchRecord]] | None = None
        if stage_spec.kind == "single":
            produces = []
            if previous is not None:
                assert previous.produces is not None
                produces = previous.produces
            produces_exist = all((out / item.path).exists() for item in produces)
            decision = decide_single(
                current_key=current_key,
                current_components=components,
                success=previous,
                attempt=attempt,
                produces_exist=produces_exist,
                artifacts_match=artifacts_match,
                failure=failure,
            )
        else:
            partial = _validated_partial(
                store.read_partial(stage_name, current_key), out, keying_session.fingerprints
            )
            decision = decide_batch(
                current_key=current_key,
                current_components=components,
                success=previous,
                partial=partial,
                attempt=attempt,
                output_exists=lambda path: (out / path).exists(),
                artifacts_match=artifacts_match,
                failure=failure,
            )

        probe_key = current_key
        probe_partial = partial
        source_changed = (
            previous is not None
            and previous.executed_source_fingerprint.fingerprint != source_fingerprint.fingerprint
        )
        if source_changed and previous_access is not None and (decision.status != "hit" or force):
            # An accepted source change may continue using the old access set
            # while probing a reusable materialization. Once this run will
            # actually execute, use the whole Config as the conservative input
            # basis and let the execution record a new access set.
            components = compute_key_components(
                stage_spec,
                ctx_for_key,
                upstream_keys,
                cached_inputs,
                config_access=None,
                dependencies=merge_dependencies(pipeline_type.depends, stage_spec.depends),
                fingerprint_session=keying_session.fingerprints,
            )
            current_key = input_key(components)
            attempt = store.read_attempt(stage_name)
            if stage_spec.kind == "single":
                decision = decide_single(
                    current_key=current_key,
                    current_components=components,
                    success=previous,
                    attempt=attempt,
                    produces_exist=produces_exist,
                    artifacts_match=artifacts_match,
                    failure=failure,
                )
            else:
                full_key_partial = _validated_partial(
                    store.read_partial(stage_name, current_key),
                    out,
                    keying_session.fingerprints,
                )
                if review == "accepted" and probe_key != current_key and probe_partial:
                    partial = dict(probe_partial)
                    partial.update(full_key_partial or {})
                    partial_adoption = (probe_key, current_key, partial)
                else:
                    partial = full_key_partial
                decision = decide_batch(
                    current_key=current_key,
                    current_components=components,
                    success=previous,
                    partial=partial,
                    attempt=attempt,
                    output_exists=lambda path: (out / path).exists(),
                    artifacts_match=artifacts_match,
                    failure=failure,
                )

        # `failed` is the read-only state of the last attempt. Once a run has
        # been requested, retry it using any validated batch checkpoints.
        if decision.status == "failed":
            decision = (
                Decision(
                    "resume",
                    "resume-after-failure",
                    decision.resume_skip,
                    decision.resume_total,
                )
                if decision.resume_skip
                else Decision("needs-run", "retry-failed")
            )

        if review == "rerun-required":
            decision = Decision("needs-run", "source-change")
        if force:
            decision = Decision("needs-run", "forced")
        if execute and decision.status == "hit":
            _refresh_fingerprint_cache(
                store=store,
                previous=previous,
                components=components,
                keying_session=keying_session,
            )
        if decision.status == "hit":
            reporter.lifecycle(stage_name, decision.status, decision.reason)
            reporter.input_key(stage_name, current_key)
            outcome = display_plan.outcome(stage_name, decision.status, decision.reason, None)
            outcomes.append(outcome)
            reporter.record(outcome)
            continue
        _validate_static_produces_location(stage_spec.produces, ctx_for_key)
        if partial_adoption is not None:
            old_key, new_key, adopted = partial_adoption
            for batch in adopted.values():
                store.write_batch(stage_name, new_key, batch)
            store.clear_partial(stage_name, old_key)
        started = time.monotonic()
        reporter.lifecycle(stage_name, "run", decision.reason)
        reporter.input_key(stage_name, current_key)
        store.write_attempt(
            stage_name,
            AttemptMarker(
                input_key=current_key,
                source_fingerprint=source_fingerprint.fingerprint,
                started_at=_now(),
                touched_existing=previous is not None,
            ),
        )
        access = ConfigAccess()
        ctx = Ctx(
            config=RecordingConfig(config, access),
            args=args,
            out=out,
            store=store,
            resume_skip=decision.resume_skip,
            stage_name=stage_name,
            stage_display=_stage_display(stage_spec),
            declared_needs=declared_needs,
            cell=Cell(stage_spec.cell),
            cell_out=cell_output_path(out, stage_spec),
            need_cells=stage_spec.need_cells,
        )
        if stage_spec.kind == "single":
            try:
                await _execute_stage(instance, stage_spec, ctx)
            except Exception as error:
                _record_stage_failure(store, pipeline_type, stage_name, error)
                raise
            produces = _produced_paths(stage_spec.produces, ctx)
            elapsed = time.monotonic() - started
            committed_access = _merge_config_access(previous, source_fingerprint, access.resolve())
            commit_components = _commit_components(components, config, committed_access)
            keying_session.write_success(
                store,
                SuccessRecord(
                    pipeline=pipeline_type.__name__,
                    stage=stage_name,
                    kind="single",
                    input_key=input_key(commit_components),
                    key_components=commit_components,
                    executed_source_fingerprint=source_fingerprint,
                    artifact_fingerprint=artifacts_root_fingerprint(
                        [item.artifact for item in produces]
                    ),
                    produces=produces,
                    committed_at=_now(),
                    elapsed=elapsed,
                ),
            )
        else:
            if force or review == "rerun-required":
                store.clear_partial(stage_name)
            elif not decision.resume_skip:
                store.clear_partial(stage_name, current_key)
            partial_for_outputs = partial if decision.resume_skip else None
            outputs_by_index = _batch_outputs_from_records(
                previous=None,
                partial=partial_for_outputs,
                out=out,
                force=force,
            )
            warned_without_resume = False
            partial_enabled = True
            saw_yield = False
            batch_iterator = _execute_batch(instance, stage_spec, ctx).__aiter__()
            while True:
                try:
                    yielded_index, index_paths = await anext(batch_iterator)
                except StopAsyncIteration:
                    break
                except Exception as error:
                    _record_stage_failure(store, pipeline_type, stage_name, error)
                    raise
                saw_yield = True
                if not ctx._used_resume and not warned_without_resume:
                    warnings.warn(
                        f"batch stage {stage_name!r} yielded without iterating ctx.resume; "
                        "its outputs are not resumable. Wrap your iterable in ctx.resume(...) "
                        "to enable per-batch checkpoint resume.",
                        stacklevel=2,
                    )
                    warned_without_resume = True
                if not ctx._used_resume and partial_enabled:
                    store.clear_partial(stage_name, current_key)
                    outputs_by_index.clear()
                    partial_enabled = False
                index = yielded_index if yielded_index is not None else len(outputs_by_index)
                yielded = []
                for path in index_paths:
                    absolute = path if path.is_absolute() else ctx.cell_out / path
                    if not absolute.exists():
                        hint = _cwd_relative_path_hint(path, ctx.cell_out)
                        if hint is not None:
                            raise ValueError(hint)
                        raise FileNotFoundError(f"Yielded varve output does not exist: {absolute}")
                    if stage_spec.cell:
                        _relative_to_out(
                            absolute,
                            ctx.cell_out,
                            description="Yielded matrix stage output",
                        )
                    yielded.append(_relative_to_out(absolute, out))
                if partial_enabled:
                    store.write_batch(
                        stage_name,
                        current_key,
                        BatchRecord(
                            index=index,
                            yielded=yielded,
                            artifacts=[
                                artifact_fingerprint(
                                    out / item,
                                    out,
                                    session=keying_session.fingerprints,
                                    force_rehash=True,
                                )
                                for item in yielded
                            ],
                            committed_at=_now(),
                            total=ctx._resume_total,
                        ),
                    )
                outputs_by_index[index] = yielded
            if not ctx._used_resume:
                store.clear_partial(stage_name, current_key)
                if not saw_yield:
                    outputs_by_index.clear()
            outputs = [
                OutputHandle(
                    index=index,
                    path=path,
                    artifact=artifact_fingerprint(
                        out / path,
                        out,
                        session=keying_session.fingerprints,
                        force_rehash=True,
                    ),
                )
                for index, paths in sorted(outputs_by_index.items())
                for path in paths
            ]
            elapsed = time.monotonic() - started
            committed_access = _merge_config_access(previous, source_fingerprint, access.resolve())
            commit_components = _commit_components(components, config, committed_access)
            keying_session.write_success(
                store,
                SuccessRecord(
                    pipeline=pipeline_type.__name__,
                    stage=stage_name,
                    kind="batch",
                    input_key=input_key(commit_components),
                    key_components=commit_components,
                    executed_source_fingerprint=source_fingerprint,
                    artifact_fingerprint=artifacts_root_fingerprint(
                        [item.artifact for item in outputs],
                        positions=_batch_artifact_positions(outputs),
                    ),
                    outputs=outputs,
                    committed_at=_now(),
                    elapsed=elapsed,
                ),
            )
        store.clear_attempt(stage_name)
        store.clear_failure(stage_name)
        store.clear_review(stage_name)
        store.clear_partial(stage_name)
        keying_session.refresh_fingerprints()
        reporter.lifecycle(stage_name, "done", f"{elapsed:.2f}s")
        outcome = display_plan.outcome(stage_name, decision.status, decision.reason, elapsed)
        outcomes.append(outcome)
        reporter.record(outcome)
    return outcomes


def run(
    pipeline: type[Pipeline],
    config,
    *,
    args=None,
    upto: str | None = None,
    downstream: str | None = None,
    only: str | None = None,
    force: bool = False,
    cli_out: Path | None = None,
    branch: str = "main",
    is_temporary: bool = False,
    temporary_config: dict[str, Any] | None = None,
    axes: dict[str, tuple[str, ...]] | None = None,
    slices: tuple[str, ...] = (),
    temporary_axes: dict[str, tuple[str, ...]] | None = None,
    graph: PipelineGraph | None = None,
    display_mode: RunDisplayMode = "auto",
    rehash: bool = False,
) -> list[StageOutcome]:
    if args is None:
        args = pipeline.Args()
    out = pipeline.output_root(
        config,
        cli_out=cli_out,
        branch=branch,
        is_temporary=is_temporary,
    )
    store = Store(out)
    graph = graph or build_graph(pipeline, axes)
    if is_temporary:
        logging.getLogger("varve").info("running temporary branch %s at %s", branch, out)
    store.root.mkdir(parents=True, exist_ok=True)
    with OutputLock(store.root):
        store.ensure_initialized(
            pipeline.__name__,
            module=pipeline.import_module_name(),
            temporary_config=temporary_config,
            temporary_axes=temporary_axes,
        )
        selected = selected_stages(
            graph,
            upto=upto,
            downstream=downstream,
            only=only,
            slices=slices,
        )
        reporter = RunReporter(
            build_run_display_plan(graph, selected, store, mode=display_mode),
            logging.getLogger("varve"),
        )
        try:
            return asyncio.run(
                _drive(
                    pipeline,
                    graph,
                    config,
                    args=args,
                    out=out,
                    upto=upto,
                    downstream=downstream,
                    only=only,
                    force=force,
                    execute=True,
                    display_mode=display_mode,
                    reporter=reporter,
                    slices=slices,
                    keying_session=_KeyingSession(
                        fingerprints=FingerprintSession(force_rehash=rehash)
                    ),
                )
            )
        except Exception as error:
            reporter.failure_current(error)
            raise


def record_source_review(
    pipeline: type[Pipeline],
    config: Any,
    *,
    decision: str,
    args: Any = None,
    targets: tuple[str, ...] = (),
    cli_out: Path | None = None,
    branch: str = "main",
    is_temporary: bool = False,
    axes: dict[str, tuple[str, ...]] | None = None,
    graph: PipelineGraph | None = None,
) -> list[str]:
    """Atomically validate and record accept/reject decisions for source changes."""

    if decision not in {"accept", "reject"}:
        raise ValueError(f"Unknown source review decision: {decision}")
    args = args if args is not None else pipeline.Args()
    graph = graph or build_graph(pipeline, axes)
    out = pipeline.output_root(config, cli_out=cli_out, branch=branch, is_temporary=is_temporary)
    store = Store(out)
    with OutputLock(store.root):
        probes = probe_pipeline(pipeline, config, args=args, out=out, graph=graph)
        by_name = {probe.stage: probe for probe in probes}
        if targets:
            names: list[str] = []
            for target in targets:
                names.extend(graph.names_for(target))
            selected = list(dict.fromkeys(names))
        else:
            selected = [probe.stage for probe in probes if probe.source_review == "pending"]
        if not selected:
            raise ValueError("No source changes require review")
        for name in selected:
            probe = by_name.get(name)
            if probe is None:
                raise ValueError(f"Unknown varve stage: {name}")
            if probe.previous is None or probe.source_review == "confirmed":
                raise ValueError(f"Stage has no source changes: {name}")
        now = _now()
        for name in selected:
            store.write_review(
                name,
                ReviewRecord(
                    source_fingerprint=by_name[name].source_fingerprint.fingerprint,
                    source_observation=by_name[name].source_fingerprint,
                    decision=decision,
                    decided_at=now,
                ),
            )
        return selected


def evaluate_state(
    pipeline: type[Pipeline],
    config,
    *,
    args=None,
    upto: str | None = None,
    downstream: str | None = None,
    only: str | None = None,
    cli_out: Path | None = None,
    branch: str = "main",
    is_temporary: bool = False,
    axes: dict[str, tuple[str, ...]] | None = None,
    graph: PipelineGraph | None = None,
    _keying_session: _KeyingSession | None = None,
    _record_callback: Callable[[str, SuccessRecord | None], None] | None = None,
) -> list[StageOutcome]:
    if args is None:
        args = pipeline.Args()
    out = pipeline.output_root(
        config,
        cli_out=cli_out,
        branch=branch,
        is_temporary=is_temporary,
    )
    graph = graph or build_graph(pipeline, axes)
    return asyncio.run(
        _drive(
            pipeline,
            graph,
            config,
            args=args,
            out=out,
            upto=upto,
            downstream=downstream,
            only=only,
            force=False,
            execute=False,
            display_mode="expand",
            keying_session=_keying_session,
            record_callback=_record_callback,
        )
    )
