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
- Except for truly trivial changes such as an isolated typo fix, explain the problem or motivation in a short opening paragraph, then describe the resulting behavior in unordered bullets grouped by topic. Use concrete declarative sentences with enough detail to explain important changes and their boundaries. Include meaningful performance or metric changes; omit routine test counts and pass/fail summaries.

```text
feat(cli): skip manual branches in default runs

Supplementary branches should run only when requested.

- Branches marked `manual: true` are skipped before pipeline import and state evaluation.
- `--all` and explicit targets still include manual branches.
```

## Releases

release-please manages `CHANGELOG.md`. It derives release notes from Conventional Commits and updates the changelog through the automated release PR created or refreshed after pushes to `main`; contributors must not add an `Unreleased` section or hand-write release entries.

The release PR also owns the version in `pyproject.toml` and `.release-please-manifest.json`. Merging that PR creates the GitHub release, builds the distributions, and publishes them to PyPI through `.github/workflows/release.yml`.
