"""Resolve varve branches into Config objects and output roots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

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


@dataclass(frozen=True)
class ResolvedBranch:
    config: Any
    branch: str
    is_temporary: bool
    output_base: Path | None
    temporary_config: dict[str, Any] | None = None
    axes: dict[str, tuple[str, ...]] | None = None
    temporary_axes: dict[str, tuple[str, ...]] | None = None


def _settings_type(config_type: type[BaseModel]) -> type[BaseSettings]:
    class VarveSettings(BaseSettings):
        model_config = SettingsConfigDict(
            env_nested_delimiter="__",
            env_file=".env",
        )

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    fields: dict[str, Any] = {
        name: (field.annotation, field) for name, field in config_type.model_fields.items()
    }
    return create_model(f"{config_type.__name__}VarveSettings", __base__=VarveSettings, **fields)


def config_from_init(config_type: type[ConfigT], init_kwargs: dict[str, Any]) -> ConfigT:
    settings_type = _settings_type(config_type)
    settings = settings_type(**init_kwargs)
    return config_type.model_validate(settings.model_dump())


def _snapshot(config: Any) -> dict[str, Any]:
    if not hasattr(config, "model_dump"):
        raise TypeError("Temporary varve branches require a pydantic Config model")
    return config.model_dump(mode="json")


def _main_config(
    pipeline: type[Pipeline],
    raw_main: dict[str, Any],
    *,
    cli_out: Path | None,
    allow_bare_output_root: bool,
) -> Any:
    try:
        return config_from_init(pipeline.Config, raw_main)
    except ValidationError:
        if allow_bare_output_root and cli_out is not None:
            return SimpleNamespace()
        raise


def _temporary_from_manifest(
    main_base: Path, branch: str
) -> tuple[dict[str, Any], dict[str, tuple[str, ...]]]:
    manifest = Store(main_base / ".tmp" / branch).read_manifest()
    if manifest is None or manifest.temporary_config is None:
        raise ValueError(f"Unknown varve branch {branch!r}")
    return manifest.temporary_config, {
        name: tuple(values) for name, values in (manifest.temporary_axes or {}).items()
    }


def resolve_branch(
    pipeline: type[Pipeline],
    *,
    branch: str,
    override_json: str | None,
    cli_out: Path | None,
    allow_bare_output_root: bool = False,
) -> ResolvedBranch:
    validate_branch_name(branch)
    branches = load_branches(pipeline.varve_config_path())
    main_definition = branches.get("main")
    raw_main = main_definition.config if main_definition is not None else {}
    main_axes = normalize_axes(
        pipeline, main_definition.axes if main_definition is not None else None
    )

    if override_json is not None:
        if branch in branches and branch != "main":
            raise ValueError("--override is only supported on main or temporary branches")

        final_config = config_from_init(pipeline.Config, merge_override(raw_main, override_json))
        temporary_config = _snapshot(final_config)
        if cli_out is not None:
            main_base = Path(cli_out)
        else:
            main_config = config_from_init(pipeline.Config, raw_main)
            main_base = pipeline.default_output_root(main_config)
        temporary_axes = main_axes
        resolved_branch = (
            override_branch_name(temporary_config, temporary_axes) if branch == "main" else branch
        )
        validate_branch_name(resolved_branch)

        manifest = Store(main_base / ".tmp" / resolved_branch).read_manifest()
        if manifest is not None:
            if manifest.temporary_config is None:
                raise ValueError(f"Unknown varve branch {resolved_branch!r}")
            assert_same_config(manifest.temporary_config, temporary_config, branch=resolved_branch)
            stored_axes = {
                name: tuple(values) for name, values in (manifest.temporary_axes or {}).items()
            }
            if stored_axes != temporary_axes:
                raise ValueError(
                    f"Temporary varve branch {resolved_branch!r} was created with different axes"
                )

        return ResolvedBranch(
            config=final_config,
            branch=resolved_branch,
            is_temporary=True,
            output_base=main_base,
            temporary_config=temporary_config,
            axes=temporary_axes,
            temporary_axes=temporary_axes,
        )

    if branch in branches:
        definition = branches[branch]
        axes = normalize_axes(pipeline, definition.axes)
        config = config_from_init(pipeline.Config, definition.config)
        temporary_config = _snapshot(config) if definition.is_temporary else None
        temporary_axes = axes if definition.is_temporary else None
        if definition.is_temporary:
            assert temporary_config is not None
            main_base = (
                Path(cli_out) if cli_out is not None else pipeline.default_output_root(config)
            )
            manifest = Store(main_base / ".tmp" / branch).read_manifest()
            if manifest is not None:
                if manifest.temporary_config is None:
                    raise ValueError(f"Unknown varve branch {branch!r}")
                assert_same_config(manifest.temporary_config, temporary_config, branch=branch)
                stored_axes = {
                    name: tuple(values) for name, values in (manifest.temporary_axes or {}).items()
                }
                if stored_axes != axes:
                    raise ValueError(
                        f"Temporary varve branch {branch!r} was created with different axes"
                    )
        return ResolvedBranch(
            config=config,
            branch=branch,
            is_temporary=definition.is_temporary,
            output_base=Path(cli_out) if cli_out is not None else None,
            axes=axes,
            temporary_config=temporary_config,
            temporary_axes=temporary_axes,
        )
    if branch == "main":
        main_config = _main_config(
            pipeline,
            raw_main,
            cli_out=cli_out,
            allow_bare_output_root=allow_bare_output_root,
        )
        return ResolvedBranch(
            config=main_config,
            branch="main",
            is_temporary=False,
            output_base=Path(cli_out) if cli_out is not None else None,
            axes=main_axes,
        )

    if cli_out is not None:
        main_base = Path(cli_out)
    else:
        main_config = _main_config(
            pipeline,
            raw_main,
            cli_out=None,
            allow_bare_output_root=False,
        )
        main_base = pipeline.default_output_root(main_config)
    temporary_config, temporary_axes = _temporary_from_manifest(main_base, branch)
    return ResolvedBranch(
        config=pipeline.Config.model_validate(temporary_config),
        branch=branch,
        is_temporary=True,
        output_base=main_base,
        temporary_config=temporary_config,
        axes=temporary_axes,
        temporary_axes=temporary_axes,
    )
