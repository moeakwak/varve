from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import varve.context
from varve.context import Ctx
from varve.store.store import Store


class FakeBar:
    def __init__(self, *, desc: str, total: int | None, initial: int, unit: str) -> None:
        self.desc = desc
        self.total = total
        self.initial = initial
        self.unit = unit
        self.updates = 0
        self.postfixes: list[str] = []
        self.closed = False

    def update(self, value: int) -> None:
        self.updates += value

    def set_postfix_str(self, text: str) -> None:
        self.postfixes.append(text)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def captured_bars(monkeypatch) -> list[FakeBar]:
    created: list[FakeBar] = []

    def fake_make_tqdm_progress(**kwargs) -> FakeBar:
        bar = FakeBar(**kwargs)
        created.append(bar)
        return bar

    monkeypatch.setattr(varve.context, "_make_tqdm_progress", fake_make_tqdm_progress)
    return created


def _ctx(tmp_path: Path, **kwargs) -> Ctx:
    return Ctx(config={}, out=tmp_path, store=Store(tmp_path), **kwargs)


def test_ctx_carries_args_when_provided(tmp_path: Path) -> None:
    args = object()
    ctx = _ctx(tmp_path, args=args)
    assert ctx.args is args


def test_ctx_args_defaults_to_none(tmp_path: Path) -> None:
    assert _ctx(tmp_path).args is None


def _collect(ctx: Ctx, iterable, **resume_kwargs) -> list[tuple[int, object]]:
    async def run() -> list[tuple[int, object]]:
        out: list[tuple[int, object]] = []
        async for index, item in ctx.resume(iterable, **resume_kwargs):
            out.append((index, item))
        return out

    return asyncio.run(run())


def test_resume_progress_defaults_to_stage_name_and_seeds_skipped(
    tmp_path: Path,
    captured_bars: list[FakeBar],
) -> None:
    ctx = _ctx(tmp_path, resume_skip=frozenset({0}), stage_name="render")
    assert _collect(ctx, ["zero", "one", "two"]) == [(1, "one"), (2, "two")]
    assert len(captured_bars) == 1
    bar = captured_bars[0]
    assert bar.desc == "render"  # default label is the stage name
    assert bar.total == 3
    assert bar.initial == 1  # skipped index 0 seeds the initial count
    assert bar.unit == "batch"
    assert bar.updates == 2  # one update per resumed item
    assert bar.closed


def test_resume_progress_desc_and_unit_overrides(
    tmp_path: Path,
    captured_bars: list[FakeBar],
) -> None:
    ctx = _ctx(tmp_path, stage_name="render")
    _collect(ctx, ["a", "b"], desc="items", unit="row")
    bar = captured_bars[0]
    assert bar.desc == "items"
    assert bar.unit == "row"
    assert bar.total == 2
    assert bar.initial == 0


def test_resume_progress_disabled_creates_no_bar(
    tmp_path: Path,
    captured_bars: list[FakeBar],
) -> None:
    ctx = _ctx(tmp_path, stage_name="render")
    assert _collect(ctx, ["a", "b"], progress=False) == [(0, "a"), (1, "b")]
    assert captured_bars == []


def test_resume_progress_postfix_annotates_each_item(
    tmp_path: Path,
    captured_bars: list[FakeBar],
) -> None:
    ctx = _ctx(tmp_path, stage_name="render")
    _collect(ctx, [{"d": "x"}, {"d": "y"}], postfix=lambda item: item["d"])
    assert captured_bars[0].postfixes == ["x", "y"]
