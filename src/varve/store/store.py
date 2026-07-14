"""Persistent snapshot store for varve runs."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError

from varve.models import (
    SCHEMA_VERSION,
    AttemptMarker,
    BatchRecord,
    FailureRecord,
    Manifest,
    ReviewRecord,
    SuccessRecord,
    VarveModel,
)

ModelT = TypeVar("ModelT", bound=VarveModel)


class CorruptStore(Exception):  # noqa: N818 - public-facing store must surface corruption explicitly.
    """Raised when a store file exists but cannot be parsed as expected."""


def _atomic_write_json(path: Path, model: VarveModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        model.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _read_model(
    path: Path,
    model_type: type[ModelT],
    *,
    require_current_schema: bool = False,
) -> ModelT | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if require_current_schema and data.get("schema_version") != SCHEMA_VERSION:
            return None
        return model_type.model_validate(data)
    except (json.JSONDecodeError, OSError, ValidationError) as error:
        raise CorruptStore(f"Cannot read varve store file {path}: {error}") from error


class Store:
    """Latest-wins snapshot store for a varve run, holding success records, attempt markers, and partial scratch (no append-only history)."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.root = output_root / ".varve"

    def read_manifest(self) -> Manifest | None:
        return _read_model(self.root / "manifest.json", Manifest)

    def ensure_initialized(
        self,
        pipeline: str,
        *,
        module: str | None = None,
        temporary_config: dict[str, Any] | None = None,
        temporary_axes: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".gitignore").write_text("*\n", encoding="utf-8")
        normalized_axes = (
            {name: list(values) for name, values in temporary_axes.items()}
            if temporary_axes is not None
            else None
        )

        manifest_path = self.root / "manifest.json"
        manifest = self.read_manifest()
        if manifest is None:
            _atomic_write_json(
                manifest_path,
                Manifest(
                    pipeline=pipeline,
                    module=module,
                    temporary_config=temporary_config,
                    temporary_axes=normalized_axes,
                ),
            )
            return
        if manifest.pipeline != pipeline:
            raise ValueError(
                f"Varve store belongs to {manifest.pipeline}, not {pipeline}: {manifest_path}"
            )
        if temporary_config is not None and manifest.temporary_config != temporary_config:
            raise ValueError(f"Varve store has a different temporary config: {manifest_path}")
        if normalized_axes is not None and manifest.temporary_axes != normalized_axes:
            raise ValueError(f"Varve store has different temporary axes: {manifest_path}")
        if manifest.schema_version != SCHEMA_VERSION:
            logging.getLogger("varve").warning(
                "rebuilding varve store schema %s as schema %s: %s",
                manifest.schema_version,
                SCHEMA_VERSION,
                manifest_path,
            )
            _atomic_write_json(
                manifest_path,
                manifest.model_copy(
                    update={
                        "schema_version": SCHEMA_VERSION,
                        "module": module if module is not None else manifest.module,
                    }
                ),
            )
            for directory in ("reviews", "failures", "attempts", "partial"):
                shutil.rmtree(self.root / directory, ignore_errors=True)
            return
        if module is not None and manifest.module != module:
            _atomic_write_json(
                manifest_path,
                manifest.model_copy(update={"module": module}),
            )

    def _stage_path(self, directory: str, stage: str) -> Path:
        return self.root / directory / f"{stage}.json"

    def read_success(self, stage: str) -> SuccessRecord | None:
        return _read_model(
            self._stage_path("stages", stage),
            SuccessRecord,
            require_current_schema=True,
        )

    def write_success(self, record: SuccessRecord) -> None:
        _atomic_write_json(self._stage_path("stages", record.stage), record)

    def read_attempt(self, stage: str) -> AttemptMarker | None:
        return _read_model(self._stage_path("attempts", stage), AttemptMarker)

    def write_attempt(self, stage: str, marker: AttemptMarker) -> None:
        _atomic_write_json(self._stage_path("attempts", stage), marker)

    def clear_attempt(self, stage: str) -> None:
        self._stage_path("attempts", stage).unlink(missing_ok=True)

    def read_review(self, stage: str) -> ReviewRecord | None:
        return _read_model(self._stage_path("reviews", stage), ReviewRecord)

    def write_review(self, stage: str, record: ReviewRecord) -> None:
        _atomic_write_json(self._stage_path("reviews", stage), record)

    def clear_review(self, stage: str) -> None:
        self._stage_path("reviews", stage).unlink(missing_ok=True)

    def read_failure(self, stage: str) -> FailureRecord | None:
        return _read_model(self._stage_path("failures", stage), FailureRecord)

    def write_failure(self, stage: str, record: FailureRecord) -> None:
        _atomic_write_json(self._stage_path("failures", stage), record)

    def clear_failure(self, stage: str) -> None:
        self._stage_path("failures", stage).unlink(missing_ok=True)

    def read_partial(
        self,
        stage: str,
        input_key: str,
    ) -> dict[int, BatchRecord] | None:
        partial_root = self.root / "partial" / stage / input_key
        if not partial_root.exists():
            return None
        batches_root = partial_root / "batches"
        batches: dict[int, BatchRecord] = {}
        for path in sorted(batches_root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as error:
                raise CorruptStore(f"Cannot read varve store file {path}: {error}") from error
            if "artifacts" not in raw:
                continue
            try:
                batch = BatchRecord.model_validate(raw)
            except ValidationError as error:
                raise CorruptStore(f"Cannot read varve store file {path}: {error}") from error
            batches[batch.index] = batch
        return batches

    def write_batch(self, stage: str, input_key: str, record: BatchRecord) -> None:
        _atomic_write_json(
            self.root / "partial" / stage / input_key / "batches" / f"{record.index}.json",
            record,
        )

    def clear_partial(self, stage: str, input_key: str | None = None) -> None:
        path = self.root / "partial" / stage
        if input_key is not None:
            path /= input_key
        shutil.rmtree(path, ignore_errors=True)
