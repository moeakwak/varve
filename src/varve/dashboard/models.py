"""Dashboard-only models assembled from varve store snapshots."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

StageStatus = Literal["ok", "artifact-missing", "interrupted", "corrupt"]
OverallStatus = Literal["ok", "artifact-missing", "interrupted", "corrupt", "empty"]


class ExperimentEntry(BaseModel):
    output_root: Path
    experiment_id: str
    experiment_name: str | None
    branch: str


class ArtifactState(BaseModel):
    path: Path
    exists: bool


class StageState(BaseModel):
    name: str
    status: StageStatus
    artifacts: list[ArtifactState]
    committed_at: datetime | None
    upstreams: list[str]


class ExperimentState(BaseModel):
    entry: ExperimentEntry
    stages: list[StageState]
    order: list[str]
    overall: OverallStatus
