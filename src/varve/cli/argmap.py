"""Map pydantic Args models to argparse options and back."""

from __future__ import annotations

import argparse
import json
import types
from collections.abc import Iterator, Mapping
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

_UNION_ORIGINS = (Union, types.UnionType)
_MAPPING_ORIGINS = (dict, Mapping)
_BARE_UNSUPPORTED_TYPES = (dict, Mapping, tuple, set)
_DEST_PREFIX = "__varve_args__."
# CLI sentinel that maps an optional scalar field to None, matching the default
# pydantic-settings `cli_parse_none_str`.
_NONE_TOKEN = "null"
STAGE_SELECTOR_HELP = (
    "Base selects all active cells; omitted axes are wildcards; full coordinates select one cell."
)


def add_stage_selection(
    parser: argparse.ArgumentParser,
    selector_help: str | None = None,
    *,
    verb: str | None = None,
) -> None:
    group = parser.add_mutually_exclusive_group()
    descriptions = {
        "upto": "the selector and all upstream stages",
        "downstream": "the selector and all downstream stages",
        "only": "only the selector",
    }
    for option, description in descriptions.items():
        help_text = selector_help if verb is None else f"{verb} {description}. {selector_help}"
        group.add_argument(f"--{option}", metavar="STAGE_SELECTOR", help=help_text)


def add_review_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stage",
        action="append",
        default=[],
        metavar="BASE_STAGE",
        help="Review this base Stage; repeat to take a union. Coordinates are not accepted.",
    )


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
        f"argmap does not support args field {dotted!r} of type {annotation}; "
        "simplify the Args model or handle this field outside the CLI."
    )


def _dest(dotted: str) -> str:
    return f"{_DEST_PREFIX}{dotted}"


def _option_is_available(parser: argparse.ArgumentParser, *options: str) -> bool:
    return all(option not in parser._option_string_actions for option in options)


def _iter_fields(args_type: type[BaseModel], prefix: str = "") -> Iterator[tuple[str, Any, Any]]:
    for name, field in args_type.model_fields.items():
        dotted = f"{prefix}{name}"
        inner = _unwrap_optional(field.annotation)
        if _is_model_type(inner):
            yield from _iter_fields(inner, f"{dotted}.")
        else:
            yield dotted, field, inner


def args_option_arities(args_type: type[BaseModel]) -> dict[str, int]:
    """Return possible Args option strings without validating field support."""
    result: dict[str, int] = {}
    for dotted, _field, inner in _iter_fields(args_type):
        flag = "--" + dotted.replace("_", "-")
        if inner is bool:
            result[flag] = 0
            result["--no-" + dotted.replace("_", "-")] = 0
        else:
            result[flag] = 1
    return result


def _help_text(dotted: str, description: str | None) -> str:
    return description or f"Set Args.{dotted}."


def _register_scalar(
    parser: argparse.ArgumentParser,
    dotted: str,
    field: Any,
    choices: tuple[Any, ...] | list[Any] | None = None,
) -> None:
    """Register a single-value option, folding in optional-null and choices."""
    flag = "--" + dotted.replace("_", "-")
    if not _option_is_available(parser, flag):
        return
    kwargs: dict[str, Any] = {
        "dest": _dest(dotted),
        "default": argparse.SUPPRESS,
        "help": _help_text(dotted, field.description),
        "metavar": dotted.replace(".", "_").upper(),
    }
    resolved = list(choices) if choices is not None else None
    if _is_optional(field.annotation):
        # `--field null` parses to None before argparse checks choices, so the
        # sentinel must be a valid choice when choices are constrained.
        kwargs["type"] = _parse_optional
        if resolved is not None:
            resolved = [*resolved, None]
    if resolved is not None:
        kwargs["choices"] = resolved
    parser.add_argument(flag, **kwargs)


def register_args(
    parser: argparse.ArgumentParser,
    args_type: type[BaseModel],
) -> None:
    """Register one argparse option per supported Args field."""
    for dotted, field, inner in _iter_fields(args_type):
        flag = "--" + dotted.replace("_", "-")
        origin = get_origin(inner)

        if inner in _BARE_UNSUPPORTED_TYPES:
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
                help=_help_text(dotted, field.description),
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
                help=_help_text(dotted, field.description),
            )
        else:
            choices = None
            if origin is Literal:
                values = get_args(inner)
                choices = values if all(isinstance(value, str) for value in values) else None
            elif _is_str_enum(inner):
                choices = [member.value for member in inner]
            elif origin in _MAPPING_ORIGINS or origin in _UNION_ORIGINS or origin is not None:
                _reject(dotted, field.annotation)
            _register_scalar(parser, dotted, field, choices)


def collect_cli_args_namespace(
    namespace: argparse.Namespace,
    args_type: type[BaseModel],
) -> dict[str, Any]:
    """Collect CLI-provided fields into nested settings init kwargs."""
    raw_namespace = vars(namespace)
    result: dict[str, Any] = {}
    for dotted, _field, _inner in _iter_fields(args_type):
        dest = _dest(dotted)
        if dest not in raw_namespace:
            continue
        target = result
        path = dotted.split(".")
        for name in path[:-1]:
            target = target.setdefault(name, {})
        target[path[-1]] = raw_namespace[dest]
    return result


def model_from_namespace(namespace: argparse.Namespace, args_type: type[BaseModel]) -> BaseModel:
    return args_type.model_validate(collect_cli_args_namespace(namespace, args_type))
