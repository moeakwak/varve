"""Runtime capture of which top-level Config fields a stage reads.

A stage's durable output depends only on the config fields it actually reads.
Varve supports two Config shapes: a pydantic ``BaseModel`` and a plain ``dict``
with string keys. For the duration of a stage run the config is wrapped by
:func:`record_config`, which records every top-level field read; anything we
cannot attribute to a specific field (``model_dump``, dynamic ``getattr`` of an
unknown name, a missing dict key, whole-object iteration, equality, or
pickling) conservatively marks the whole config as depended-on.

Projecting the config onto the recorded field set is sound for unchanged
source: fields that were not read cannot affect the output.  Source review is
separate from the input key; reusing a source change also reuses
responsibility for reusing the old access set when deciding whether an
existing materialization is still valid.  Any execution under a different
source fingerprint starts from the whole Config and records a new field set.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def check_config(config: Any) -> None:
    """Reject Config values other than a pydantic model or a string-keyed dict."""

    if isinstance(config, BaseModel):
        return
    if isinstance(config, dict):
        bad = [key for key in config if not isinstance(key, str)]
        if bad:
            raise TypeError(f"varve dict Config keys must be strings; got {bad[0]!r}")
        return
    raise TypeError(
        "varve Config must be a pydantic BaseModel or a dict, "
        f"got {type(config).__module__}.{type(config).__qualname__}"
    )


def _unwrap(config: Any) -> Any:
    """Reconstruct a pickled recording proxy as the bare config."""

    return config


def unwrap_config(config: Any) -> Any:
    """Return the bare config behind a recording proxy, or ``config`` itself.

    The unwrapped value is no longer observed, so this marks the whole config as
    depended-on.
    """

    if isinstance(config, (RecordingConfig, RecordingDict)):
        access = object.__getattribute__(config, "_access")
        access.mark_all()
        return object.__getattribute__(config, "_config")
    return config


def _read_only(*_args: Any, **_kwargs: Any) -> Any:
    raise TypeError("ctx.config is read-only during a stage run")


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

        return None if self._all else sorted(self._fields)


class RecordingConfig:
    """Transparent proxy over a pydantic Config that records field reads.

    ``__class__`` reports the wrapped model type, so ``isinstance`` checks,
    ``singledispatch``, and pydantic fields annotated with the Config type
    accept the proxy (pydantic keeps it as-is; models that revalidate instances
    read the whole config). ``type(proxy)`` still reveals the proxy;
    ``proxy.model_copy()`` returns a plain instance and reads the whole config.

    Only plain top-level field reads are attributed precisely. Methods, dunder
    access, unknown attribute names, equality, hashing, ``repr``, and
    whole-object access (iteration, ``__dict__``, pickling) mark the whole config
    as depended-on. Nested models are returned unwrapped, so reads on them fold
    into their top-level field.
    """

    __slots__ = ("_config", "_access", "_fields")

    def __init__(self, config: BaseModel, access: ConfigAccess) -> None:
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_access", access)
        object.__setattr__(self, "_fields", frozenset(type(config).model_fields))

    @property  # type: ignore[misc]
    def __class__(self) -> type:  # pyright: ignore[reportIncompatibleMethodOverride]
        return type(object.__getattribute__(self, "_config"))

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

    __setattr__ = _read_only
    __delattr__ = _read_only

    def _whole(self) -> BaseModel:
        object.__getattribute__(self, "_access").mark_all()
        return object.__getattribute__(self, "_config")

    def __iter__(self) -> Any:
        # pydantic models are iterable (yields every field); treat as read-all.
        return iter(self._whole())

    def __eq__(self, other: object) -> bool:
        return self._whole() == unwrap_config(other)

    def __hash__(self) -> int:
        return hash(self._whole())

    def __repr__(self) -> str:
        return repr(self._whole())

    def __str__(self) -> str:
        return str(self._whole())

    def __reduce_ex__(self, protocol: int) -> Any:
        # Pickling (e.g. shipping config into a subprocess) reads everything;
        # serialize the real config so the far side gets a usable object.
        # Reconstruct via _unwrap rather than the config's own __newobj__ reduce,
        # whose class check rejects being returned from a differently-typed proxy.
        return (_unwrap, (self._whole(),))


class RecordingDict(dict):
    """Read-only ``dict`` proxy over a dict Config that records key reads.

    The proxy is a real ``dict`` subclass holding a shallow copy of the config,
    so ``isinstance(proxy, dict)``, ``json.dumps``, ``dict(proxy)``, and
    ``**proxy`` work. Reading a present top-level key (``[]``, ``get``, ``in``)
    is attributed precisely; nested values are returned unwrapped, so
    ``proxy["a"]["b"]`` records ``a``. A missing key, ``keys``/``items``/
    ``values``, iteration, ``len``, truthiness, equality, ``repr``, copying, and
    pickling mark the whole config as depended-on. Mutation raises.

    Native serializers that read dict storage directly, such as pydantic-core
    serializing an ``Any`` field or orjson, bypass the proxy and are not
    recorded; hand them ``dict(proxy)`` instead.
    """

    __slots__ = ("_config", "_access")

    def __init__(self, config: dict[str, Any], access: ConfigAccess) -> None:
        super().__init__(config)
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_access", access)

    def _whole(self) -> dict[str, Any]:
        object.__getattribute__(self, "_access").mark_all()
        return object.__getattribute__(self, "_config")

    def _read(self, key: Any) -> bool:
        config = object.__getattribute__(self, "_config")
        if key in config:
            object.__getattribute__(self, "_access").record(key)
            return True
        object.__getattribute__(self, "_access").mark_all()
        return False

    def __getitem__(self, key: Any) -> Any:
        self._read(key)
        return object.__getattribute__(self, "_config")[key]

    def get(self, key: Any, default: Any = None) -> Any:
        return object.__getattribute__(self, "_config")[key] if self._read(key) else default

    def __contains__(self, key: object) -> bool:
        return self._read(key)

    def keys(self) -> Any:
        return self._whole().keys()

    def values(self) -> Any:
        return self._whole().values()

    def items(self) -> Any:
        return self._whole().items()

    def __iter__(self) -> Any:
        return iter(self._whole())

    def __reversed__(self) -> Any:
        return reversed(self._whole())

    def __len__(self) -> int:
        return len(self._whole())

    def __eq__(self, other: object) -> bool:
        return self._whole() == unwrap_config(other)

    def __ne__(self, other: object) -> bool:
        return not self == other

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return repr(self._whole())

    def __or__(self, other: Any) -> Any:
        return self._whole() | unwrap_config(other)

    def __ror__(self, other: Any) -> Any:
        return unwrap_config(other) | self._whole()

    def copy(self) -> dict[str, Any]:
        return self._whole().copy()

    def __reduce_ex__(self, protocol: Any) -> Any:
        return (_unwrap, (self._whole(),))

    __setitem__ = _read_only
    __delitem__ = _read_only
    __ior__ = _read_only
    __setattr__ = _read_only
    clear = _read_only
    pop = _read_only
    popitem = _read_only
    setdefault = _read_only
    update = _read_only


def record_config(config: Any, access: ConfigAccess) -> Any:
    """Wrap ``config`` in the recording proxy matching its supported shape."""

    check_config(config)
    if isinstance(config, BaseModel):
        return RecordingConfig(config, access)
    return RecordingDict(config, access)


def project_config(config_data: dict[str, Any], access_fields: list[str] | None) -> dict[str, Any]:
    """Project a config dump onto ``access_fields``.

    ``None`` means "all fields depended-on" and returns the dump unchanged.
    Fields absent from the dump (for example, removed from Config since the set
    was recorded) are skipped. Source review remains responsible for deciding
    whether a changed Config-reading implementation may reuse the old set.
    """

    if access_fields is None:
        return config_data
    return {name: config_data[name] for name in access_fields if name in config_data}
