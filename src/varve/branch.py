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


def load_branch(yaml_path: Path | None, branch: str) -> tuple[dict[str, Any], bool]:
    """Load one branch config from a branches.yaml file.

    Missing `main` falls back to schema defaults, represented as an empty dict.
    Non-main branches must be present.
    """
    validate_branch_name(branch)
    if yaml_path is None or not Path(yaml_path).exists():
        if branch == "main":
            return {}, False
        raise ValueError(f"Unknown varve branch {branch!r}: no branches.yaml was found")

    raw = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"branches.yaml must be a mapping of branch names to configs: {yaml_path}")
    for name in raw:
        validate_branch_name(name)

    if branch not in raw:
        if branch == "main":
            return {}, False
        raise ValueError(f"Unknown varve branch {branch!r} in {yaml_path}")

    section = raw[branch]
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ValueError(f"Varve branch {branch!r} must be a mapping in {yaml_path}")

    config = dict(section)
    is_temporary = config.pop("is_temporary", False)
    if not isinstance(is_temporary, bool):
        raise ValueError(f"Varve branch {branch!r} has non-boolean is_temporary in {yaml_path}")
    return config, is_temporary


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def derive_override_branch(
    base_config: Mapping[str, Any],
    override_json: str,
    *,
    base_name: str,
    name: str | None = None,
) -> tuple[dict[str, Any], str, bool]:
    """Apply an override JSON object and derive a temporary branch name."""
    validate_branch_name(base_name)
    override = json.loads(override_json)
    if not isinstance(override, Mapping):
        raise ValueError("--override must be a JSON object")

    normalized = json.dumps(override, sort_keys=True, separators=(",", ":"))
    branch = name or f"{base_name}_override_{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:12]}"
    validate_branch_name(branch)
    return _deep_merge(base_config, override), branch, True
