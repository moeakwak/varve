# varve Architecture

`varve` is a small single-machine runner for Python-defined pipelines with a materialized, content-addressed cache. Users define `Pipeline` classes and stages; varve owns branch-aware output roots, cache keys, store records, generated CLI commands, and clean/status behavior.

For author-facing behavior and examples, see the [user guide](GUIDE.md). For the project overview and quick start, see the [README](../README.md).

## Package layout

```text
src/varve/
├── __init__.py          # public re-export surface
├── pipeline.py          # Pipeline base class, stage collection, CLI hook
├── matrix.py            # axes and branch-scoped concrete graph expansion
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
├── engine/              # cache-state decisions, runner, and run display
├── cli/                 # generated Pipeline.cli() commands and Rich status rendering
└── dashboard/           # discovery and varve ls/show/refresh
```

## Public surface

Only these names are exported from `varve.__all__`:

```python
from varve import Axis, Ctx, Pipeline, JSON, KeySpec, StageSpec, batch_stage, matrix, stage
```

`Pipeline.graph(axes)` is the public convenience entry point for constructing an immutable, branch-scoped `PipelineGraph`. The graph view exposes concrete stages, topology, base-to-cell lookup, and selection. `PipelineGraph` and the lower-level `build_graph(pipeline, axes)` constructor live in `varve.matrix` and are supported graph APIs, but they are not re-exported from the top-level package; author code should normally prefer `Pipeline.graph()`.

Everything else is internal unless this document, `GUIDE.md`, or `README.md` says otherwise. `Store`, persisted models, keying helpers, runner helpers, and dashboard models may be used by internal code and tests through full module paths, but they are not public API.

## Dependency direction

- Low-level packages: `keying`, `store`, and `engine.state`. They depend only on leaf modules such as `models`, `log`, and `keyspec`.
- Middle layer: `branch_config` and `engine.runner`.
- Top layers: `cli` and `dashboard`.
- Public-facing modules such as `pipeline`, `decorators`, and `context` may use internals to keep the user API small.
- The only intentional reverse edge is the lazy import inside `Pipeline.cli()`: `from varve.cli.app import main`.

There is no import-direction tool. Keep the graph boring by review.

## Cache and store

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

Upstream keys enter through reads. A stage body takes upstream outputs via `ctx.input(stage)` for exactly one path or `ctx.inputs(stage)` for a list, and both require `stage` to appear in the current stage's `needs=`, because only declared upstreams are folded into the content key.

A record resolves to one of six cache states: `hit`, `stale`, `artifact-missing`, `dirty`, `resume`, and `no-cache`.

## Source dependency discovery

`keying/dependencies.py` performs bounded, positive source discovery and returns both stable flat source components and a dependency DAG. `keying/keys.py` consumes the flat components for hashing, while `status.py` consumes the same result for explanation; there is no second discovery implementation in the CLI.

The stage function and explicit `uses` roots are strict inputs. Inferred project functions, whole classes, stable values, and narrow module-file fallbacks are best effort: a failure to inspect or hash one inferred branch is logged only at debug level and cannot block status evaluation or execution. Explicit `uses`, `KeySpec`, stage source, and store corruption retain their existing strict behavior.

Discovery follows only directly resolvable globals, closure cells, defaults, nested code objects, simple `module.attr` reads, class-owned methods, and project base classes. It intentionally does not perform type propagation, control-flow analysis, registry inspection, factory return inference, dynamic import tracking, or runtime dispatch analysis. Calls through `self` are runtime dispatch and must be represented with `uses` or `KeySpec` when they affect caching.

`Pipeline.auto_uses_packages` controls inferred recursion. `None` selects the stage function's top-level package, an explicit tuple replaces that scope, and `()` disables inferred package recursion without removing stage source or explicit `uses` roots.

## Decision probes and status

`engine.runner._probe_stage` is the shared ready-stage decision unit for both execution probes and structured status. `probe_pipeline()` walks the full topology by default, so every displayed decision key uses the same whole-pipeline upstream projection; structured status and dashboard callers keep that complete default.

Scoped runs validate less. An internal stage filter probes only the direct upstreams outside the execution selection and their recursive ancestor closure, in original topology order. Every stage in that closure must be a hit, and current decision keys propagate through it exactly as in a full probe. This keeps `--only`, `--downstream`, and sliced runs from inspecting unrelated source or file key inputs, without weakening validation of external upstream state.

Probes compute source dependencies before checking for missing upstream records, so an invalid explicit `uses` stays strict while valid source dependencies remain available even when other key inputs are not.

`status.py` turns probes into immutable, cell-aware view models: it groups concrete cells by base stage and, for stale stages, compares current source components against the previous successful record. Every concrete stage is probed before display selection and folding, so folding never changes a cache decision or a cell's identity. Aggregate status uses the least-to-most-severe order in `engine.state`, and aggregate duration sums recorded cell durations while carrying the recorded count so the renderer can flag missing values.

`cli/status.py` renders the folded summary, axis-column cell tables, single-stage key inputs, progressively expanded dependency DAG, and source-change markers. It reads base names, canonical coordinates, axis order, and logical needs from the view model rather than parsing concrete stage names; qualified names, stored source locators, and known edge reasons are compacted only at this rendering boundary.

The command is read-only — it does not execute stages, initialize the store, or persist dependency graphs. A displayed decision key is the current cache-decision input, not a promise that the next execution will commit the same final key after recording config access.

## Config access projection

A stage's output depends only on the `Config` fields it reads, so folding the whole `Config` into every `content_key` over-invalidates: adding a tool or toggling a flag would rerun stages that never look at that field. Varve instead records which top-level fields each stage reads and keys only on those.

During a run the stage's `ctx.config` is a transparent recording proxy. Plain top-level field reads (`ctx.config.tools`, including reads inside helpers the config is passed to) are captured precisely; any access that cannot be attributed to one field — `model_dump()`, `getattr` of an unknown name, iteration, `__dict__`, pickling — marks the whole `Config` as depended-on (`config_access = None`, the conservative fallback). The recorded set is stored on the success record and, on the next run, the `Config` is projected onto it before hashing, so changing an unread field is a hit.

Soundness rests on the source component: if a stage's code (or a discovered callable) changes to read a new field, its source hash changes and it reruns, re-recording the set. The first run of a stage, and any run after a source change, key on the whole `Config` and then record the precise set for subsequent runs. Keying is two-phase: the hit/stale decision projects onto the previous run's set, and the committed key projects onto this run's actual reads (unioned with the previous set when the source is unchanged, so a resume that skips batches or a data-dependent branch not taken never drops a real dependency).

Config reads must be deterministic for fixed keyed inputs, and must happen on every stage entry rather than only inside resume-skipped per-item work; batch stages conventionally read `Config` at the top to build their job list, which satisfies this. A stage that ships the raw `Config` object into a subprocess and reads fields there is not captured — extract the values in the parent process (which reads them through the proxy) and pass those.

`Config` keeps its whole-value role for provenance and branch identity: `override_branch_name`, the manifest snapshot, and anything a stage writes to run metadata still see the full `Config`. Only keying is projected.

## Batch execution

Batch stages are scheduled serially by the runner, which keeps store transitions and partial writes deterministic. A stage body may still parallelize work within an item using `asyncio.gather(...)`, process pools, or long-lived worker sessions.

Resume is positional: varve records the completed indexes from `ctx.resume(...)` and skips them on the next run with the same content key. This requires deterministic iterable order, so callers should sort unstable inputs before passing them in; varve intentionally does not provide order-independent batch resume under the current content-key model. A batch item may yield zero paths — the completed index is still recorded, but item-level output completeness is the stage's responsibility.

Batch stages that yield without first iterating `ctx.resume(...)` are allowed but non-resumable: the runner warns, ignores old partial state for that run, and writes no new partial state from those yielded outputs.

## Output roots and branches

`Pipeline.default_output_root(config)` returns the base output root. The CLI can override that with `--out`. Varve then appends the selected branch:

```text
base/<branch>        # persistent branches
base/.tmp/<branch>   # temporary override branches
```

`varve.yaml` is discovered next to the pipeline module. Missing `varve.yaml` is allowed for `main`.

Each branch section has three independent facets: `config`, `axes`, and `is_temporary`. `config` controls stage behavior and participates in content keys. `axes` selects a branch's active matrix domain and controls graph construction without entering content keys. Temporary manifests snapshot both the validated Config and normalized active axes so later status and refresh operations reconstruct the same graph.

Recorded artifact paths are always output-root-relative. Ordinary stages write managed artifacts through `ctx.out`; matrix cells write through `ctx.cell_out`, described under Matrix graph expansion.

## Matrix graph expansion

`Pipeline.stages()` collects branch-independent stage templates. After branch resolution, `Pipeline.graph(axes)` delegates to `build_graph(pipeline, axes)` to construct an immutable `PipelineGraph`: it expands each matrix template into concrete cell stages, resolves logical dependencies by equality on shared `Axis` object identities, and computes the concrete topology once. Runners, status, clean, and dashboard state all consume that branch-scoped graph, and dashboard discovery remains zero-import.

Concrete cell names encode coordinates in declaration order, so store slots and partial records stay structurally unchanged: a cell is an ordinary stage with an independent name and content key. Coordinates do not add a key component. A matrix-only internal layout version does enter the content key, so records and partial state from a previous artifact layout become stale instead of being mixed with current outputs. Logical `ctx.input()` and `ctx.inputs()` calls map to the aligned concrete upstream cells, ordered by upstream axis declaration order and then batch index.

Within one probe or run command, concrete cells from the same matrix template share source dependency discovery. File key inputs share a filesystem snapshot across consecutive read-only probes and cache hits, so a repeated path is resolved and inspected once and any required content hash is reused.

After every stage that actually executes and completes successfully, the runner discards its filesystem observations before keying the next stage, because executed stage code may legitimately change a later stage's declared file input. A run's read-only external-upstream validation likewise uses a separate filesystem snapshot from subsequent stage execution. These caches are isolated by the full source-discovery inputs and never survive the command; persisted stage fingerprints remain independent inputs to the cache decision, and no store schema or content-key shape changes.

Run display grouping is an engine-level view over the selected concrete topology. One command-scoped display plan supplies the live plan, lifecycle reporter, and CLI outcome table, so the three layers cannot disagree and grouping cannot affect execution order. Reporters count selected completions by base-stage metadata rather than assuming cells are adjacent in topological order.

Ordinary stages are never folded. In `auto` mode a matrix group folds when at least `AUTO_COMPACT_MIN_CELLS` (currently 8) selected cells belong to it, unless any selected cell has a successful record whose elapsed time is at least `AUTO_EXPAND_SLOW_SECONDS` (currently 30 seconds); smaller groups and known slow groups expand. `run --expand` and `run --compact` override the decision. Compact info-level logging emits one group start and one completion summary, preserves concrete cell lifecycle and content keys at debug level, and always surfaces slow cells and failures by concrete identity. These rules inspect existing success metadata only and do not alter records or keys.

`Ctx` receives structured stage-display metadata from the graph. Matrix batch progress defaults to canonical cell values in declared axis order, without parsing or changing the concrete stage name; ordinary stages retain the stage name, and an explicit `ctx.resume(desc=...)` remains authoritative.

Generated `status` output folds concrete cells by base template by default. `--expand` dispatches by selection: a matrix base or an untargeted matrix pipeline renders cells with one column per declared axis, while an ordinary stage, concrete cell, or non-matrix pipeline retains the one-level source dependency view. `--all` likewise keeps the complete source dependency tree for ordinary stages, concrete cells, and non-matrix pipelines; a matrix base or untargeted matrix pipeline must select one concrete cell first. `--deps` and `--deps-all` provide explicit source-dependency equivalents for exactly one concrete cell or ordinary stage. All four expansion flags remain mutually exclusive.

Managed matrix artifacts are contained under `ctx.cell_out`, which is `<output-root>/.matrix/<base-stage>/<axis-name>=<canonical-id>/...`; each coordinate occupies one directory level in the stage's axis declaration order. For example, `score@bench=unimer,model=qwen3-vl-8b` writes under `.matrix/score/bench=unimer/model=qwen3-vl-8b/` while its concrete stage and store identity remains `score@bench=unimer,model=qwen3-vl-8b`. Relative `produces` declarations and batch yields resolve under `ctx.cell_out`; absolute paths must remain inside it. Store records still hold paths relative to the branch output root, so artifact existence checks, upstream reads, and stage clean operate from recorded paths rather than reconstructing physical paths. Ordinary stages retain `ctx.cell_out == ctx.out`.

## CLI and config

`Pipeline.cli(argv)` delegates to `varve.cli.app.main` and provides `run`, `status`, `plan`, `list`, and `clean`.

`argparse` parses commands and generated `Args` flags. `pydantic-settings` builds semantic `Config` values from branch/override values, environment variables, `.env`, and model defaults.

Config priority:

```text
branch or override value > env > dotenv (.env) > field default
```

`run --override JSON` creates a temporary branch by deep-merging JSON over `main`. `run --expand` and `run --compact` control only matrix display folding and are mutually exclusive.

Do not add Click or Typer. The strict `argparse` behavior is intentional.

## Clean safety

All clean operations require a valid `.varve/manifest.json` anchor under the selected output root.

Full clean removes the whole output root after rejecting dangerous roots such as empty paths, `/`, the home directory, and the current working directory. Pipelines can narrow allowed full-clean roots by overriding `Pipeline.clean_roots(config)`.

Per-stage clean only deletes recorded artifacts and store records for the selected downstream closure. It does not use `allowed_roots`; its boundary is the manifest anchor plus recorded artifact paths.

## Dashboard

The top-level `varve` console script reads existing stores. Discovery is zero-import and stops descending once it finds a valid branch output root, so materialized artifacts are never treated as further scan roots. Temporary branches under `out/.tmp` are filtered out unless `--include-temp` is passed.

`varve ls`, `show`, and `refresh` use the same exact state loader. It imports stored manifest modules, resolves branches, builds graphs, fingerprints current inputs, and probes artifacts. `ls` renders `STATUS` and hit/total `STAGES`; `refresh` runs branches whose evaluated status is executable — `artifact-missing`, `dirty`, `no-cache`, `resume`, or `stale`.

The dashboard and the generated `Pipeline.cli()` commands share `style.py` for status colors and console construction, so both render the same aligned tables and semantic colors. Rich drops color automatically when output is not a terminal.

## Known limitations

- Source fingerprints use `ast.dump`; a CPython minor-version upgrade may invalidate caches.
- Windows is not supported yet because locking uses `fcntl`.
