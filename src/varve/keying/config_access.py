"""Runtime capture of which top-level Config fields a stage reads.

A stage's durable output depends only on the config fields it actually reads.
We wrap the config in a :class:`RecordingConfig` for the duration of a stage
run and record every top-level field access; anything we cannot attribute to a
specific field (``model_dump``, dynamic ``getattr`` of an unknown name, whole
object iteration or pickling) conservatively marks the whole config as
depended-on.

Combined with the source hash already folded into the content key, projecting
the config onto the recorded field set is sound: fields that were not read
cannot affect the output, and a code change that reads a new field changes the
source hash and forces a rerun (after which the field set is re-recorded).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _unwrap(config: BaseModel) -> BaseModel:
    """Reconstruct a pickled :class:`RecordingConfig` as the bare config."""

    return config


class ConfigAccess:
    """Accumulates the top-level config fields read during one stage run.

    ``mark_all`` latches an "every field" state used whenever a read cannot be
    attributed to a single declared field.
    """

    def __init__(self) -> None:
        self._fields: set[str] = set()
        self._all = False

    def record(self, name: str) -> None:
        self._fields.add(name)

    def mark_all(self) -> None:
        self._all = True

    def resolve(self) -> list[str] | None:
        """Return the sorted recorded fields, or ``None`` when all fields matter."""

        if self._all:
            return None
        return sorted(self._fields)


class RecordingConfig:
    """Transparent proxy over a validated Config that records field reads.

    Only plain top-level field reads are attributed precisely. Methods, dunder
    access, unknown attribute names, and whole-object access (iteration,
    ``__dict__``, pickling) mark the whole config as depended-on. Nested models
    are returned unwrapped, so reads on them fold into their top-level field.
    """

    __slots__ = ("_config", "_access", "_fields")

    def __init__(self, config: BaseModel, access: ConfigAccess) -> None:
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_access", access)
        object.__setattr__(self, "_fields", frozenset(type(config).model_fields))

    def __getattr__(self, name: str) -> Any:
        # Reached only for names not resolved as slots, i.e. every attribute of
        # the wrapped config, including its methods and dunders such as
        # ``__dict__`` (slots leave no instance dict, so it routes here too).
        config = object.__getattribute__(self, "_config")
        access = object.__getattribute__(self, "_access")
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            access.record(name)
            return getattr(config, name)
        access.mark_all()
        return getattr(config, name)

    def __iter__(self) -> Any:
        # pydantic models are iterable (yields every field); treat as read-all.
        access = object.__getattribute__(self, "_access")
        access.mark_all()
        return iter(object.__getattribute__(self, "_config"))

    def __reduce_ex__(self, protocol: int) -> Any:
        # Pickling (e.g. shipping config into a subprocess) reads everything;
        # serialize the real config so the far side gets a usable object.
        # Reconstruct via _unwrap rather than the config's own __newobj__ reduce,
        # whose class check rejects being returned from a differently-typed proxy.
        access = object.__getattribute__(self, "_access")
        access.mark_all()
        return (_unwrap, (object.__getattribute__(self, "_config"),))


def project_config(config_data: dict[str, Any], access_fields: list[str] | None) -> dict[str, Any]:
    """Project a config dump onto ``access_fields``.

    ``None`` means "all fields depended-on" and returns the dump unchanged.
    Fields absent from the dump (e.g. removed from Config since the set was
    recorded) are skipped; such a schema change also moves the source hash.
    """

    if access_fields is None:
        return config_data
    return {name: config_data[name] for name in access_fields if name in config_data}
