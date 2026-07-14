from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import varve.context
from varve.context import Ctx
from varve.models import (
    ArtifactFingerprint,
    KeyComponents,
    OutputHandle,
    ProducedPath,
    SourceFingerprint,
    SourceObservation,
    SuccessRecord,
)
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


def _key_components() -> KeyComponents:
    return KeyComponents(
        config={},
        inputs={},
        values={},
        upstreams={},
        rerun_source_fingerprint="source",
    )


def _source() -> SourceObservation:
    fingerprint = SourceFingerprint(fingerprint="source", files=[])
    return SourceObservation(rerun=fingerprint, review=fingerprint)


def _artifact(path: str) -> ArtifactFingerprint:
    return ArtifactFingerprint(root=path, kind="file", manifest=[], fingerprint=f"hash:{path}")


def _single_record(stage: str, paths: list[str]) -> SuccessRecord:
    return SuccessRecord(
        pipeline="Demo",
        stage=stage,
        kind="single",
        input_key=f"{stage}-key",
        key_components=_key_components(),
        executed_source=_source(),
        artifact_fingerprint="artifacts",
        produces=[ProducedPath(path=path, kind="file", artifact=_artifact(path)) for path in paths],
        committed_at="now",
    )


def _batch_record(stage: str, paths: list[str]) -> SuccessRecord:
    return SuccessRecord(
        pipeline="Demo",
        stage=stage,
        kind="batch",
        input_key=f"{stage}-key",
        key_components=_key_components(),
        executed_source=_source(),
        artifact_fingerprint="artifacts",
        outputs=[
            OutputHandle(index=index, path=path, artifact=_artifact(path))
            for index, path in enumerate(paths)
        ],
        committed_at="now",
    )


def test_input_returns_exactly_one_upstream_output(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write_success(_single_record("sample", ["sample.txt"]))
    ctx = Ctx(config={}, out=tmp_path, store=store, declared_needs=frozenset({"sample"}))

    assert ctx.input("sample") == tmp_path / "sample.txt"


def test_input_rejects_multiple_outputs_with_inputs_hint(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write_success(_batch_record("parts", ["a.txt", "b.txt"]))
    ctx = Ctx(config={}, out=tmp_path, store=store, declared_needs=frozenset({"parts"}))

    with pytest.raises(ValueError, match="Use ctx.inputs"):
        ctx.input("parts")


def test_inputs_always_returns_a_list(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write_success(_single_record("sample", ["sample.txt"]))
    store.write_success(_batch_record("parts", ["a.txt", "b.txt"]))
    ctx = Ctx(
        config={},
        out=tmp_path,
        store=store,
        declared_needs=frozenset({"sample", "parts"}),
    )

    assert ctx.inputs("sample") == [tmp_path / "sample.txt"]
    assert ctx.inputs("parts") == [tmp_path / "a.txt", tmp_path / "b.txt"]


def test_input_requires_declared_need(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write_success(_single_record("sample", ["sample.txt"]))
    ctx = Ctx(
        config={}, out=tmp_path, store=store, stage_name="summary", declared_needs=frozenset()
    )

    with pytest.raises(ValueError, match="declare it in needs"):
        ctx.input("sample")


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


def test_resume_progress_matrix_default_uses_canonical_values(
    tmp_path: Path,
    captured_bars: list[FakeBar],
) -> None:
    ctx = _ctx(
        tmp_path,
        stage_name="score@bench=ocrbench-v2-formula,model=qwen3-vl-8b-instruct",
        stage_display=("ocrbench-v2-formula", "qwen3-vl-8b-instruct"),
    )

    _collect(ctx, ["item"])

    assert captured_bars[0].desc == "ocrbench-v2-formula / qwen3-vl-8b-instruct"


def test_resume_progress_explicit_desc_is_unchanged_for_matrix_cell(
    tmp_path: Path,
    captured_bars: list[FakeBar],
) -> None:
    ctx = _ctx(
        tmp_path,
        stage_name="score@bench=a,model=b",
        stage_display=("a", "b"),
    )

    _collect(ctx, ["item"], desc="custom axis-aware description")

    assert captured_bars[0].desc == "custom axis-aware description"


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
