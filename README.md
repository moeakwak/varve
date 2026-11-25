# varve

[![PyPI](https://img.shields.io/pypi/v/varve.svg)](https://pypi.org/project/varve/) [![License](https://img.shields.io/pypi/l/varve.svg)](LICENSE)

Varve runs Python pipelines on one machine and caches every output it materializes. A pipeline is a plain Python class; its stages are methods. Re-running executes only the units whose inputs, upstream artifacts, Config fields, or source code actually changed, and `status` tells you which ones and why.

A varve is an annual layer of lake sediment: thin, ordered, and datable. Varve treats stage outputs the same way. Each unit of work writes one read-only layer whose key records what it was built from.

What it is:

- Zero daemon, zero services. State lives in one SQLite file per pipeline (`.varve/state.db`); commands are short-lived processes.
- Pure-Python definitions. No DSL, no YAML graph. Dependencies are declared by parameter names, so editors and type checkers see ordinary methods.
- One model from a 20-line script to thousands of shards. A stage without axes is one unit; `foreach=` expands it into static cells, resolver-driven shards (including shards discovered from an upstream stage's output), or both, scheduled with async, thread, or process-pool windows.

What it is not: a distributed scheduler, a workflow server, a data-version-control system, or a remote artifact store.

## Install

Varve requires Python 3.10 or newer and a POSIX system.

```bash
pip install varve           # core library and CLIs
pip install 'varve[ui]'     # plus the local read-only web UI (Starlette + Uvicorn)
```

## Quick start

Create `demo.py`:

```python
from pathlib import Path

from pydantic import BaseModel

from varve import Ctx, Pipeline, stage


class Config(BaseModel):
    prefix: str = "hello"


class Demo(Pipeline):
    Config = Config

    @stage(produces="items.txt")
    def prepare(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "items.txt").write_text("alpha\nbeta\n")

    @stage(produces="result.txt", publish=True)
    def render(self, ctx: Ctx[Config], prepare: Path) -> None:
        items = prepare.read_text().splitlines()
        lines = [f"{ctx.config.prefix} {item}" for item in items]
        (ctx.out / "result.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(Demo.cli())
```

```bash
python demo.py run      # executes prepare, then render
python demo.py run      # both stages are hits; nothing executes
python demo.py status   # per-stage decisions
cat out/main/result.txt
```

`render` names `prepare` as a parameter, which makes it a dependency and injects the path of `prepare`'s artifact. Each stage writes into its private `ctx.out` directory; varve verifies `produces=`, fingerprints the files, makes them read-only, and moves them to `out/.internal/main/<stage>/`. `publish=True` hardlinks `result.txt` into the human-facing `out/main/`.

Varve records which `Config` fields each stage reads. Changing `prefix` (in `varve.yaml`, the environment, or the default) reruns `render` only; `prepare` never read it. Editing `render`'s body reruns `render` with reason `source-changed`; editing an unrelated helper in the same file reruns nothing. Deleting a managed file reruns its unit with `artifact-missing`.

## A short tour

```python
import json
from pathlib import Path

from pydantic import BaseModel

from varve import Axis, Ctx, Pipeline, stage


class Config(BaseModel):
    data_dir: Path = Path("data")


class Args(BaseModel):
    workers: int = 2


# A static axis: a fixed, ordered domain.
MODEL = Axis("model", ["small", "large"])

# A dynamic axis: resolved from Config at probe time; each unit receives one
# path, and only that file (by size and mtime) enters the unit's key.
SHARD = Axis(
    "shard",
    resolver=lambda ctx: {p.stem: p for p in sorted(ctx.config.data_dir.glob("*.txt"))},
    input_fingerprint="stat",
)


class Tour(Pipeline):
    Config = Config
    Args = Args

    @stage(foreach=SHARD, produces="words.json", workers=lambda ctx: ctx.args.workers)
    def tokenize(self, ctx: Ctx[Config, Args], shard: Path) -> None:
        words = shard.read_text().split()
        (ctx.out / "words.json").write_text(json.dumps(words))
        ctx.metric(items=len(words))

    # Shares SHARD with tokenize: 1-to-1, so `tokenize` is one file.
    @stage(foreach=(MODEL, SHARD), produces="score.json")
    def score(self, ctx: Ctx, model: str, tokenize: Path) -> None:
        words = json.loads(tokenize.read_text())
        factor = 1 if model == "small" else 2
        (ctx.out / "score.json").write_text(json.dumps(factor * len(words)))

    # Drops SHARD: fan-in, so `score` is {shard_key: path} for this model.
    @stage(foreach=MODEL, produces="total.json", publish={"total.json": "{model}.json"})
    def total(self, ctx: Ctx, model: str, score: dict[str, Path]) -> None:
        total = sum(json.loads(path.read_text()) for path in score.values())
        (ctx.out / "total.json").write_text(json.dumps(total))


if __name__ == "__main__":
    raise SystemExit(Tour.cli())
```

- **Units.** `tokenize` has one unit per file, `score` one per (model, shard), `total` one per model. Each unit has its own key, record, and artifact directory such as `out/.internal/main/score/model=large/shard=s2/`. Adding a file runs only the new units; modifying one reruns its `tokenize` unit, and downstream units only if that unit's output bytes changed; removing one marks its units `orphaned` (reclaimed by `clean --orphans`).
- **Alignment.** Upstream and downstream units are matched through the `Axis` objects they share; axes only the upstream has are folded into an ordered `dict` (fan-in).
- **Concurrency.** `workers=2` on a plain `def` stage runs units in a `spawn` process pool; `async def` stages run in an asyncio window; `backend="thread"` uses threads. `Args` fields become CLI flags (`run --workers 8`) and never enter keys.
- **Upstream-driven shards.** A resolver may take an upstream stage as a parameter, for example one that writes `batches.json`: the stage stays `awaiting` until that upstream is done, then expands into one unit per batch file, and each unit keys on its own batch file only.
- **Resources.** `Resource(name, depends=..., preflight=..., open=..., scope=...)` packages a session (an engine, a server endpoint, a client) together with the files and sources it depends on. Stages receive the opened session by parameter name; nothing is opened when every unit is a hit.
- **Cross-pipeline inputs.** `counts = Ref("corpus", stage="count")` on a pipeline class injects another pipeline's committed artifacts like an upstream stage, aligned by axis name, read from the upstream `state.db` without importing it. `varve run` orders and runs whole chains declared in a `varve.toml`.

## Command line

Every pipeline gets a generated CLI (`python demo.py COMMAND` or `python -m package COMMAND`):

| Command | Purpose |
| --- | --- |
| `run` | Execute every unit that is not a hit (`--only/--upto/--downstream`, `--force`, `--failed-only`, `--slice`, `--sample`, `--temp`, `--override`). |
| `status [STAGE[@AXIS=ID,...]]` | Per-stage decisions with reasons and labels; `--expand` lists units. |
| `plan` | The stage DAG with per-stage decision counts. |
| `ls` | Stage declarations and stored branches. |
| `inspect STAGE[@AXIS=ID,...]` | One unit's paths, key segments that changed, source symbols (`--sources`), captured log (`--log`), and last traceback. |
| `reuse STAGE` | Accept the current source change of a stage without rerunning it. |
| `clean` | Remove managed outputs: the whole store, `--branch`, `--storage`, or `--orphans`. |

The top-level `varve` command works across a workspace: `varve ls` (zero-import overview), `varve plan` / `varve run` (ordered `(pipeline, branch)` runs following `Ref`s, with `--set FIELD=VALUE` routed to pipelines whose `Args` declare it and a printed `--from` resume command on failure), `varve status|inspect|reuse|clean MODULE`, and `varve ui` (requires `varve[ui]`). Set `VARVE_ROOT_BASE=/scratch/dir` to run any pipeline or chain in a scratch location without touching real outputs.

## Documentation

- [User guide](docs/GUIDE.md): the complete author-facing reference.
- [Architecture](docs/ARCHITECTURE.md): package layout, store schema, execution lifecycle, keying, and boundaries.
- [Contributing](CONTRIBUTING.md): setup, public API policy, style, commits, and releases.
- [Changelog](CHANGELOG.md): released behavior and migration notes.

## Platform and stability

Varve is Unix-only (it uses `fcntl` locks and hardlinks). Source fingerprints hash Python ASTs, so a CPython minor-version upgrade may rerun stages.

Varve follows SemVer, but 0.x releases are alpha: a minor release may change the public API or the `.varve/` store schema. There is no store migration; after a schema change, `clean` and rerun.

## License

MIT. See [LICENSE](LICENSE).
