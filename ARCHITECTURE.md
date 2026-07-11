# varve Architecture

`varve` is a small single-machine runner for Python-defined pipelines with a materialized, content-addressed cache. Users define `Pipeline` classes and stages; varve owns branch-aware output roots, cache keys, store records, generated CLI commands, and clean/status behavior.

## Package Layout

```text
src/varve/
├── __init__.py          # public re-export surface
├── pipeline.py          # Pipeline base class, stage collection, CLI hook
├── decorators.py        # @stage, @batch_stage, StageSpec
├── context.py           # Ctx passed to stage methods
├── status.py            # read-only structured pipeline and stage status
├── branch.py            # varve.yaml and override branch helpers
├── branch_config.py     # Config construction and output-root selection
├── keyspec.py           # JSON and KeySpec declarations
├── models.py            # persisted pydantic store models
├── style.py             # shared Rich status colors for cli and dashboard
├── keying/              # source discovery and file/config/upstream key components
├── store/               # file lock and latest-wins Store
├── engine/              # cache-state decisions and runner
├── cli/                 # generated Pipeline.cli() commands and Rich status rendering
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
└── partial/<stage>/<content_key>/
```

`schema_version` is a shared envelope version for manifests and successful stage records, so both advance together even when a schema change affects only one record type. Version 4 adds `temporary_axes` to manifests because older strict readers reject that new field. Current readers remain backward-compatible with version 2 and 3 manifests and success records; in an older manifest, a missing `temporary_axes` field defaults to `None`. Varve reads these records in place and does not migrate or rewrite the store merely because its schema version is old.

`content_key` includes stage source, discovered project callables, the `Config` projected onto the fields the stage actually reads, declared `KeySpec.files`, declared `KeySpec.values`, and upstream content keys. Batch partial state is scoped directly by `content_key`.

## Source Dependency Discovery

`keying/dependencies.py` performs bounded, positive source discovery and returns both stable flat source components and a dependency DAG. `keying/keys.py` consumes the flat components for hashing, while `status.py` consumes the same result for explanation; there is no second discovery implementation in the CLI.

The stage function and explicit `uses` roots are strict inputs. Inferred project functions, whole classes, stable values, and narrow module-file fallbacks are best effort: a failure to inspect or hash one inferred branch is logged only at debug level and cannot block status evaluation or execution. Explicit `uses`, `KeySpec`, stage source, and store corruption retain their existing strict behavior.

Discovery follows only directly resolvable globals, closure cells, defaults, nested code objects, simple `module.attr` reads, class-owned methods, and project base classes. It intentionally does not perform type propagation, control-flow analysis, registry inspection, factory return inference, dynamic import tracking, or runtime dispatch analysis. Calls through `self` are runtime dispatch and must be represented with `uses` or `KeySpec` when they affect caching.

`Pipeline.auto_uses_packages` controls inferred recursion. `None` selects the stage function's top-level package, an explicit tuple replaces that scope, and `()` disables inferred package recursion without removing stage source or explicit `uses` roots.

## Decision Probes And Status

`engine.runner._probe_stage` is the shared ready-stage decision unit for execution probes and structured status. `probe_pipeline()` walks the full topology by default so each displayed decision key uses the same whole-pipeline upstream projection. Structured status and dashboard callers retain that complete default. Scoped run validation uses an internal stage filter to probe only direct upstreams outside the execution selection and their recursive ancestor closure, in the original graph topology order. Every stage in that closure must be a hit, and current decision keys propagate through the closure just as they do in a full probe. This prevents `--only`, `--downstream`, and sliced runs from inspecting unrelated source or file key inputs without weakening validation of external upstream state. Probes compute source dependencies before checking missing upstream records, so invalid explicit `uses` remains strict while valid source dependencies remain available when key inputs are unavailable.

`status.py` converts probes into immutable view models and compares current source components with the previous successful record for stale stages. `cli/status.py` renders the folded summary, single-stage key inputs, progressively expanded dependency DAG, and source change markers; qualified names, stored source locators, and known edge reasons are compacted only at this rendering boundary. The command is read-only: it does not execute stages, initialize the store, or persist dependency graphs. A displayed decision key is the current read-only cache decision input, not a promise that the next execution will commit the same final key after recording config access.

## Config Access Projection

A stage's output depends only on the `Config` fields it reads, so folding the whole `Config` into every `content_key` over-invalidates: adding a tool or toggling a flag would rerun stages that never look at that field. Varve instead records which top-level fields each stage reads and keys only on those.

During a run the stage's `ctx.config` is a transparent recording proxy. Plain top-level field reads (`ctx.config.tools`, including reads inside helpers the config is passed to) are captured precisely; any access that cannot be attributed to one field — `model_dump()`, `getattr` of an unknown name, iteration, `__dict__`, pickling — marks the whole `Config` as depended-on (`config_access = None`, the conservative fallback). The recorded set is stored on the success record and, on the next run, the `Config` is projected onto it before hashing, so changing an unread field is a hit.

Soundness rests on the source component: if a stage's code (or a discovered callable) changes to read a new field, its source hash changes and it reruns, re-recording the set. The first run of a stage, and any run after a source change, key on the whole `Config` and then record the precise set for subsequent runs. Keying is two-phase: the hit/stale decision projects onto the previous run's set, and the committed key projects onto this run's actual reads (unioned with the previous set when the source is unchanged, so a resume that skips batches or a data-dependent branch not taken never drops a real dependency).

Config reads must be deterministic for fixed keyed inputs, and must happen on every stage entry rather than only inside resume-skipped per-item work; batch stages conventionally read `Config` at the top to build their job list, which satisfies this. A stage that ships the raw `Config` object into a subprocess and reads fields there is not captured — extract the values in the parent process (which reads them through the proxy) and pass those.

`Config` keeps its whole-value role for provenance and branch identity: `override_branch_name`, the manifest snapshot, and anything a stage writes to run metadata still see the full `Config`. Only keying is projected.

Recorded artifact paths are output-root-relative. Stage bodies should write through `ctx.out`.

Stage bodies read upstream outputs through `ctx.input(stage)` for exactly one path or `ctx.inputs(stage)` for a list of paths. Both helpers require `stage` to be declared in the current stage's `needs=` list, because only declared upstreams are folded into the content key.

Known cache states are `dirty`, `hit`, `artifact-missing`, `stale`, `no-cache`, and `resume`.

Batch resume records completed indexes from `ctx.resume(...)`. This requires deterministic iterable order; callers should sort unstable inputs before passing them to `ctx.resume(...)`. Varve intentionally does not provide order-independent batch resume under the current content-key model.

Batch stages that yield without first iterating `ctx.resume(...)` are allowed but non-resumable. The runner warns, ignores old partial state for that run, and does not write new partial state from those yielded outputs.

Batch stages are scheduled serially by the runner. Stage bodies may still perform parallel work inside each batch item with `asyncio.gather(...)`, process pools, or long-lived worker sessions. A batch item may yield zero paths; the completed index is recorded, but item-level output completeness is the stage's responsibility.

## Output Roots And Branches

`Pipeline.default_output_root(config)` returns the base output root. The CLI can override that with `--out`. Varve then appends the selected branch:

```text
base/<branch>        # persistent branches
base/.tmp/<branch>   # temporary override branches
```

`varve.yaml` is discovered next to the pipeline module. Missing `varve.yaml` is allowed for `main`.

Each branch section has three independent facets: `config`, `axes`, and `is_temporary`. `config` controls stage behavior and participates in content keys. `axes` selects a branch's active matrix domain and controls graph construction without entering content keys. Temporary manifests snapshot both the validated Config and normalized active axes so later status and refresh operations reconstruct the same graph.

## Matrix Graph Expansion

`Pipeline.stages()` collects branch-independent stage templates. After branch resolution, `build_graph(pipeline, axes)` constructs an immutable `PipelineGraph`: it expands each matrix template into concrete cell stages, resolves logical dependencies by equality on shared `Axis` object identities, and computes the concrete topology once. Runners, status, clean, and dashboard state all consume that branch-scoped graph. Dashboard discovery remains zero-import.

Concrete cell names encode coordinates in declaration order. Store slots and partial records therefore remain unchanged structurally: a cell is an ordinary stage with an independent name and content key. Coordinates do not add a key component. Logical `ctx.input()` and `ctx.inputs()` calls map to the aligned concrete upstream cells, ordered by upstream axis declaration order and then batch index.

Within one probe or run command, concrete cells from the same matrix template share source dependency discovery. File key inputs share a filesystem snapshot across consecutive read-only probes and cache hits, so repeated paths are resolved and inspected once and any required content hash is reused. After every stage that actually executes and completes successfully, the runner discards filesystem observations before keying the next stage, because executed stage code may legitimately change a later stage's declared file input; source discovery remains shared for the whole command. A run's read-only external-upstream validation likewise uses a separate filesystem snapshot from subsequent stage execution. The caches are isolated by the full source-discovery inputs and never survive the command; persisted stage fingerprints remain independent inputs to the cache decision, and no store schema or content-key shape changes.

Managed matrix artifacts are contained under `ctx.cell_out`, which is `<output-root>/<cell-name>/`. Relative `produces` declarations and batch yields resolve there; absolute paths must remain inside it. Store records continue to hold paths relative to the branch output root. Ordinary stages retain the existing output behavior.

## CLI And Config

`Pipeline.cli(argv)` delegates to `varve.cli.app.main` and provides `run`, `status`, `plan`, `list`, and `clean`.

`argparse` parses commands and generated `Args` flags. `pydantic-settings` builds semantic `Config` values from branch/override values, environment variables, `.env`, and model defaults.

Config priority:

```text
branch or override value > env > dotenv (.env) > field default
```

`run --override JSON` creates a temporary branch by deep-merging JSON over `main`.

Do not add Click or Typer. The strict `argparse` behavior is intentional.

## Clean Safety

All clean operations require a valid `.varve/manifest.json` anchor under the selected output root.

Full clean removes the whole output root after rejecting dangerous roots such as empty paths, `/`, the home directory, and the current working directory. Pipelines can narrow allowed full-clean roots by overriding `Pipeline.clean_roots(config)`.

Per-stage clean only deletes recorded artifacts and store records for the selected downstream closure. It does not use `allowed_roots`; its boundary is the manifest anchor plus recorded artifact paths.

## Dashboard

The top-level `varve` console script reads existing stores.

Temporary branches under `out/.tmp` are filtered by default and included only with `--include-temp`. Discovery is zero-import; state rendering imports stored manifest modules only after discovery. `refresh` runs branches whose evaluated status is executable: `artifact-missing`, `dirty`, `no-cache`, `resume`, or `stale`.

The dashboard and the generated `Pipeline.cli()` commands share `style.py` for status colors and console construction, so both render the same aligned tables and semantic status colors. Rich drops color automatically when output is not a terminal.

## Known Limitations

- Source fingerprints use `ast.dump`; a CPython minor-version upgrade may invalidate caches.
- Windows is not supported yet because locking uses `fcntl`.
