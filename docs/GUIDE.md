# Varve User Guide

This guide is the working reference for authoring varve pipelines: how to define stages, declare durable inputs and outputs, read cache decisions, resume batch work, expand matrix stages, select branches, and operate stored pipelines. For a short introduction, start with the [README](../README.md); for implementation boundaries and invariants, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Contents

- [Installation](#installation)
- [Defining a pipeline](#defining-a-pipeline)
- [Stages, dependencies, and artifacts](#stages-dependencies-and-artifacts)
- [Input keys, source invalidation, and Review](#input-keys-source-invalidation-and-review)
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

## Input keys, source invalidation, and Review

A stage input key is derived from:

- the Config fields recorded for that stage;
- evaluated `Dependencies.inputs` contents;
- evaluated `Dependencies.values` JSON;
- concrete upstream artifact fingerprints;
- the Stage callable and declared deterministic Python source fingerprint;
- internal semantic versions where required for safe invalidation.

### Source files

Varve fingerprints the complete AST node for each Stage callable, including decorators, signature, docstring, and body. Changing it produces `needs-run · source-changed` without Review. Declare other deterministic Python files or directories with `Dependencies.sources`:

```python
@stage(depends=Dependencies(sources=[Path("shared/normalization")]), produces="normalized.json")
def normalize(self, ctx: Ctx) -> None:
    external_helper(ctx.out / "normalized.json")
```

Comments and formatting do not change the normalized AST fingerprint; docstrings and other runtime-visible AST do. Varve does not infer imports, calls, registries, factories, or runtime dispatch, so helper files outside the definition files must be declared explicitly.

The remaining AST in the pipeline and callable definition files is observed separately. It excludes every Stage callable collected from that pipeline, so changing Stage A does not open Review for Stage B merely because both methods share a file. Use `Dependencies.review_sources` for additional Python sources whose changes may leave existing materializations valid:

```python
@stage(
    depends=Dependencies(review_sources=[Path("shared/report_layout.py")]),
    produces="report.html",
)
def report(self, ctx: Ctx) -> None:
    ...
```

A changed Review fingerprint opens `needs-review` only when a success or validated current-key partial could otherwise be reused. `reuse` preserves that reusable state; `invalidate` makes the Stage require a rerun. If another input, deterministic source, upstream artifact, or artifact-integrity change already requires execution from the beginning, Review is unnecessary. Declared source roots must exist and contain Python files only; a missing or renamed root is an evaluation error, while membership changes inside an existing directory change its fingerprint. If one actual file falls under both policies, deterministic rerun wins; declaring the same normalized root in both fields is an error.

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

Dependency resolvers receive a stable context with `config`, `out`, `cell`, and `cell_out`; runtime `args` are deliberately unavailable. This keeps exact status evaluation and bulk execution reproducible without persisting command-line execution controls.

### Status values

Varve reports these stage states:

| Status | Meaning |
| --- | --- |
| `hit` | The current key matches a successful record and all artifacts exist. |
| `needs-review` | Source differs from the successful materialization and no decision is bound to the current fingerprint. |
| `needs-run` | A run is required; the reason names changed inputs, deterministic source invalidation, Stage invalidation, artifact damage, or interruption. |
| `resume` | A matching batch has resumable partial indexes. |
| `failed` | The last stage attempt raised an exception. |
| `error` | Varve could not evaluate the stage reliably. |

Execution status remains one of `hit`, `needs-run`, `resume`, `failed`, or `error`. Review source adds two orthogonal facts: relationship is `not-applicable`, `current`, or `changed`, and a changed fingerprint has decision `none`, `reuse`, or `invalidate`. The effective overlay maps changed plus none to `needs-review · source-changed`, changed plus invalidate to `needs-run · source-changed`, and changed plus reuse back to the execution result. No reusable baseline is not-applicable and does not require Review; a current fingerprint ignores unrelated old ReviewRecords. Review Decision belongs to the logical base Stage, while execution status, success, failure, attempt, partial state, and artifacts remain concrete-stage or Cell state.

A normal run validates its selected concrete stages and required external upstreams before any Stage body starts. Any pending Stage Review returns exit code 2 and prints topology-ordered base Stage names. `run --force` validates the same preflight and reruns selected concrete stages without writing ReviewRecord or changing the decision of unselected Cells. External upstreams that force will only reuse must still be current or have a resolved Review. A force attempt records the current rerun and Review source fingerprints, so a later normal run can resume partial produced by that current source; old-source partial is cleared when the forced attempt starts. Successful execution stores the source observation frozen at attempt start and does not clear the shared Stage ReviewRecord. `status` is read-only: it does not initialize a store, execute stages, establish a source baseline, or rewrite an old-schema store.

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

Resume is positional. Varve records completed indexes under the execution input key and skips them on the next run only when their artifact fingerprints still match. Current-key partial uses its AttemptMarker or FailureRecord source provenance before any older success baseline: partial from the current Review fingerprint resumes directly, partial from an older Review fingerprint requires one Stage-level `reuse` or `invalidate` decision, and partial under another input key is ignored. `reuse` preserves validated indexes; `invalidate` leaves them on disk until execution begins and then clears them before restarting from zero. The iterable must have deterministic order for fixed inputs; sort unstable sources before passing them to `ctx.resume()`.

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

Coordinates are part of the concrete stage identity rather than a separate key component. Each Cell has its own success record, attempt marker, failure record, partial state, input key, selection identity, and artifact root. All Cells of one logical Matrix Stage share a single base-stage ReviewRecord and therefore one `reuse` or `invalidate` decision.

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

Execution, status, plan, and clean selectors use `STAGE` or `STAGE@AXIS=VALUE[,AXIS=VALUE...]`. A bare Matrix base selects all active Cells, a partial selector filters the named axes and treats omitted axes as wildcards, and full coordinates select one Cell. Axis input order does not matter, but canonical selectors always follow declaration order. Review commands are intentionally different: `--stage` accepts only ordinary or Matrix base Stage names because a Review Decision always covers the whole logical Stage. A coordinate target fails before any write and reports the copyable base name.

`run --only score@bench=a` selects the matching active score Cells without automatically running their upstreams; external upstreams must already be current. `--upto`, `--downstream`, and `--only` are mutually exclusive and apply their respective closure after the shared resolver returns concrete seeds. `clean --downstream` applies descendant closure, while status applies no implicit upstream or downstream expansion. Repeated Review base targets form a topology-ordered stable union. `--slice axis=id` remains a separate temporary-run constraint across selected stages and their aligned upstream closure.

Run display uses one policy for the plan, lifecycle log, and outcome table. In automatic mode, large matrix groups fold to a single summary line, while small groups and groups with known slow cells stay expanded. `run --expand` always shows concrete cells and `run --compact` always folds matrix groups; failures and slow cells keep their concrete identities, and `-v` keeps concrete lifecycle and key diagnostics. The exact fold thresholds live in [Matrix graph expansion](ARCHITECTURE.md#matrix-graph-expansion).

Matrix batch progress defaults to canonical coordinate values in axis declaration order instead of the full concrete stage name. An explicit `ctx.resume(desc=...)` remains authoritative.

`status` probes the complete graph and then folds Cells by base Stage, including effective-status counts, logical needs, recorded durations, and one Stage Review field. A single changed plus undecided Cell promotes its Matrix group and pipeline to `needs-review`. A partial selector heading shows its canonical selector and matched count; add `--expand` to render axis columns, or select a concrete Cell to inspect its individual input, artifact, execution, source relationship, and changed source files. The Stage decision is rendered once at the group level and is never stored or presented as a Cell-owned decision.

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

Temporary manifests snapshot Config and axes so later generated commands reconstruct the same graph. Top-level commands exclude temporary stores unless `--include-temp` is explicit.

## Generated pipeline CLI

Place `raise SystemExit(MyPipeline.cli())` in the pipeline module and run commands through Python:

```bash
python pipeline.py run
python pipeline.py status
python pipeline.py reuse
python pipeline.py invalidate
python pipeline.py plan
python pipeline.py ls
python pipeline.py clean
```

### run

```text
run [--branch NAME] [--override JSON]
    [--only STAGE_SELECTOR | --upto STAGE_SELECTOR | --downstream STAGE_SELECTOR]
    [--slice AXIS=ID] [--force] [--rehash] [--expand | --compact] [--out PATH]
```

`--upto` selects resolved seeds and their upstream closure. `--downstream` selects seeds and descendants. `--only` selects exactly the resolved ordinary Stage, Matrix base, partial subset, or concrete Cell. Before a scoped execution, upstream stages outside the selection must have current successful records and artifacts and must not require Review. `--force` reruns selected concrete stages after the complete preflight succeeds; it never writes a Stage ReviewRecord. `--rehash` ignores persisted stat shortcuts while evaluating inputs, sources, and existing artifacts.

### status

```text
status [STAGE_SELECTOR] [--branch NAME]
    [--expand] [--rehash] [--out PATH]
```

The default is a concise effective-status summary with one `REVIEW` value per logical Stage. For an ordinary Stage or concrete Matrix Cell, `--expand` shows detailed input, artifact, attempt, failure, execution reason, source relationship, the owning Stage Review, and changed source files. For a Matrix base or partial subset, `--expand` shows the selected Cells and renders Review once in the Stage heading.

### reuse and invalidate

```text
reuse [--stage BASE_STAGE]... [--branch NAME] [--out PATH]
invalidate [--stage BASE_STAGE]... [--branch NAME] [--out PATH]
```

Without `--stage`, these commands process every logical Stage with a current Review candidate, including Stages with an earlier decision for the same fingerprint so the decision can be corrected. Repeated base names form a topology-ordered stable union; coordinates and partial selectors fail before the first write. `reuse` preserves an otherwise reusable success or validated partial; `invalidate` maps it to `needs-run · source-changed` and clears stale partial only when execution starts. Repeating the same decision is an idempotent success that preserves `decided_at`, while the opposite decision atomically replaces that Stage record. Both commands validate all observations and targets before writing, bind decisions to the exact current Review fingerprint, never execute a Stage body, and never rewrite success, attempt, failure, partial, artifact fingerprint, or managed artifact state.

### plan and ls

```text
plan [--branch NAME] [--only STAGE_SELECTOR | --upto STAGE_SELECTOR | --downstream STAGE_SELECTOR] [--out PATH]
ls
```

`plan` resolves the selected branch and prints concrete topological order without evaluating keys or executing stages. `ls` shows branch-independent stage templates with `STAGE`, `KIND`, `NEEDS`, and `MATRIX` and does not probe the store.

### clean

```text
clean [--branch NAME] [--downstream STAGE_SELECTOR] [--out PATH] [--yes]
```

Without `--downstream`, clean removes the complete selected output root after confirmation. With a selector, it deletes recorded artifacts and store state for the concrete seeds and their descendants. Clean does not infer ownership from filenames; it relies on manifest anchors and recorded paths.

All commands accept the global `-v` or `--verbose` flag before the command. Generated Args flags are available on `run`, `status`, `reuse`, `invalidate`, and `clean`.

## Top-level CLI

The installed `varve` command discovers existing branch stores from manifests. MODULE is the exact persisted Python module shown by the first column of `varve ls`. Single commands with dynamic Args require MODULE immediately after the command: `COMMAND MODULE [OPTIONS]`.

```bash
varve ls [MODULE]
varve status MODULE [--stage STAGE_SELECTOR]
varve run MODULE | varve run --all
varve reuse MODULE [--stage BASE_STAGE]... | varve reuse --all
varve invalidate MODULE [--stage BASE_STAGE]... | varve invalidate --all
varve plan MODULE
varve clean MODULE
```

`varve ls` exact-evaluates each selected entry through the shared status collector and one command observation session. `--prefix`, `--branch`, and `--include-temp` filter discovery before import and evaluation; repeatable `--status` filters effective rows afterward. A discovery scope with no entries returns 1, while a successful evaluation whose status filter matches no rows returns 0. The overview displays complete MODULE selectors with `BRANCH` and effective `STATUS`; wide terminals add duration and last run, while narrow terminals use stacked rows instead of truncating MODULE. Manifest, import, resolve, and evaluate errors occupy rows without stopping later entries.

`varve ls MODULE` is branch-independent and shares the generated `ls` renderer. `status MODULE`, `run MODULE`, `reuse MODULE`, `invalidate MODULE`, `plan MODULE`, and `clean MODULE` restore the existing manifest output identity and call the same single-pipeline services as generated commands. They do not accept `--out`, `--override`, or `--slice`. Top-level status supports one execution selector; top-level `reuse` and `invalidate` support repeatable base Stage targets. Run, status, clean, reuse, and invalidate register the selected pipeline's Args after resolving MODULE; plan and structure listing do not instantiate Args.

`run --all`, `reuse --all`, and `invalidate --all` accept `--root`, `--prefix`, `--branch`, and `--include-temp`; bulk run additionally accepts `--rehash`. Bulk commands use each pipeline's default Args and reject pipeline-specific flags; bulk Review does not accept Stage selection. Each store has its own lock and commit, failures do not stop later entries, and the command returns 1 if any entry failed. Bulk run exact-evaluates each entry, skips hits and complete pipelines blocked only by `needs-review`, runs `needs-run`, `resume`, or `failed` entries, refreshes observations after each attempt, and exact-evaluates final state. It returns 0 when all entries are complete, 2 when `needs-review` is the only incomplete reason, and 1 for failed, error, needs-run, resume, or mixed incomplete results.

## Clean safety and recovery

All clean operations require a valid `.varve/manifest.json` anchor. Full clean rejects dangerous roots such as `/`, the home directory, and the current working directory. Override `Pipeline.clean_roots(config)` to restrict full clean further for a particular pipeline.

Per-stage clean removes only recorded managed artifacts and the selected concrete stages' success, attempt, failure, and partial state. Paths are checked against the branch output root, and Matrix artifacts remain contained by their Cell roots. Targeted ordinary, Matrix base, and partial-selector clean preserve the shared base Stage ReviewRecord because inactive Cells may still need that decision when reactivated. Full store clean removes every ReviewRecord with the output root.

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
