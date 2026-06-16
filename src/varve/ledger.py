"""Persistent ledger storage for varve runs."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from varve.models import (
    BatchRecord,
    Manifest,
    PartialMeta,
    SuccessRecord,
    VarveModel,
)

ModelT = TypeVar("ModelT", bound=VarveModel)


class CorruptLedger(Exception):  # noqa: N818 - plan and public API use this term.
    """Raised when a ledger file exists but cannot be parsed as expected."""


def _atomic_write_json(path: Path, model: VarveModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        model.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _read_model[ModelT: VarveModel](path: Path, model_type: type[ModelT]) -> ModelT | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return model_type.model_validate(data)
    except (json.JSONDecodeError, OSError, ValidationError) as error:
        raise CorruptLedger(f"Corrupt varve ledger file: {path}") from error


class Ledger:
    """Atomic JSON ledger rooted at `<output_root>/.varve`."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.root = output_root / ".varve"

    def ensure_initialized(self, experiment: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".gitignore").write_text("*\n", encoding="utf-8")

        manifest_path = self.root / "manifest.json"
        manifest = _read_model(manifest_path, Manifest)
        if manifest is None:
            _atomic_write_json(manifest_path, Manifest(experiment=experiment))
            return
        if manifest.experiment != experiment:
            raise ValueError(
                f"Varve ledger belongs to {manifest.experiment}, not {experiment}: "
                f"{manifest_path}"
            )

    def read_success(self, stage: str) -> SuccessRecord | None:
        return _read_model(self.root / "stages" / f"{stage}.json", SuccessRecord)

    def write_success(self, record: SuccessRecord) -> None:
        _atomic_write_json(self.root / "stages" / f"{record.stage}.json", record)

    def read_attempt(self, stage: str):
        from varve.models import AttemptMarker

        return _read_model(self.root / "attempts" / f"{stage}.json", AttemptMarker)

    def write_attempt(self, stage: str, marker) -> None:
        _atomic_write_json(self.root / "attempts" / f"{stage}.json", marker)

    def clear_attempt(self, stage: str) -> None:
        (self.root / "attempts" / f"{stage}.json").unlink(missing_ok=True)

    def read_partial(
        self,
        stage: str,
        run_key: str,
    ) -> tuple[PartialMeta, dict[int, BatchRecord]] | None:
        partial_root = self.root / "partial" / stage / run_key
        meta = _read_model(partial_root / "meta.json", PartialMeta)
        if meta is None:
            return None
        batches_root = partial_root / "batches"
        batches: dict[int, BatchRecord] = {}
        if batches_root.exists():
            for path in sorted(batches_root.glob("*.json")):
                batch = _read_model(path, BatchRecord)
                if batch is None:
                    continue
                batches[batch.index] = batch
        return meta, batches

    def write_partial_meta(self, stage: str, run_key: str, meta: PartialMeta) -> None:
        _atomic_write_json(self.root / "partial" / stage / run_key / "meta.json", meta)

    def write_batch(self, stage: str, run_key: str, record: BatchRecord) -> None:
        _atomic_write_json(
            self.root / "partial" / stage / run_key / "batches" / f"{record.index}.json",
            record,
        )

    def clear_partial(self, stage: str, run_key: str | None = None) -> None:
        if run_key is None:
            shutil.rmtree(self.root / "partial" / stage, ignore_errors=True)
        else:
            shutil.rmtree(self.root / "partial" / stage / run_key, ignore_errors=True)
