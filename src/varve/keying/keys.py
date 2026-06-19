"""Content key assembly."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from varve.keying.astkey import source_hash
from varve.keying.fingerprint import files_fingerprints, json_sha256
from varve.keyspec import JSON, KeySpec
from varve.models import FileFingerprint, KeyComponents


class StageSpecLike(Protocol):
    func: Callable[..., Any]
    uses: tuple[Callable[..., Any], ...]
    keyspec: KeySpec
    needs: tuple[str, ...]


def _config_data(config: Any) -> dict[str, Any]:
    if isinstance(config, BaseModel):
        return config.model_dump(mode="json")
    if isinstance(config, dict):
        return config
    return vars(config)


def _pick_config_values(config: Any, fields: tuple[str, ...]) -> dict[str, JSON]:
    data = _config_data(config)
    values: dict[str, JSON] = {}
    for field in fields:
        if field not in data:
            raise ValueError(f"Config field declared in varve key does not exist: {field}")
        values[field] = data[field]
    return values


def compute_key_components(
    stage_spec: StageSpecLike,
    ctx: Any,
    upstream_keys: Mapping[str, str],
    cached_files: Mapping[str, list[FileFingerprint]] | None = None,
) -> KeyComponents:
    source = {"stage": source_hash(stage_spec.func)}
    for helper in stage_spec.uses:
        helper_module = getattr(helper, "__module__", "")
        helper_qualname = getattr(helper, "__qualname__", getattr(helper, "__name__", repr(helper)))
        helper_name = f"{helper_module}.{helper_qualname}" if helper_module else helper_qualname
        source_key = f"uses.{helper_name}"
        if source_key in source:
            raise ValueError(f"Duplicate varve uses source key: {source_key}")
        source[source_key] = source_hash(helper)

    config = _pick_config_values(ctx.config, stage_spec.keyspec.config)
    files = files_fingerprints(ctx, stage_spec.keyspec.files, cached_by_name=cached_files)
    values = {name: getter(ctx) for name, getter in sorted(stage_spec.keyspec.values.items())}
    upstreams = {name: {"content_key": upstream_keys[name]} for name in sorted(stage_spec.needs)}

    return KeyComponents(
        source=source,
        config=config,
        files=files,
        values=values,
        upstreams=upstreams,
    )


def _file_digest_view(files: dict[str, list[FileFingerprint]]) -> dict[str, str]:
    digest_view: dict[str, str] = {}
    for name, members in sorted(files.items()):
        digest_view[name] = json_sha256(sorted(member.sha256 for member in members))
    return digest_view


def content_key(components: KeyComponents) -> str:
    digest_view = {
        "source": components.source,
        "config": components.config,
        "files": _file_digest_view(components.files),
        "values": components.values,
        "upstreams": components.upstreams,
    }
    return json_sha256(digest_view)


def run_key(content_key_value: str, partition_values: dict[str, JSON]) -> str:
    return json_sha256({"content_key": content_key_value, "partition": partition_values})
