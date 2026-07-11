# AGENTS.md

## Read First

- User-facing positioning and examples: `README.md`.
- Complete author-facing behavior: `docs/GUIDE.md`.
- Package layout, dependency direction, cache/store, CLI/config, clean, and dashboard boundaries: `docs/ARCHITECTURE.md`.
- Contributor setup, public API policy, style, and commit requirements: `CONTRIBUTING.md`. Read it before creating any varve commit.

## Agent Rules

`varve` is an independent Python infrastructure submodule. Documentation, comments, examples, and user-facing messages in this submodule must be English.

Keep the public import surface small:

```python
from varve import Axis, Ctx, Pipeline, JSON, KeySpec, StageSpec, batch_stage, matrix, stage
```

Prefer compact pre-1.0 APIs over compatibility shims. If an old public name is awkward and there are no external users, update current callers and docs instead of keeping deprecated aliases.

When changing author-facing behavior, update `README.md`, `docs/GUIDE.md`, and `docs/ARCHITECTURE.md` as appropriate in the same change. Release/version/changelog design lives in the workspace spec, not in this submodule.
