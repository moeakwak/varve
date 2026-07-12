"""Dashboard-only models assembled from varve store snapshots."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from varve.engine.state import Status

PipelineStatus = Status
ErrorPhase = Literal["manifest", "import", "resolve", "evaluate"]


class StateError(BaseModel):
    phase: ErrorPhase
    message: str


class PipelineEntry(BaseModel):
    output_root: Path
    pipeline_id: str
    pipeline_name: str | None
    branch: str
    module: str | None = None
    manifest_error: str | None = None


class ArtifactState(BaseModel):
    path: Path
    exists: bool


class StageState(BaseModel):
    name: str
    status: Status
    reason: str
    artifacts: list[ArtifactState]
    committed_at: datetime | None
    upstreams: list[str]
    elapsed: float | None = None
    failure: str | None = None
    source_review: Literal["confirmed", "pending", "accepted", "rerun-required"] = "confirmed"


class PipelineState(BaseModel):
    entry: PipelineEntry
    stages: list[StageState]
    status: PipelineStatus
    error: StateError | None = None

    @property
    def pending_reviews(self) -> int:
        return sum(stage.source_review == "pending" for stage in self.stages)

    @property
    def complete(self) -> bool:
        return self.status == "hit" and self.pending_reviews == 0
