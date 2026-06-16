"""Stage execution runner."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from varve.context import Ctx
from varve.experiment import Experiment
from varve.keys import compute_key_components, content_key, run_key
from varve.ledger import Ledger
from varve.lock import OutputLock
from varve.log import get_logger
from varve.models import (
    AttemptMarker,
    BatchRecord,
    OutputHandle,
    PartialMeta,
    ProducedPath,
    SuccessRecord,
)
from varve.state import Decision, Status, decide_batch, decide_single


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    status: Status
    reason: str
    elapsed: float | None


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _relative_to_out(path: Path, out: Path) -> str:
    return str(path.resolve().relative_to(out.resolve()))


def _produced_paths(produces, ctx: Ctx) -> list[ProducedPath]:
    if produces is None:
        return []
    raw = produces(ctx) if callable(produces) else produces
    paths = [raw] if isinstance(raw, str) else list(raw)
    result = []
    for item in paths:
        path = ctx.out / item
        if not path.exists():
            raise FileNotFoundError(f"Declared varve output does not exist: {path}")
        result.append(ProducedPath(path=str(item), kind="dir" if path.is_dir() else "file"))
    return result


def _success_outputs_exist(record: SuccessRecord, out: Path) -> bool:
    if record.kind == "single":
        assert record.produces is not None
        return all((out / item.path).exists() for item in record.produces)
    assert record.outputs is not None
    return all((out / item.path).exists() for item in record.outputs)


def _partition_values(stage_spec, config) -> dict[str, Any]:
    data = config.model_dump(mode="json") if hasattr(config, "model_dump") else vars(config)
    return {name: data[name] for name in stage_spec.partition_key}


def _stage_sets(experiment_type: type[Experiment]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    stages = experiment_type.stages()
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
    experiment_type: type[Experiment],
    *,
    target: str | None = None,
    only: str | None = None,
    downstream: str | None = None,
) -> set[str]:
    specified = [item is not None for item in (target, only, downstream)]
    if sum(specified) > 1:
        raise ValueError("target, only, and downstream are mutually exclusive")
    stages = experiment_type.stages()
    for name in (target, only, downstream):
        if name is not None and name not in stages:
            raise ValueError(f"Unknown varve stage: {name}")
    ancestors, descendants = _stage_sets(experiment_type)
    if only is not None:
        return {only}
    if downstream is not None:
        return _closure(downstream, descendants)
    if target is not None:
        return _closure(target, ancestors)
    return set(stages)


def _upstream_keys(stage_spec, ledger: Ledger) -> dict[str, str]:
    keys: dict[str, str] = {}
    for name in stage_spec.needs:
        record = ledger.read_success(name)
        if record is None:
            raise ValueError(f"Upstream stage has no success record: {name}")
        keys[name] = record.content_key
    return keys


def _upstream_keys_from_known(
    stage_spec,
    ledger: Ledger,
    known_content_keys: dict[str, str],
) -> dict[str, str]:
    keys: dict[str, str] = {}
    for name in stage_spec.needs:
        if name in known_content_keys:
            keys[name] = known_content_keys[name]
            continue
        record = ledger.read_success(name)
        if record is None:
            raise ValueError(f"Upstream stage has no success record: {name}")
        keys[name] = record.content_key
    return keys


def _validate_external_upstreams(
    experiment_type: type[Experiment],
    selected: set[str],
    ledger: Ledger,
    out: Path,
) -> None:
    stages = experiment_type.stages()
    for stage_name in selected:
        for upstream in stages[stage_name].needs:
            if upstream in selected:
                continue
            attempt = ledger.read_attempt(upstream)
            record = ledger.read_success(upstream)
            if attempt is not None:
                raise ValueError(f"Upstream stage is dirty: {upstream}")
            if record is None:
                raise ValueError(f"Upstream stage has not been built: {upstream}")
            if not _success_outputs_exist(record, out):
                raise ValueError(f"Upstream stage artifacts are missing: {upstream}")


def _batch_outputs_from_records(
    *,
    previous: SuccessRecord | None,
    partial: tuple[PartialMeta, dict[int, BatchRecord]] | None,
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
        _, batches = partial
        for index, batch in batches.items():
            if all((out / path).exists() for path in batch.yielded):
                outputs[index] = list(batch.yielded)
    return outputs


async def _execute_stage(instance, stage_spec, ctx: Ctx) -> None:
    result = stage_spec.func(instance, ctx)
    if inspect.isawaitable(result):
        await result


async def _execute_batch(instance, stage_spec, ctx: Ctx):
    generator = stage_spec.func(instance, ctx)
    if not hasattr(generator, "__aiter__"):
        raise TypeError(f"Batch stage must return an async iterator: {stage_spec.name}")
    async for yielded in generator:
        if isinstance(yielded, list | tuple):
            yield ctx._current_batch_index, [Path(item) for item in yielded]
        else:
            yield ctx._current_batch_index, [Path(yielded)]


async def _drive(
    experiment_type: type[Experiment],
    config,
    *,
    target: str | None,
    only: str | None,
    downstream: str | None,
    force: bool,
    dry: bool,
) -> list[StageOutcome]:
    out = experiment_type.output_root(config)
    ledger = Ledger(out)
    selected = selected_stages(
        experiment_type,
        target=target,
        only=only,
        downstream=downstream,
    )
    if not dry:
        _validate_external_upstreams(experiment_type, selected, ledger, out)

    instance = experiment_type()
    outcomes: list[StageOutcome] = []
    known_content_keys: dict[str, str] = {}
    logger = get_logger()
    logger.info("plan: %s", " -> ".join(name for name in experiment_type.topo_order() if name in selected))

    for stage_name in experiment_type.topo_order():
        if stage_name not in selected:
            continue
        stage_spec = experiment_type.stages()[stage_name]
        if dry:
            missing_upstream = any(ledger.read_success(name) is None for name in stage_spec.needs)
            if missing_upstream:
                outcomes.append(StageOutcome(stage_name, "no-cache", "no cache", None))
                continue
        upstream_keys = (
            _upstream_keys_from_known(stage_spec, ledger, known_content_keys)
            if dry
            else _upstream_keys(stage_spec, ledger)
        )
        previous = ledger.read_success(stage_name)
        cached_files = previous.key_components.files if previous is not None else None
        ctx_for_key = Ctx(config=config, out=out, ledger=ledger)
        components = compute_key_components(stage_spec, ctx_for_key, upstream_keys, cached_files)
        current_key = content_key(components)
        known_content_keys[stage_name] = current_key
        attempt = ledger.read_attempt(stage_name)
        partition = _partition_values(stage_spec, config) if stage_spec.kind == "batch" else {}
        current_run_key = run_key(current_key, partition)

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
            )
        else:
            partial = ledger.read_partial(stage_name, current_run_key)
            attempt_for_decision = attempt
            if previous is None and partial is not None:
                attempt_for_decision = None
            decision = decide_batch(
                current_key=current_key,
                current_components=components,
                current_partition=partition,
                run_key=current_run_key,
                success=previous,
                partial=partial,
                attempt=attempt_for_decision,
                output_exists=lambda path: (out / path).exists(),
            )

        if force:
            decision = Decision("stale" if previous else "no-cache", "forced")
        if dry or decision.status == "hit":
            logger.info("[%s] %s%s", stage_name, decision.status, f" · {decision.reason}" if decision.reason != decision.status else "")
            logger.debug("[%s] content_key %s", stage_name, current_key)
            outcomes.append(StageOutcome(stage_name, decision.status, decision.reason, None))
            continue
        if decision.status == "unrecoverable":
            raise RuntimeError(f"[{stage_name}] {decision.reason}")

        started = time.monotonic()
        logger.info("[%s] run · %s", stage_name, decision.reason)
        logger.debug("[%s] content_key %s", stage_name, current_key)
        ledger.write_attempt(
            stage_name,
            AttemptMarker(
                content_key=current_key,
                started_at=_now(),
                touched_existing=previous is not None,
            ),
        )
        ctx = Ctx(config=config, out=out, ledger=ledger, resume_skip=decision.resume_skip)
        if stage_spec.kind == "single":
            await _execute_stage(instance, stage_spec, ctx)
            produces = _produced_paths(stage_spec.produces, ctx)
            ledger.write_success(
                SuccessRecord(
                    experiment=experiment_type.__name__,
                    stage=stage_name,
                    kind="single",
                    content_key=current_key,
                    key_components=components,
                    produces=produces,
                    committed_at=_now(),
                )
            )
        else:
            if force:
                ledger.clear_partial(stage_name, current_run_key)
            partial = ledger.read_partial(stage_name, current_run_key)
            outputs_by_index = _batch_outputs_from_records(
                previous=previous,
                partial=partial,
                out=out,
                force=force,
            )
            ledger.write_partial_meta(
                stage_name,
                current_run_key,
                PartialMeta(
                    content_key=current_key,
                    partition_values=partition,
                    started_at=_now(),
                ),
            )
            async for yielded_index, index_paths in _execute_batch(instance, stage_spec, ctx):
                index = yielded_index if yielded_index is not None else len(outputs_by_index)
                yielded = []
                for path in index_paths:
                    absolute = path if path.is_absolute() else out / path
                    if not absolute.exists():
                        raise FileNotFoundError(f"Yielded varve output does not exist: {absolute}")
                    yielded.append(_relative_to_out(absolute, out))
                ledger.write_batch(
                    stage_name,
                    current_run_key,
                    BatchRecord(index=index, yielded=yielded, committed_at=_now()),
                )
                outputs_by_index[index] = yielded
            outputs = [
                OutputHandle(index=index, path=path)
                for index, paths in sorted(outputs_by_index.items())
                for path in paths
            ]
            ledger.write_success(
                SuccessRecord(
                    experiment=experiment_type.__name__,
                    stage=stage_name,
                    kind="batch",
                    content_key=current_key,
                    key_components=components,
                    partition_values=partition,
                    outputs=outputs,
                    committed_at=_now(),
                )
            )
        ledger.clear_attempt(stage_name)
        elapsed = time.monotonic() - started
        logger.info("[%s] done · %.2fs", stage_name, elapsed)
        outcomes.append(
            StageOutcome(stage_name, decision.status, decision.reason, elapsed)
        )
    return outcomes


def run(
    experiment: type[Experiment],
    config,
    *,
    target: str | None = None,
    only: str | None = None,
    downstream: str | None = None,
    force: bool = False,
    dry: bool = False,
) -> list[StageOutcome]:
    out = experiment.output_root(config)
    if dry:
        return asyncio.run(
            _drive(
                experiment,
                config,
                target=target,
                only=only,
                downstream=downstream,
                force=force,
                dry=True,
            )
        )
    ledger = Ledger(out)
    ledger.root.mkdir(parents=True, exist_ok=True)
    with OutputLock(ledger.root):
        ledger.ensure_initialized(experiment.__name__)
        return asyncio.run(
            _drive(
                experiment,
                config,
                target=target,
                only=only,
                downstream=downstream,
                force=force,
                dry=False,
            )
        )
