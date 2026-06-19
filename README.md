# varve

`varve` is a small Python library for serial experiment orchestration with a materialized, content-addressed cache. It is intentionally thin: experiments own their output paths and file formats, while varve owns the store that records which stage successfully produced which durable artifacts for a given content key.

For maintainers, see [ARCHITECTURE.md](ARCHITECTURE.md) for the current package layout and cache model, and [AGENTS.md](AGENTS.md) for development rules and dependency boundaries.

```python
from pathlib import Path
from pydantic import BaseModel
from varve import Experiment, stage

class Config(BaseModel):
    out: Path
    seed: int = 1

class Demo(Experiment):
    Config = Config

    @stage(produces="sample.txt", key=["seed"])
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(str(ctx.config.seed))

if __name__ == "__main__":
    raise SystemExit(Demo.cli())
```

Commands:

- `run [TARGET]`: run the selected stage set, using cached artifacts when valid.
- `status [TARGET]`: show cache state without executing stages.
- `plan`: print the stage order or graph.
- `list`: list declared stages.
- `clean [TARGET] --yes`: remove store records and artifacts.

## Configuration sources

`run`, `status`, and `clean` build an experiment `Config` from multiple sources, in priority order:

```text
CLI flag > env > dotenv (.env) > yaml (--config) > field default
```

Nested environment variables use `__` as the delimiter, for example `INNER__NAME` for `inner.name`.

The CLI is strict: unknown options fail instead of being ignored. Config flags are generated at runtime from the experiment `Config`, so varve keeps an `argparse` front-end rather than introducing typer or click.

## Known limitations

- varve hashes declared key inputs and source ASTs, not arbitrary business artifacts or undeclared helper dependencies. If an output is changed outside varve, use `clean` to reset the affected stages.
- Source AST fingerprints are derived from `ast.dump`, whose output format can change between CPython minor versions. Upgrading the Python interpreter may therefore invalidate every stage's source hash at once, forcing a full rebuild. Run `clean` to reset after such an upgrade.
