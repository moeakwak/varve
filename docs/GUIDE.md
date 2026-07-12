# Varve User Guide

This guide is the working reference for authoring varve pipelines: how to define stages, declare durable inputs and outputs, read cache decisions, resume batch work, expand matrix stages, select branches, and operate stored pipelines. For a short introduction, start with the [README](../README.md); for implementation boundaries and invariants, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Contents

- [Installation](#installation)
- [Defining a pipeline](#defining-a-pipeline)
- [Stages, dependencies, and artifacts](#stages-dependencies-and-artifacts)
- [Cache keys and decisions](#cache-keys-and-decisions)
- [Resumable batch stages](#resumable-batch-stages)
- [Matrix stages](#matrix-stages)
- [Branches and output roots](#branches-and-output-roots)
- [Generated pipeline CLI](#generated-pipeline-cli)
- [Dashboard CLI](#dashboard-cli)
- [Clean safety and recovery](#clean-safety-and-recovery)
- [Limitations](#limitations)

## Installation

Varve requires Python 3.10 or newer and currently supports Unix-like systems.

```bash
pip install varve
```

The top-level public import surface is:

```python
from varve import Axis, Ctx, Dependencies, JSON, Pipeline, StageSpec, batch_stage, matrix, stage
```

`Pipeline.graph()` also provides a supported branch-scoped graph view. Its `PipelineGraph` return type and the lower-level `build_graph()` constructor live in `varve.matrix` rather than the top-level package.

## Defining a pipeline

A pipeline is a `Pipeline` subclass with a pydantic `Config` model and one or more decorated instance methods. Calling `Pipeline.cli()` gives the module its generated command line.

```python
from pydantic import BaseModel
from varve import Ctx, Pipeline, stage


class Config(BaseModel):
    language: str = "en"


class Demo(Pipeline):
    Config = Config

    @stage(produces="message.txt")
    def message(self, ctx: Ctx) -> None:
        (ctx.out / "message.txt").write_text(ctx.config.language)


if __name__ == "__main__":
    raise SystemExit(Demo.cli())
```

The default output base is `out/` next to the pipeline module. Varve appends `main` or another selected branch, so the example writes under `out/main/`. Override `Pipeline.default_output_root(config)` when output placement is part of the pipeline, or pass `--out PATH` for one command.

### Config and Args

`Config` contains semantic values that can affect durable outputs. Varve builds it with pydantic-settings using this priority:

```text
branch or override value > environment > .env > field default
```

Stage code reads it through `ctx.config`. Varve records which top-level fields each stage reads and, after the stage's first successful run, keys it only on those fields, so changing an unrelated field is still a hit. Operations that cannot be attributed to one field, such as `model_dump()` or iterating the whole model, conservatively depend on the complete Config. See [Config access projection](ARCHITECTURE.md#config-access-projection) for the exact two-phase keying.

An optional `Args` pydantic model defines operational command-line flags for `run`, `status`, and `clean`:

```python
class Args(BaseModel):
    progress: bool = True
    workers: int = 4


class Demo(Pipeline):
    Config = Config
    Args = Args

    @stage()
    def inspect(self, ctx: Ctx[Config, Args]) -> None:
        print(ctx.args.workers)
```

Use Config for durable behavior and input locations, and Args only for operational controls such as worker counts, progress display, and disposable scratch locations. A value that can change durable input selection or output semantics belongs in Config; do not route it from Args through `Dependencies.inputs` or `Dependencies.values`. Pydantic `Path` fields are allowed in Config and serialize canonically into branch and input identities.

## Stages, dependencies, and artifacts

Use `@stage` for synchronous or asynchronous work that executes once and commits a fixed set of managed files or directories. `produces=` accepts a path, a list of paths, or a callable resolved from the runtime context. Relative paths resolve from the stage's managed output root. A successful record stores output paths relative to the branch root.

```python
@stage(produces=["table.parquet", "summary.json"])
def analyze(self, ctx: Ctx) -> None:
    write_table(ctx.out / "table.parquet")
    write_summary(ctx.out / "summary.json")
```

Declare execution and data dependencies with `needs=`. It accepts a stage name, several names, or method references already defined in the class body.

```python
@stage(produces="raw.txt")
def extract(self, ctx: Ctx) -> None:
    (ctx.out / "raw.txt").write_text("data")

@stage(needs="extract", produces="normalized.txt")
def normalize(self, ctx: Ctx) -> None:
    raw = ctx.input("extract")
    (ctx.out / "normalized.txt").write_text(raw.read_text().upper())
```

`ctx.input("extract")` requires exactly one recorded upstream artifact. `ctx.inputs("extract")` always returns `list[Path]` and is appropriate for stages or matrix fan-in that produce several artifacts. Reading an undeclared upstream fails because its artifact fingerprint would otherwise be absent from the downstream input key.

Varve fingerprints managed output contents before committing a successful stage and checks them again when evaluating its record. Missing and changed artifacts produce `needs-run` with `artifact-missing` or `artifact-changed`. Stage code may write unrecorded scratch files, but varve does not track, expose, or remove them during per-stage clean.

The stage materialization fingerprint preserves the order exposed by `ctx.inputs()`: declaration order for ordinary `produces`, and batch index plus yield order within each index for batch stages. Reordering the same artifact set therefore invalidates downstream consumers whose positional input changed.

## Input keys and source review

A stage input key is derived from:

- the Config fields recorded for that stage;
- evaluated `Dependencies.inputs` contents;
- evaluated `Dependencies.values` JSON;
- concrete upstream artifact fingerprints;
- internal semantic versions where required for safe invalidation.

### Source files

Varve automatically observes the Python files defining the pipeline class and stage callable. Declare other Python files or directories explicitly:

```python
@stage(depends=Dependencies(sources=[Path("shared/normalization")]), produces="normalized.json")
def normalize(self, ctx: Ctx) -> None:
    external_helper(ctx.out / "normalized.json")
```

Comments and formatting do not change the normalized AST fingerprint; docstrings and other runtime-visible AST do. Varve does not infer imports, calls, registries, factories, or runtime dispatch. A changed fingerprint must be accepted or rejected before `run` executes any selected stage.

### Files and values outside Python

Use `Dependencies` for durable non-stage inputs:

```python
from pathlib import Path
from varve import Dependencies, stage


@stage(
    depends=Dependencies(
        inputs={"dataset": lambda ctx: Path("data/input.jsonl")},
        values={"schema": lambda ctx: {"version": 3}},
    ),
    produces="result.json",
)
def evaluate(self, ctx: Ctx) -> None:
    ...
```

Varve snapshots normalized paths, sizes, and mtimes so unchanged files do not need to be rehashed repeatedly within a command. The durable file key component uses the sorted content hashes evaluated under each declared name. A missing declared file is a strict error. Values must be JSON-compatible.

Dependency resolvers receive a stable context with `config`, `out`, `cell`, and `cell_out`; runtime `args` are deliberately unavailable. This keeps dashboard evaluation and refresh reproducible without persisting command-line execution controls.

### Status values

Varve reports these stage states:

| Status | Meaning |
| --- | --- |
| `hit` | The current key matches a successful record and all artifacts exist. |
| `needs-run` | A run is required; the reason names changed inputs, source rejection, artifact damage, or interruption. |
| `resume` | A matching batch has resumable partial indexes. |
| `failed` | The last stage attempt raised an exception. |
| `error` | Varve could not evaluate the stage reliably. |

Source review is displayed independently as `confirmed`, `pending`, `accepted`, or `rerun-required`. A pending review blocks the selected run before any stage starts. `status` is read-only: it does not initialize a store, execute stages, establish a source baseline, or rewrite an older store schema.

## Resumable batch stages

Use `@batch_stage` for an async generator that yields the paths produced by each item. Batch outputs come from yields, so `@batch_stage` does not accept `produces=`.

```python
from varve import Ctx, Pipeline, batch_stage


class Demo(Pipeline):
    Config = Config

    @batch_stage()
    async def render(self, ctx: Ctx):
        items = sorted(load_items())
        async for index, item in ctx.resume(items, unit="item"):
            path = ctx.out / "parts" / f"{index:04d}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            render_one(item, path)
            yield path
```

Resume is positional. Varve records completed indexes and skips them on the next run with the same input key when their artifact fingerprints still match. The iterable must therefore have deterministic order for fixed inputs; sort unstable sources before passing them to `ctx.resume()`.

A successful batch record is not resumable partial state. If one of its managed artifacts is missing, the stage reports `needs-run · artifact-missing` and starts from the beginning; remaining success outputs are not silently reused as checkpoints.

`ctx.resume()` can show tqdm progress and accepts `desc`, `total`, `unit`, and a `postfix` callable. An item may yield zero or several paths. Varve records the completed index but does not validate item-level output shape; validate shape in a downstream stage when it matters.

A batch stage that yields without iterating `ctx.resume()` is allowed but non-resumable. Varve warns, does not retain resumable partial state for that run, and starts the stage body again after failure.

Varve schedules concrete stages serially to keep store transitions and partial output deterministic. Parallelism inside a stage remains ordinary Python: use `asyncio.gather`, a process pool, threads for appropriate I/O, or a long-lived worker/client managed by the stage body.

## Matrix stages

`Axis` declares a reusable ordered coordinate domain. Values may be strings, integers, or Enum members and must map to unique canonical ids. Stack `@matrix(...)` above `@stage` or `@batch_stage`; the stage must accept keyword-only parameters whose names exactly match its axes.

```python
from enum import Enum
from varve import Axis, Ctx, Pipeline, matrix, stage


class Model(str, Enum):
    SMALL = "small"
    LARGE = "large"


BENCH = Axis("bench", ["ocrbench", "unimer"])
MODEL = Axis("model", list(Model))


class Evaluation(Pipeline):
    Config = Config

    @matrix(BENCH)
    @stage(produces="ground-truth.parquet")
    def prepare(self, ctx: Ctx, *, bench: str) -> None:
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        prepare_one(bench, ctx.cell_out / "ground-truth.parquet")

    @matrix(BENCH, MODEL)
    @stage(needs="prepare", produces="score.json")
    def score(self, ctx: Ctx, *, bench: str, model: Model) -> None:
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        score_one(bench, model, ctx.input("prepare"), ctx.cell_out / "score.json")

    @stage(needs="score", produces="summary.json")
    def summarize(self, ctx: Ctx) -> None:
        summarize_all(ctx.inputs("score"), ctx.out / "summary.json")
```

### Expansion and wiring

Each coordinate combination becomes a concrete stage such as `score@bench=unimer,model=large`. Shared Axis objects align by equal coordinate value: one score cell reads the prepare cell for the same benchmark. An axis that exists only upstream becomes fan-in: the ordinary summarize stage reads score artifacts across every active benchmark and model in deterministic upstream-axis order.

Coordinates are part of the concrete stage identity rather than a separate key component. Each cell has its own store record, attempt marker, partial state, input key, source review, selection identity, and artifact root.

### Cell context and artifacts

Matrix code receives typed coordinate values as keyword-only parameters and can also inspect them through `ctx.cell["model"]` or `ctx.cell.model`. `ctx.out` remains the branch output root. `ctx.cell_out` is the managed root for the current cell:

```text
<branch-root>/.matrix/<base-stage>/<axis>=<id>/<axis>=<id>/...
```

Relative `produces` declarations and batch yields resolve under `ctx.cell_out`; absolute managed paths must still remain inside it. Ordinary stages retain `ctx.cell_out == ctx.out`.

### Active domains and graph access

Branches may activate a subset of each Axis by canonical id. Omitted axes use their complete declared domain, and active values always retain declaration order.

`Pipeline.graph(axes)` builds the immutable concrete graph for an active domain. It exposes concrete stages, topology, base-to-cell lookup, and selection helpers. `PipelineGraph` and `build_graph()` are also available from `varve.matrix`, but most author code should use the pipeline method rather than instantiate graph types.

### Matrix selection and display

`run --only score` selects every active score cell without automatically running its upstreams; external upstreams must already be current. `--upto`, `--downstream`, and `--only` are mutually exclusive. `--slice axis=id` narrows selected coordinates plus their aligned upstream closure and is allowed only for temporary branches.

Run display uses one policy for the plan, lifecycle log, and outcome table. In automatic mode, large matrix groups fold to a single summary line, while small groups and groups with known slow cells stay expanded. `run --expand` always shows concrete cells and `run --compact` always folds matrix groups; failures and slow cells keep their concrete identities, and `-v` keeps concrete lifecycle and key diagnostics. The exact fold thresholds live in [Matrix graph expansion](ARCHITECTURE.md#matrix-graph-expansion).

Matrix batch progress defaults to canonical coordinate values in axis declaration order instead of the full concrete stage name. An explicit `ctx.resume(desc=...)` remains authoritative.

`status` folds cells by base stage by default, including status counts, review counts, logical needs, and recorded durations. Select a base with `status score --expand` to render axis columns, or select a concrete cell to inspect its individual input, artifact, and source-review state.

## Branches and output roots

`varve.yaml` lives next to the pipeline module. It is optional when `main` can use Config defaults. Each branch has independent `config`, `axes`, and `is_temporary` facets:

```yaml
main:
  config:
    bootstrap: 1000
  axes:
    model: [small]

full:
  config:
    bootstrap: 5000
```

The previous flat Config format is rejected; Config fields must live under `config:`. Axis ids must exist in their declared Axis domains.

A `varve.yaml` branch with `is_temporary: true` uses the temporary output namespace and snapshots its validated Config and axes just like an override branch. This is useful for an explicitly named, reproducible temporary target that should remain excluded from normal dashboard discovery.

Varve resolves the output base from `--out` or `Pipeline.default_output_root(config)` and then appends the branch:

```text
<base>/<branch>        # persistent branch
<base>/.tmp/<branch>   # temporary branch
```

`run --override '{"bootstrap": 200}'` deep-merges JSON over `main` and creates a temporary branch whose generated name hashes the complete Config snapshot and active axes. Supplying a new `--branch NAME` with an override gives the temporary branch an explicit name; it cannot collide with another named `varve.yaml` branch. Reusing that temporary name with different Config or axes fails rather than mixing materializations.

Temporary manifests snapshot Config and axes so later `status`, `clean`, `show`, and `refresh` reconstruct the same graph. Use `--include-temp` on dashboard commands when temporary stores should be discovered.

## Generated pipeline CLI

Place `raise SystemExit(MyPipeline.cli())` in the pipeline module and run commands through Python:

```bash
python pipeline.py run
python pipeline.py status
python pipeline.py accept
python pipeline.py reject
python pipeline.py plan
python pipeline.py list
python pipeline.py clean
```

### run

```text
run [--branch NAME] [--override JSON]
    [--only STAGE | --upto STAGE | --downstream STAGE]
    [--slice AXIS=ID] [--force] [--rehash] [--expand | --compact] [--out PATH]
```

`--upto` selects a stage or base and its upstream closure. `--downstream` selects it and its descendants. `--only` selects exactly the named ordinary stage, concrete cell, or every cell of a base stage. Before a scoped execution, upstream stages outside the selection must have current successful records and artifacts. `--force` ignores cache decisions for selected stages but preserves topology and store safety. `--rehash` ignores persisted stat shortcuts while evaluating inputs and existing artifacts.

### status

```text
status [STAGE] [--branch NAME]
    [--expand] [--rehash] [--out PATH]
```

The default is a concise pipeline summary. For an ordinary stage or concrete matrix cell, `--expand` shows detailed input, artifact, attempt, failure, and source-review state. For a matrix base, `--expand` shows its concrete cells.

### accept and reject

```text
accept [STAGE ...] [--branch NAME] [--out PATH]
reject [STAGE ...] [--branch NAME] [--out PATH]
```

Without targets, these commands process every pending source review. A base matrix stage selects all active cells, while a concrete cell selects only itself. `accept` keeps the existing materialization reusable; `reject` marks it `needs-run · source-change`. Both commands only record a decision bound to the current source fingerprint and never execute a stage or rewrite a success record. A later source change opens a new pending review.

### plan and list

```text
plan [--branch NAME] [--only STAGE | --upto STAGE | --downstream STAGE] [--out PATH]
list
```

`plan` resolves the selected branch and prints concrete topological order without evaluating keys or executing stages. `list` shows the branch-independent template structure and declared matrix axes.

### clean

```text
clean [--branch NAME] [--downstream STAGE] [--out PATH] [--yes]
```

Without `--downstream`, clean removes the complete selected output root after confirmation. With a stage, it deletes the recorded artifacts and store state for that concrete downstream closure. Clean does not infer ownership from filenames; it relies on manifest anchors and recorded paths.

All commands accept the global `-v` or `--verbose` flag before the command. Generated Args flags are available on `run`, `status`, `accept`, `reject`, and `clean`.

## Dashboard CLI

The installed `varve` command discovers branch stores from manifests:

```bash
varve ls [--root DIR] [--include-temp] [--rehash]
varve show <pipeline_id> [--root DIR] [--branch NAME] [--include-temp] [--rehash]
varve refresh [--root DIR] [--prefix MODULE_PREFIX] [--include-temp] [--rehash]
varve accept <pipeline_id> [STAGE ...] [--root DIR] [--branch NAME]
varve reject <pipeline_id> [STAGE ...] [--root DIR] [--branch NAME]
```

`varve ls` imports each pipeline, resolves its branch, builds the concrete graph, fingerprints current inputs, and probes artifacts before reporting exact state. `show` provides the same exact state in detail for one store. `refresh` skips an entire pipeline when it has pending source reviews, continues evaluating later pipelines, executes eligible `needs-run`, `resume`, or `failed` pipelines, and then reloads exact state after every attempt. Its final report preserves review-required, failed, evaluation-error, and still-pending categories together. It returns 0 only when every selected pipeline is complete, 2 when pending review is the only incomplete reason, and 1 for every other incomplete result.

## Clean safety and recovery

All clean operations require a valid `.varve/manifest.json` anchor. Full clean rejects dangerous roots such as `/`, the home directory, and the current working directory. Override `Pipeline.clean_roots(config)` to restrict full clean further for a particular pipeline.

Per-stage clean removes only recorded managed artifacts and corresponding store state. Paths are checked against the branch output root, and matrix artifacts remain contained by their cell roots.

If a stage body raises after its attempt begins, Varve records a `FailureRecord` and reports `failed`; a matching batch partial also reports its resumable progress. If a process exits without recording that failure, a valid partial reports `resume`, while a bare attempt reports `needs-run · interrupted`. Running the pipeline again retries an ordinary failure or continues eligible batch indexes. If a successful artifact was deleted, rerun rebuilds the `artifact-missing` stage. Use `clean --downstream STAGE` when intentional invalidation should remove both one stage and all materializations that consume it.

## Limitations

- Stages execute serially at the varve scheduler level.
- Stores and artifacts are local filesystem state; there is no remote backend.
- Windows is not supported because locking uses `fcntl`.
- Source discovery is bounded and cannot infer every dynamic Python dependency.
- Source fingerprints use `ast.dump`; a CPython minor-version upgrade may rebuild cached stages.
- Input and artifact trees do not support symlinks.
- The stat hash shortcut assumes unchanged path, inode, size, and mtime imply unchanged content; use the force-rehash diagnostic when that assumption is suspect.
- Batch resume is positional and requires deterministic iterable order.

Varve is an alpha 0.x project. Review the [changelog](../CHANGELOG.md) before upgrading across minor versions.
