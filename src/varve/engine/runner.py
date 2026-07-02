"""Stage execution runner."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from varve.context import Ctx
from varve.engine.state import Decision, Status, decide_batch, decide_single
from varve.experiment import Experiment
from varve.keying.keys import compute_key_components, content_key, run_key
from varve.models import (
    AttemptMarker,
    BatchRecord,
    KeyComponents,
    OutputHandle,
    PartialMeta,
    ProducedPath,
    SuccessRecord,
)
from varve.store.lock import OutputLock
from varve.store.store import Store


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    status: Status
    reason: str
    elapsed: float | None


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _relative_to_out(path: Path, out: Path) -> str:
    resolved = path.resolve()
    out_resolved = out.resolve()
    try:
        return str(resolved.relative_to(out_resolved))
    except ValueError as error:
        raise ValueError(
            f"Yielded varve output must live inside the output root: {resolved} "
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
) -> None:
    """Rewrite a hit stage's success record when file fingerprints drifted.

    A content-key hit guarantees identical file sha256 digests, since the key
    only folds in digests. But a file may have been touched (new mtime/size)
    without its content changing. The freshly computed `components` already
    carry the refreshed fingerprints; persisting them avoids re-hashing the
    same unchanged bytes on every subsequent run while leaving the content key
    untouched. Only the size/mtime metadata moves.
    """

    if previous is None:
        return
    if previous.key_components.files == components.files:
        return
    refreshed = previous.model_copy(
        update={
            "key_components": previous.key_components.model_copy(update={"files": components.files})
        }
    )
    store.write_success(refreshed)


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


def _stage_sets(
    experiment_type: type[Experiment],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
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
    upto: str | None = None,
    downstream: str | None = None,
) -> set[str]:
    specified = [item is not None for item in (upto, downstream)]
    if sum(specified) > 1:
        raise ValueError("upto and downstream are mutually exclusive")
    stages = experiment_type.stages()
    for name in (upto, downstream):
        if name is not None and name not in stages:
            raise ValueError(f"Unknown varve stage: {name}")
    ancestors, descendants = _stage_sets(experiment_type)
    if downstream is not None:
        return _closure(downstream, descendants)
    if upto is not None:
        return _closure(upto, ancestors)
    return set(stages)


def _upstream_keys(
    stage_spec,
    store: Store,
    known_content_keys: dict[str, str] | None = None,
) -> dict[str, str]:
    keys: dict[str, str] = {}
    for name in stage_spec.needs:
        if known_content_keys is not None and name in known_content_keys:
            keys[name] = known_content_keys[name]
            continue
        record = store.read_success(name)
        if record is None:
            raise ValueError(f"Upstream stage has no success record: {name}")
        keys[name] = record.content_key
    return keys


def _validate_external_upstreams(
    experiment_type: type[Experiment],
    selected: set[str],
    store: Store,
    out: Path,
) -> None:
    stages = experiment_type.stages()
    for stage_name in selected:
        for upstream in stages[stage_name].needs:
            if upstream in selected:
                continue
            attempt = store.read_attempt(upstream)
            record = store.read_success(upstream)
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
    args,
    out: Path,
    upto: str | None,
    downstream: str | None,
    force: bool,
    execute: bool,
) -> list[StageOutcome]:
    store = Store(out)
    selected = selected_stages(
        experiment_type,
        upto=upto,
        downstream=downstream,
    )
    if execute:
        _validate_external_upstreams(experiment_type, selected, store, out)

    instance = experiment_type()
    outcomes: list[StageOutcome] = []
    known_content_keys: dict[str, str] = {}
    logger = logging.getLogger("varve")
    logger.info(
        "plan: %s", " -> ".join(name for name in experiment_type.topo_order() if name in selected)
    )

    for stage_name in experiment_type.topo_order():
        if stage_name not in selected:
            continue
        stage_spec = experiment_type.stages()[stage_name]
        if not execute:
            missing_upstream = any(store.read_success(name) is None for name in stage_spec.needs)
            if missing_upstream:
                outcomes.append(StageOutcome(stage_name, "no-cache", "no cache", None))
                continue
        upstream_keys = _upstream_keys(
            stage_spec,
            store,
            known_content_keys if not execute else None,
        )
        previous = store.read_success(stage_name)
        cached_files = previous.key_components.files if previous is not None else None
        ctx_for_key = Ctx(config=config, args=args, out=out, store=store)
        components = compute_key_components(stage_spec, ctx_for_key, upstream_keys, cached_files)
        current_key = content_key(components)
        known_content_keys[stage_name] = current_key
        attempt = store.read_attempt(stage_name)
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
            partial = store.read_partial(stage_name, current_run_key)
            partial_run_key = (
                run_key(partial[0].content_key, partial[0].partition_values)
                if partial is not None
                else None
            )
            attempt_for_decision = attempt
            if previous is None and partial is not None:
                attempt_for_decision = None
            decision = decide_batch(
                current_key=current_key,
                current_components=components,
                current_partition=partition,
                run_key=current_run_key,
                partial_run_key=partial_run_key,
                success=previous,
                partial=partial,
                attempt=attempt_for_decision,
                output_exists=lambda path: (out / path).exists(),
            )

        if force:
            decision = Decision("stale" if previous else "no-cache", "forced")
        if execute and decision.status == "hit":
            _refresh_fingerprint_cache(
                store=store,
                previous=previous,
                components=components,
            )
        if not execute or decision.status == "hit":
            logger.info(
                "[%s] %s%s",
                stage_name,
                decision.status,
                f" · {decision.reason}" if decision.reason != decision.status else "",
            )
            logger.debug("[%s] content_key %s", stage_name, current_key)
            outcomes.append(StageOutcome(stage_name, decision.status, decision.reason, None))
            continue
        if decision.status == "unrecoverable":
            raise RuntimeError(f"[{stage_name}] {decision.reason}")

        started = time.monotonic()
        logger.info("[%s] run · %s", stage_name, decision.reason)
        logger.debug("[%s] content_key %s", stage_name, current_key)
        store.write_attempt(
            stage_name,
            AttemptMarker(
                content_key=current_key,
                started_at=_now(),
                touched_existing=previous is not None,
            ),
        )
        ctx = Ctx(
            config=config,
            args=args,
            out=out,
            store=store,
            resume_skip=decision.resume_skip,
            stage_name=stage_name,
        )
        if stage_spec.kind == "single":
            await _execute_stage(instance, stage_spec, ctx)
            produces = _produced_paths(stage_spec.produces, ctx)
            elapsed = time.monotonic() - started
            store.write_success(
                SuccessRecord(
                    experiment=experiment_type.__name__,
                    stage=stage_name,
                    kind="single",
                    content_key=current_key,
                    key_components=components,
                    produces=produces,
                    committed_at=_now(),
                    elapsed=elapsed,
                )
            )
        else:
            if force or decision.status != "resume":
                store.clear_partial(stage_name, current_run_key)
            partial_for_outputs = partial if decision.status == "resume" else None
            outputs_by_index = _batch_outputs_from_records(
                previous=previous,
                partial=partial_for_outputs,
                out=out,
                force=force,
            )
            store.write_partial_meta(
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
                        hint = _cwd_relative_path_hint(path, out)
                        if hint is not None:
                            raise ValueError(hint)
                        raise FileNotFoundError(f"Yielded varve output does not exist: {absolute}")
                    yielded.append(_relative_to_out(absolute, out))
                store.write_batch(
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
            elapsed = time.monotonic() - started
            store.write_success(
                SuccessRecord(
                    experiment=experiment_type.__name__,
                    stage=stage_name,
                    kind="batch",
                    content_key=current_key,
                    key_components=components,
                    partition_values=partition,
                    outputs=outputs,
                    committed_at=_now(),
                    elapsed=elapsed,
                )
            )
        store.clear_attempt(stage_name)
        logger.info("[%s] done · %.2fs", stage_name, elapsed)
        outcomes.append(StageOutcome(stage_name, decision.status, decision.reason, elapsed))
    return outcomes


def run(
    experiment: type[Experiment],
    config,
    *,
    args=None,
    upto: str | None = None,
    downstream: str | None = None,
    force: bool = False,
    cli_out: Path | None = None,
    branch: str = "main",
    is_temporary: bool = False,
    temporary_config: dict[str, Any] | None = None,
) -> list[StageOutcome]:
    if args is None:
        args = experiment.Args()
    out = experiment.output_root(
        config,
        cli_out=cli_out,
        branch=branch,
        is_temporary=is_temporary,
    )
    store = Store(out)
    store.root.mkdir(parents=True, exist_ok=True)
    with OutputLock(store.root):
        store.ensure_initialized(
            experiment.__name__,
            module=experiment.import_module_name(),
            temporary_config=temporary_config,
        )
        return asyncio.run(
            _drive(
                experiment,
                config,
                args=args,
                out=out,
                upto=upto,
                downstream=downstream,
                force=force,
                execute=True,
            )
        )


def evaluate_state(
    experiment: type[Experiment],
    config,
    *,
    args=None,
    upto: str | None = None,
    downstream: str | None = None,
    cli_out: Path | None = None,
    branch: str = "main",
    is_temporary: bool = False,
) -> list[StageOutcome]:
    if args is None:
        args = experiment.Args()
    out = experiment.output_root(
        config,
        cli_out=cli_out,
        branch=branch,
        is_temporary=is_temporary,
    )
    return asyncio.run(
        _drive(
            experiment,
            config,
            args=args,
            out=out,
            upto=upto,
            downstream=downstream,
            force=False,
            execute=False,
        )
    )
