"""Natural-language renderers for source-review actions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.console import Console
from rich.text import Text

from varve.engine.review import ReviewAction, ReviewGroupResult, SourceReviewResult


@dataclass(frozen=True)
class BulkReviewEntry:
    module: str
    branch: str
    result: SourceReviewResult


@dataclass(frozen=True)
class BulkReviewFailure:
    module: str
    branch: str
    error: str


def _words(decision: ReviewAction) -> tuple[str, str]:
    try:
        return {"reuse": ("Reused", "reused"), "invalidate": ("Invalidated", "invalidated")}[
            decision
        ]
    except KeyError as error:
        raise ValueError(f"Unknown source review decision: {decision}") from error


def _action_style(decision: ReviewAction, kind: str) -> str:
    return f"varve.review.{decision}_{kind}"


def _append_count(text: Text, value: int, label: str, decision: ReviewAction) -> None:
    text.append(str(value), style=_action_style(decision, "action"))
    text.append(f" {label}")


def _plural(value: int, singular: str, plural: str | None = None) -> str:
    return singular if value == 1 else plural or f"{singular}s"


def _group_line(group: ReviewGroupResult, decision: ReviewAction) -> Text:
    _, past = _words(decision)
    line = Text()
    line.append(group.canonical_target, style="varve.review.stage")
    line.append(": ")
    if group.recorded:
        _append_count(
            line,
            len(group.recorded),
            _plural(len(group.recorded), "decision recorded", "decisions recorded"),
            decision,
        )
    if group.already_decided:
        if group.recorded:
            line.append(", ")
        line.append(str(len(group.already_decided)), style=_action_style(decision, "already"))
        line.append(f" already {past}", style=_action_style(decision, "already"))
    if group.did_not_need_review:
        if group.recorded or group.already_decided:
            line.append("; ")
        line.append(
            f"{len(group.did_not_need_review)} "
            + _plural(
                len(group.did_not_need_review),
                "stage did not need review",
                "stages did not need review",
            ),
            style="varve.review.noop",
        )
    line.append(".")
    return line


def render_source_review(console: Console, result: SourceReviewResult) -> None:
    """Render one pipeline's explicit reuse/invalidate result."""

    title, past = _words(result.decision)
    if not result.has_source_changes and not result.did_not_need_review:
        console.print("No source changes require review.", style="varve.review.noop")
        return
    if not result.has_source_changes and len(result.did_not_need_review) == 1:
        line = Text()
        line.append(result.did_not_need_review[0], style="varve.review.stage")
        line.append(" did not need review.", style="varve.review.noop")
        console.print(line)
        return

    if not result.has_source_changes:
        console.print("No source changes require review.", style="varve.review.noop")
        return

    console.print(
        f"{title} source changes",
        style=_action_style(result.decision, "heading"),
    )
    console.print()
    for group in result.groups:
        console.print(_group_line(group, result.decision))
    console.print()
    if result.recorded:
        total = Text("Recorded ", style="varve.review.total")
        _append_count(
            total,
            len(result.recorded),
            _plural(len(result.recorded), "review decision"),
            result.decision,
        )
        total.append(" across ")
        total.append(
            str(len(result.recorded)),
            style=_action_style(result.decision, "action"),
        )
        total.append(f" {_plural(len(result.recorded), 'source-changed stage')}.")
        console.print(total)
    else:
        console.print("No review decisions changed.", style="varve.review.noop")


def render_bulk_source_review(
    console: Console,
    decision: ReviewAction,
    entries: Sequence[BulkReviewEntry],
    failures: Sequence[BulkReviewFailure] = (),
) -> None:
    """Render a bounded review summary across pipeline branches."""

    title, past = _words(decision)
    changed_entries = [entry for entry in entries if entry.result.has_source_changes]
    no_source_count = len(entries) - len(changed_entries)
    if not changed_entries and not failures:
        console.print("No source changes require review.", style="varve.review.noop")
        return

    console.print(f"{title} source changes", style=_action_style(decision, "heading"))
    console.print()
    for entry in changed_entries:
        line = Text()
        line.append(entry.module, style="varve.review.module")
        line.append(" [")
        line.append(entry.branch, style="varve.review.branch")
        line.append("]: ")
        if entry.result.recorded:
            count = len(entry.result.recorded)
            _append_count(
                line,
                count,
                _plural(count, "decision recorded", "decisions recorded"),
                decision,
            )
        if entry.result.already_decided:
            if entry.result.recorded:
                line.append("; ")
            style = _action_style(decision, "already")
            line.append(str(len(entry.result.already_decided)), style=style)
            line.append(f" already {past}", style=style)
        line.append(".")
        console.print(line)
    for failure in failures:
        line = Text()
        line.append(failure.module, style="varve.review.module")
        line.append(" [")
        line.append(failure.branch, style="varve.review.branch")
        line.append(f"]: {failure.error}.", style="varve.review.error")
        console.print(line)

    console.print()
    recorded = sum(len(entry.result.recorded) for entry in changed_entries)
    if recorded:
        total = Text("Recorded ", style="varve.review.total")
        _append_count(
            total,
            recorded,
            _plural(recorded, "review decision"),
            decision,
        )
        total.append(" across ")
        total.append(str(len(changed_entries)), style=_action_style(decision, "action"))
        total.append(f" {_plural(len(changed_entries), 'pipeline branch', 'pipeline branches')}.")
        console.print(total)
    elif changed_entries:
        console.print("No review decisions changed.", style="varve.review.noop")
    if no_source_count:
        console.print(
            f"{no_source_count} "
            + _plural(no_source_count, "pipeline branch had", "pipeline branches had")
            + " no source changes.",
            style="varve.review.noop",
        )
    if failures:
        console.print(
            f"{len(failures)} "
            + _plural(len(failures), "pipeline branch failed", "pipeline branches failed")
            + ".",
            style="varve.review.error",
        )
