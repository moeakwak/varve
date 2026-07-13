# varve

[![PyPI](https://img.shields.io/pypi/v/varve.svg)](https://pypi.org/project/varve/) [![License](https://img.shields.io/pypi/l/varve.svg)](LICENSE)

Varve runs Python pipelines and caches their outputs, so re-running only re-executes stages whose deterministic inputs or managed artifacts require it. Python source changes open an explicit review gate instead of silently choosing whether to rerun.

A varve is an annual layer of lake sediment — thin, ordered, and datable. Varve treats pipeline outputs the same way: each stage writes a materialized layer whose input key records config, external inputs, explicit values, and upstream artifact contents. When nothing changes, nothing re-runs; when something does, `status` tells you exactly what and why.

It runs on a single machine — no daemon, no database, no pipeline DSL. Stages are ordinary Python methods, and the `run` / `status` / `accept` / `reject` / `plan` / `ls` / `clean` CLI is generated from your pipeline class. Varve is built for local experiments, evaluations, dataset preparation, render/compare jobs, and report generation, where Python is already the source of truth. It is not a distributed scheduler, a deployment platform, a data-version-control system, or a remote artifact store.

## Install

Varve requires Python 3.10 or newer.

```bash
pip install varve
```

## Quick start

Create `demo.py`:

```python
from pydantic import BaseModel
from varve import Ctx, Pipeline, stage


class Config(BaseModel):
    prefix: str = "hello"


class Demo(Pipeline):
    Config = Config

    @stage(produces="items.txt")
    def prepare(self, ctx: Ctx) -> None:
        (ctx.out / "items.txt").write_text("alpha\nbeta\n")

    @stage(needs="prepare", produces="result.txt")
    def render(self, ctx: Ctx) -> None:
        items = ctx.input("prepare").read_text().splitlines()
        (ctx.out / "result.txt").write_text(
            "\n".join(f"{ctx.config.prefix} {item}" for item in items)
        )


if __name__ == "__main__":
    raise SystemExit(Demo.cli())
```

Run it twice, then look at what happened:

```bash
python demo.py run
python demo.py run
python demo.py status
python demo.py plan
```

The first run executes both stages and records their keys and artifacts under `out/main/`. The second run is a straight cache hit. Changing `prefix`, changing a declared external input, or deleting `result.txt` makes the affected stage non-current for a reason `status` will name. Editing Python source opens a review gate that must be resolved with `accept` or `reject` before execution.

## How it works

Each stage declares upstream stages with `needs=`, non-stage dependencies with `depends=`, and outputs with `produces=`. Varve builds an input key from Config, declared inputs and values, and the actual fingerprints of upstream artifacts.

Running a stage writes a record into `<output-root>/.varve/`: the committed key plus the output paths, relative to the branch root. The next command recomputes the key and checks that the recorded artifacts still exist — a matching key is not a hit if the file it points to is gone. The store is latest-wins and guarded by an output-root lock.

Inside a stage, `ctx.input("prepare")` returns the single artifact of an upstream and `ctx.inputs("prepare")` returns all of them in deterministic order. Both require the name to appear in `needs=`.

## Core capabilities

### Source review and deterministic inputs

Varve observes the Python files that define the pipeline and stage, plus paths declared with `Dependencies.sources`. A source change opens a `needs-review` gate: use `accept` when existing artifacts remain valid or `reject` when the stage must rerun. The relationship remains `changed`; the decision determines whether the effective status is reusable or `needs-run · source-changed`. Varve does not infer call graphs. External data and explicit values belong in `Dependencies.inputs` and `Dependencies.values`.

### Resumable batch stages

A `@batch_stage` iterates deterministic work through `ctx.resume(...)` and yields the files each item produces. Interrupt a run and the next one skips the indexes that already finished, continuing the same keyed batch. Varve schedules stages serially; a stage body is still free to use asyncio, process pools, or long-lived clients internally.

### Matrix stages

Stack `@matrix(...)` over a stage to expand a Cartesian product of axes into independently keyed cells. Shared axes align dependencies automatically; axes that exist only upstream become a deterministic fan-in.

```python
from varve import Axis, Ctx, Pipeline, matrix, stage

BENCH = Axis("bench", ["ocrbench", "unimer"])
MODEL = Axis("model", ["small", "large"])


class Evaluation(Pipeline):
    Config = Config

    @matrix(BENCH, MODEL)
    @stage(produces="score.json")
    def score(self, ctx: Ctx, *, bench: str, model: str) -> None:
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        evaluate(bench, model, ctx.cell_out / "score.json")
```

Each cell gets a concrete identity like `score@bench=unimer,model=large` and its own artifact directory under `.matrix/score/bench=unimer/model=large/`. A StageSelector may name the base, a partial subset such as `score@bench=unimer`, or one concrete cell; omitted axes are wildcards and canonical output follows declaration order. Large matrices fold to one line per base stage in `run` and `status` output — keeping concrete failures and slow cells visible — and `--expand` shows every cell.

### Branches and temporary runs

An optional `varve.yaml` splits a branch's semantic `config` from its active matrix `axes`. Persistent branches materialize under `out/<branch>/`. `run --override JSON` spins up an isolated throwaway branch under `out/.tmp/<branch>/`, snapshotting both the validated Config and the active axes so generated commands and top-level commands with `--include-temp` can find it later.

### Generated and top-level CLIs

Every `Pipeline` gets seven commands:

| Command | Purpose |
| --- | --- |
| `run` | Evaluate cache decisions and execute the selected stages. |
| `status` | Explain each stage's input key, materialization, artifact, and source-review state. |
| `plan` | Print the selected concrete topology without executing it. |
| `ls` | Show branch-independent stage templates and matrix axes. |
| `clean` | Safely remove a whole output root or a recorded downstream closure. |
| `accept` | Mark current source changes as not requiring a rerun. |
| `reject` | Mark current source changes as requiring a rerun. |

Generated `accept` and `reject` default to every source-changed stage in the pipeline; repeat `--stage STAGE_SELECTOR` for a narrower union. A normal `run` stops before executing any stage when its selection or required external upstreams contain `needs-review`. `run --force` validates the complete preflight, records `reject` for source-changed stages inside the execution selection, and then runs them; source-current stages in the force plan do not gain a persisted force intent.

The top-level `varve` command finds existing stores by manifest exact module. `varve ls` exact-evaluates the discovered branches and reports `MODULE`, `BRANCH`, and effective `STATUS`; it does not expose separate review or stage-count columns. `varve ls MODULE`, `status MODULE`, `run MODULE`, `accept MODULE`, `reject MODULE`, `plan MODULE`, and `clean MODULE` reuse the generated command backends and renderers. Single dynamic commands use `COMMAND MODULE [OPTIONS]`, so MODULE precedes pipeline-specific Args flags.

`varve run --all`, `accept --all`, and `reject --all` operate on entries selected by `--root`, `--prefix`, `--branch`, and `--include-temp`; bulk run also accepts `--rehash`. Bulk review uses each pipeline's default Args and records each store independently. Bulk run skips hits and `needs-review`, executes eligible branches, refreshes observations after every attempt, and reports exact final state. Use repeatable `varve ls --status STATUS` to filter evaluated rows.

## Documentation

- [User guide](docs/GUIDE.md): stage authoring, keys, batch resume, matrix, branches, CLI behavior, dashboard, and recovery.
- [Architecture](docs/ARCHITECTURE.md): package boundaries, store invariants, graph expansion, probing, and implementation decisions.
- [Contributing](CONTRIBUTING.md): setup, public API policy, style, commits, and releases.
- [Changelog](CHANGELOG.md): released behavior and migration notes.

## Platform and stability

Varve is currently Unix-only, because output locking uses `fcntl`. Source fingerprints use `ast.dump`, so a CPython minor-version upgrade may rebuild cached stages.

Varve follows SemVer, but 0.x releases are alpha: a minor release may change the public API or the `.varve/` store schema, so check the changelog before upgrading.

## License

MIT. See [LICENSE](LICENSE).
