"""Branch selection helpers for varve pipelines."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_branch_name(name: str) -> str:
    """Validate a branch name before it is interpolated into output paths."""
    if not isinstance(name, str) or BRANCH_NAME_RE.fullmatch(name) is None:
        raise ValueError(
            f"Invalid varve branch name {name!r}; branch names must match "
            "[A-Za-z0-9][A-Za-z0-9._-]* and stay within one path segment."
        )
    return name


class BranchDefinition(BaseModel):
    """One ``varve.yaml`` branch: Config fields, axis restrictions, and flags."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    config: dict[str, Any] = {}
    axes: dict[str, list[str]] = {}
    is_temporary: bool = False
    manual: bool = False


def validation_message(error: ValidationError) -> str:
    """One line per pydantic error: ``location: message``."""

    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
        for item in error.errors()
    )


def load_branches(yaml_path: Path | None) -> dict[str, BranchDefinition]:
    """Load all branch configs from a varve.yaml file."""
    if yaml_path is None or not Path(yaml_path).exists():
        return {}

    raw = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"varve.yaml must be a mapping of branch names to configs: {yaml_path}")
    for name in raw:
        validate_branch_name(name)
    branches: dict[str, BranchDefinition] = {}
    for name, section in raw.items():
        try:
            branches[name] = BranchDefinition.model_validate(section or {})
        except ValidationError as error:
            raise ValueError(
                f"Varve branch {name!r} in {yaml_path}: {validation_message(error)} "
                "(a branch holds config:, axes:, is_temporary:, and manual:)"
            ) from None
    return branches


def canonical_config_json(config: Mapping[str, Any]) -> str:
    """Return stable JSON for a validated config snapshot."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)


def assert_same_config(left: Mapping[str, Any], right: Mapping[str, Any], *, branch: str) -> None:
    """Raise when a named temporary branch is reused with a different config."""
    if canonical_config_json(left) != canonical_config_json(right):
        raise ValueError(
            f"Temporary varve branch {branch!r} was created with a different config; "
            "use a different --branch name or clean the existing temporary branch first."
        )
