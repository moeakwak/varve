"""Map pydantic Config models to argparse options and back."""

from __future__ import annotations

import argparse
import json
import types
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

_UNION_ORIGINS = (Union, types.UnionType)
_MAPPING_ORIGINS = (dict, Mapping)
_BARE_UNSUPPORTED_TYPES = (dict, Mapping, tuple, set)
_DEST_PREFIX = "__varve_config__."
# CLI sentinel that maps an optional scalar field to None, matching the default
# pydantic-settings `cli_parse_none_str`.
_NONE_TOKEN = "null"


def _unwrap_optional(annotation: Any) -> Any:
    if get_origin(annotation) in _UNION_ORIGINS:
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _is_optional(annotation: Any) -> bool:
    return get_origin(annotation) in _UNION_ORIGINS and type(None) in get_args(annotation)


def _is_str_enum(annotation: Any) -> bool:
    return (
        isinstance(annotation, type)
        and issubclass(annotation, Enum)
        and all(isinstance(member.value, str) for member in annotation)
    )


def _parse_optional(raw: str) -> Any:
    """Map the CLI null sentinel to None; pass any other token through untouched."""
    return None if raw == _NONE_TOKEN else raw


def _is_model_type(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _reject(dotted: str, annotation: Any) -> None:
    raise TypeError(
        f"argmap does not support config field {dotted!r} of type {annotation}; "
        "simplify the Config or handle this field outside the CLI."
    )


def _dest(dotted: str) -> str:
    return f"{_DEST_PREFIX}{dotted}"


def _option_is_available(parser: argparse.ArgumentParser, *options: str) -> bool:
    return all(option not in parser._option_string_actions for option in options)


def config_option_arities(
    config_type: type[BaseModel],
    *,
    prefix: str = "",
) -> dict[str, int]:
    """Return possible config option strings without validating field support."""
    result: dict[str, int] = {}
    for name, field in config_type.model_fields.items():
        dotted = f"{prefix}{name}"
        flag = "--" + dotted.replace("_", "-")
        inner = _unwrap_optional(field.annotation)
        if _is_model_type(inner):
            result.update(config_option_arities(inner, prefix=f"{dotted}."))
        elif inner is bool:
            result[flag] = 0
            result["--no-" + dotted.replace("_", "-")] = 0
        else:
            result[flag] = 1
    return result


def _register_scalar(
    parser: argparse.ArgumentParser,
    *,
    flag: str,
    dotted: str,
    is_optional: bool,
    choices: tuple[Any, ...] | list[Any] | None = None,
) -> None:
    """Register a single-value option, folding in optional-null and choices."""
    if not _option_is_available(parser, flag):
        return
    kwargs: dict[str, Any] = {"dest": _dest(dotted), "default": argparse.SUPPRESS}
    resolved = list(choices) if choices is not None else None
    if is_optional:
        # `--field null` parses to None before argparse checks choices, so the
        # sentinel must be a valid choice when choices are constrained.
        kwargs["type"] = _parse_optional
        if resolved is not None:
            resolved = [*resolved, None]
    if resolved is not None:
        kwargs["choices"] = resolved
    parser.add_argument(flag, **kwargs)


def register_config_args(
    parser: argparse.ArgumentParser,
    config_type: type[BaseModel],
    *,
    prefix: str = "",
) -> None:
    """Register one argparse option per supported Config field."""
    for name, field in config_type.model_fields.items():
        dotted = f"{prefix}{name}"
        flag = "--" + dotted.replace("_", "-")
        is_optional = _is_optional(field.annotation)
        inner = _unwrap_optional(field.annotation)
        origin = get_origin(inner)

        if _is_model_type(inner):
            register_config_args(parser, inner, prefix=f"{dotted}.")
        elif inner in _BARE_UNSUPPORTED_TYPES:
            _reject(dotted, field.annotation)
        elif inner is bool:
            negative_flag = "--no-" + dotted.replace("_", "-")
            if not _option_is_available(parser, flag, negative_flag):
                continue
            parser.add_argument(
                flag,
                dest=_dest(dotted),
                action=argparse.BooleanOptionalAction,
                default=argparse.SUPPRESS,
            )
        elif origin is list or inner is list:
            if not _option_is_available(parser, flag):
                continue
            parser.add_argument(
                flag,
                dest=_dest(dotted),
                type=json.loads,
                metavar="JSON",
                default=argparse.SUPPRESS,
            )
        elif origin is Literal:
            values = get_args(inner)
            choices = values if all(isinstance(value, str) for value in values) else None
            _register_scalar(
                parser, flag=flag, dotted=dotted, is_optional=is_optional, choices=choices
            )
        elif _is_str_enum(inner):
            _register_scalar(
                parser,
                flag=flag,
                dotted=dotted,
                is_optional=is_optional,
                choices=[member.value for member in inner],
            )
        elif origin in _MAPPING_ORIGINS or origin in _UNION_ORIGINS or origin is not None:
            _reject(dotted, field.annotation)
        else:
            _register_scalar(parser, flag=flag, dotted=dotted, is_optional=is_optional)


def collect_cli_config_namespace(
    namespace: argparse.Namespace,
    config_type: type[BaseModel],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    """Collect CLI-provided fields into nested settings init kwargs."""
    raw_namespace = vars(namespace)
    result: dict[str, Any] = {}
    for name, field in config_type.model_fields.items():
        dotted = f"{prefix}{name}"
        inner = _unwrap_optional(field.annotation)
        if _is_model_type(inner):
            nested = collect_cli_config_namespace(namespace, inner, prefix=f"{dotted}.")
            if nested:
                result[name] = nested
        else:
            dest = _dest(dotted)
            if dest in raw_namespace:
                result[name] = raw_namespace[dest]
    return result
