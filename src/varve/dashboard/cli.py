"""Top-level varve dashboard CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from varve.dashboard.discovery import discover_experiments
from varve.dashboard.render import render_detail, render_overview
from varve.dashboard.state import load_state


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "show":
        return _show(args.root, args.experiment, args.branch, args.include_temp)
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
