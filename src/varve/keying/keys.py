"""Input key assembly."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from varve.decorators import StageSpec
from varve.dependencies import Dependencies, DependencyContext
from varve.keying.config_access import project_config
from varve.keying.fingerprint import (
    FingerprintSession,
    file_digest_view,
    files_fingerprints,
    json_sha256,
)
from varve.models import FileFingerprint, KeyComponents

_MATRIX_LAYOUT_KEY = "__varve_matrix_layout__"
_MATRIX_LAYOUT_VERSION = 2


def config_data(config: Any) -> dict[str, Any]:
    if isinstance(config, BaseModel):
        return config.model_dump(mode="json")
    if isinstance(config, dict):
        return config
    return vars(config)


def compute_key_components(
    stage_spec: StageSpec,
    ctx: Any,
    upstream_keys: Mapping[str, str],
    cached_inputs: Mapping[str, list[FileFingerprint]] | None = None,
    *,
    config_access: list[str] | None = None,
    dependencies: Dependencies | None = None,
    fingerprint_session: FingerprintSession | None = None,
) -> KeyComponents:
    """Assemble a stage's key components.

    `config_access` projects the config onto the top-level fields the stage is
    known to read (from the previous success record); `None` folds in the whole
    config, the conservative default used on the first run and after source
    changes.
    """

    config = project_config(config_data(ctx.config), config_access)
    dependencies = dependencies or stage_spec.depends
    dependency_ctx = DependencyContext(
        config=ctx.config,
        out=ctx.out,
        cell=ctx.cell,
        cell_out=ctx.cell_out,
    )
    files = files_fingerprints(
        dependency_ctx,
        dependencies.inputs,
        cached_by_name=cached_inputs,
        session=fingerprint_session,
    )
    values = {name: getter(dependency_ctx) for name, getter in sorted(dependencies.values.items())}
    if stage_spec.cell:
        if _MATRIX_LAYOUT_KEY in values:
            raise ValueError(
                f"Dependencies.values name {_MATRIX_LAYOUT_KEY!r} is reserved by varve"
            )
        values[_MATRIX_LAYOUT_KEY] = _MATRIX_LAYOUT_VERSION
    need_cells = getattr(stage_spec, "need_cells", None) or {}
    has_matrix_fan_in = any(len(names) > 1 for names in need_cells.values())
    upstreams = {
        name: {
            "artifact_fingerprint": upstream_keys[name],
            **({"position": str(index)} if has_matrix_fan_in else {}),
        }
        for index, name in enumerate(stage_spec.needs)
    }

    return KeyComponents(
        config=config,
        inputs=files,
        values=values,
        upstreams=upstreams,
        config_access=config_access,
    )


def input_key(components: KeyComponents) -> str:
    digest_view = {
        "config": components.config,
        "inputs": file_digest_view(components.inputs),
        "values": components.values,
        "upstreams": components.upstreams,
    }
    return json_sha256(digest_view)
