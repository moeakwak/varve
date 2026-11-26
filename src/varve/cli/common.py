"""Arguments, error reporting, and command bodies shared by ``Pipeline.cli()`` and ``varve``.

Importing this module does not load the engine: the command bodies and
:func:`user_errors` import it on demand, so ``varve ls`` stays engine-free.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from rich.console import Console

from varve.style import make_console

if TYPE_CHECKING:
    from varve.pipeline import Pipeline


def user_errors() -> tuple[type[BaseException], ...]:
    """Errors reported as one line; importing them loads the engine, so only on demand."""

    from varve.engine.probe import ProbeError
    from varve.engine.refs import RefResolutionError
    from varve.engine.resources import ResourceError
    from varve.graph import GraphError
    from varve.store.db import SchemaError
    from varve.store.lock import LockBusyError

    return (
        ValueError,
        GraphError,
        ProbeError,
        RefResolutionError,
        ResourceError,
        SchemaError,
        LockBusyError,
        FileNotFoundError,
        sqlite3.OperationalError,
    )


def print_error(error: BaseException) -> None:
    errors = make_console(stderr=True)
    errors.print("[red]error:[/] ", end="")
    errors.print(str(error), markup=False, highlight=False, soft_wrap=True)


def report_errors(
    command: Callable[[], int],
    errors: Callable[[], tuple[type[BaseException], ...]] = user_errors,
) -> int:
    """Run ``command``; validation errors and ``errors()`` print one message and return 1."""

    try:
        return command()
    except KeyboardInterrupt:  # outside a run's own handling (startup, shutdown)
        make_console(stderr=True).print("[yellow]interrupted[/]")
        return 130
    except ValidationError as error:
        make_console(stderr=True).print(
            f"[red]error:[/] invalid configuration or arguments\n{error}", soft_wrap=True
        )
        return 1
    except Exception as error:
        if not isinstance(error, errors()):
            raise
        print_error(error)
        return 1


# -- arguments ------------------------------------------------------------------


def add_branch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--branch", default="main", metavar="NAME", help="Branch (default: main).")


def add_status_args(parser: argparse.ArgumentParser) -> None:
    add_branch(parser)
    parser.add_argument("--expand", action="store_true", help="List every unit.")
    parser.add_argument("--rehash", action="store_true", help="Verify artifacts by content hash.")


def add_inspect_args(parser: argparse.ArgumentParser) -> None:
    add_branch(parser)
    parser.add_argument("--sources", action="store_true", help="List the stage's source closure.")
    parser.add_argument("--log", action="store_true", help="Print the unit's captured log.")


def add_clean_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--branch", metavar="NAME", help="Only this branch.")
    parser.add_argument("--storage", metavar="NAME", help="Only this storage.")
    parser.add_argument("--orphans", action="store_true", help="Only orphaned units.")
    parser.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")


# -- command bodies -------------------------------------------------------------


def status_command(
    console: Console,
    pipeline: type[Pipeline],
    selector: str | None,
    ns: argparse.Namespace,
    **options: Any,
) -> int:
    from varve.cli import render
    from varve.engine.service import parse_unit_selector, probe_pipeline

    stage, coords = parse_unit_selector(selector) if selector else (None, {})
    expand = bool(ns.expand or stage)
    view = probe_pipeline(
        pipeline,
        branch=ns.branch,
        stages=[stage] if stage else None,
        rehash=ns.rehash,
        expand=expand,
        **options,
    )
    render.render_status(console, view, coords=coords, expand=expand)
    return 0


def inspect_command(
    console: Console,
    pipeline: type[Pipeline],
    selector: str,
    ns: argparse.Namespace,
    **options: Any,
) -> int:
    from varve.cli import render
    from varve.engine.service import inspect_pipeline

    result = inspect_pipeline(pipeline, selector, branch=ns.branch, **options)
    render.render_inspect(console, result, sources=ns.sources, log=ns.log)
    return 0


def reuse_command(
    console: Console, pipeline: type[Pipeline], stage: str, ns: argparse.Namespace, **options: Any
) -> int:
    from varve.cli import render
    from varve.engine.service import reuse_stage

    render.render_reuse(console, reuse_stage(pipeline, stage, branch=ns.branch, **options))
    return 0


def clean_command(
    console: Console, pipeline: type[Pipeline], ns: argparse.Namespace, **options: Any
) -> int:
    from varve.cli import render
    from varve.engine.clean import clean_pipeline, plan_clean

    selection = {"branch": ns.branch, "storage": ns.storage, "orphans": ns.orphans, **options}
    plan = plan_clean(pipeline, **selection)
    if not plan.paths and not plan.rows:
        console.print(f"Nothing to clean for {plan.description}.")
        return 0
    render.render_clean_plan(console, plan)
    if not ns.yes and not _confirm("Remove these?"):
        console.print("Aborted.")
        return 1
    done = clean_pipeline(pipeline, **selection)
    console.print(f"Removed {len(done.paths)} path(s) and {done.rows} unit row(s).")
    return 0


def _confirm(message: str) -> bool:
    try:
        answer = input(f"{message} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
