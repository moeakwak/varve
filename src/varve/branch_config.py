"""Resolve varve branches into Config objects and output roots."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, TypeVar

from pydantic import BaseModel, ValidationError, create_model
from pydantic_settings import BaseSettings, SettingsConfigDict

from varve.branch import (
    assert_same_config,
    load_branches,
    merge_override,
    override_branch_name,
    validate_branch_name,
)
from varve.matrix import normalize_axes
from varve.pipeline import Pipeline
from varve.store.store import Store

ConfigT = TypeVar("ConfigT", bound=BaseModel)


class ResolvedBranch(NamedTuple):
    config: Any
    branch: str
    output_base: Path | None
    axes: dict[str, tuple[str, ...]] | None = None
    temporary_config: dict[str, Any] | None = None

    @property
    def is_temporary(self) -> bool:
        return self.temporary_config is not None

    @property
    def temporary_axes(self) -> dict[str, tuple[str, ...]] | None:
        return self.axes if self.is_temporary else None


@cache
def _settings_type(config_type: type[BaseModel]) -> type[BaseSettings]:
    class VarveSettings(BaseSettings):
        model_config = SettingsConfigDict(
            env_nested_delimiter="__",
            env_file=".env",
        )

    fields: dict[str, Any] = {
        name: (field.annotation, field) for name, field in config_type.model_fields.items()
    }
    return create_model(f"{config_type.__name__}VarveSettings", __base__=VarveSettings, **fields)


def config_from_init(config_type: type[ConfigT], init_kwargs: dict[str, Any]) -> ConfigT:
    settings = _settings_type(config_type)(**init_kwargs)
    return config_type.model_validate(settings.model_dump())


def _snapshot(config: Any) -> dict[str, Any]:
    if not hasattr(config, "model_dump"):
        raise TypeError("Temporary varve branches require a pydantic Config model")
    return config.model_dump(mode="json")


def _main_config(
    pipeline: type[Pipeline],
    raw_main: dict[str, Any],
    *,
    cli_out: Path | None = None,
    allow_bare_output_root: bool = False,
) -> Any:
    try:
        return config_from_init(pipeline.Config, raw_main)
    except ValidationError:
        if allow_bare_output_root and cli_out is not None:
            return SimpleNamespace()
        raise


def _manifest_axes(manifest) -> dict[str, tuple[str, ...]]:
    return {name: tuple(values) for name, values in (manifest.temporary_axes or {}).items()}


def _validate_temporary_store(
    main_base: Path,
    branch: str,
    config: dict[str, Any],
    axes: dict[str, tuple[str, ...]],
) -> None:
    manifest = Store(main_base / ".tmp" / branch).read_manifest()
    if manifest is None:
        return
    if manifest.temporary_config is None:
        raise ValueError(f"Unknown varve branch {branch!r}")
    assert_same_config(manifest.temporary_config, config, branch=branch)
    if _manifest_axes(manifest) != axes:
        raise ValueError(f"Temporary varve branch {branch!r} was created with different axes")


def resolve_branch(
    pipeline: type[Pipeline],
    *,
    branch: str,
    override_json: str | None,
    cli_out: Path | None,
    allow_bare_output_root: bool = False,
) -> ResolvedBranch:
    validate_branch_name(branch)
    output_base = Path(cli_out) if cli_out is not None else None
    branches = load_branches(pipeline.varve_config_path())
    main_definition = branches.get("main")
    raw_main = main_definition.config if main_definition is not None else {}
    main_axes = normalize_axes(
        pipeline, main_definition.axes if main_definition is not None else None
    )

    if override_json is not None:
        if branch in branches and branch != "main":
            raise ValueError("--override is only supported on main or temporary branches")
        config = config_from_init(pipeline.Config, merge_override(raw_main, override_json))
        temporary_config = _snapshot(config)
        main_base = output_base or pipeline.default_output_root(
            config_from_init(pipeline.Config, raw_main)
        )
        branch = override_branch_name(temporary_config, main_axes) if branch == "main" else branch
        validate_branch_name(branch)
        _validate_temporary_store(main_base, branch, temporary_config, main_axes)
        return ResolvedBranch(config, branch, main_base, main_axes, temporary_config)
    if branch in branches:
        definition = branches[branch]
        axes = normalize_axes(pipeline, definition.axes)
        config = config_from_init(pipeline.Config, definition.config)
        if not definition.is_temporary:
            return ResolvedBranch(config, branch, output_base, axes)
        temporary_config = _snapshot(config)
        main_base = output_base or pipeline.default_output_root(config)
        _validate_temporary_store(main_base, branch, temporary_config, axes)
        return ResolvedBranch(config, branch, output_base, axes, temporary_config)
    if branch == "main":
        return ResolvedBranch(
            _main_config(
                pipeline,
                raw_main,
                cli_out=cli_out,
                allow_bare_output_root=allow_bare_output_root,
            ),
            branch,
            output_base,
            main_axes,
        )
    main_base = output_base or pipeline.default_output_root(_main_config(pipeline, raw_main))
    manifest = Store(main_base / ".tmp" / branch).read_manifest()
    if manifest is None or manifest.temporary_config is None:
        raise ValueError(f"Unknown varve branch {branch!r}")
    temporary_config = manifest.temporary_config
    axes = _manifest_axes(manifest)
    config = pipeline.Config.model_validate(temporary_config)
    return ResolvedBranch(config, branch, main_base, axes, temporary_config)
