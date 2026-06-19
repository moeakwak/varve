# AGENTS.md

## Project Boundary

`varve` is an independent Python infrastructure submodule for materialized-cache
orchestration. It is depended on by experiments and should be maintained at a
publishable quality bar, even when it is developed inside this workspace.

Keep the public surface small and stable:

- Public API: `Ctx`, `Experiment`, `JSON`, `KeySpec`, `StageSpec`, `batch_stage`, `stage`.
- Internal implementation: `src/varve/{keying,store,engine,cli}` and persisted store schemas.
- Internal workspace notes, migration plans, and exploratory design records do not belong in
  this submodule.

Documentation, comments, examples, and user-facing messages in this submodule should be in
English.

## Dependency Direction

Keep imports moving in one direction:

- `keying`, `store`, and `engine.state` are low-level packages. They must not depend on each
  other, `engine.runner`, public-surface modules, or `cli`. Prefer dependencies on leaf
  top-level modules such as `models`, `log`, and `keyspec`.
- `engine.runner` may depend on `keying`, `store`, `engine.state`, and the public-facing
  top-level modules. It must not depend on `cli`.
- `cli` is the top layer. It may depend on `engine`, `store`, and public-facing top-level
  modules.
- Public-facing top-level modules such as `experiment`, `decorators`, and `context` may depend
  on internal packages when that is needed to keep the user API small.

The only controlled reverse edge is inside `Experiment.cli()`:

```python
from varve.cli.app import main
```

That import must stay inside the method body. Do not add other reverse imports from public
surface modules into `cli`.

Do not add import-linter or another dependency-direction gate unless the project explicitly
chooses to do so. For now, this file and review discipline are the enforcement mechanism.

## Store Naming

The core storage class is `Store` in `varve.store.store`.

`Store` is a latest-wins snapshot store under the experiment output root. It owns:

- current success records in `.varve/stages/*.json`;
- attempt markers in `.varve/attempts/*.json`;
- partial batch scratch in `.varve/partial/<stage>/<run_key>/`.

It is not an append-only history. New code should use `Store`, `store`, and `corrupt-store`
language consistently.

`CorruptStore` is the associated exception for malformed store files. It belongs to the
internal store surface and is not exported from `varve.__all__`.

`Ctx(..., ledger=...)` is only a legacy keyword alias for `Ctx(..., store=...)`. Runner code and
new call sites must pass `store=`.

## CLI Responsibilities

The CLI has two layers:

- `argparse` front-end: parses `argv`, subcommands, target selection, command flags, and config
  flags.
- `pydantic-settings` back-end: merges environment variables, `.env`, YAML, and defaults, then
  validates the experiment `Config`.

The only handoff between the layers is argmap output: nested init kwargs collected from explicit
CLI config flags. The settings layer must not parse `argv`.

Do not introduce typer or click. The current CLI intentionally uses strict `argparse` behavior:
unknown options and missing option values fail instead of being ignored.

Only `run`, `status`, and `clean` require a `Config`. `plan` and `list` must keep working even
when a `Config` contains fields argmap cannot expose.

## Config Sources

Config priority is:

```text
CLI flag > env > dotenv (.env) > yaml (--config) > field default
```

Nested environment variables use `__` as the delimiter. The `.env` file is read from the current
working directory through pydantic-settings.

Nested fields deep-merge at field level across sources. The current `model_config` does not set
`nested_model_default_partial_update`; the merge behavior comes from pydantic-settings source
deep merge, not from partial mutation of a default nested model instance.

## Clean Safety

`clean` has two different safety paths:

- Full clean (`target is None`) removes the output root after manifest validation,
  `_validate_destructive`, and confirmation. `allowed_roots` only applies here.
- Per-stage clean validates the manifest anchor, expands the target's downstream closure, reads
  success records, and deletes only recorded output paths inside the output root.

The dangerous-root blacklist is part of the destructive-clean boundary. Keep rejecting empty
paths, `/`, the home directory, and the current working directory for full clean. Experiments
declare business-allowed full-clean roots by overriding `Experiment.clean_roots(config)`.

Per-stage clean must stay independent of `allowed_roots`; its boundary is the manifest anchor
plus success-record path closure.

## Public API Contract

Keep this import stable:

```python
from varve import Ctx, Experiment, JSON, KeySpec, StageSpec, batch_stage, stage
```

`Store` is not part of the public API. It may be imported by internal modules and tests through
`varve.store.store`, but it should not be re-exported from `varve`.

When changing signatures used by experiment authors, update README and architecture docs in the
same change.
