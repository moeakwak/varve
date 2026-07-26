"""Derive, validate, and match the user-facing selector of a pipeline."""

from __future__ import annotations


def validate_selector(name: str) -> str:
    """Return a declared selector, rejecting anything but a dotted identifier path."""

    if not name or not all(part.isidentifier() for part in name.split(".")):
        raise ValueError(f"varve_name must be a dotted path of Python identifiers: {name!r}")
    return name


def selector_from_module(module: str) -> str:
    """Return the package that owns a pipeline module's output directory.

    Stores live in `out/` next to the module that defines the pipeline, so the
    package holding that module is the identity users address. It is also the
    name `python -m` accepts whenever the package has a `__main__.py`.
    """

    package, _, leaf = module.rpartition(".")
    return package or leaf


def matches_selector(selector: str, query: str) -> bool:
    """Accept a selector by its full path or by any of its dotted suffixes."""

    return selector == query or selector.endswith(f".{query}")
