"""Pydantic schemas persisted in the varve store."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

SCHEMA_VERSION = 6


class VarveModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Manifest(VarveModel):
    schema_version: int = SCHEMA_VERSION
    pipeline: str
    module: str | None = None
    temporary_config: dict[str, Any] | None = None
    temporary_axes: dict[str, list[str]] | None = None


class FileFingerprint(VarveModel):
    path: str
    kind: Literal["file", "dir"] = "file"
    inode: int
    size: int
    mtime_ns: int
    algorithm: Literal["sha256"] = "sha256"
    cache_version: int = 1
    content_hash: str


class ArtifactManifestEntry(VarveModel):
    path: str
    kind: Literal["file", "dir"]
    fingerprint: FileFingerprint | None = None


class ArtifactFingerprint(VarveModel):
    root: str
    kind: Literal["file", "dir"]
    manifest: list[ArtifactManifestEntry]
    fingerprint: str


class SourceManifestEntry(VarveModel):
    path: str
    cache_path: str
    digest: str
    inode: int
    size: int
    mtime_ns: int
    algorithm: Literal["ast-sha256"] = "ast-sha256"
    cache_version: int = 1


class SourceFingerprint(VarveModel):
    fingerprint: str
    files: list[SourceManifestEntry]


class SourceObservation(VarveModel):
    rerun: SourceFingerprint
    review: SourceFingerprint


class KeyComponents(VarveModel):
    config: dict[str, Any]
    inputs: dict[str, list[FileFingerprint]]
    values: dict[str, Any]
    upstreams: dict[str, dict[str, str]]
    # Top-level config fields this stage read at runtime; None means every field
    # matters (conservative fallback). `config` above is projected onto this set,
    # so unread config fields never enter the input key. Older records without
    # this field default to None and rerun once, then self-heal.
    config_access: list[str] | None = None
    rerun_source_fingerprint: str = ""


class OutputHandle(VarveModel):
    index: int
    path: str
    artifact: ArtifactFingerprint


class ProducedPath(VarveModel):
    path: str
    kind: Literal["file", "dir"]
    artifact: ArtifactFingerprint


class SuccessRecord(VarveModel):
    schema_version: int = SCHEMA_VERSION
    pipeline: str
    stage: str
    kind: Literal["single", "batch"]
    input_key: str
    key_components: KeyComponents
    executed_source: SourceObservation
    artifact_fingerprint: str
    outputs: list[OutputHandle] | None = None
    produces: list[ProducedPath] | None = None
    committed_at: str
    elapsed: float | None = None

    @property
    def paths(self) -> list[str]:
        if self.kind == "single":
            assert self.produces is not None
            return [item.path for item in self.produces]
        assert self.outputs is not None
        return [item.path for item in sorted(self.outputs, key=lambda item: item.index)]

    @model_validator(mode="after")
    def validate_outputs_shape(self) -> SuccessRecord:
        if self.kind == "batch":
            if self.outputs is None or self.produces is not None:
                raise ValueError("batch success records must have outputs and no produces")
        elif self.outputs is not None or self.produces is None:
            raise ValueError("single success records must have produces and no outputs")
        return self


class BatchRecord(VarveModel):
    index: int
    yielded: list[str]
    artifacts: list[ArtifactFingerprint]
    committed_at: str
    total: int | None = None


class AttemptMarker(VarveModel):
    input_key: str
    rerun_source_fingerprint: str
    review_source_fingerprint: str
    started_at: str
    touched_existing: bool


class ReviewRecord(VarveModel):
    review_fingerprint: str
    review_observation: SourceFingerprint
    decision: Literal["reuse", "invalidate"]
    decided_at: str


class FailureRecord(VarveModel):
    pipeline: str
    stage: str
    input_key: str
    rerun_source_fingerprint: str
    review_source_fingerprint: str
    exception_type: str
    message: str
    failed_at: str
