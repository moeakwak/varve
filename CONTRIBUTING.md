# Contributing

Varve is still alpha-stage. Keep changes small, tested, and easy to review.

## Setup

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run pyright
```

`pyright` is currently a pre-flight signal, not a blocking quality bar.

## Public API

The public import surface is intentionally small:

```python
from varve import Axis, Ctx, JSON, Dependencies, Pipeline, StageSpec, batch_stage, matrix, stage
```

Do not re-export internal store, keying, runner, or dashboard types from `varve.__all__`. Treat public API and `.varve/` store schema changes as breaking.

## Style

- User-facing docs, examples, comments, and messages are English.
- Follow the package boundaries in `docs/ARCHITECTURE.md`.
- Do not add Click, Typer, import-linter, or release tooling beyond what is already documented.

## Commits

- Use Conventional Commits: `<type>(<scope>)<!>: <subject>`.
- Append `!` only when the final change breaks compatibility with the most recent released version. Judge compatibility against that release, never against an earlier commit in the same unreleased series.
- Adding, revising, or removing functionality that has not appeared in a release is not a breaking change. If a later commit eliminates a break introduced by an earlier unreleased commit, rewrite or squash the series so the obsolete `!` and `BREAKING CHANGE` notice are removed from the earlier commit as well.
- Use `!` for public API or store schema breaks that remain relative to the most recent release.
- Except for truly trivial changes such as an isolated typo fix, include a Markdown-formatted body that explains why the change is needed and summarizes its meaningful behavior or architecture changes. Prefer a short bullet list when the commit contains multiple points.

```text
feat(scope): concise subject

- Explain the motivation or user-visible outcome.
- Summarize the important implementation or compatibility details.
```

## Releases

`CHANGELOG.md`, the version in `pyproject.toml`, and `.release-please-manifest.json` are owned by release-please. Do not hand-edit them on `main` or feature branches; such edits are overwritten on the next release-please run. The only legitimate hand-edit is tuning that release PR before it finalizes the release.
