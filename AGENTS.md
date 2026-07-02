# AGENTS.md

## Project Boundary

`varve` is an independent Python infrastructure submodule for materialized-cache orchestration. It is depended on by experiments and should be maintained at a publishable quality bar, even when it is developed inside this workspace.

Keep the public surface small and stable:

- Public API: `Ctx`, `Experiment`, `JSON`, `KeySpec`, `StageSpec`, `batch_stage`, `stage`.
- Internal implementation: `src/varve/{keying,store,engine,cli}` and persisted store schemas.
- Internal workspace notes, migration plans, and exploratory design records do not belong in this submodule.

Documentation, comments, examples, and user-facing messages in this submodule should be in English.

`varve` is still early and currently has only the workspace studies as real users. Prefer clean, compact APIs over compatibility shims when an old surface is awkward. Do not keep deprecated aliases or dual names just to avoid updating current call sites; update the call sites and the docs in the same change.

## Dependency Direction

Keep imports moving in one direction:

- `keying`, `store`, and `engine.state` are low-level packages. They must not depend on each other, `engine.runner`, public-surface modules, or `cli`. Prefer dependencies on leaf top-level modules such as `models`, `log`, and `keyspec`.
- `engine.runner` may depend on `keying`, `store`, `engine.state`, and the public-facing top-level modules. It must not depend on `cli`.
- `cli` is the top layer. It may depend on `engine`, `store`, and public-facing top-level modules.
- Public-facing top-level modules such as `experiment`, `decorators`, and `context` may depend on internal packages when that is needed to keep the user API small.

The only controlled reverse edge is inside `Experiment.cli()`:

```python
from varve.cli.app import main
```

That import must stay inside the method body. Do not add other reverse imports from public surface modules into `cli`.

Do not add import-linter or another dependency-direction gate unless the project explicitly chooses to do so. For now, this file and review discipline are the enforcement mechanism.

## Store Naming

The core storage class is `Store` in `varve.store.store`.

`Store` is a latest-wins snapshot store under the experiment output root. It owns:

- current success records in `.varve/stages/*.json`;
- attempt markers in `.varve/attempts/*.json`;
- partial batch scratch in `.varve/partial/<stage>/<run_key>/`.

It is not an append-only history. New code should use `Store`, `store`, and corrupt store language consistently.

`CorruptStore` is the associated exception for malformed store files. It belongs to the internal store surface and is not exported from `varve.__all__`.

The experiment output root is a varve-owned runtime value. Experiment Config models must not declare `out` or `output_root` fields for it. Experiments provide a canonical root by overriding `Experiment.default_output_root(config)`. varve appends the selected branch to that base: `base/<branch>` for persistent branches and `base/.tmp/<branch>` for temporary branches. Stage bodies and helper functions must write through `ctx.out`.

## CLI Responsibilities

The CLI has two layers:

- `argparse` front-end: parses `argv`, subcommands, target selection, built-in command flags, and generated Args flags.
- `pydantic-settings` back-end: merges branch values, environment variables, `.env`, and defaults, then validates the experiment `Config`.

The handoff between the layers is explicit data: argmap output builds `Args`, and the selected branch mapping builds `Config`. The settings layer must not parse `argv`.

Do not introduce typer or click. The current CLI intentionally uses strict `argparse` behavior: unknown options and missing option values fail instead of being ignored. `--out`, `--branch`, and `--override` are built-in command options, not generated model flags. `--override` belongs to `run` only.

Only `run`, `status`, and `clean` require `Config` and `Args`. `plan` and `list` must keep working even when a model contains fields argmap cannot expose.

## Args and Config Sources

Config priority is:

```text
branch or override value > env > dotenv (.env) > field default
```

`varve.yaml` is discovered next to the experiment module by default; missing files are allowed for `main`, which then uses Config defaults. `--branch` is the only branch selector. `--override` deep-merges JSON over `main` and creates or reuses a temporary branch under `.tmp/`; `status` and `clean` locate temporary branches with `--branch NAME` and never accept `--override`. CLI flags generated from `Args` do not enter the content key.

Nested environment variables use `__` as the delimiter. The `.env` file is read from the current working directory through pydantic-settings.

Nested fields deep-merge at field level across sources. The current `model_config` does not set `nested_model_default_partial_update`; the merge behavior comes from pydantic-settings source deep merge, not from partial mutation of a default nested model instance.

Config sources are for semantic configuration only. Output-root selection is resolved separately from `--out` or `Experiment.default_output_root(config)`, then varve appends `branch` / `is_temporary`.

## Clean Safety

`clean` has two different safety paths:

- Full clean (`target is None`) removes the output root after manifest validation, `_validate_destructive`, and confirmation. `allowed_roots` only applies here.
- Per-stage clean validates the manifest anchor, expands the target's downstream closure, reads success records, and deletes only recorded output paths inside the output root.

The dangerous-root blacklist is part of the destructive-clean boundary. Keep rejecting empty paths, `/`, the home directory, and the current working directory for full clean. Experiments declare business-allowed full-clean roots by overriding `Experiment.clean_roots(config)`.

Per-stage clean must stay independent of `allowed_roots`; its boundary is the manifest anchor plus success-record path closure.

## Public API Contract

Keep this import stable:

```python
from varve import Ctx, Experiment, JSON, KeySpec, StageSpec, batch_stage, stage
```

`Store` is not part of the public API. It may be imported by internal modules and tests through `varve.store.store`, but it should not be re-exported from `varve`.

When changing signatures used by experiment authors, update README and architecture docs in the same change.

Stage source dependencies are automatic by default. `@stage` and `@batch_stage` should keep `auto_uses=True` unless an experiment deliberately wants to opt out. Use `additional_uses=(...)` only for dynamic callables that the automatic project-callable scan cannot see; do not reintroduce a full manual `uses` list API.

## Commit Messages

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>)<!>: <subject>`.

### Types

- `feat` — a user-facing feature or behavior change.
- `fix` — a bug fix.
- `perf` — a performance improvement.
- `docs`, `chore`, `ci`, `test`, `style`, `refactor`, `build`, `revert` — supporting changes.

Append `!` after the type/scope to mark a breaking change, e.g. `feat(cli)!: ...`. Because `varve` keeps a small public surface, treat any change to the public API or persisted store schema as breaking.

### Scopes

Prefer an existing scope naming the affected area: `keying`, `store`, `engine`, `cli`, `experiment`, `models`. Omit the scope only when a change genuinely spans multiple areas.

### Subject

Keep the subject short, imperative, and lower-case after the type/scope prefix. Use backticks around code identifiers — commands, types, methods — in the subject:

```bash
git commit -m 'feat(cli): support `Literal`/`Enum` choices'
git commit -m 'fix(store): guard batch `produces` against missing output root'
git commit -m 'refactor(context)!: require `store` in `Ctx`'
```

### Body

For large commits with several important changes, add a body after a blank line, formatted as a Markdown unordered list with one bullet per important change.
