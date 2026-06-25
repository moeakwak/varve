"""Content key assembly."""

from __future__ import annotations

import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Protocol, Union, get_args, get_origin

from pydantic import BaseModel

from varve.keying.astkey import source_hash
from varve.keying.fingerprint import files_fingerprints, json_sha256
from varve.keyspec import JSON, KeySpec
from varve.models import FileFingerprint, KeyComponents

_UNION_ORIGINS = (Union, types.UnionType)


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


def _annotation_contains_path(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        return bool(args) and _annotation_contains_path(args[0])
    if origin in _UNION_ORIGINS or origin is not None:
        return any(_annotation_contains_path(arg) for arg in get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return any(
            _annotation_contains_path(field.annotation)
            for field in annotation.model_fields.values()
        )
    return isinstance(annotation, type) and issubclass(annotation, Path)


def _value_contains_path(value: Any) -> bool:
    if isinstance(value, Path):
        return True
    if isinstance(value, BaseModel):
        return any(_value_contains_path(getattr(value, name)) for name in type(value).model_fields)
    if isinstance(value, Mapping):
        return any(_value_contains_path(item) for item in value.keys()) or any(
            _value_contains_path(item) for item in value.values()
        )
    if isinstance(value, list | tuple | set | frozenset):
        return any(_value_contains_path(item) for item in value)
    return False


def _validate_config_has_no_paths(config: Any) -> None:
    if isinstance(config, BaseModel):
        bad_fields = [
            name
            for name, field in type(config).model_fields.items()
            if _annotation_contains_path(field.annotation)
            or _value_contains_path(getattr(config, name))
        ]
        if bad_fields:
            fields = ", ".join(sorted(bad_fields))
            raise TypeError(
                f"Config fields must not contain Path values ({fields}); put input locations "
                "in Args and fingerprint their content with KeySpec.files."
            )
        return
    if _value_contains_path(config):
        raise TypeError(
            "Config fields must not contain Path values; put input locations in Args and "
            "fingerprint their content with KeySpec.files."
        )


def _callable_label(func: Callable[..., Any]) -> str:
    module = getattr(func, "__module__", "")
    qualname = getattr(func, "__qualname__", getattr(func, "__name__", repr(func)))
    return f"{module}.{qualname}" if module else qualname


def _direct_same_module_callables(func: Callable[..., Any]) -> dict[str, Callable[..., Any]]:
    code = getattr(func, "__code__", None)
    globals_dict = getattr(func, "__globals__", None)
    module = getattr(func, "__module__", None)
    if code is None or globals_dict is None or module is None:
        return {}
    result: dict[str, Callable[..., Any]] = {}
    for name in code.co_names:
        value = globals_dict.get(name)
        if callable(value) and getattr(value, "__module__", None) == module and value is not func:
            result[name] = value
    return result


def _validate_uses_cover_direct_same_module_calls(stage_spec: StageSpecLike) -> None:
    registered = set(stage_spec.uses)
    for owner in (stage_spec.func, *stage_spec.uses):
        missing = [
            name
            for name, value in sorted(_direct_same_module_callables(owner).items())
            if value not in registered and value is not owner
        ]
        if missing:
            owner_label = _callable_label(owner)
            listed = ", ".join(missing)
            raise ValueError(
                f"Varve callable {owner_label} directly calls same-module helper(s) not "
                f"listed in uses: {listed}. Add them to uses. This guard only covers "
                "direct same-module global function calls; aliases, methods, indirect calls, "
                "closures, and decorator wrappers are not detected."
            )


def compute_key_components(
    stage_spec: StageSpecLike,
    ctx: Any,
    upstream_keys: Mapping[str, str],
    cached_files: Mapping[str, list[FileFingerprint]] | None = None,
) -> KeyComponents:
    _validate_config_has_no_paths(ctx.config)
    _validate_uses_cover_direct_same_module_calls(stage_spec)
    source = {"stage": source_hash(stage_spec.func)}
    for helper in stage_spec.uses:
        helper_name = _callable_label(helper)
        source_key = f"uses.{helper_name}"
        if source_key in source:
            raise ValueError(f"Duplicate varve uses source key: {source_key}")
        source[source_key] = source_hash(helper)

    config = _config_data(ctx.config)
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
