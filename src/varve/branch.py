"""Branch selection helpers for varve experiments."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_branch_name(name: str) -> str:
    """Validate a branch name before it is interpolated into output paths."""
    if not isinstance(name, str) or BRANCH_NAME_RE.fullmatch(name) is None:
        raise ValueError(
            f"Invalid varve branch name {name!r}; branch names must match "
            "[A-Za-z0-9][A-Za-z0-9._-]* and stay within one path segment."
        )
    return name


def load_branches(yaml_path: Path | None) -> dict[str, tuple[dict[str, Any], bool]]:
    """Load all branch configs from a varve.yaml file."""
    if yaml_path is None or not Path(yaml_path).exists():
        return {}

    raw = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"varve.yaml must be a mapping of branch names to configs: {yaml_path}")
    for name in raw:
        validate_branch_name(name)

    result: dict[str, tuple[dict[str, Any], bool]] = {}
    for branch, section in raw.items():
        if section is None:
            section = {}
        if not isinstance(section, Mapping):
            raise ValueError(f"Varve branch {branch!r} must be a mapping in {yaml_path}")

        config = dict(section)
        is_temporary = config.pop("is_temporary", False)
        if not isinstance(is_temporary, bool):
            raise ValueError(f"Varve branch {branch!r} has non-boolean is_temporary in {yaml_path}")
        result[branch] = (config, is_temporary)
    return result


def load_branch(yaml_path: Path | None, branch: str) -> tuple[dict[str, Any], bool]:
    """Load one branch config from a varve.yaml file.

    Missing `main` falls back to schema defaults, represented as an empty dict.
    Non-main branches must be present.
    """
    validate_branch_name(branch)
    branches = load_branches(yaml_path)
    if branch in branches:
        return branches[branch]
    if branch == "main":
        return {}, False
    if yaml_path is None or not Path(yaml_path).exists():
        raise ValueError(f"Unknown varve branch {branch!r}: no varve.yaml was found")
    raise ValueError(f"Unknown varve branch {branch!r} in {yaml_path}")


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def merge_override(base_config: Mapping[str, Any], override_json: str) -> dict[str, Any]:
    """Apply an override JSON object to a raw config mapping."""
    override = json.loads(override_json)
    if not isinstance(override, Mapping):
        raise ValueError("--override must be a JSON object")
    return _deep_merge(base_config, override)


def canonical_config_json(config: Mapping[str, Any]) -> str:
    """Return stable JSON for a validated config snapshot."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)


def override_branch_name(config: Mapping[str, Any]) -> str:
    """Derive the hash override branch name from a complete config snapshot."""
    digest = hashlib.sha256(canonical_config_json(config).encode("utf-8")).hexdigest()[:12]
    return f"main_override_{digest}"


def assert_same_config(left: Mapping[str, Any], right: Mapping[str, Any], *, branch: str) -> None:
    """Raise when a named temporary branch is reused with a different config."""
    if canonical_config_json(left) != canonical_config_json(right):
        raise ValueError(
            f"Temporary varve branch {branch!r} was created with a different config; "
            "use a different --branch name or clean the existing temporary branch first."
        )


def derive_override_branch(
    base_config: Mapping[str, Any],
    override_json: str,
    *,
    base_name: str,
    name: str | None = None,
) -> tuple[dict[str, Any], str, bool]:
    """Apply an override JSON object and derive a temporary branch name."""
    validate_branch_name(base_name)
    merged = merge_override(base_config, override_json)
    branch = (
        name
        or f"{base_name}_override_{hashlib.sha256(canonical_config_json(merged).encode('utf-8')).hexdigest()[:12]}"
    )
    validate_branch_name(branch)
    return merged, branch, True
