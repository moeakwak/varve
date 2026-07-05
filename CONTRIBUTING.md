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
from varve import Ctx, JSON, KeySpec, Pipeline, StageSpec, batch_stage, stage
```

Do not re-export internal store, keying, runner, or dashboard types from `varve.__all__`. Treat public API and `.varve/` store schema changes as breaking.

## Style

- User-facing docs, examples, comments, and messages are English.
- Follow the package boundaries in `ARCHITECTURE.md`.
- Do not add Click, Typer, import-linter, or release tooling beyond what is already documented.
- Use Conventional Commits: `<type>(<scope>)<!>: <subject>`.
- Append `!` for public API or store schema breaks.

## Releases

`CHANGELOG.md`, the version in `pyproject.toml`, and `.release-please-manifest.json` are owned by release-please. Do not hand-edit them on `main` or feature branches; such edits are overwritten on the next release-please run. The only legitimate hand-edit is tuning that release PR before it finalizes the release.
