# varve

`varve` is a small Python library for serial experiment orchestration with a materialized, content-addressed cache. It is intentionally thin: experiments own their output formats and default output-root policy, while varve owns output-root resolution, `ctx.out`, and the store that records which stage successfully produced which durable artifacts for a given content key.

For maintainers, see [ARCHITECTURE.md](ARCHITECTURE.md) for the current package layout and cache model, and [AGENTS.md](AGENTS.md) for development rules and dependency boundaries.

```python
from pathlib import Path
from pydantic import BaseModel
from varve import Experiment, stage

class Config(BaseModel):
    seed: int = 1

class Demo(Experiment):
    Config = Config

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("result/demo")

    @stage(produces="sample.txt", key=["seed"])
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(str(ctx.config.seed))

if __name__ == "__main__":
    raise SystemExit(Demo.cli())
```

Commands:

- `run [TARGET] [--out PATH]`: run the selected stage set, using cached artifacts when valid.
- `status [TARGET] [--out PATH]`: show cache state without executing stages.
- `plan`: print the stage order or graph.
- `list`: list declared stages.
- `clean [TARGET] [--out PATH] --yes`: remove store records and artifacts.

Top-level dashboard:

- `varve ls [--root DIR]`: scan a directory tree for varve stores and print an overview.
- `varve show <experiment_id> [--root DIR]`: print one discovered store's stage details and
  recorded dependency edges.

The dashboard is read-only and never imports experiment modules. It discovers experiments by
looking for `.varve/manifest.json` under the scan root. The `experiment_id` is the output root's
path relative to the scan root with path separators replaced by dots, such as
`analysis.transform_phases.rewrite_reach`; it is not a Python import path.

Dashboard status is a store snapshot, not a dry-run cache decision. It only includes stages that
have records in the store, and it does not recompute content keys, so it cannot report that source
or key inputs changed after the last run.

## Output paths

Stage outputs are anchored at the experiment output root (`ctx.out`). Static
`@stage(produces=...)` entries are interpreted relative to `ctx.out`.

The output root is not a `Config` field. `run`, `status`, and `clean` resolve it
by taking explicit `--out PATH` when provided, otherwise calling
`Experiment.default_output_root(config)`, then passing the base path through
`Experiment.resolve_output_root(base, config)`. Stage code should write through
`ctx.out`; helper functions that create sidecars should receive `ctx.out` from
their stage instead of reading an output path from `ctx.config`.

Batch stages record the paths they yield. Yield either an absolute path under
`ctx.out`, or a path relative to `ctx.out`. Relative batch output paths are not
interpreted relative to the current working directory.

Batch stages get one overall `tqdm` progress bar for the resumed iterable by
default. The bar is labeled with the stage name and seeds its initial count
from already-completed indexes, so resumed runs do not restart from zero:

```python
async for index, item in ctx.resume(items):
    ...
```

Pass `progress=False` to disable the bar, `desc=...` to override the label,
`unit=...` to change the counted noun, `total=...` when the iterable has no
`len()`, and `postfix=lambda item: ...` to annotate the bar with per-item
context.

## Configuration sources

`run`, `status`, and `clean` build an experiment `Config` from multiple sources, in priority order:

```text
CLI flag > env > dotenv (.env) > yaml (--config) > field default
```

Nested environment variables use `__` as the delimiter, for example `INNER__NAME` for `inner.name`.

The CLI is strict: unknown options fail instead of being ignored. Config flags are generated at runtime from the experiment `Config`, while `--out` is a built-in command option owned by varve rather than a generated Config flag. This is why varve keeps an `argparse` front-end rather than introducing typer or click.

## Known limitations

- varve hashes declared key inputs and source ASTs, not arbitrary business artifacts or undeclared helper dependencies. If an output is changed outside varve, use `clean` to reset the affected stages.
- Source AST fingerprints are derived from `ast.dump`, whose output format can change between CPython minor versions. Upgrading the Python interpreter may therefore invalidate every stage's source hash at once, forcing a full rebuild. Run `clean` to reset after such an upgrade.
