"""Natural-language renderers for source-review actions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from rich.console import Console
from rich.text import Text

from varve.engine.review import ReviewAction, ReviewStageResult, SourceReviewResult


class BulkReviewEntry(NamedTuple):
    module: str
    branch: str
    result: SourceReviewResult


class BulkReviewFailure(NamedTuple):
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


def _plural(value: int, singular: str, plural: str | None = None) -> str:
    return singular if value == 1 else plural or f"{singular}s"


def _group_line(stage: ReviewStageResult, decision: ReviewAction) -> Text:
    _, past = _words(decision)
    if stage.outcome == "recorded":
        detail = Text.assemble(("1", _action_style(decision, "action")), " decision recorded")
    elif stage.outcome == "already-decided":
        detail = Text(f"1 already {past}", style=_action_style(decision, "already"))
    else:
        detail = Text("1 stage did not need review", style="varve.review.noop")
    return Text.assemble(
        (stage.stage, "varve.review.stage"),
        ": ",
        detail,
        ".",
    )


def render_source_review(console: Console, result: SourceReviewResult) -> None:
    """Render one pipeline's explicit reuse/invalidate result."""

    title, past = _words(result.decision)
    if not result.has_source_changes:
        if len(result.did_not_need_review) == 1:
            console.print(
                Text.assemble(
                    (result.did_not_need_review[0], "varve.review.stage"),
                    (" did not need review.", "varve.review.noop"),
                )
            )
        else:
            console.print("No source changes require review.", style="varve.review.noop")
        return

    console.print(
        f"{title} source changes",
        style=_action_style(result.decision, "heading"),
    )
    console.print()
    for stage in result.stages:
        console.print(_group_line(stage, result.decision))
    console.print()
    if result.recorded:
        count = len(result.recorded)
        console.print(
            Text.assemble(
                ("Recorded ", "varve.review.total"),
                (str(count), _action_style(result.decision, "action")),
                f" {_plural(count, 'review decision')} across ",
                (str(count), _action_style(result.decision, "action")),
                f" {_plural(count, 'source-changed stage')}.",
            )
        )
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
        line = Text.assemble(
            (entry.module, "varve.review.module"),
            " [",
            (entry.branch, "varve.review.branch"),
            "]: ",
        )
        if entry.result.recorded:
            count = len(entry.result.recorded)
            line.append(str(count), style=_action_style(decision, "action"))
            line.append(f" {_plural(count, 'decision recorded', 'decisions recorded')}")
        if entry.result.already_decided:
            if entry.result.recorded:
                line.append("; ")
            style = _action_style(decision, "already")
            line.append(str(len(entry.result.already_decided)), style=style)
            line.append(f" already {past}", style=style)
        line.append(".")
        console.print(line)
    for failure in failures:
        console.print(
            Text.assemble(
                (failure.module, "varve.review.module"),
                " [",
                (failure.branch, "varve.review.branch"),
                (f"]: {failure.error}.", "varve.review.error"),
            )
        )

    console.print()
    recorded = sum(len(entry.result.recorded) for entry in changed_entries)
    if recorded:
        branches = len(changed_entries)
        console.print(
            Text.assemble(
                ("Recorded ", "varve.review.total"),
                (str(recorded), _action_style(decision, "action")),
                f" {_plural(recorded, 'review decision')} across ",
                (str(branches), _action_style(decision, "action")),
                f" {_plural(branches, 'pipeline branch', 'pipeline branches')}.",
            )
        )
    elif changed_entries:
        console.print("No review decisions changed.", style="varve.review.noop")
    if no_source_count:
        console.print(
            f"{no_source_count} {_plural(no_source_count, 'pipeline branch had', 'pipeline branches had')} no source changes.",
            style="varve.review.noop",
        )
    if failures:
        console.print(
            f"{len(failures)} {_plural(len(failures), 'pipeline branch failed', 'pipeline branches failed')}.",
            style="varve.review.error",
        )
