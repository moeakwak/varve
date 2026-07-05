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

- Public API: `Pipeline`, `@stage`, `@batch_stage`, `KeySpec`, `Ctx`, `JSON`, and `StageSpec`.
- Generated pipeline commands:
  - `run [--branch NAME] [--override JSON] [--upto STAGE | --downstream STAGE] [--force] [--out PATH]`
  - `status [--branch NAME] [--upto STAGE | --downstream STAGE] [--out PATH]`
  - `plan [--upto STAGE | --downstream STAGE]`
  - `list`
  - `clean [--branch NAME] [--downstream STAGE] [--out PATH] [--yes]`
- `run`, `status`, and `clean` also accept generated flags from the pipeline's `Args` model.
- Cache states for hits, stale records, missing artifacts, dirty attempts, resumable batches, and stages with no cache record.
- `ctx.input(...)`, `ctx.inputs(...)`, and `ctx.resume(...)` for stage bodies.
- `KeySpec.files` for pinning input file contents into the content key.

## Branches

`varve.yaml` lives next to the pipeline module. The `main` branch is the default and may rely entirely on Config defaults when the file is missing.

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
