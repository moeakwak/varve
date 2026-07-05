"""Persistent snapshot store for varve runs."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError

from varve.models import (
    AttemptMarker,
    BatchRecord,
    Manifest,
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


def _read_model(path: Path, model_type: type[ModelT]) -> ModelT | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return model_type.model_validate(data)
    except (json.JSONDecodeError, OSError, ValidationError) as error:
        raise CorruptStore(f"Corrupt varve store file: {path}") from error


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
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".gitignore").write_text("*\n", encoding="utf-8")

        manifest_path = self.root / "manifest.json"
        manifest = self.read_manifest()
        if manifest is None:
            _atomic_write_json(
                manifest_path,
                Manifest(
                    pipeline=pipeline,
                    module=module,
                    temporary_config=temporary_config,
                ),
            )
            return
        if manifest.pipeline != pipeline:
            raise ValueError(
                f"Varve store belongs to {manifest.pipeline}, not {pipeline}: {manifest_path}"
            )
        if temporary_config is not None and manifest.temporary_config != temporary_config:
            raise ValueError(f"Varve store has a different temporary config: {manifest_path}")
        if module is not None and manifest.module != module:
            _atomic_write_json(
                manifest_path,
                Manifest(
                    pipeline=manifest.pipeline,
                    module=module if module is not None else manifest.module,
                    temporary_config=manifest.temporary_config,
                ),
            )

    def read_success(self, stage: str) -> SuccessRecord | None:
        return _read_model(self.root / "stages" / f"{stage}.json", SuccessRecord)

    def write_success(self, record: SuccessRecord) -> None:
        _atomic_write_json(self.root / "stages" / f"{record.stage}.json", record)

    def read_attempt(self, stage: str) -> AttemptMarker | None:
        return _read_model(self.root / "attempts" / f"{stage}.json", AttemptMarker)

    def write_attempt(self, stage: str, marker: AttemptMarker) -> None:
        _atomic_write_json(self.root / "attempts" / f"{stage}.json", marker)

    def clear_attempt(self, stage: str) -> None:
        (self.root / "attempts" / f"{stage}.json").unlink(missing_ok=True)

    def read_partial(
        self,
        stage: str,
        content_key: str,
    ) -> dict[int, BatchRecord] | None:
        partial_root = self.root / "partial" / stage / content_key
        if not partial_root.exists():
            return None
        batches_root = partial_root / "batches"
        batches: dict[int, BatchRecord] = {}
        for path in sorted(batches_root.glob("*.json")):
            batch = _read_model(path, BatchRecord)
            if batch is None:
                continue
            batches[batch.index] = batch
        return batches

    def write_batch(self, stage: str, content_key: str, record: BatchRecord) -> None:
        _atomic_write_json(
            self.root / "partial" / stage / content_key / "batches" / f"{record.index}.json",
            record,
        )

    def clear_partial(self, stage: str, content_key: str | None = None) -> None:
        path = self.root / "partial" / stage
        if content_key is not None:
            path /= content_key
        shutil.rmtree(path, ignore_errors=True)
