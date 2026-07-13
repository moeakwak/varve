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
    if decision == "accept":
        return "Accepted", "accepted"
    if decision == "reject":
        return "Rejected", "rejected"
    raise ValueError(f"Unknown source review decision: {decision}")


def _action_style(decision: ReviewAction, kind: str) -> str:
    return f"varve.review.{decision}_{kind}"


def _append_count(text: Text, value: int, label: str, decision: ReviewAction) -> None:
    text.append(str(value), style=_action_style(decision, "action"))
    text.append(f" {label}")


def _group_line(group: ReviewGroupResult, decision: ReviewAction) -> Text:
    _, past = _words(decision)
    line = Text()
    line.append(group.canonical_target, style="varve.review.stage")
    line.append(": ")
    clauses: list[Text] = []
    if group.recorded:
        clause = Text()
        _append_count(
            clause,
            len(group.recorded),
            "decision recorded" if len(group.recorded) == 1 else "decisions recorded",
            decision,
        )
        clauses.append(clause)
    if group.already_decided:
        clause = Text()
        clause.append(str(len(group.already_decided)), style=_action_style(decision, "already"))
        clause.append(f" already {past}", style=_action_style(decision, "already"))
        clauses.append(clause)
    for index, clause in enumerate(clauses):
        if index:
            line.append(", ")
        line.append_text(clause)
    if group.did_not_need_review:
        if clauses:
            line.append("; ")
        line.append(
            f"{len(group.did_not_need_review)} "
            + (
                "cell did not need review"
                if len(group.did_not_need_review) == 1
                else "cells did not need review"
            ),
            style="varve.review.noop",
        )
    line.append(".")
    return line


def render_source_review(console: Console, result: SourceReviewResult) -> None:
    """Render one pipeline's explicit accept/reject result."""

    title, past = _words(result.decision)
    if result.exact_target is not None:
        line = Text()
        if result.recorded:
            line.append(title, style=_action_style(result.decision, "action"))
            line.append(" source change for ")
            line.append(result.exact_target, style="varve.review.stage")
            line.append(".")
        elif result.already_decided:
            line.append(result.exact_target, style="varve.review.stage")
            line.append(" was already ")
            line.append(past, style=_action_style(result.decision, "already"))
            line.append(".")
        else:
            line.append(result.exact_target, style="varve.review.stage")
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
            "review decision" if len(result.recorded) == 1 else "review decisions",
            result.decision,
        )
        total.append(" across ")
        total.append(
            str(len(result.source_changed_cells)),
            style=_action_style(result.decision, "action"),
        )
        total.append(
            " source-changed cell."
            if len(result.source_changed_cells) == 1
            else " source-changed cells."
        )
        console.print(total)
    elif result.has_source_changes:
        console.print("No review decisions changed.", style="varve.review.noop")
    else:
        console.print("No source changes require review.", style="varve.review.noop")


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
        parts = []
        if entry.result.recorded:
            parts.append(
                (
                    len(entry.result.recorded),
                    "decision recorded"
                    if len(entry.result.recorded) == 1
                    else "decisions recorded",
                    "action",
                )
            )
        if entry.result.already_decided:
            parts.append((len(entry.result.already_decided), f"already {past}", "already"))
        for index, (count, label, style_kind) in enumerate(parts):
            if index:
                line.append("; ")
            line.append(str(count), style=_action_style(decision, style_kind))
            line.append(
                f" {label}",
                style=_action_style(decision, style_kind) if style_kind == "already" else "",
            )
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
            "review decision" if recorded == 1 else "review decisions",
            decision,
        )
        total.append(" across ")
        total.append(str(len(changed_entries)), style=_action_style(decision, "action"))
        total.append(" pipeline branch." if len(changed_entries) == 1 else " pipeline branches.")
        console.print(total)
    elif changed_entries:
        console.print("No review decisions changed.", style="varve.review.noop")
    if no_source_count:
        console.print(
            f"{no_source_count} "
            + ("pipeline branch had" if no_source_count == 1 else "pipeline branches had")
            + " no source changes.",
            style="varve.review.noop",
        )
    if failures:
        console.print(
            f"{len(failures)} "
            + ("pipeline branch failed." if len(failures) == 1 else "pipeline branches failed."),
            style="varve.review.error",
        )
