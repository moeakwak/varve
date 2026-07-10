"""Tests for the shared Rich styling module."""

from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.text import Text

from varve import style
from varve.style import VarveStatusHighlighter, status_text


def test_status_text_uses_semantic_style() -> None:
    assert status_text("hit").style == "green"
    assert status_text("dirty").style == "red"


def test_status_text_unknown_status_is_unstyled() -> None:
    assert status_text("mystery").style == ""


def test_dependency_styles_are_available_from_shared_theme() -> None:
    expected = {
        "stage": "bold cyan",
        "function": "cyan",
        "class": "magenta",
        "module": "blue",
        "value": "green",
        "broad": "yellow",
        "metadata": "dim",
    }
    assert style.DEPENDENCY_STYLES == expected
    for kind in expected:
        assert style._THEME.styles[f"varve.dependency.{kind}"]


def test_highlighter_maps_tokens_to_theme_styles() -> None:
    highlighter = VarveStatusHighlighter()
    text = Text("run done no-cache artifact-missing")
    highlighter.highlight(text)
    spans = {text.plain[span.start : span.end]: span.style for span in text.spans}
    assert spans["run"] == "varve.run"
    assert spans["done"] == "varve.done"
    assert spans["no-cache"] == "varve.no_cache"
    assert spans["artifact-missing"] == "varve.artifact_missing"


def test_highlighter_marks_stage_name() -> None:
    highlighter = VarveStatusHighlighter()
    text = Text("[render_ablation] done · 0.31s")
    highlighter.highlight(text)
    spans = {text.plain[span.start : span.end]: span.style for span in text.spans}
    assert spans["[render_ablation]"] == "varve.stage"
    assert spans["done"] == "varve.done"


def test_highlighter_accents_refresh_header() -> None:
    highlighter = VarveStatusHighlighter()
    text = Text("▸ refresh studies.exp.demo --branch main")
    highlighter.highlight(text)
    spans = {text.plain[span.start : span.end]: span.style for span in text.spans}
    assert spans["▸ refresh studies.exp.demo --branch main"] == "varve.refresh"


def test_themed_console_renders_status_color() -> None:
    buffer = StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        color_system="standard",
        theme=style._THEME,
        highlighter=VarveStatusHighlighter(),
    )
    console.print("run done", markup=False)
    output = buffer.getvalue()
    assert "\x1b[36mrun\x1b[0m" in output  # varve.run -> cyan
    assert "\x1b[32mdone\x1b[0m" in output  # varve.done -> green
