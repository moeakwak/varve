"""Content key assembly."""

from __future__ import annotations

import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Protocol, Union, get_args, get_origin

from pydantic import BaseModel

from varve.keying.astkey import source_hash
from varve.keying.config_access import project_config
from varve.keying.dependencies import SourceDependencies, discover_source_dependencies
from varve.keying.fingerprint import files_fingerprints, json_sha256
from varve.keyspec import KeySpec
from varve.models import FileFingerprint, KeyComponents

_UNION_ORIGINS = (Union, types.UnionType)


class StageSpecLike(Protocol):
    @property
    def func(self) -> Callable[..., Any]: ...

    @property
    def auto_uses(self) -> bool: ...

    @property
    def uses(self) -> tuple[Callable[..., Any], ...]: ...

    @property
    def keyspec(self) -> KeySpec: ...

    @property
    def needs(self) -> tuple[str, ...]: ...


def config_data(config: Any) -> dict[str, Any]:
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


def compute_source_dependencies(
    stage_spec: StageSpecLike,
    *,
    auto_uses_packages: tuple[str, ...] | None = None,
) -> SourceDependencies:
    discovered = discover_source_dependencies(
        stage_spec.func,
        explicit_uses=stage_spec.uses,
        auto_uses=stage_spec.auto_uses,
        packages=auto_uses_packages,
    )
    return discovered.with_component("stage", source_hash(stage_spec.func))


def compute_key_components(
    stage_spec: StageSpecLike,
    ctx: Any,
    upstream_keys: Mapping[str, str],
    cached_files: Mapping[str, list[FileFingerprint]] | None = None,
    *,
    config_access: list[str] | None = None,
    auto_uses_packages: tuple[str, ...] | None = None,
    source_dependencies: SourceDependencies | None = None,
) -> KeyComponents:
    """Assemble a stage's key components.

    `config_access` projects the config onto the top-level fields the stage is
    known to read (from the previous success record); `None` folds in the whole
    config, the conservative default used on the first run and after source
    changes.
    """

    _validate_config_has_no_paths(ctx.config)
    source_result = source_dependencies or compute_source_dependencies(
        stage_spec,
        auto_uses_packages=auto_uses_packages,
    )

    config = project_config(config_data(ctx.config), config_access)
    files = files_fingerprints(ctx, stage_spec.keyspec.files, cached_by_name=cached_files)
    values = {name: getter(ctx) for name, getter in sorted(stage_spec.keyspec.values.items())}
    upstreams = {name: {"content_key": upstream_keys[name]} for name in sorted(stage_spec.needs)}

    return KeyComponents(
        source=source_result.components,
        config=config,
        files=files,
        values=values,
        upstreams=upstreams,
        config_access=config_access,
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
