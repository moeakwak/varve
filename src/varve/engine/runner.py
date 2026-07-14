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

from varve.context import Ctx
from varve.decorators import ProducesItem, ProducesSpec
from varve.dependencies import merge_dependencies
from varve.engine.review import (
    ReviewAction,
    ReviewCandidate,
    SourceReviewResult,
    apply_review_writes,
    plan_source_review,
)
from varve.engine.run_display import (
    RunDisplayMode,
    RunReporter,
    StageOutcome,
    build_run_display_plan,
)
from varve.engine.state import (
    Decision,
    SourceReviewState,
    decide,
    effective_reason,
    effective_status,
)
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
    AttemptMarker,
    BatchRecord,
    FailureRecord,
    KeyComponents,
    OutputHandle,
    ProducedPath,
    ReviewRecord,
    SourceFingerprint,
    SourceObservation,
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
    source_observation: SourceObservation
    source_review: SourceReviewState
    failure: FailureRecord | None = None
    unavailable_reason: str | None = None
    _partial: dict[int, BatchRecord] | None = field(default=None, repr=False)
    _artifacts_match: bool = field(default=True, repr=False)


@dataclass
class _KeyingSession:
    """Command-scoped source, filesystem, and success-record snapshots."""

    fingerprints: FingerprintSession = field(default_factory=FingerprintSession)
    sources: SourceFingerprintSession = field(default_factory=SourceFingerprintSession)
    records: dict[tuple[Path, str], SuccessRecord | object] = field(default_factory=dict)
    reviews: dict[tuple[Path, str], ReviewRecord | object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fingerprints.force_rehash:
            self.sources.force_rehash = True

    def refresh_fingerprints(self) -> None:
        """Discard filesystem observations after a successful stage."""

        self.fingerprints = FingerprintSession(force_rehash=self.fingerprints.force_rehash)

    def refresh_observations(self) -> None:
        """Discard source, filesystem, and record observations after possible side effects."""

        self.fingerprints = FingerprintSession(force_rehash=self.fingerprints.force_rehash)
        self.sources = SourceFingerprintSession(force_rehash=self.sources.force_rehash)
        self.records.clear()
        self.reviews.clear()

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

    def read_review(self, store: Store, stage_spec) -> ReviewRecord | None:
        stage = (
            stage_spec if isinstance(stage_spec, str) else stage_spec.base_name or stage_spec.name
        )
        key = (store.root, stage)
        cached = self.reviews.get(key, _RECORD_UNOBSERVED)
        if cached is _RECORD_UNOBSERVED:
            record = store.read_review(stage)
            self.reviews[key] = _RECORD_MISSING if record is None else record
            return record
        return None if cached is _RECORD_MISSING else cached  # type: ignore[return-value]

    def source_observation(
        self,
        pipeline_type: type[Pipeline],
        stage_spec,
        store: Store,
    ) -> SourceObservation:
        cached: list[SourceObservation | SourceFingerprint] = []
        review = self.read_review(store, stage_spec)
        if review is not None:
            cached.append(review.review_observation)
        previous = self.read_success(store, stage_spec.name)
        if previous is not None:
            cached.append(previous.executed_source)
        return self.sources.observe(
            pipeline_type,
            stage_spec,
            cached=tuple(cached),
        )


@dataclass(frozen=True)
class _Runtime:
    pipeline: type[Pipeline]
    graph: PipelineGraph
    config: Any
    args: Any
    out: Path
    store: Store
    keying: _KeyingSession

    def context(
        self,
        stage_spec,
        *,
        config: Any = None,
        resume_skip: set[int] | frozenset[int] = frozenset(),
    ) -> Ctx[Any, Any]:
        return Ctx(
            config=self.config if config is None else config,
            args=self.args,
            out=self.out,
            store=self.store,
            resume_skip=frozenset(resume_skip),
            stage_name=stage_spec.name,
            stage_display=tuple(axis.id_of(value) for axis, value in stage_spec.cell),
            declared_needs=frozenset(stage_spec.logical_needs),
            cell=Cell(stage_spec.cell),
            cell_out=cell_output_path(self.out, stage_spec),
            need_cells=stage_spec.need_cells,
        )


_RECORD_UNOBSERVED = object()
_RECORD_MISSING = object()


class ReviewRequiredError(Exception):
    """Raised before execution when selected stages have undecided source changes."""

    def __init__(self, stages: list[str]) -> None:
        self.stages = stages
        super().__init__(
            "Source review required for: " + ", ".join(stages) + ". Run reuse or invalidate first."
        )


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _record_stage_failure(
    runtime: _Runtime,
    stage_name: str,
    error: Exception,
) -> None:
    attempt = runtime.store.read_attempt(stage_name)
    if attempt is None:
        return
    runtime.store.write_failure(
        stage_name,
        FailureRecord(
            pipeline=runtime.pipeline.__name__,
            stage=stage_name,
            input_key=attempt.input_key,
            rerun_source_fingerprint=attempt.rerun_source_fingerprint,
            review_source_fingerprint=attempt.review_source_fingerprint,
            exception_type=type(error).__name__,
            message=str(error),
            failed_at=_now(),
        ),
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


def _managed_output(
    path: Path,
    ctx: Ctx[Any, Any],
    description: str,
    *,
    matrix_description: str | None = None,
    cwd_hint: bool = False,
) -> tuple[Path, str]:
    absolute = path if path.is_absolute() else ctx.cell_out / path
    if not absolute.exists():
        hint = _cwd_relative_path_hint(path, ctx.cell_out) if cwd_hint else None
        if hint is not None:
            raise ValueError(hint)
        raise FileNotFoundError(f"{description} does not exist: {absolute}")
    if ctx.cell:
        _relative_to_out(absolute, ctx.cell_out, description=matrix_description or description)
    return absolute, _relative_to_out(absolute, ctx.out, description=description)


def _refresh_fingerprint_cache(
    *,
    runtime: _Runtime,
    previous: SuccessRecord | None,
    components: KeyComponents,
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
    runtime.keying.write_success(runtime.store, refreshed)


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
        path, relative = _managed_output(declared, ctx, "Declared varve output")
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


def _success_outputs_exist(runtime: _Runtime, record: SuccessRecord) -> bool:
    return all((runtime.out / path).exists() for path in record.paths)


def _current_artifacts(
    runtime: _Runtime,
    record: SuccessRecord,
) -> str:
    out = runtime.out
    session = runtime.keying.fingerprints
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
    return artifacts_root_fingerprint(current, positions=positions)


def _review_state(
    runtime: _Runtime,
    stage_spec,
    current: SourceObservation,
    baseline: str | None,
) -> SourceReviewState:
    if baseline is None:
        return SourceReviewState("not-applicable")
    if baseline == current.review.fingerprint:
        return SourceReviewState("current")
    review = runtime.keying.read_review(runtime.store, stage_spec)
    if review is None or review.review_fingerprint != current.review.fingerprint:
        return SourceReviewState("changed")
    return SourceReviewState("changed", review.decision)


def _partial_review_fingerprint(
    runtime: _Runtime,
    stage_name: str,
    current_key: str,
) -> str | None:
    attempt = runtime.store.read_attempt(stage_name)
    if attempt is not None and attempt.input_key == current_key:
        return attempt.review_source_fingerprint
    failure = runtime.store.read_failure(stage_name)
    if failure is not None and failure.input_key == current_key:
        return failure.review_source_fingerprint
    return None


def _source_review(
    runtime: _Runtime,
    stage_spec,
    previous: SuccessRecord | None,
    current: SourceObservation,
    *,
    current_key: str | None = None,
    partial: dict[int, BatchRecord] | None = None,
) -> SourceReviewState:
    baseline = (
        _partial_review_fingerprint(runtime, stage_spec.name, current_key)
        if partial and current_key is not None
        else None
    )
    if baseline is None and previous is not None:
        baseline = previous.executed_source.review.fingerprint
    return _review_state(runtime, stage_spec, current, baseline)


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
    runtime: _Runtime,
    stage_spec,
    known_upstream_fingerprints: dict[str, str] | None = None,
) -> dict[str, str]:
    keys: dict[str, str] = {}
    for name in stage_spec.needs:
        if known_upstream_fingerprints is not None and name in known_upstream_fingerprints:
            keys[name] = known_upstream_fingerprints[name]
            continue
        record = runtime.keying.read_success(runtime.store, name)
        if record is None:
            raise ValueError(f"Upstream stage has no success record: {name}")
        if _success_outputs_exist(runtime, record):
            keys[name] = _current_artifacts(runtime, record)
        else:
            keys[name] = record.artifact_fingerprint
    return keys


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
    source: SourceObservation,
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
    if (
        previous.executed_source.rerun.fingerprint != source.rerun.fingerprint
        or previous.executed_source.review.fingerprint != source.review.fingerprint
    ):
        return recorded
    return sorted(set(prev_access) | set(recorded))


_INFER_CONFIG_ACCESS = object()


def _decision_for_key(
    runtime: _Runtime,
    stage_spec,
    current_key: str,
    components: KeyComponents,
    previous: SuccessRecord | None,
    artifacts_match: bool,
) -> tuple[Decision, dict[int, BatchRecord] | None, FailureRecord | None]:
    attempt = runtime.store.read_attempt(stage_spec.name)
    failure = runtime.store.read_failure(stage_spec.name)
    partial = None
    options: dict[str, Any]
    if stage_spec.kind == "batch":
        partial = _validated_partial(
            runtime.store.read_partial(stage_spec.name, current_key),
            runtime.out,
            runtime.keying.fingerprints,
        )
        options = {"partial": partial, "output_exists": lambda path: (runtime.out / path).exists()}
    else:
        produces = previous.produces if previous is not None else []
        assert produces is not None
        options = {"produces_exist": all((runtime.out / item.path).exists() for item in produces)}
    return (
        decide(
            kind=stage_spec.kind,
            current_key=current_key,
            current_components=components,
            success=previous,
            attempt=attempt,
            artifacts_match=artifacts_match,
            failure=failure,
            **options,
        ),
        partial,
        failure,
    )


def _empty_source_observation() -> SourceObservation:
    empty = SourceFingerprint(fingerprint="error", files=[])
    return SourceObservation(rerun=empty, review=empty)


def _unavailable_probe(
    stage: str,
    decision: Decision,
    unavailable_reason: str,
    source: SourceObservation,
    *,
    previous: SuccessRecord | None = None,
    review: SourceReviewState = SourceReviewState("not-applicable"),
    failure: FailureRecord | None = None,
) -> StageProbe:
    return StageProbe(
        stage=stage,
        decision=decision,
        decision_key=None,
        components=None,
        previous=previous,
        source_observation=source,
        source_review=review,
        failure=failure,
        unavailable_reason=unavailable_reason,
    )


def _resolve_source_review(
    runtime: _Runtime,
    stage_spec,
    *,
    previous: SuccessRecord | None,
    source_observation: SourceObservation,
    decision_key: str,
    decision: Decision,
    partial: dict[int, BatchRecord] | None,
    artifacts_match: bool,
) -> SourceReviewState:
    """Apply current-key partial first, then success baseline for reusable hits only."""

    if partial:
        baseline = _partial_review_fingerprint(runtime, stage_spec.name, decision_key)
        if baseline is not None:
            return _review_state(runtime, stage_spec, source_observation, baseline)

    if (
        previous is not None
        and previous.executed_source.review.fingerprint == source_observation.review.fingerprint
    ):
        return SourceReviewState("current")
    otherwise_reusable = (
        previous is not None
        and previous.input_key == decision_key
        and artifacts_match
        and decision.status in {"hit", "failed", "resume"}
    )
    baseline = None
    if otherwise_reusable:
        assert previous is not None
        baseline = previous.executed_source.review.fingerprint
    return _review_state(runtime, stage_spec, source_observation, baseline)


def _stage_decision(
    runtime: _Runtime,
    stage_spec,
    *,
    source_observation: SourceObservation,
    known_upstream_fingerprints: dict[str, str] | None = None,
    source_review: SourceReviewState | None = None,
    config_access: list[str] | None | object = _INFER_CONFIG_ACCESS,
) -> StageProbe:
    previous = runtime.keying.read_success(runtime.store, stage_spec.name)
    inferred_config_access = config_access is _INFER_CONFIG_ACCESS
    if config_access is _INFER_CONFIG_ACCESS:
        provisional_review = source_review or SourceReviewState("not-applicable")
        config_access = (
            previous.key_components.config_access
            if previous is not None
            and not (
                provisional_review.relationship == "changed"
                and provisional_review.decision == "invalidate"
            )
            else None
        )
    ctx = runtime.context(stage_spec)
    components = compute_key_components(
        stage_spec,
        ctx,
        _upstream_keys(runtime, stage_spec, known_upstream_fingerprints),
        previous.key_components.inputs if previous is not None else None,
        config_access=config_access,  # type: ignore[arg-type]
        dependencies=merge_dependencies(runtime.pipeline.depends, stage_spec.depends),
        fingerprint_session=runtime.keying.fingerprints,
        rerun_source_fingerprint=source_observation.rerun.fingerprint,
    )
    decision_key = input_key(components)
    artifacts_match = True
    if previous is not None and _success_outputs_exist(runtime, previous):
        artifact_root = _current_artifacts(runtime, previous)
        artifacts_match = artifact_root == previous.artifact_fingerprint
    decision, partial, failure = _decision_for_key(
        runtime,
        stage_spec,
        decision_key,
        components,
        previous,
        artifacts_match,
    )
    if (
        inferred_config_access
        and stage_spec.kind == "batch"
        and config_access is not None
        and partial is None
    ):
        attempt = runtime.store.read_attempt(stage_spec.name)
        provenance = attempt or failure
        if provenance is not None and provenance.input_key != decision_key:
            full_config_probe = _stage_decision(
                runtime,
                stage_spec,
                source_observation=source_observation,
                known_upstream_fingerprints=known_upstream_fingerprints,
                config_access=None,
            )
            if (
                full_config_probe.decision_key == provenance.input_key
                and full_config_probe._partial is not None
            ):
                return full_config_probe
    review = source_review or _resolve_source_review(
        runtime,
        stage_spec,
        previous=previous,
        source_observation=source_observation,
        decision_key=decision_key,
        decision=decision,
        partial=partial,
        artifacts_match=artifacts_match,
    )
    if (
        config_access is not None
        and previous is not None
        and source_review is None
        and review.relationship == "changed"
        and review.decision == "invalidate"
        and previous.key_components.config_access is not None
    ):
        # Invalidated review should not reuse the previous projected access set.
        return _stage_decision(
            runtime,
            stage_spec,
            source_observation=source_observation,
            known_upstream_fingerprints=known_upstream_fingerprints,
            source_review=review,
            config_access=None,
        )
    return StageProbe(
        stage=stage_spec.name,
        decision=decision,
        decision_key=decision_key,
        components=components,
        previous=previous,
        source_observation=source_observation,
        source_review=review,
        failure=failure,
        _partial=partial,
        _artifacts_match=artifacts_match,
    )


def _probe_stage(
    runtime: _Runtime,
    stage_name: str,
    *,
    known_upstream_fingerprints: dict[str, str],
    source_observation: SourceObservation,
) -> StageProbe:
    stage_spec = runtime.graph.stages[stage_name]
    probe = _stage_decision(
        runtime,
        stage_spec,
        known_upstream_fingerprints=known_upstream_fingerprints,
        source_observation=source_observation,
    )
    attempt = runtime.store.read_attempt(stage_name)
    if (
        probe.previous is None
        and attempt is not None
        and runtime.store.read_failure(stage_name) is None
        and stage_spec.kind == "single"
    ):
        try:
            _produced_paths(stage_spec.produces, runtime.context(stage_spec))
        except FileNotFoundError:
            pass
    return probe


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
    runtime = _Runtime(pipeline_type, graph, config, args, out, store, keying_session)
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
            source_observation = keying_session.source_observation(pipeline_type, stage_spec, store)
        except Exception as error:  # noqa: BLE001 - status must retain evaluation errors.
            probes.append(
                _unavailable_probe(
                    stage_name,
                    Decision("error", str(error)),
                    str(error),
                    _empty_source_observation(),
                    previous=keying_session.read_success(store, stage_name),
                )
            )
            continue
        if schema_migration:
            assert manifest is not None
            probes.append(
                _unavailable_probe(
                    stage_name,
                    Decision("needs-run", "schema-migration"),
                    f"store schema {manifest.schema_version} must be rebuilt as schema {SCHEMA_VERSION}",
                    source_observation,
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
                _unavailable_probe(
                    stage_name,
                    Decision("needs-run", "no-cache"),
                    f"upstream {missing_upstream} has no success record",
                    source_observation,
                    previous=previous,
                    review=_source_review(runtime, stage_spec, previous, source_observation),
                )
            )
            continue
        previous = keying_session.read_success(store, stage_name)
        try:
            probe = _probe_stage(
                runtime,
                stage_name,
                known_upstream_fingerprints=known_upstream_fingerprints,
                source_observation=source_observation,
            )
        except Exception as error:  # noqa: BLE001 - status must retain evaluation errors.
            probe = _unavailable_probe(
                stage_name,
                Decision("error", str(error)),
                str(error),
                source_observation,
                previous=previous,
                review=_source_review(runtime, stage_spec, previous, source_observation),
                failure=store.read_failure(stage_name),
            )
        probes.append(probe)
        if probe.previous is not None and _success_outputs_exist(runtime, probe.previous):
            known_upstream_fingerprints[stage_name] = _current_artifacts(runtime, probe.previous)
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


def _display(
    graph: PipelineGraph,
    selected: set[str],
    store: Store,
    mode: RunDisplayMode,
    reporter: RunReporter | None = None,
) -> tuple[Any, RunReporter]:
    if reporter is None:
        plan = build_run_display_plan(graph, selected, store, mode=mode)
        reporter = RunReporter(plan, logging.getLogger("varve"))
    return reporter.plan, reporter


async def _drive(
    runtime: _Runtime,
    *,
    upto: str | None,
    downstream: str | None,
    only: str | None,
    force: bool,
    display_mode: RunDisplayMode,
    reporter: RunReporter | None = None,
    slices: tuple[str, ...] = (),
) -> list[StageOutcome]:
    graph, store, out, keying_session = (
        runtime.graph,
        runtime.store,
        runtime.out,
        runtime.keying,
    )
    selected = selected_stages(
        graph,
        upto=upto,
        downstream=downstream,
        only=only,
        slices=slices,
    )
    display_plan, reporter = _display(graph, selected, store, display_mode, reporter)
    preflight_names = graph.closure(selected)
    preflight = probe_pipeline(
        runtime.pipeline,
        runtime.config,
        args=runtime.args,
        out=out,
        graph=graph,
        _keying_session=keying_session,
        _stage_names=preflight_names,
    )
    preflight_by_stage = {probe.stage: probe for probe in preflight}
    external = preflight_names.difference(selected)
    pending_bases: list[str] = []
    seen_pending: set[str] = set()
    for probe in preflight:
        if not (
            probe.source_review.relationship == "changed" and probe.source_review.decision == "none"
        ):
            continue
        if force and probe.stage not in external:
            continue
        base = graph.stages[probe.stage].base_name or probe.stage
        if base in seen_pending:
            continue
        seen_pending.add(base)
        pending_bases.append(base)
    if pending_bases:
        raise ReviewRequiredError(pending_bases)
    errors = [probe for probe in preflight if probe.decision.status == "error"]
    if errors:
        details = "; ".join(f"{probe.stage}: {probe.decision.reason}" for probe in errors)
        raise ValueError(f"Cannot evaluate selected stages: {details}")
    unavailable_external = [
        (probe, status, effective_reason(probe.decision.reason, probe.source_review))
        for probe in preflight
        if probe.stage in external
        and (status := effective_status(probe.decision.status, probe.source_review)) != "hit"
    ]
    if unavailable_external:
        details = "; ".join(
            f"{probe.stage}: {status}: {reason}" for probe, status, reason in unavailable_external
        )
        raise ValueError(f"Upstream stage is not current: {details}")
    execution_source_reviews = {
        probe.stage: (
            SourceReviewState("changed", "invalidate")
            if force and probe.source_review.relationship == "changed"
            else probe.source_review
        )
        for probe in preflight
        if probe.stage in selected
    }
    instance = runtime.pipeline()
    outcomes: list[StageOutcome] = []
    reporter.log_plan()

    for stage_name in graph.topo_order():
        if stage_name not in selected:
            continue
        stage_spec = graph.stages[stage_name]
        reporter.start(stage_name)
        source_observation = preflight_by_stage[stage_name].source_observation
        review = execution_source_reviews[stage_name]
        probe = _stage_decision(
            runtime,
            stage_spec,
            source_observation=source_observation,
            source_review=review,
        )
        previous = probe.previous
        previous_access = probe.components.config_access if probe.components is not None else None
        components = probe.components
        current_key = probe.decision_key
        assert components is not None and current_key is not None
        partial_adoption: tuple[str, str, dict[int, BatchRecord]] | None = None
        decision, partial, failure = probe.decision, probe._partial, probe.failure

        probe_key = current_key
        probe_partial = partial
        if (
            review.relationship == "changed"
            and previous_access is not None
            and (decision.status != "hit" or force)
        ):
            # A reused source change may continue using the old access set
            # while probing a reusable materialization. Once this run will
            # actually execute, use the whole Config as the conservative input
            # basis and let the execution record a new access set.
            probe = _stage_decision(
                runtime,
                stage_spec,
                source_observation=source_observation,
                source_review=review,
                config_access=None,
            )
            components, current_key = probe.components, probe.decision_key
            assert components is not None and current_key is not None
            decision, full_key_partial, failure = probe.decision, probe._partial, probe.failure
            if stage_spec.kind == "batch":
                if (
                    review.relationship == "changed"
                    and review.decision == "reuse"
                    and probe_key != current_key
                    and probe_partial
                ):
                    partial = dict(probe_partial)
                    partial.update(full_key_partial or {})
                    partial_adoption = (probe_key, current_key, partial)
                else:
                    partial = full_key_partial
                if partial is not full_key_partial:
                    decision = decide(
                        kind="batch",
                        current_key=current_key,
                        current_components=components,
                        success=previous,
                        partial=partial,
                        attempt=store.read_attempt(stage_name),
                        output_exists=lambda path: (out / path).exists(),
                        artifacts_match=probe._artifacts_match,
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

        if review.relationship == "changed" and review.decision == "invalidate":
            decision = Decision("needs-run", "source-changed")
        if force:
            decision = Decision("needs-run", "forced")
        if decision.status == "hit":
            _refresh_fingerprint_cache(
                runtime=runtime,
                previous=previous,
                components=components,
            )
            reporter.lifecycle(stage_name, decision.status, decision.reason)
            reporter.input_key(stage_name, current_key)
            outcome = display_plan.outcome(stage_name, decision.status, decision.reason, None)
            outcomes.append(outcome)
            reporter.record(outcome)
            continue
        access = ConfigAccess()
        ctx = runtime.context(
            stage_spec,
            config=RecordingConfig(runtime.config, access),
            resume_skip=decision.resume_skip,
        )
        _validate_static_produces_location(stage_spec.produces, ctx)
        if partial_adoption is not None:
            old_key, new_key, adopted = partial_adoption
            for batch in adopted.values():
                store.write_batch(stage_name, new_key, batch)
            store.clear_partial(stage_name, old_key)
        if stage_spec.kind == "batch":
            if force or (review.relationship == "changed" and review.decision == "invalidate"):
                store.clear_partial(stage_name)
            elif not decision.resume_skip:
                store.clear_partial(stage_name, current_key)
        started = time.monotonic()
        reporter.lifecycle(stage_name, "run", decision.reason)
        reporter.input_key(stage_name, current_key)
        store.write_attempt(
            stage_name,
            AttemptMarker(
                input_key=current_key,
                rerun_source_fingerprint=source_observation.rerun.fingerprint,
                review_source_fingerprint=source_observation.review.fingerprint,
                started_at=_now(),
                touched_existing=previous is not None,
            ),
        )
        if stage_spec.kind == "single":
            try:
                await _execute_stage(instance, stage_spec, ctx)
            except Exception as error:
                _record_stage_failure(runtime, stage_name, error)
                raise
            produces = _produced_paths(stage_spec.produces, ctx)
            outputs = None
            artifact_root = artifacts_root_fingerprint([item.artifact for item in produces])
        else:
            produces = None
            partial_for_outputs = partial if decision.resume_skip else None
            outputs_by_index = {
                index: list(batch.yielded)
                for index, batch in (partial_for_outputs or {}).items()
                if all((out / path).exists() for path in batch.yielded)
            }
            partial_enabled = True
            saw_yield = False
            batch_iterator = _execute_batch(instance, stage_spec, ctx).__aiter__()
            while True:
                try:
                    yielded_index, index_paths = await anext(batch_iterator)
                except StopAsyncIteration:
                    break
                except Exception as error:
                    _record_stage_failure(runtime, stage_name, error)
                    raise
                saw_yield = True
                if not ctx._used_resume and partial_enabled:
                    warnings.warn(
                        f"batch stage {stage_name!r} yielded without iterating ctx.resume; "
                        "its outputs are not resumable. Wrap your iterable in ctx.resume(...) "
                        "to enable per-batch checkpoint resume.",
                        stacklevel=2,
                    )
                    store.clear_partial(stage_name, current_key)
                    outputs_by_index.clear()
                    partial_enabled = False
                index = yielded_index if yielded_index is not None else len(outputs_by_index)
                yielded = []
                for path in index_paths:
                    _, relative = _managed_output(
                        path,
                        ctx,
                        "Yielded varve output",
                        matrix_description="Yielded matrix stage output",
                        cwd_hint=True,
                    )
                    yielded.append(relative)
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
            artifact_root = artifacts_root_fingerprint(
                [item.artifact for item in outputs],
                positions=[
                    (index, ordinal)
                    for index, paths in sorted(outputs_by_index.items())
                    for ordinal, _ in enumerate(paths)
                ],
            )
        elapsed = time.monotonic() - started
        resumed_without_access_baseline = (
            stage_spec.kind == "batch"
            and bool(decision.resume_skip)
            and (
                previous is None
                or previous.executed_source.rerun.fingerprint
                != source_observation.rerun.fingerprint
                or previous.executed_source.review.fingerprint
                != source_observation.review.fingerprint
            )
        )
        committed_access = (
            None
            if resumed_without_access_baseline
            else _merge_config_access(previous, source_observation, access.resolve())
        )
        commit_components = components.model_copy(
            update={
                "config": project_config(config_data(runtime.config), committed_access),
                "config_access": committed_access,
                "rerun_source_fingerprint": source_observation.rerun.fingerprint,
            }
        )
        keying_session.write_success(
            store,
            SuccessRecord(
                pipeline=runtime.pipeline.__name__,
                stage=stage_name,
                kind=stage_spec.kind,
                input_key=input_key(commit_components),
                key_components=commit_components,
                executed_source=source_observation,
                artifact_fingerprint=artifact_root,
                produces=produces,
                outputs=outputs,
                committed_at=_now(),
                elapsed=elapsed,
            ),
        )
        store.clear_attempt(stage_name)
        store.clear_failure(stage_name)
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
        keying = _KeyingSession(fingerprints=FingerprintSession(force_rehash=rehash))
        runtime = _Runtime(pipeline, graph, config, args, out, store, keying)
        try:
            return asyncio.run(
                _drive(
                    runtime,
                    upto=upto,
                    downstream=downstream,
                    only=only,
                    force=force,
                    display_mode=display_mode,
                    reporter=reporter,
                    slices=slices,
                )
            )
        except Exception as error:
            reporter.failure_current(error)
            raise


def record_source_review(
    pipeline: type[Pipeline],
    config: Any,
    *,
    decision: ReviewAction,
    args: Any = None,
    targets: tuple[str, ...] = (),
    cli_out: Path | None = None,
    branch: str = "main",
    is_temporary: bool = False,
    axes: dict[str, tuple[str, ...]] | None = None,
    graph: PipelineGraph | None = None,
    _keying_session: _KeyingSession | None = None,
) -> SourceReviewResult:
    """Atomically validate and record Stage-level reuse/invalidate decisions."""

    if decision not in {"reuse", "invalidate"}:
        raise ValueError(f"Unknown source review decision: {decision}")
    args = args if args is not None else pipeline.Args()
    graph = graph or build_graph(pipeline, axes)
    out = pipeline.output_root(config, cli_out=cli_out, branch=branch, is_temporary=is_temporary)
    store = Store(out)
    keying = _keying_session or _KeyingSession()
    with OutputLock(store.root):
        probes = probe_pipeline(
            pipeline,
            config,
            args=args,
            out=out,
            graph=graph,
            _keying_session=keying,
        )
        errors = [probe for probe in probes if probe.decision.status == "error"]
        if errors:
            details = "; ".join(f"{probe.stage}: {probe.decision.reason}" for probe in errors)
            raise ValueError(f"Cannot evaluate source review: {details}")
        candidates = tuple(
            ReviewCandidate(
                base_stage=graph.stages[probe.stage].base_name or probe.stage,
                review_observation=probe.source_observation.review,
                source_review=probe.source_review,
            )
            for probe in probes
        )
        writes, result = plan_source_review(
            graph,
            targets,
            candidates,
            decision,
        )
        apply_review_writes(store, writes, _now())
        return result


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
    keying = _keying_session or _KeyingSession()
    store = Store(out)
    selected = selected_stages(graph, upto=upto, downstream=downstream, only=only)
    probes = probe_pipeline(
        pipeline,
        config,
        args=args,
        out=out,
        graph=graph,
        _keying_session=keying,
        _stage_names=selected,
    )
    by_stage = {probe.stage: probe for probe in probes}
    display_plan, reporter = _display(graph, selected, store, "expand")
    outcomes = []
    reporter.log_plan()
    for stage_name in graph.topo_order():
        if stage_name not in selected:
            continue
        probe = by_stage[stage_name]
        reporter.start(stage_name)
        if _record_callback is not None:
            _record_callback(stage_name, probe.previous)
            keying.discard_success(store, stage_name)
        if probe.decision_key is not None:
            reporter.lifecycle(stage_name, probe.decision.status, probe.decision.reason)
            reporter.input_key(stage_name, probe.decision_key)
        outcome = display_plan.outcome(
            stage_name, probe.decision.status, probe.decision.reason, None
        )
        outcomes.append(outcome)
        reporter.record(outcome)
    return outcomes
