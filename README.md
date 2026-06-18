# varve

`varve` is a small Python library for serial experiment orchestration with a materialized, content-addressed cache. It is intentionally thin: experiments own their output paths and file formats, while varve owns the ledger that records which stage successfully produced which durable artifacts for a given content key.

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
- `clean [TARGET] --yes`: remove ledger records and artifacts.

## Known limitations

- varve hashes declared key inputs and source ASTs, not arbitrary business artifacts or undeclared helper dependencies. If an output is changed outside varve, use `clean` to reset the affected stages.
- Source AST fingerprints are derived from `ast.dump`, whose output format can change between CPython minor versions. Upgrading the Python interpreter may therefore invalidate every stage's source hash at once, forcing a full rebuild. Run `clean` to reset after such an upgrade.
