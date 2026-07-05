# varve Architecture

`varve` is a small single-machine runner for Python-defined experiment pipelines with a materialized, content-addressed cache. Users define `Pipeline` classes and stages; varve owns branch-aware output roots, cache keys, store records, generated CLI commands, and clean/status behavior.

## Package Layout

```text
src/varve/
├── __init__.py          # public re-export surface
├── pipeline.py          # Pipeline base class, stage collection, CLI hook
├── decorators.py        # @stage, @batch_stage, StageSpec
├── context.py           # Ctx passed to stage methods
├── branch.py            # varve.yaml and override branch helpers
├── branch_config.py     # Config construction and output-root selection
├── keyspec.py           # JSON and KeySpec declarations
├── models.py            # persisted pydantic store models
├── keying/              # source/file/config/upstream key components
├── store/               # file lock and latest-wins Store
├── engine/              # cache-state decisions and runner
├── cli/                 # generated Pipeline.cli() commands
└── dashboard/           # varve ls/show/refresh over existing stores
```

## Public Surface

Only these names are exported from `varve.__all__`:

```python
from varve import Ctx, Pipeline, JSON, KeySpec, StageSpec, batch_stage, stage
```

Everything else is internal unless this document or `README.md` says otherwise. `Store`, persisted models, keying helpers, runner helpers, and dashboard models may be used by internal code and tests through full module paths, but they are not public API.

## Dependency Direction

- Low-level packages: `keying`, `store`, and `engine.state`. They depend only on leaf modules such as `models`, `log`, and `keyspec`.
- Middle layer: `branch_config` and `engine.runner`.
- Top layers: `cli` and `dashboard`.
- Public-facing modules such as `pipeline`, `decorators`, and `context` may use internals to keep the user API small.
- The only intentional reverse edge is the lazy import inside `Pipeline.cli()`: `from varve.cli.app import main`.

There is no import-direction tool. Keep the graph boring by review.

## Cache And Store

The store lives under `<output_root>/.varve/` and is latest-wins, not append-only:

```text
.varve/
├── manifest.json
├── lock
├── stages/<stage>.json
├── attempts/<stage>.json
└── partial/<stage>/<run_key>/
```

`content_key` includes stage source, discovered project callables, full `Config`, declared `KeySpec.files`, declared `KeySpec.values`, and upstream content keys. `run_key` adds batch partition values.

Recorded artifact paths are output-root-relative. Stage bodies should write through `ctx.out`.

Stage bodies read upstream outputs through `ctx.input(stage)` for exactly one path or `ctx.inputs(stage)` for a list of paths. Both helpers require `stage` to be declared in the current stage's `needs=` list, because only declared upstreams are folded into the content key.

Known cache states are `dirty`, `hit`, `artifact-missing`, `stale`, `no-cache`, `resume`, and `unrecoverable`.

## Output Roots And Branches

`Pipeline.default_output_root(config)` returns the base output root. The CLI can override that with `--out`. Varve then appends the selected branch:

```text
base/<branch>        # persistent branches
base/.tmp/<branch>   # temporary override branches
```

`varve.yaml` is discovered next to the pipeline module. Missing `varve.yaml` is allowed for `main`.

## CLI And Config

`Pipeline.cli(argv)` delegates to `varve.cli.app.main` and provides `run`, `status`, `plan`, `list`, and `clean`.

`argparse` parses commands and generated `Args` flags. `pydantic-settings` builds semantic `Config` values from branch/override values, environment variables, `.env`, and model defaults.

Config priority:

```text
branch or override value > env > dotenv (.env) > field default
```

Do not add Click or Typer. The strict `argparse` behavior is intentional.

## Clean Safety

All clean operations require a valid `.varve/manifest.json` anchor under the selected output root.

Full clean removes the whole output root after rejecting dangerous roots such as empty paths, `/`, the home directory, and the current working directory. Pipelines can narrow allowed full-clean roots by overriding `Pipeline.clean_roots(config)`.

Per-stage clean only deletes recorded artifacts and store records for the selected downstream closure. It does not use `allowed_roots`; its boundary is the manifest anchor plus recorded artifact paths.

## Dashboard

The top-level `varve` console script reads existing stores.

Temporary branches under `out/.tmp` are filtered by default and included only with `--include-temp`. Discovery is zero-import; state rendering imports stored manifest modules only after discovery. `refresh` runs branches whose evaluated status is executable: `artifact-missing`, `dirty`, `no-cache`, `resume`, or `stale`.

## Known Limitations

- Source fingerprints use `ast.dump`; a CPython minor-version upgrade may invalidate caches.
- Windows is not supported yet because locking uses `fcntl`.
