"""Source-code hashing for stage and helper callables."""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from collections.abc import Callable
from typing import Any


def _strip_docstrings(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(
            child,
            ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        ):
            continue
        body = child.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]


def _is_varve_decorator(node: ast.AST) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id in {"stage", "batch_stage"}
    if isinstance(target, ast.Attribute):
        return target.attr in {"stage", "batch_stage"}
    return False


def _strip_varve_stage_decorators(node: ast.AST, *, strip: bool) -> None:
    if not strip:
        return
    for child in ast.walk(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            child.decorator_list = [
                decorator
                for decorator in child.decorator_list
                if not _is_varve_decorator(decorator)
            ]


def _normalized_source_hash(source: str, *, strip_varve_decorators: bool = False) -> str:
    tree = ast.parse(textwrap.dedent(source))
    _strip_docstrings(tree)
    _strip_varve_stage_decorators(tree, strip=strip_varve_decorators)
    normalized = ast.dump(tree, annotate_fields=True, include_attributes=False)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def source_hash(func: Callable[..., Any]) -> str:
    """Return a stable hash of a callable's normalized AST.

    The hash intentionally ignores comments, formatting, source locations, and
    docstrings. It does not chase deeper callees; those must be listed in
    source dependencies or encoded through `KeySpec.files` / `KeySpec.values`.
    """

    try:
        source = inspect.getsource(func)
    except OSError as error:
        raise ValueError(f"Cannot inspect source for {func!r}") from error

    return _normalized_source_hash(
        source,
        strip_varve_decorators=hasattr(func, "__varve_stage__"),
    )
