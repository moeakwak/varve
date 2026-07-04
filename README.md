# varve

[![PyPI](https://img.shields.io/pypi/v/varve.svg)](https://pypi.org/project/varve/) [![License](https://img.shields.io/pypi/l/varve.svg)](LICENSE)

Varve is a small Python library for running experiment pipelines as code. Each stage is a Python method, the run/status/plan/list/clean CLI is generated for you, and every output is cached under a key derived automatically from your code, config, and pinned inputs, so re-runs only re-execute what actually changed. Single machine, no daemon, no pipeline YAML.

For the package layout, cache model, and edge-case behavior, see [ARCHITECTURE.md](ARCHITECTURE.md). For contribution guidance, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start

```python
from pathlib import Path

from pydantic import BaseModel
from varve import Pipeline, stage


class Config(BaseModel):
    seed: int = 1


class Demo(Pipeline):
    Config = Config

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(str(ctx.config.seed))

    @classmethod
    def default_output_root(cls, config):
        return Path("out")


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

## Why varve

Varve is for research and data-analysis pipelines where Python code is already the best source of truth. It is intentionally closer to a small library such as redun, Hamilton, or pydoit than to a workflow platform.

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
- Cache states for hits, stale records, missing artifacts, dirty attempts, resumable batches, and unrecoverable partition changes.
- `ctx.resume(...)` for resumable batch stages.
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
