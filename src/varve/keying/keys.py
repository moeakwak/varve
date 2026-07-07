"""Content key assembly."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Protocol, Union, get_args, get_origin

from pydantic import BaseModel

from varve.keying.astkey import source_hash
from varve.keying.config_access import project_config
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
    def additional_uses(self) -> tuple[Callable[..., Any], ...]: ...

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


def _callable_label(func: Callable[..., Any]) -> str:
    module = _module_name(func)
    qualname = getattr(func, "__qualname__", getattr(func, "__name__", repr(func)))
    return f"{module}.{qualname}" if module else qualname


def _module_name(func: Callable[..., Any]) -> str:
    module = getattr(func, "__module__", "")
    if module == "__main__":
        main_module = sys.modules.get("__main__")
        spec = getattr(main_module, "__spec__", None)
        module = getattr(spec, "name", None) or module
    return module


def _project_prefix(func: Callable[..., Any]) -> str:
    module = _module_name(func)
    return module.split(".", 1)[0] if module else ""


def _is_project_callable(value: Any, *, project_prefix: str) -> bool:
    if not callable(value):
        return False
    module = _module_name(value)
    return bool(project_prefix) and (
        module == project_prefix or module.startswith(f"{project_prefix}.")
    )


def _direct_project_callables(
    func: Callable[..., Any], *, project_prefix: str
) -> tuple[Callable[..., Any], ...]:
    code = getattr(func, "__code__", None)
    globals_dict = getattr(func, "__globals__", None)
    if code is None or globals_dict is None:
        return ()
    return tuple(
        value
        for name in code.co_names
        if (value := globals_dict.get(name)) is not None
        and _is_project_callable(value, project_prefix=project_prefix)
        and value is not func
    )


def _auto_uses(func: Callable[..., Any]) -> tuple[Callable[..., Any], ...]:
    project_prefix = _project_prefix(func)
    seen: dict[Callable[..., Any], None] = {}
    stack = list(_direct_project_callables(func, project_prefix=project_prefix))
    while stack:
        helper = stack.pop(0)
        if helper in seen:
            continue
        seen[helper] = None
        stack.extend(_direct_project_callables(helper, project_prefix=project_prefix))
    return tuple(seen)


def _effective_uses(stage_spec: StageSpecLike) -> tuple[Callable[..., Any], ...]:
    uses = (
        *(_auto_uses(stage_spec.func) if stage_spec.auto_uses else ()),
        *stage_spec.additional_uses,
    )
    project_prefix = _project_prefix(stage_spec.func)
    seen: dict[Callable[..., Any], None] = {}
    stack = list(uses)
    while stack:
        helper = stack.pop(0)
        if helper in seen:
            continue
        seen[helper] = None
        stack.extend(_direct_project_callables(helper, project_prefix=project_prefix))
    return tuple(seen)


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


def _validate_uses_cover_direct_same_module_calls(
    stage_spec: StageSpecLike, uses: tuple[Callable[..., Any], ...]
) -> None:
    registered = set(uses)
    for owner in (stage_spec.func, *uses):
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
                f"covered by source uses: {listed}. Enable auto_uses or add them to "
                "additional_uses. This guard only covers "
                "direct same-module global function calls; aliases, methods, indirect calls, "
                "closures, and decorator wrappers are not detected."
            )


def compute_key_components(
    stage_spec: StageSpecLike,
    ctx: Any,
    upstream_keys: Mapping[str, str],
    cached_files: Mapping[str, list[FileFingerprint]] | None = None,
    *,
    config_access: list[str] | None = None,
) -> KeyComponents:
    """Assemble a stage's key components.

    `config_access` projects the config onto the top-level fields the stage is
    known to read (from the previous success record); `None` folds in the whole
    config, the conservative default used on the first run and after source
    changes.
    """

    _validate_config_has_no_paths(ctx.config)
    uses = _effective_uses(stage_spec)
    _validate_uses_cover_direct_same_module_calls(stage_spec, uses)
    source = {"stage": source_hash(stage_spec.func)}
    for helper in uses:
        helper_name = _callable_label(helper)
        source_key = f"uses.{helper_name}"
        if source_key in source:
            raise ValueError(f"Duplicate varve uses source key: {source_key}")
        source[source_key] = source_hash(helper)

    config = project_config(config_data(ctx.config), config_access)
    files = files_fingerprints(ctx, stage_spec.keyspec.files, cached_by_name=cached_files)
    values = {name: getter(ctx) for name, getter in sorted(stage_spec.keyspec.values.items())}
    upstreams = {name: {"content_key": upstream_keys[name]} for name in sorted(stage_spec.needs)}

    return KeyComponents(
        source=source,
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
