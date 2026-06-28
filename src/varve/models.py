"""Pydantic schemas persisted in the varve store."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

SCHEMA_VERSION = 1


class VarveModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Manifest(VarveModel):
    schema_version: int = SCHEMA_VERSION
    experiment: str
    temporary_config: dict[str, Any] | None = None


class FileFingerprint(VarveModel):
    path: str
    size: int
    mtime: float
    sha256: str


class KeyComponents(VarveModel):
    source: dict[str, str]
    config: dict[str, Any]
    files: dict[str, list[FileFingerprint]]
    values: dict[str, Any]
    upstreams: dict[str, dict[str, str]]


class OutputHandle(VarveModel):
    index: int
    path: str


class ProducedPath(VarveModel):
    path: str
    kind: Literal["file", "dir"]


class SuccessRecord(VarveModel):
    schema_version: int = SCHEMA_VERSION
    experiment: str
    stage: str
    kind: Literal["single", "batch"]
    content_key: str
    key_components: KeyComponents
    partition_values: dict[str, Any] = {}
    outputs: list[OutputHandle] | None = None
    produces: list[ProducedPath] | None = None
    committed_at: str

    @model_validator(mode="after")
    def validate_outputs_shape(self) -> SuccessRecord:
        if self.kind == "batch":
            if self.outputs is None or self.produces is not None:
                raise ValueError("batch success records must have outputs and no produces")
        elif self.outputs is not None or self.produces is None:
            raise ValueError("single success records must have produces and no outputs")
        return self


class PartialMeta(VarveModel):
    content_key: str
    partition_values: dict[str, Any]
    started_at: str


class BatchRecord(VarveModel):
    index: int
    yielded: list[str]
    committed_at: str


class AttemptMarker(VarveModel):
    content_key: str
    started_at: str
    touched_existing: bool
