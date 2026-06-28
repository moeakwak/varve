"""Top-level varve dashboard CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from varve.dashboard.discovery import discover_experiments
from varve.dashboard.models import ExperimentEntry
from varve.dashboard.render import render_detail, render_overview
from varve.dashboard.state import import_entry_experiment, load_state, resolve_entry_branch
from varve.engine.runner import run


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "show":
        return _show(args.root, args.experiment, args.branch, args.include_temp)
    if args.command == "refresh":
        return _refresh(args.root, args.include_temp, args.prefix)
    root = args.root if args.command == "ls" else Path.cwd()
    include_temp = args.include_temp
    return _ls(root, include_temp)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="varve")
    parser.add_argument(
        "--include-temp",
        action="store_true",
        default=False,
        help="include temporary override branches under out/.tmp",
    )
    subparsers = parser.add_subparsers(dest="command")

    ls_parser = subparsers.add_parser("ls", help="list discovered experiment stores")
    ls_parser.add_argument("--root", type=Path, default=Path.cwd())
    ls_parser.add_argument(
        "--include-temp",
        action="store_true",
        default=argparse.SUPPRESS,
        help="include temporary override branches under out/.tmp",
    )

    show_parser = subparsers.add_parser("show", help="show one experiment store")
    show_parser.add_argument("experiment")
    show_parser.add_argument("--root", type=Path, default=Path.cwd())
    show_parser.add_argument("--branch", default="main")
    show_parser.add_argument(
        "--include-temp",
        action="store_true",
        default=argparse.SUPPRESS,
        help="include temporary override branches under out/.tmp",
    )

    refresh_parser = subparsers.add_parser("refresh", help="run stale discovered experiments")
    refresh_parser.add_argument("--root", type=Path, default=Path.cwd())
    refresh_parser.add_argument(
        "--prefix",
        help="only refresh experiments whose module starts with this prefix",
    )
    refresh_parser.add_argument(
        "--include-temp",
        action="store_true",
        default=argparse.SUPPRESS,
        help="include temporary override branches under out/.tmp",
    )
    return parser


def _ls(root: Path, include_temp: bool) -> int:
    entries = discover_experiments(root, include_temporary=include_temp)
    if not entries:
        print(f"No experiments found under {root}", file=sys.stderr)
        return 1
    states = [load_state(entry) for entry in entries]
    render_overview(states)
    return 0


def _show(root: Path, experiment_id: str, branch: str, include_temp: bool) -> int:
    entries = discover_experiments(root, include_temporary=include_temp)
    by_key = {(entry.experiment_id, entry.branch): entry for entry in entries}
    entry = by_key.get((experiment_id, branch))
    if entry is None:
        print(f"Unknown experiment: {experiment_id} (branch {branch})", file=sys.stderr)
        if by_key:
            print("Available experiments:", file=sys.stderr)
            for known_id, known_branch in sorted(by_key):
                print(f"  {known_id} --branch {known_branch}", file=sys.stderr)
        else:
            print(f"No experiments found under {root}", file=sys.stderr)
        return 1
    render_detail(load_state(entry))
    return 0


def _refresh(root: Path, include_temp: bool, prefix: str | None = None) -> int:
    entries = discover_experiments(root, include_temporary=include_temp)
    if prefix is not None:
        entries = [
            entry
            for entry in entries
            if entry.module is not None and entry.module.startswith(prefix)
        ]
    if not entries:
        print(f"No experiments found under {root}", file=sys.stderr)
        return 1

    refreshed = 0
    failed = 0
    for entry in entries:
        state = load_state(entry)
        if state.status != "stale":
            continue
        print(f"Refreshing {entry.experiment_id} --branch {entry.branch}")
        try:
            _run_entry(entry)
        except Exception as error:  # noqa: BLE001 - refresh should continue with later stores.
            failed += 1
            print(
                f"Failed to refresh {entry.experiment_id} --branch {entry.branch}: {error}",
                file=sys.stderr,
            )
        else:
            refreshed += 1

    if refreshed == 0 and failed == 0:
        print("No stale experiments found")
    return 1 if failed else 0


def _run_entry(entry: ExperimentEntry) -> None:
    experiment = import_entry_experiment(entry)
    resolved = resolve_entry_branch(entry, experiment)
    run(
        experiment,
        resolved.config,
        args=experiment.Args(),
        cli_out=resolved.output_base,
        branch=resolved.branch,
        is_temporary=resolved.is_temporary,
        temporary_config=resolved.temporary_config,
    )
