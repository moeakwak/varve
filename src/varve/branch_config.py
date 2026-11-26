"""Resolve a branch request into a Config and an active-axes snapshot.

Branches come from ``varve.yaml`` (``main`` always exists, with default
Config when undeclared). Temporary branches are created by ``--override``,
``--temp``, ``--slice``/``--sample``, or by naming a branch that is not
declared; their Config and active axes are snapshotted in the ``branches``
table, so later commands can address them by name alone.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, create_model
from pydantic_settings import BaseSettings, SettingsConfigDict

from varve.branch import (
    BranchDefinition,
    assert_same_config,
    canonical_config_json,
    load_branches,
    validate_branch_name,
)
from varve.keying.keys import config_data

if TYPE_CHECKING:
    from varve.pipeline import Pipeline
    from varve.store.repo import BranchRow

ConfigT = TypeVar("ConfigT", bound=BaseModel)


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
    """Build ``config_type`` with init kwargs > environment > ``.env`` > defaults."""

    settings = _settings_type(config_type)(**init_kwargs)
    return config_type.model_validate(settings.model_dump())


def build_config(pipeline: type[Pipeline], raw: Mapping[str, Any]) -> Any:
    """Construct the pipeline Config from raw branch settings (pydantic or dict Config)."""

    config_type = pipeline.Config
    if isinstance(config_type, type) and issubclass(config_type, BaseModel):
        return config_from_init(config_type, dict(raw))
    if config_type is dict or (isinstance(config_type, type) and issubclass(config_type, dict)):
        return dict(raw)
    raise TypeError(f"{pipeline.__qualname__}.Config must be a pydantic model class or dict")


def _restore_config(pipeline: type[Pipeline], snapshot: Mapping[str, Any]) -> Any:
    config_type = pipeline.Config
    if isinstance(config_type, type) and issubclass(config_type, BaseModel):
        return config_type.model_validate(dict(snapshot))
    return dict(snapshot)


@dataclass(frozen=True)
class Sample:
    """Deterministic ``--sample axis=N --seed S`` request."""

    count: int
    seed: int = 0

    def pick(self, ids: Sequence[str], scope: str) -> list[str]:
        """Pick ``count`` ids (all when fewer), keeping domain order; ``scope`` salts the RNG."""

        if self.count >= len(ids):
            return list(ids)
        chosen = set(random.Random(f"{self.seed}:{scope}").sample(range(len(ids)), self.count))
        return [item for index, item in enumerate(ids) if index in chosen]


@dataclass(frozen=True)
class ResolvedBranch:
    """A branch ready to run: its Config, active axis ids, and samples.

    ``active`` maps axis names (static or dynamic) to the ids kept active:
    ``varve.yaml`` ``axes:`` restrictions and ``--slice`` values combined.
    """

    name: str
    config: Any
    config_json: dict[str, Any]
    active: dict[str, list[str]] = field(default_factory=dict)
    samples: dict[str, Sample] = field(default_factory=dict)
    is_temp: bool = False
    manual: bool = False

    @property
    def active_axes_json(self) -> dict[str, Any]:
        """Snapshot stored in ``branches.active_axes_json``: ``[ids]`` for an id
        restriction, ``{"sample": n, "seed": s}`` for a sample, and both together
        as ``{"ids": [ids], "sample": n, "seed": s}`` (the sample is drawn from the ids)."""

        snapshot: dict[str, Any] = {name: list(ids) for name, ids in self.active.items()}
        for name, sample in self.samples.items():
            entry: dict[str, Any] = {"sample": sample.count, "seed": sample.seed}
            if name in self.active:
                entry = {"ids": list(self.active[name]), **entry}
            snapshot[name] = entry
        return snapshot


@dataclass(frozen=True)
class BranchRequest:
    """What a command asks for; see :func:`resolve_branch`."""

    name: str = "main"
    override: Mapping[str, Any] | str | None = None
    temp: bool = False
    slices: Mapping[str, Sequence[str]] = field(default_factory=dict)
    samples: Mapping[str, int] = field(default_factory=dict)
    seed: int = 0


def parse_slice(text: str, *, base: Path | None = None) -> tuple[str, list[str]]:
    """Parse ``axis=v1,v2`` or ``axis=@file`` (one id per line, ``#`` comments)."""

    name, sep, rest = text.partition("=")
    name, rest = name.strip(), rest.strip()
    if not sep or not name or not rest:
        raise ValueError(f"Invalid --slice {text!r}; expected AXIS=ID[,ID] or AXIS=@FILE")
    if rest.startswith("@"):
        path = Path(rest[1:]).expanduser()
        if base is not None and not path.is_absolute():
            path = base / path
        lines = path.read_text(encoding="utf-8").splitlines()
        ids = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    else:
        ids = [item.strip() for item in rest.split(",") if item.strip()]
    if not ids:
        raise ValueError(f"--slice {text!r} selects no ids")
    return name, ids


def parse_sample(text: str) -> tuple[str, int]:
    name, sep, count = text.partition("=")
    try:
        value = int(count)
    except ValueError:
        value = 0
    if not sep or not name.strip() or value < 1:
        raise ValueError(f"Invalid --sample {text!r}; expected AXIS=N with N >= 1")
    return name.strip(), value


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _override_mapping(override: Mapping[str, Any] | str) -> Mapping[str, Any]:
    value = json.loads(override) if isinstance(override, str) else override
    if not isinstance(value, Mapping):
        raise ValueError("--override must be a JSON object")
    return value


def derived_branch_name(
    base: str, config_json: Mapping[str, Any], active: Mapping[str, Any]
) -> str:
    """Name of the temporary branch derived from ``base`` with a given snapshot."""

    identity = canonical_config_json({"config": dict(config_json), "axes": dict(active)})
    return f"{base}_tmp_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:10]}"


def resolve_branch(
    pipeline: type[Pipeline],
    request: BranchRequest,
    *,
    stored: Callable[[str], BranchRow | None] | None = None,
) -> ResolvedBranch:
    """Resolve ``request`` against ``varve.yaml`` and stored temporary branches.

    * ``main`` and ``varve.yaml`` branches resolve to their declared Config.
    * ``--override`` merges JSON over ``main``'s Config into a temporary branch
      (named by ``--branch`` when that is a new name, else derived).
    * ``--temp``, ``--slice`` or ``--sample`` on a declared branch create a
      derived temporary branch; on a persistent branch ``--slice``/``--sample``
      require ``--temp`` or an explicit new ``--branch`` name.
    * Any other name addresses a stored temporary branch, or creates one
      from ``main`` when ``--temp``/``--slice``/``--sample`` is given.

    A named temporary branch reused with a different snapshot is an error.
    """

    name = validate_branch_name(request.name)
    branches = load_branches(pipeline.varve_config_path())
    main_def = branches.get("main", BranchDefinition({}, {}, False))
    axes_by_name = pipeline.graph().axes
    slices = {axis: list(ids) for axis, ids in request.slices.items()}
    samples = {axis: Sample(count, request.seed) for axis, count in request.samples.items()}
    for axis in [*slices, *samples]:
        if axis not in axes_by_name:
            raise ValueError(f"Unknown axis {axis!r}; axes: {sorted(axes_by_name)}")
    narrowing = bool(slices or samples)
    declared = branches.get(name)
    if name == "main" and declared is None:
        declared = main_def

    def active_for(definition: BranchDefinition, label: str) -> dict[str, list[str]]:
        active = {axis: list(ids) for axis, ids in definition.axes.items()}
        for axis, ids in active.items():
            known = axes_by_name.get(axis)
            if known is None:
                raise ValueError(
                    f"varve.yaml branch {label!r}: axes: names unknown axis {axis!r}; "
                    f"axes: {sorted(axes_by_name)}"
                )
            if known.is_static:
                unknown = [item for item in ids if item not in known.ids]
                if unknown:
                    raise ValueError(
                        f"varve.yaml branch {label!r}: axes: {axis} ids {unknown} are not "
                        f"values of axis {axis!r} (ids: {list(known.ids)})"
                    )
        for axis, ids in slices.items():
            allowed = active.get(axis)
            if allowed is not None:
                outside = [item for item in ids if item not in allowed]
                if outside:
                    raise ValueError(
                        f"--slice {axis}={','.join(outside)} is outside the branch's active ids"
                    )
            active[axis] = ids
        return active

    if request.override is not None:
        if declared is not None and name != "main":
            raise ValueError(
                f"--override applies to main or to a new temporary branch name, not {name!r}"
            )
        raw = _merge(main_def.config, _override_mapping(request.override))
        config = build_config(pipeline, raw)
        active = active_for(main_def, "main")
        resolved_name = None if name == "main" else name
        return _temporary(pipeline, resolved_name, "main", config, active, samples, stored)

    if declared is not None:
        config = build_config(pipeline, declared.config)
        active = active_for(declared, name)
        if narrowing or request.temp:
            if not declared.is_temporary and not request.temp:
                raise ValueError(
                    f"--slice/--sample would change persistent branch {name!r}; "
                    "add --temp or pass a new --branch name"
                )
            return _temporary(pipeline, None, name, config, active, samples, stored)
        snapshot = config_data(config)
        if declared.is_temporary:
            _check_stored(stored, name, snapshot, active, samples)
        return ResolvedBranch(
            name,
            config,
            snapshot,
            active,
            {},
            is_temp=declared.is_temporary,
            manual=declared.manual,
        )

    row = stored(name) if stored is not None else None
    if row is not None and not narrowing and not request.temp:
        if not row.is_temp:
            raise ValueError(f"Branch {name!r} is not declared in varve.yaml")
        return _from_row(pipeline, row)
    if narrowing or request.temp:
        config = build_config(pipeline, main_def.config)
        return _temporary(
            pipeline, name, "main", config, active_for(main_def, "main"), samples, stored
        )
    raise ValueError(
        f"Unknown branch {name!r}; declare it in varve.yaml, or create a temporary branch "
        "with --temp, --override, --slice, or --sample"
    )


def _temporary(
    pipeline: type[Pipeline],
    name: str | None,
    base: str,
    config: Any,
    active: dict[str, list[str]],
    samples: dict[str, Sample],
    stored: Callable[[str], BranchRow | None] | None,
) -> ResolvedBranch:
    snapshot = config_data(config)
    resolved = ResolvedBranch(base, config, snapshot, active, samples, is_temp=True)
    branch_name = name or derived_branch_name(base, snapshot, resolved.active_axes_json)
    validate_branch_name(branch_name)
    _check_stored(stored, branch_name, snapshot, active, samples)
    return ResolvedBranch(branch_name, config, snapshot, active, samples, is_temp=True)


def _check_stored(
    stored: Callable[[str], BranchRow | None] | None,
    name: str,
    snapshot: Mapping[str, Any],
    active: Mapping[str, list[str]],
    samples: Mapping[str, Sample],
) -> None:
    row = stored(name) if stored is not None else None
    if row is None or not row.is_temp:
        return
    assert_same_config(row.config, snapshot, branch=name)
    expected = ResolvedBranch(name, None, dict(snapshot), dict(active), dict(samples))
    if canonical_config_json(row.active_axes) != canonical_config_json(expected.active_axes_json):
        raise ValueError(
            f"Temporary varve branch {name!r} was created with different active axes; "
            "use a different --branch name or clean the existing temporary branch first."
        )


def _from_row(pipeline: type[Pipeline], row: BranchRow) -> ResolvedBranch:
    active: dict[str, list[str]] = {}
    samples: dict[str, Sample] = {}
    for axis, value in row.active_axes.items():
        if isinstance(value, Mapping):
            samples[axis] = Sample(int(value["sample"]), int(value.get("seed", 0)))
            if isinstance(value.get("ids"), list):
                active[axis] = [str(item) for item in value["ids"]]
        else:
            active[axis] = list(value)
    config = _restore_config(pipeline, row.config)
    return ResolvedBranch(
        row.name, config, dict(row.config), active, samples, is_temp=True, manual=row.manual
    )
