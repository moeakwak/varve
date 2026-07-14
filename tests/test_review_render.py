"""Tests for bounded, natural-language source-review summaries."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from varve import style
from varve.cli.review import (
    BulkReviewEntry,
    BulkReviewFailure,
    render_bulk_source_review,
    render_source_review,
)
from varve.engine.review import ReviewAction, ReviewGroupResult, SourceReviewResult


def _console(*, color: bool) -> tuple[Console, StringIO]:
    output = StringIO()
    return (
        Console(
            file=output,
            force_terminal=color,
            color_system="standard" if color else None,
            no_color=not color,
            theme=style._THEME,
            width=120,
        ),
        output,
    )


def _result(
    *,
    decision: ReviewAction = "reuse",
    target: str = "score",
    recorded: tuple[str, ...] = ("score",),
    already: tuple[str, ...] = (),
    did_not_need: tuple[str, ...] = (),
    groups: bool = True,
) -> SourceReviewResult:
    group = ReviewGroupResult(
        canonical_target=target,
        recorded=recorded,
        already_decided=already,
        did_not_need_review=did_not_need,
    )
    return SourceReviewResult(
        decision=decision,
        groups=(group,) if groups else (),
        recorded=recorded,
        already_decided=already,
        did_not_need_review=did_not_need,
    )


def test_exact_review_messages_cover_recorded_already_and_not_needed() -> None:
    console, output = _console(color=False)
    render_source_review(
        console,
        _result(target="score", recorded=("score",), already=(), did_not_need=()),
    )
    render_source_review(
        console,
        _result(
            recorded=(),
            already=("score",),
            target="score",
        ),
    )
    render_source_review(
        console,
        _result(
            recorded=(),
            did_not_need=("score",),
            target="score",
        ),
    )
    render_source_review(
        console,
        _result(
            decision="invalidate",
            recorded=("score",),
            target="score",
        ),
    )

    lines = output.getvalue().splitlines()
    assert "Reused source changes" in lines
    assert any("already reused" in line for line in lines)
    assert any("did not need review" in line for line in lines)
    assert "Invalidated source changes" in lines


def test_broad_review_summary_uses_natural_language_and_distinct_noops() -> None:
    console, output = _console(color=False)
    render_source_review(
        console,
        _result(
            target="score",
            recorded=("score",),
            already=(),
            did_not_need=(),
        ),
    )
    assert "Reused source changes" in output.getvalue()
    assert "1 decision recorded" in output.getvalue()
    assert "source-changed stage" in output.getvalue()

    console, output = _console(color=False)
    render_source_review(
        console,
        _result(recorded=(), already=("score",)),
    )
    assert output.getvalue().endswith("No review decisions changed.\n")

    console, output = _console(color=False)
    render_source_review(
        console,
        _result(recorded=(), groups=False),
    )
    assert output.getvalue() == "No source changes require review.\n"


def test_broad_all_current_or_no_baseline_only_prints_no_source_changes() -> None:
    console, output = _console(color=False)
    render_source_review(
        console,
        _result(
            recorded=(),
            did_not_need=("score",),
        ),
    )

    assert output.getvalue() == "score did not need review.\n"


def test_invalidate_summary_uses_yellow_not_red_and_plain_text_stays_complete() -> None:
    console, output = _console(color=True)
    render_source_review(console, _result(decision="invalidate"))
    rendered = output.getvalue()

    assert "\x1b[" in rendered
    assert "33m" in rendered
    assert "31m" not in rendered
    assert "Invalidated source changes" in rendered
    assert "Recorded" in rendered


def test_review_semantic_colors_cover_stage_module_noop_and_error() -> None:
    console, output = _console(color=True)
    render_source_review(
        console,
        _result(
            recorded=("a",),
            did_not_need=("b",),
        ),
    )
    render_bulk_source_review(
        console,
        "reuse",
        (BulkReviewEntry("studies.exp.demo", "main", _result()),),
        (BulkReviewFailure("studies.exp.failed", "dev", "locked"),),
    )
    rendered = output.getvalue()

    assert "36m" in rendered  # stage cyan
    assert "34m" in rendered  # module blue
    assert "2m" in rendered  # branch/no-op dim
    assert "31m" in rendered  # real error red
    assert "32m" in rendered  # reuse action green


def test_bulk_review_folds_pipeline_branches_and_reports_failures() -> None:
    console, output = _console(color=False)
    render_bulk_source_review(
        console,
        "reuse",
        (
            BulkReviewEntry("studies.exp.one", "main", _result()),
            BulkReviewEntry(
                "studies.exp.two",
                "dev",
                _result(recorded=(), groups=False),
            ),
        ),
        (BulkReviewFailure("studies.exp.failed", "main", "output is locked"),),
    )

    plain = output.getvalue()
    assert "studies.exp.one [main]: 1 decision recorded." in plain
    assert "studies.exp.failed [main]: output is locked." in plain
    assert "Recorded 1 review decision across 1 pipeline branch." in plain
    assert "1 pipeline branch had no source changes." in plain
    assert "1 pipeline branch failed." in plain

    console, output = _console(color=False)
    render_bulk_source_review(
        console,
        "invalidate",
        (BulkReviewEntry("studies.exp.one", "main", _result(decision="invalidate")),),
    )
    assert "Invalidated source changes" in output.getvalue()
    assert "studies.exp.one [main]: 1 decision recorded." in output.getvalue()
