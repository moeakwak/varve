# varve

[![PyPI](https://img.shields.io/pypi/v/varve.svg)](https://pypi.org/project/varve/) [![License](https://img.shields.io/pypi/l/varve.svg)](LICENSE)

Varve runs Python-defined pipelines with code-aware materialized caching. Each stage is a Python method, the run/status/plan/list/clean CLI is generated for you, and every output is cached under a key derived automatically from your code, config, and pinned inputs, so re-runs only re-execute what actually changed. Single machine, no daemon, no pipeline YAML.

A varve is an annual layer of lake sediment: thin, ordered, and datable. This library uses the same idea for pipeline outputs: materialized layers whose keys record the code, config, inputs, and upstream layers that produced them.

For the package layout, cache model, and edge-case behavior, see [ARCHITECTURE.md](ARCHITECTURE.md). For contribution guidance, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start

```python
from pydantic import BaseModel
from varve import Pipeline, stage


class Config(BaseModel):
    seed: int = 1


class Demo(Pipeline):
    Config = Config

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(str(ctx.config.seed))


if __name__ == "__main__":
    raise SystemExit(Demo.cli())
```

Run the pipeline:

```bash
python demo.py run
python demo.py status
python demo.py plan
python demo.py list
python demo.py clean --yes
```

By default, outputs are written next to the pipeline module under `out/main/`. Use `--out PATH` to choose a different output base.

## Batch stages

Use `@batch_stage` for resumable batch work. The stage iterates through `ctx.resume(...)`, writes one or more files per item, and yields the paths it produced. If a run fails halfway through, the next run skips completed batch indexes and continues from the remaining items.

```python
from pathlib import Path

from pydantic import BaseModel
from varve import Ctx, Pipeline, batch_stage, stage


class Config(BaseModel):
    batch_size: int = 100


class Args(BaseModel):
    progress: bool = True


class Demo(Pipeline):
    Config = Config
    Args = Args

    @stage(produces="items.txt")
    def prepare(self, ctx: Ctx[Config, Args]) -> None:
        (ctx.out / "items.txt").write_text("alpha\nbeta\ngamma\n")

    @batch_stage(needs="prepare")
    async def process(self, ctx: Ctx[Config, Args]):
        items = ctx.input("prepare").read_text().splitlines()
        async for index, item in ctx.resume(items, progress=ctx.args.progress):
            path = ctx.out / "parts" / f"{index:04d}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item.upper())
            yield path

    @stage(needs="process", produces="summary.txt")
    def summarize(self, ctx: Ctx[Config, Args]) -> None:
        parts = [path.read_text() for path in ctx.inputs("process")]
        (ctx.out / "summary.txt").write_text("\n".join(parts))
```

`ctx.input("stage")` returns exactly one upstream output path and fails if the stage produced zero or many paths. `ctx.inputs("stage")` always returns `list[Path]`. Both require the upstream stage to be declared in `needs=`, so the upstream content key is part of the downstream cache key.

`needs=` accepts stage names as strings or method references defined earlier in the class body, such as `@stage(needs=prepare)`. Strings are usually clearer across inheritance boundaries.

Batch resume is index-based: varve records completed positions from `ctx.resume(...)` and skips those positions on the next run. The iterable order must therefore be deterministic for resume correctness. If source order is unstable, sort it before passing it to `ctx.resume(...)`; varve does not provide order-independent batch resume.

Batch stages run serially at the varve level so partial writes stay simple and deterministic. If each item can use parallelism internally, use normal Python tools such as `asyncio.gather(...)`, a process pool, or a long-lived worker/session inside the batch stage body.

Varve warns when a batch stage yields outputs without first iterating `ctx.resume(...)`, because those outputs cannot be resumed safely. Such stages may still complete successfully, but varve treats them as non-resumable: failed runs do not leave resumable partial state, and later runs start from the stage body instead of recorded batch positions. For resumable batches, a batch item may yield zero paths; varve records the completed index but does not validate item-level completeness.

## Matrix stages

Use `Axis` and stack `@matrix(...)` above `@stage` or `@batch_stage` to turn a Cartesian product into independently keyed cells. Coordinates are injected as typed keyword-only parameters. Shared axes align dependencies automatically; axes present only upstream become a deterministic fan-in.

```python
from varve import Axis, Ctx, Pipeline, matrix, stage

BENCH = Axis("bench", ["ocrbench_v2", "unimer"])
MODEL = Axis("model", ["qwen3-vl-8b", "internvl3-8b"])

class Evaluation(Pipeline):
    Config = Config

    @matrix(BENCH)
    @stage(produces="ground-truth.parquet")
    def prepare(self, ctx: Ctx, *, bench: str) -> None:
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        write_ground_truth(bench, ctx.cell_out / "ground-truth.parquet")

    @matrix(BENCH, MODEL)
    @stage(needs="prepare", produces="score.json")
    def score(self, ctx: Ctx, *, bench: str, model: str) -> None:
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        score_one(bench, model, ctx.input("prepare"), ctx.cell_out / "score.json")

    @stage(needs="score", produces="summary.json")
    def summarize(self, ctx: Ctx) -> None:
        write_summary(ctx.inputs("score"), ctx.out / "summary.json")
```

Each cell is a concrete stage such as `score@bench=unimer,model=qwen3-vl-8b`. Its managed artifact directory is `<output-root>/.matrix/score/bench=unimer/model=qwen3-vl-8b/`: the base stage comes first, followed by one `<axis-name>=<canonical-id>` level for each axis in declaration order. This physical layout does not change the concrete stage or store identity. Matrix artifacts must live under `ctx.cell_out`; relative declared and yielded paths resolve there. `ctx.out` remains the branch output root, and ordinary stages keep `ctx.cell_out == ctx.out`. Key getters can read coordinates through `ctx.cell.model` or `ctx.cell["model"]`.

Use `--only score` to select every active cell of one base stage. `--slice model=qwen3-vl-8b` selects matching cells plus their aligned upstream closure and is accepted only for temporary branches. Matrix cells remain serial at the varve scheduler level.

`run` uses an `auto` matrix display policy for its plan, live log, and outcome table. A selected matrix group with at least 8 cells is folded into one base-stage summary unless any selected cell has a previous successful duration of at least 30 seconds; groups with 7 or fewer selected cells and groups with known slow cells stay expanded. A folded group prints a bounded start line and a completion summary with status counts, executed-cell count, and total execution time. Cache hits and short successful cells do not print individual info lines, while `-v` retains concrete lifecycle and content-key diagnostics. Slow cells and failures always identify the full concrete cell. Use `run --expand` to force every concrete cell or `run --compact` to force matrix-group summaries; the flags are mutually exclusive and do not affect execution, selection, cache keys, or artifact identities.

For a matrix batch stage, the default `ctx.resume()` progress description uses only canonical coordinate values in axis declaration order, such as `ocrbench-v2-formula / qwen3-vl-8b-instruct`, instead of repeating the full concrete stage name. An explicit `desc=` is used unchanged, and ordinary stages retain their stage name as the default.

`status` folds matrix cells by base stage by default. Each matrix row reports the most severe cell status, a stable status-count distribution, the sum of recorded cell durations (with the recorded fraction when any duration is missing), and logical `needs`. `status score` shows the same one-row group summary; add `--expand` to render one table per base stage with a separate column for every axis. Select a concrete name such as `status score@bench=unimer,model=qwen3-vl-8b` for the full single-cell view, where `--expand` and `--all` retain their source-dependency meanings.

## Why varve

Varve is for pipelines where Python code is already the best source of truth. It is intentionally closer to a small library such as redun, Hamilton, or pydoit than to a workflow platform.

It is designed for local experiment, research, and data-processing workflows: dataset preparation, evaluation runs, render/compare batches, generated reports, and other repeatable jobs that need materialized outputs without a service.

Unlike DVC, varve is not data version control. Unlike Snakemake, it does not introduce a separate DSL. Unlike Prefect, Dagster, or Airflow, it has no scheduler service, worker fleet, or deployment model.

The core design choices are:

- **Pipelines are Python code.** Stages are instance methods, dependencies are declared with `needs=`, and semantic configuration is a pydantic model.
- **Cache keys are code-aware by default.** Varve fingerprints stage source, automatically discovered project callables, full Config values, declared input files, declared JSON values, and upstream content keys.
- **Outputs are materialized.** Successful stage records point at durable files under the output root, so missing artifacts are detected instead of silently treated as cache hits.
- **Single machine, no service.** Varve uses an in-process runner and a file-system store. There is no daemon, database, or remote backend.

## Features

- Public API: `Pipeline`, `@stage`, `@batch_stage`, `Axis`, `@matrix`, `KeySpec`, `Ctx`, `JSON`, and `StageSpec`.
- Generated pipeline commands:
  - `run [--branch NAME] [--override JSON] [--only STAGE | --upto STAGE | --downstream STAGE] [--slice AXIS=ID] [--force] [--expand | --compact] [--out PATH]`
  - `status [STAGE] [--branch NAME] [--expand | --all | --deps | --deps-all] [--out PATH]`
  - `plan [--branch NAME] [--only STAGE | --upto STAGE | --downstream STAGE]`
  - `list`
  - `clean [--branch NAME] [--downstream STAGE] [--out PATH] [--yes]`
- `run`, `status`, and `clean` also accept generated flags from the pipeline's `Args` model.
- Cache states for hits, stale records, missing artifacts, dirty attempts, resumable batches, and stages with no cache record.
- `list`, `status`, and the `run` summary print as color-coded aligned tables, and the live run log stamps every line with its own timestamp and status colors. `refresh` prefixes each pipeline header with a `▸` accent so its stage lines read as a group. Color is dropped automatically when output is not a terminal.
- `ctx.input(...)`, `ctx.inputs(...)`, and `ctx.resume(...)` for stage bodies.
- `KeySpec.files` for pinning input file contents into the content key.

## Source dependencies

`needs`, `uses`, `auto_uses`, `auto_uses_packages`, and `KeySpec` describe different relationships. `needs` declares stage execution and data dependencies. `uses` declares Python functions or classes whose source must be part of a stage key. `auto_uses` enables best-effort positive discovery from the stage and explicit `uses` roots. `auto_uses_packages` replaces the default inferred package scope; `None` uses the stage's top-level package and `()` disables inferred package recursion. `KeySpec` pins files and explicit values that are outside Python source discovery.

```python
class Demo(Pipeline):
    Config = Config
    auto_uses_packages = ("my_project", "shared_tools")

    @stage(uses=[external_helper])
    def normalize(self, ctx):
        return external_helper(ctx.config)
```

Automatic discovery follows references that can be resolved directly from Python bytecode and the function environment: project globals, closures, defaults, nested code objects, and simple `module.attr` reads. Functions are followed transitively within the configured packages. Project classes are hashed as a whole, including dependencies referenced by their owned methods and project base classes. A directly read project module may conservatively contribute its single source file.

Discovery is always non-blocking and never claims to find a complete call graph. Dynamic calls, registries, `getattr`, factories, parameter type propagation, runtime dispatch, and sibling Pipeline methods reached through `self` are not inferred. Use `uses` or `KeySpec` whenever those inputs need a strict cache guarantee. Stage source, explicit `uses`, and explicit `KeySpec` inputs remain strict even when automatic discovery is enabled.

Use `status` to inspect cache state and the source dependencies included in each decision key. The default summary folds matrix cells and source dependencies, limits long NEEDS lists, and shows DURATION from the most recent successful stage execution for usable cache states. For an ordinary stage or concrete matrix cell, `--expand` adds one source dependency level and `--all` renders the complete discovered DAG, preserving the behavior of non-matrix pipelines. `--deps` and `--deps-all` are explicit equivalents that require exactly one ordinary stage or concrete cell. For a matrix base, `--expand` instead renders its cells and `--all` asks you to select a concrete cell first; an untargeted `--expand` renders all matrix groups when the pipeline contains a matrix, while a non-matrix pipeline continues to expand source dependencies for all stages. Dependency-expanded views also show the decision and stored keys. For stale stages, they mark changed and added source dependencies inline and list dependencies removed since the stored run. Automatically inferred dependencies are left unmarked, while dependencies declared through `uses` retain an `[explicit]` label.

```bash
python demo.py status
python demo.py status normalize
python demo.py status normalize --expand
python demo.py status normalize --all
python demo.py status normalize --deps  # explicit equivalent to --expand
```

## Branches

`varve.yaml` lives next to the pipeline module. The `main` branch is the default and may rely entirely on Config defaults when the file is missing.

Branch sections separate behavior config from the active matrix domain:

```yaml
main:
  config:
    bootstrap_b: 1000
  axes:
    model: [qwen3-vl-8b, internvl3-8b]

full: {}
```

An omitted axis uses its complete declared domain. Axis ids must come from the corresponding `Axis`; their order always follows the declaration. The previous flat branch Config format is intentionally rejected—move those fields under `config:`.

Varve resolves the output root from `--out` or `Pipeline.default_output_root(config)`, then appends the selected branch:

```text
out/<branch>        # persistent branches
out/.tmp/<branch>   # temporary override branches
```

Use `run --override '{"field": "value"}'` to deep-merge JSON over `main` and create a temporary branch. `status` and `clean` locate that branch later with `--branch NAME`.

## Dashboard

The top-level `varve` command discovers existing stores without requiring a custom dashboard entrypoint:

```bash
varve ls [--root DIR] [--include-temp]
varve show <pipeline_id> [--root DIR] [--branch NAME] [--include-temp]
varve refresh [--root DIR] [--prefix MODULE_PREFIX] [--include-temp]
```

Dashboard commands are secondary tooling. The primary interface remains each pipeline's generated CLI.

## Platform support

Varve is currently Unix-only. The output-root lock uses `fcntl`, so Windows support requires a future lock implementation.

Source fingerprints use `ast.dump`. A CPython minor-version upgrade may invalidate stage source hashes and rebuild caches.

## API stability

Varve follows SemVer, but 0.x releases are alpha releases. Minor releases may include breaking changes to the public API or to the `.varve/` store schema. Read `CHANGELOG.md` before upgrading.

## Non-goals

- Remote storage or data version control.
- Distributed scheduling or cluster execution.
- Workflow platform, server, or DAG visualization service.
- Cross-pipeline lineage or observability platform.

## License

MIT. See [LICENSE](LICENSE).
