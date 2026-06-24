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
        return _show(args.root, args.experiment)
    root = args.root if args.command == "ls" else Path.cwd()
    return _ls(root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="varve")
    subparsers = parser.add_subparsers(dest="command")

    ls_parser = subparsers.add_parser("ls", help="list discovered experiment stores")
    ls_parser.add_argument("--root", type=Path, default=Path.cwd())

    show_parser = subparsers.add_parser("show", help="show one experiment store")
    show_parser.add_argument("experiment")
    show_parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser


def _ls(root: Path) -> int:
    entries = discover_experiments(root)
    if not entries:
        print(f"No experiments found under {root}", file=sys.stderr)
        return 1
    states = [load_state(entry) for entry in entries]
    render_overview(states)
    return 0


def _show(root: Path, experiment_id: str) -> int:
    entries = discover_experiments(root)
    by_id = {entry.experiment_id: entry for entry in entries}
    entry = by_id.get(experiment_id)
    if entry is None:
        print(f"Unknown experiment: {experiment_id}", file=sys.stderr)
        if by_id:
            print("Available experiments:", file=sys.stderr)
            for known_id in sorted(by_id):
                print(f"  {known_id}", file=sys.stderr)
        else:
            print(f"No experiments found under {root}", file=sys.stderr)
        return 1
    render_detail(load_state(entry))
    return 0
