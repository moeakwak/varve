"""Map pydantic Config models to argparse options and back."""

from __future__ import annotations

import argparse
import json
import types
from collections.abc import Mapping
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

_UNION_ORIGINS = (Union, types.UnionType)
_MAPPING_ORIGINS = (dict, Mapping)
_BARE_UNSUPPORTED_TYPES = (dict, Mapping, tuple, set)
_DEST_PREFIX = "__varve_config__."


def _unwrap_optional(annotation: Any) -> Any:
    if get_origin(annotation) in _UNION_ORIGINS:
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


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
        elif origin in _MAPPING_ORIGINS or origin in _UNION_ORIGINS or origin is not None:
            _reject(dotted, field.annotation)
        else:
            if not _option_is_available(parser, flag):
                continue
            parser.add_argument(flag, dest=_dest(dotted), default=argparse.SUPPRESS)


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
