# varve Architecture

## Overview

`varve` is a materialized, content-addressed cache for serial experiment orchestration.

Experiments own output formats and default output-base policy. `varve` owns branch-aware output root resolution, `ctx.out`, and the store under the output root. The store records which stage successfully produced which durable artifacts for a content key. The runner uses stage declarations, source fingerprints, the full Config, file fingerprints, values, and upstream content keys to decide whether a stage is a cache hit, stale, resumable, dirty, or missing cached state.

The package is intentionally small. The public API is for experiment authors; the `keying`, `store`, `engine`, and `cli` packages are internal implementation surfaces.

## Package Layout

```text
src/varve/
├── __init__.py          # Public re-export surface.
├── context.py           # Runtime Ctx passed to stage methods.
├── decorators.py        # @stage, @batch_stage, and StageSpec metadata.
├── branch.py            # Branch name validation, varve.yaml loading, and override merging.
├── branch_config.py     # Config construction, branch resolution, and output-base selection.
├── experiment.py        # Experiment base class, branch-aware output roots, stage collection, CLI hook.
├── keyspec.py           # JSON type and KeySpec declarations.
├── log.py               # Logger and CLI logging helpers.
├── models.py            # Pydantic models persisted in the store.
├── keying/
│   ├── astkey.py        # Source AST fingerprinting for stage/helper callables.
│   ├── fingerprint.py   # Canonical JSON hashing and file fingerprints.
│   └── keys.py          # Key component assembly, content_key, and run_key.
├── store/
│   ├── lock.py          # Single-writer output-root lock.
│   └── store.py         # Latest-wins snapshot Store and CorruptStore.
├── engine/
│   ├── state.py         # Pure cache-state decision functions.
│   └── runner.py        # Stage selection, key computation, execution, and store writes.
├── dashboard/
│   ├── discovery.py     # Zero-import store discovery under a scan root.
│   ├── state.py         # Read-only store snapshot loading and DAG reconstruction.
│   ├── render.py        # Rich overview and detail rendering.
│   └── cli.py           # Top-level varve ls/show entry point.
└── cli/
    ├── app.py           # argparse CLI and pydantic-settings Config construction.
    ├── argmap.py        # Args model to CLI flag mapping.
    └── clean.py         # Destructive clean operations and safety checks.
```

Internal imports use full module paths such as `varve.store.store.Store` and `varve.keying.keys.content_key`.

## Dependency Direction

```mermaid
flowchart TD
  public["public surface: __init__, experiment, decorators, context"]
  leaves["leaf top-level modules: models, log, keyspec"]
  keying["src/varve/keying"]
  store["src/varve/store"]
  state["src/varve/engine/state"]
  runner["src/varve/engine/runner"]
  branch_config["src/varve/branch_config.py"]
  dashboard["src/varve/dashboard"]
  cli["src/varve/cli"]

  keying --> leaves
  store --> leaves
  state --> leaves
  branch_config --> store
  branch_config --> public
  runner --> keying
  runner --> store
  runner --> state
  runner --> public
  dashboard --> store
  dashboard --> leaves
  dashboard --> runner
  dashboard --> branch_config
  cli --> runner
  cli --> store
  cli --> branch_config
  cli --> public
  public --> store
  public --> cli_exception["Experiment.cli() lazy import only"]
```

Rules:

- `keying`, `store`, and `engine.state` stay low level: they do not import each other, `engine.runner`, public-surface modules, or `cli`.
- `keying` only depends on leaf top-level modules such as `models`, `log`, and `keyspec`.
- `branch_config` constructs Config objects and resolves branches above the store layer.
- `engine.runner` composes the lower layers and owns orchestration.
- `dashboard` is a top-level read/refresh package. It may read `store`, resolve branches, evaluate engine state, and run refreshes, but no lower layer imports `dashboard`.
- `cli` is the top layer and may call runner, clean, store, and public-facing modules.
- `Experiment.cli()` has the only controlled reverse edge. It lazily imports `varve.cli.app.main` inside the method body.

There is no automated import-direction checker. Dependency direction is enforced by this document and code review.

## Public Import Surface vs Internal Surface

The public import surface is exactly the seven names exported from `varve.__all__`:

```python
Ctx
Experiment
JSON
KeySpec
StageSpec
batch_stage
stage
```

Experiment authors should be able to write:

```python
from varve import Ctx, Experiment, JSON, KeySpec, StageSpec, batch_stage, stage
```

Internal surfaces include `Store`, `CorruptStore`, `run_key`, `content_key`, `Manifest`, `SuccessRecord`, `PartialMeta`, `BatchRecord`, `AttemptMarker`, `StageOutcome`, and CLI helper functions. They may be imported by internal modules and tests through their full paths, but they are not exported from `varve`.

The experiment author contract also includes `Experiment` methods used to declare
runtime policy and entry points: `cli()`, `default_output_root()`,
`clean_roots()`, `varve_config_path()`, `output_root()`, `stages()`, and
`topo_order()`.

## Cache Semantics Overview

The store lives at:

```text
<output_root>/.varve/
├── manifest.json
├── .gitignore
├── lock                 # OS file-lock marker for the active writer.
├── stages/<stage>.json
├── attempts/<stage>.json
└── partial/<stage>/<run_key>/
    ├── meta.json
    └── batches/<index>.json
```

`Store` is a latest-wins snapshot store:

- `stages/<stage>.json` stores the current successful record for a stage.
- `attempts/<stage>.json` records an in-progress or interrupted attempt marker.
- `partial/<stage>/<run_key>/` stores resumable batch scratch for a specific content key and partition.

There is no append-only history.

Recorded artifact paths are output-root-relative. Static `@stage(produces=...)` declarations are resolved against `ctx.out`. Batch stages may yield absolute paths under `ctx.out` or paths already relative to `ctx.out`; relative batch paths are not current-working-directory-relative.

The output root is not part of the experiment `Config`. `run`, `status`, and `clean` resolve an output base from explicit `--out`/`cli_out` when present, otherwise from `Experiment.default_output_root(config)`. varve then appends the selected branch: `base/<branch>` for persistent branches and `base/.tmp/<branch>` for temporary override branches. The resolved value is used for `Store(out)` and every stage `Ctx(out=out, args=args, store=store)`.

`Ctx.resume(iterable, progress=True, desc=..., unit=..., total=..., postfix=...)` keeps resume semantics unchanged while showing one `tqdm` progress bar for the whole resumed iterable. The bar is enabled by default and labeled with the stage name; skipped indexes seed its initial value, so resumed runs do not restart the displayed count from zero. Pass `progress=False` to disable it.

### Keys

`content_key` hashes a canonical JSON view of:

- normalized source hashes for the stage function, automatically discovered project callables reached from the stage body, and any `additional_uses` helpers;
- the full experiment Config;
- sha256 digests for declared files from `KeySpec.files`;
- declared JSON values from `KeySpec.values`;
- upstream stage content keys.

File fingerprint metadata stores path, size, mtime, and sha256. The content key only folds in file digests. On a cache hit, runner may refresh stored size/mtime metadata when digests are unchanged.

Config models must not contain `Path` fields or Path values. Input locations belong in `Args`, and input content belongs in the content key through `KeySpec.files`.

`auto_uses` is enabled by default on `stage` and `batch_stage`. It recursively follows direct global function/class references within the stage's top-level project package. `additional_uses` exists for dynamic dispatch, registry lookups, object-held callables, or other source dependencies that the bytecode-name scan cannot see. The scan intentionally ignores decorator arguments, `produces`, `KeySpec.files`, and `KeySpec.values`.

`run_key` hashes the `content_key` together with batch `partition_values`. It is used to locate partial batch scratch for resume.

### Status Values

`engine.state.Status` declares:

```text
dirty
hit
artifact-missing
stale
no-cache
resume
unrecoverable
```

Malformed store files raise `CorruptStore` from `varve.store.store`.

### Decision Inputs

Cache decisions are pure functions in `engine.state` and are driven by:

- the current content key and key components;
- the latest success record, if any;
- the attempt marker, if any;
- artifact existence under the output root;
- batch partial records and `run_key`;
- batch partition values.

Runner adds orchestration-specific inputs around those decisions: selected stages, upstream success records, `force`, output locking, and actual stage execution. The engine-level `evaluate_state(...)` entry point computes the same decisions without executing stages or writing store records.

### Decision Boundaries

- `hit`: success record content key matches and recorded artifacts exist.
- `dirty`: an attempt marker exists, so cached success is not trusted. When a batch stage has no success record but does have partial scratch, runner currently does not pass the attempt marker into the batch decision, so the stage can resume or rerun from `no-cache`.
- `artifact-missing`: success key matches but some recorded artifacts are missing. Batch stages may skip still-existing indexes through `Ctx.resume`.
- `stale`: a success record exists but its content key differs from the current key. The reason is computed from source, config, files, values, or upstream differences.
- `no-cache`: there is no success record and no matching partial scratch.
- `resume`: a batch stage has matching partial scratch and no success record.
- `unrecoverable`: a batch success key matches but artifacts are missing after partition values changed, so runner cannot safely map existing artifacts to current partitions.

`force=True` overrides the decision to rerun as `stale` when a previous success exists or `no-cache` when it does not.

## CLI Architecture

`Experiment.cli(argv)` delegates to `varve.cli.app.main`.

The CLI uses `argparse` for command parsing:

- `run [--branch name] [--override json] [--upto stage | --downstream stage] [--force] [--out path]`
- `status [--branch name] [--upto stage | --downstream stage] [--out path]`
- `clean [--branch name] [--downstream stage] [--out path] [--yes]`
- `plan [--upto stage | --downstream stage]`
- `list`

`run`, `status`, and `clean` require an experiment `Config` and `Args`. `plan` and `list` do not construct either model; they can still run when the models contain fields not supported by argmap.

`argmap` registers supported Args fields as CLI flags:

- scalar fields become `--field-name`;
- nested `BaseModel` fields become dotted flags such as `--inner.name`;
- bool fields support positive and negative flags, such as `--enabled` and `--no-enabled`;
- list fields accept JSON through `json.loads`;
- unsupported dict, mapping, tuple, set, and union shapes fail fast for config commands.

Command flags and Args fields are kept separate by using private argparse destinations for generated flags. This prevents command arguments such as `--force` or `--out` from polluting same-named Args fields.

`--out`, `--branch`, and `--override` are built-in command options. `--override` is accepted only by `run`; `status` and `clean` locate temporary branches with `--branch NAME`. Built-in options are not generated from experiment models, and experiment Config models should not declare output-root or branch-selection fields.

Unknown options are strict. Before dynamic Args flags are registered, config commands pre-scan the selected command's arguments so unknown options or missing option values fail as parser errors instead of triggering config registration for the wrong command.

## Dashboard

The top-level `varve` console script provides a dashboard over existing stores:

- `varve ls [--root DIR]` discovers `<experiment>/out/<branch>/.varve/manifest.json` files under the scan root and prints an overview table.
- `varve show <experiment_id> [--root DIR] [--branch NAME]` prints one store's stage details and dependency edges.
- `varve refresh [--root DIR] [--prefix MODULE_PREFIX]` runs executable discovered experiment branches. With `--prefix`, it only considers manifest modules starting with that prefix.

Discovery is intentionally zero-import. Read-only dashboard commands stay non-mutating after discovery: they import the manifest module, resolve the selected branch, and use the same engine state evaluator as `status`. Dashboard experiment status is the aggregate engine `Status`, or `error` when manifest parsing, import, branch/config resolution, or evaluation fails.

`refresh` uses the same evaluated state and runs only branches with executable statuses: `artifact-missing`, `dirty`, `no-cache`, `resume`, or `stale`. Stores outside the branch output layout are skipped.

Stage rows follow `Experiment.topo_order()` and use each stage's engine `STATUS` and `REASON`. Single-stage artifacts are read from `SuccessRecord.produces`; batch artifacts are read from `SuccessRecord.outputs`.

The detail view prints dependency edges from declared stage `needs`, only when both endpoints are present in the evaluated stage list. Declared stages can appear before they have a success record; artifacts and timing stay blank until a success exists.

## Args and Config Sources

`cli.app._settings_type()` builds a temporary `BaseSettings` subclass around the experiment `Config` model.

Config sources only construct semantic configuration. Output-root selection is resolved separately by the runner and clean paths. Args are built directly from generated CLI flags and model defaults.

`varve.yaml` is discovered next to the experiment module by default. The selected branch mapping is passed as settings init kwargs. `--override JSON` deep-merges over `main` and derives a temporary branch name from the canonical JSON of the fully validated Config, unless a non-main temporary `--branch NAME` is provided.

Practical source priority is:

```text
branch or override value > env > dotenv (.env) > field default
```

Implementation details:

- Branch values are passed as settings init kwargs.
- Environment variables are read by pydantic-settings.
- Nested environment names use `env_nested_delimiter="__"`.
- `.env` is enabled with `env_file=".env"` and is read from the current working directory.
- The resulting settings model is dumped and validated back into the experiment `Config` type.

Nested fields deep-merge across sources. The current `model_config` does not enable `nested_model_default_partial_update`; nested merge behavior relies on pydantic-settings source deep merge, not on partial mutation of nested default model instances.

## Clean Security Model

All clean operations acquire the output-root lock and validate the manifest anchor:

- `.varve/manifest.json` must exist.
- The manifest experiment name must match the current experiment class name.

Full clean (`target is None`) then:

- calls `_validate_destructive(root, allowed_roots)`;
- rejects empty roots, `/`, the home directory, and the current working directory;
- applies `allowed_roots` if provided;
- requires `_confirm` unless `yes=True`;
- removes the whole output root.

Experiments declare business-allowed full-clean roots by overriding `Experiment.clean_roots(config)`. The CLI passes that value into `clean(..., allowed_roots=...)`. The default is `None`, which leaves only the dangerous-root blacklist and manifest anchor guard.

Per-stage clean (`target is not None`) then:

- checks that the target stage exists;
- expands the downstream closure from the target;
- reads success records for that closure;
- validates recorded output paths stay under the output root;
- requires `_confirm` unless `yes=True`;
- deletes recorded artifacts, stage success records, attempt markers, and partial scratch for the selected closure.

`allowed_roots` does not apply to per-stage clean. Its safety boundary is manifest anchor plus success-record path closure.

## Known Limitations

- Source AST fingerprints use `ast.dump`. The normalized dump can change across CPython versions, so a Python upgrade may invalidate source hashes and force rebuilds.

## Non-Goals

- No import-linter or automated dependency-direction checker.
- No studies dependency, migration layer, or workspace-specific consumer behavior inside this package.
- No broad external backward-compatibility guarantee beyond the documented public API while there are no external consumers.
