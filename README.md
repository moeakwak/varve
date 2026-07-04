# varve

`varve` is a small Python library for serial experiment orchestration with a
materialized, content-addressed cache.

For the current package layout, cache model, and edge-case behavior, see
[ARCHITECTURE.md](ARCHITECTURE.md). For development rules and dependency
boundaries, see [AGENTS.md](AGENTS.md).

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

Pipeline commands:

- `run [--branch NAME] [--override JSON] [--upto STAGE | --downstream STAGE] [--force] [--out PATH]`
- `status [--branch NAME] [--upto STAGE | --downstream STAGE] [--out PATH]`
- `plan [--upto STAGE | --downstream STAGE]`
- `list`
- `clean [--branch NAME] [--downstream STAGE] [--out PATH] [--yes]`

Dashboard commands:

- `varve ls [--root DIR]`
- `varve show <experiment_id> [--root DIR] [--branch NAME]`
- `varve refresh [--root DIR] [--prefix MODULE_PREFIX]`

Pipelines may define `Args` for execution flags and `Config` for semantic
configuration. `varve.yaml` lives next to the experiment module, `main` is the
default branch, and `run --override '{"field": "value"}'` creates a temporary
branch under `.tmp/`.

Stage code writes through `ctx.out`. varve resolves the output root from
`--out` or `Pipeline.default_output_root(config)`, then appends the selected
branch: `base/<branch>` for persistent branches and `base/.tmp/<branch>` for
temporary branches.

Known limitations:

- Source fingerprints use `ast.dump`, so a CPython minor-version upgrade may
  invalidate stage source hashes.
- If an output is changed outside varve, use `clean` to reset the affected
  stages.
