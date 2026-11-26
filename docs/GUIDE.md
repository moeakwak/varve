# Varve User Guide

This guide is the reference for authoring and operating varve pipelines. For a short introduction, start with the [README](../README.md); for implementation structure and invariants, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Contents

- [Concepts](#concepts)
- [Defining a pipeline](#defining-a-pipeline)
- [The `@stage` decorator](#the-stage-decorator)
- [Parameter injection](#parameter-injection)
- [The unit context](#the-unit-context)
- [Axes](#axes)
- [Alignment and fan-in](#alignment-and-fan-in)
- [Outputs: `ctx.out`, `produces`, `publish`](#outputs-ctxout-produces-publish)
- [Roots and storage tiers](#roots-and-storage-tiers)
- [Resources](#resources)
- [Dependencies](#dependencies)
- [Keys and invalidation](#keys-and-invalidation)
- [Statuses, reasons, and labels](#statuses-reasons-and-labels)
- [Concurrency and observability](#concurrency-and-observability)
- [Branches](#branches)
- [Generated pipeline CLI](#generated-pipeline-cli)
- [Cross-pipeline `Ref`](#cross-pipeline-ref)
- [Workspaces: `varve.toml`](#workspaces-varvetoml)
- [Top-level `varve` CLI](#top-level-varve-cli)
- [Relocating roots: `VARVE_ROOT_BASE`](#relocating-roots-varve_root_base)
- [Web UI](#web-ui)
- [Crash recovery and clean safety](#crash-recovery-and-clean-safety)
- [Limitations](#limitations)

## Concepts

- **Stage**: a method of a `Pipeline` subclass decorated with `@stage`.
- **Cell**: the coordinates of a stage's static axes, for example `dataset=alpha`. A stage without static axes has one empty cell. Resources and dependencies may be computed per cell.
- **Unit**: one stage after expanding all of its axes, static and dynamic, for example `label@dataset=alpha,batch=3`. A unit is what varve executes, caches, and commits atomically. A stage without `foreach=` is a single unit.
- **Branch**: a named configuration of the whole pipeline (`main`, branches declared in `varve.yaml`, or temporary branches). Each branch has its own unit records and managed directories.
- **Run**: one execution of `run` for one branch, recorded with per-stage timing.

`status`, `plan`, `inspect`, `reuse`, and `run` first **probe**: they expand stages into units in topological order and compute each unit's decision (`hit`, `needs-run` with a reason, or `failed`). `run` then executes the units that are not hits, stage by stage.

The public API is exactly seven names:

```python
from varve import Axis, Ctx, Dependencies, Pipeline, Ref, Resource, stage
```

## Defining a pipeline

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

`Pipeline.cli(argv=None)` runs the generated CLI and returns the exit code. A package that holds a pipeline usually adds a `__main__.py` so `python -m package COMMAND` works. Guard it with `if __name__ == "__main__":`, because the top-level `varve` command imports `package.__main__` to find the pipeline class when the package itself does not expose it:

```python
from corpus.run import Corpus

if __name__ == "__main__":
    raise SystemExit(Corpus.cli())
```

### Class attributes

| Attribute | Meaning |
| --- | --- |
| `Config` | A pydantic `BaseModel` subclass (or `dict`) holding durable settings. Fields a unit reads through `ctx.config` enter its key. Default: an empty model. |
| `Args` | A pydantic `BaseModel` subclass holding runtime-only options (worker counts, verbosity, scratch paths). Fields become CLI flags and never enter keys. Default: an empty model. |
| `defaults` | Stage defaults, a mapping with any of `storage`, `resources`, `depends`, `workers`, `backend`, `max_failures`. `resources` and `depends` are merged with each stage's own (defaults first); the scalar settings apply when a stage leaves them unset. Other keys are an error. |
| `varve_name` | Overrides the pipeline's selector (see [Top-level `varve` CLI](#top-level-varve-cli)). Must be a dotted path of identifiers. |
| `Ref` attributes | Cross-pipeline inputs, injected by attribute name (see [Cross-pipeline `Ref`](#cross-pipeline-ref)). |

Classmethods you may override: `default_root(config)` (where `.varve/` lives; default: the directory of the module that defines the class), `storage_roots(config)` (named storage roots; default `{}`), and `publish_root(config)` (default: the `default` storage root). See [Roots and storage tiers](#roots-and-storage-tiers).

Stages are collected in definition order, base classes first; a subclass may override a stage and keep its position. A stage name may not shadow a `Pipeline` attribute (`cli`, `graph`, `stages`, ...). `Pipeline.graph()` builds and validates the stage graph once per class and raises `GraphError` for invalid declarations; every command calls it first.

### Config construction

For a pydantic `Config`, each branch's Config is built with pydantic-settings using this priority:

```text
branch value (varve.yaml config: or --override) > environment variable > .env in the cwd > field default
```

Environment variables use the field name (case-insensitive, no prefix); nested fields use `__` as the delimiter. So `PREFIX=hi python demo.py status` reports `render` as `config-changed`. Relative `Path` values are used as written, so they resolve against the process's working directory.

A plain `Config = dict` is also accepted: the branch's `config:` mapping is used as is (string keys only).

During a unit, `ctx.config` is a read-only recording proxy. A pydantic proxy passes `isinstance(ctx.config, Config)` and can be assigned to pydantic fields typed with the Config class; a dict proxy is a real `dict`. Plain top-level field reads are recorded precisely; nested models are returned unwrapped, so a read of `ctx.config.render.dpi` records `render`. Anything that cannot be attributed to one field (`model_dump()`, iteration, `__dict__`, equality, hashing, pickling, `getattr` of an unknown name, `model_copy()`) records the whole Config. Writing to `ctx.config` raises `TypeError`.

### Args

```python
class Args(BaseModel):
    workers: int = 2
```

Each field becomes a flag on `run`, `status`, and `plan` (`--workers 8`). Supported field types: scalars (`str`, `int`, `float`, `Path`, ...), `bool` (`--flag` / `--no-flag`), `Literal[...]` of strings and string `Enum`s (choices), `list[...]` (a JSON value), `Optional[...]` (the token `null` maps to `None`), and nested models (dotted flags such as `--render.dpi`). `dict`, `tuple`, `set`, and other unions are rejected when the parser is built. A field whose flag would collide with a built-in option such as `--branch` gets no flag. Stages read Args through `ctx.args`; `workers=` callables and resources receive them through `RunCtx.args`. Args are not read from the environment.

## The `@stage` decorator

```python
@stage(
    *,
    foreach=None,       # Axis | tuple[Axis, ...]
    needs=None,         # str | stage method | Ref | a sequence of those
    resources=None,     # Sequence[Resource] | Callable[[DepCtx], Sequence[Resource]]
    depends=None,       # Dependencies | Callable[[DepCtx], Dependencies]
    workers=None,       # int | Callable[[RunCtx], int]; default 1
    exclusive=None,     # "axis=id" | "a=x,b=y" | a sequence of those
    backend=None,       # "auto" | "async" | "process" | "thread"; default "auto"
    max_failures=None,  # int | None
    storage=None,       # storage name; default "default"
    publish=None,       # True | str | Sequence[str] | Mapping[str, str]
    produces=None,      # str | Sequence[str]
    version=None,       # int | str
)
```

`None` means "use `Pipeline.defaults`, else the built-in default". Every parameter that accepts a sequence of strings also accepts a single string.

| Parameter | Meaning |
| --- | --- |
| `foreach` | The axes the stage expands over, in order. The order defines `unit_key` (`"a=x,b=y"`), the directory layout (`a=x/b=y`), and which static axes a dynamic axis' resolver may read (they must precede it). Repeating an axis is an error. |
| `needs` | Extra upstream stages or Refs that enter the key without being injected (their aligned artifact is available through `ctx.upstream(name)`). Injected parameters already create dependencies; use `needs=` only for the rest. |
| `resources` | `Resource` objects whose sessions the stage can receive by name, or a callable returning them per cell. With a callable, any otherwise unknown parameter name is assumed to be a resource and checked per cell. |
| `depends` | Files, values, and Python sources outside the pipeline file that affect the stage (see [Dependencies](#dependencies)); a callable is evaluated per cell. |
| `workers` | The size of the stage's sliding window: how many units run at once. A callable receives a `RunCtx` (with `config` and `args`), so `workers=lambda ctx: ctx.args.workers` makes it a CLI flag. |
| `exclusive` | Coordinate selectors whose units must run alone: such a unit starts only when no other unit of the stage is running and blocks the window while it runs. A list is an OR; `"a=x,b=y"` requires both. Selector axes must be in `foreach`; static ids are validated. |
| `backend` | `auto` picks `async` for `async def` stages, inline execution in the main thread for `def` stages with one worker, and a `spawn` process pool otherwise. `thread` must be chosen explicitly. `async` requires `async def`; `process` and `thread` require `def`. |
| `max_failures` | Stop submitting units after this many failures in the stage; running units finish. Default: run every unit. |
| `storage` | Which storage root holds the stage's managed outputs. Unknown names fail when a command opens the pipeline. |
| `publish` | Hardlink managed files into the branch's publish directory after every unit of the stage is `ok` (see [Outputs](#outputs-ctxout-produces-publish)). |
| `produces` | Files the stage must write under `ctx.out`. A single string also makes downstream stages receive that file instead of the unit directory. Changing `produces` invalidates the stage's units in every mode (reason `version-changed`; the order of a list does not matter). |
| `version` | Switches the stage to explicit versioning: source edits no longer invalidate it (they only add the `code-modified` label); changing `version` does. |

The scheduling keywords `workers`, `exclusive`, `max_failures`, and `backend` are excluded from the stage's source fingerprint: changing them never reruns units (unless the file falls back to whole-file hashing). All other decorator arguments are part of it.

The decorated method must return `None` (any other value fails the unit with `StageReturnError`, a `TypeError`) and must not be a generator. `async def` stages are supported. Validation that does not need a Config (binding, cycles, publish templates, exclusive selectors, backend compatibility) happens in `Pipeline.graph()`.

## Parameter injection

Parameters after `self` are bound by name when the graph is built:

1. `ctx` receives the unit's `Ctx`. Annotate it as `Ctx`, `Ctx[Config]`, or `Ctx[Config, Args]` for type checking. A pipeline with many stages usually defines one module-level alias, `StageCtx = Ctx[Config, Args]`, and annotates `ctx: StageCtx`; varve cannot derive the parameters from the class's `Config`/`Args` attributes, because type checkers never see them.
2. A name of an axis in this stage's `foreach` receives that axis' value for the unit (see [Axes](#axes)).
3. A name of another stage, or of a `Ref` class attribute, creates a dependency and receives the aligned upstream artifact: a `Path` for 1-to-1 alignment (the file when the upstream declares `produces="file"`, else the unit directory), or an ordered `dict` for fan-in.
4. A name of a resource of the stage (from `resources=` or `defaults["resources"]`) receives the opened session (`None` for a resource without `open`).

Any other name is a `GraphError` that lists the candidates. `*args`, `**kwargs`, and positional-only parameters are rejected; a stage may not depend on itself; an axis name that is not in the stage's `foreach` is rejected. Stage, axis, and Ref names share one namespace within a pipeline, and static resource names may not reuse them. Because binding is by name, annotations stay free for the real types (`Path`, `dict[str, Path]`, a session class), which keeps `pyright` and editors useful.

## The unit context

`Ctx` attributes and methods:

| Member | Meaning |
| --- | --- |
| `ctx.out` | The unit's private, initially empty output directory (a staging directory; see [Outputs](#outputs-ctxout-produces-publish)). Always a directory. |
| `ctx.config` | The branch Config, as a recording proxy. |
| `ctx.args` | The run's Args. |
| `ctx.branch`, `ctx.stage` | Branch and stage names. |
| `ctx.unit_key` | `"a=x,b=y"`, or `""` for a stage without axes. |
| `ctx.coords` | `{axis_name: id}` for every axis of the unit. |
| `ctx.cell` | Static axis values; a mapping that also supports attribute access (`ctx.cell.dataset`). |
| `ctx.upstream(name)` | What an upstream stage or Ref would inject, including `needs=`-only ones. |
| `ctx.metric(**values)` | Record unit metrics (see [Concurrency and observability](#concurrency-and-observability)). |
| `ctx.progress(iterable, total=None, desc=None)` | Iterate while reporting progress to the live display. |

Callables evaluated outside a unit get smaller contexts:

- `DepCtx(config, cell)` for `depends=` and `resources=` callables, `Resource.depends` callables, and axis resolvers. It has no Args: anything that shapes keys must come from Config.
- `RunCtx(config, args, branch, stage, root, cell)` for `workers=` callables and `Resource.preflight`/`open`. `cell` is empty except for `scope="unit"` resources.

In `Resource.preflight` and `Resource.open`, `RunCtx.config` is a recording proxy: the fields read there enter the config access of the units that use the resource (see [Resources](#resources)). Config reads through `DepCtx` (in `depends`/`resources` callables and resolvers) are not recorded; what those callables derive from Config reaches the key through the inputs, values, paths, and resources they return. Reads in `workers=` callables are not recorded, since they only affect scheduling.

## Axes

```python
Axis(name, values=None, *, resolver=None, input_fingerprint="content")
Axis.files(name, upstream, pattern, *, key=None, allow_empty=False, input_fingerprint="content")
```

Exactly one of `values` and `resolver` is required; `Axis.files` declares a dynamic axis over the files an upstream stage wrote (see [File axes](#file-axes-axisfiles)). Axis names must be Python identifiers other than `self` and `ctx`, and unique within a pipeline: two different `Axis` objects with the same name are an error. Stages share an axis by using the same `Axis` object.

Every item of an axis has a canonical string **id** (used in unit keys, selectors, paths, and `ctx.coords`) and a **value** (what a parameter of the axis' name receives). Ids must be non-empty, must not be `.` or `..`, and must not contain `/`, `,`, `=`, or NUL. Unsafe path characters are percent-encoded in directory names only.

### Static axes

```python
MODEL = Axis("model", ["small", "large"])
PART = Axis("part", [1, 2, 3])
```

Values are `str`, `int`, or `Enum` members (not `bool`), kept in declaration order. The id of a string is itself, of an int its decimal form, and of an Enum member its value (a string as is, an int in decimal, anything else the member name). The injected value is the declared value, so `part` receives the int `1` and an Enum axis receives the member. Branches can restrict static axes to a subset (see [Branches](#branches)).

### Dynamic axes

A dynamic axis resolves its items at probe time:

```python
SHARD = Axis(
    "shard",
    resolver=lambda ctx: {p.stem: p for p in sorted(ctx.config.data_dir.glob("*.txt"))},
    input_fingerprint="stat",
)
WORD = Axis("word", resolver=lambda ctx: ctx.config.words)
```

The resolver returns one of:

- `Mapping[key, Path]`: each unit receives the `Path`, which enters that unit's `inputs` key segment under the axis name, fingerprinted by content or, with `input_fingerprint="stat"`, by `(relpath, size, mtime_ns)`.
- `Mapping[key, value]` with non-`Path` values: each unit receives the value, which enters the `values` key segment. Mixing `Path` and non-`Path` values is an error.
- `Sequence[key]`: each unit receives the key itself, which enters the `values` segment.

Keys are `str` or `int` (ids are their string forms), unique, and kept in resolver order. Values must be JSON-serializable for keying; NaN or Infinity in a value is an evaluation error of the stage. `input_fingerprint` is only valid on dynamic axes.

Resolver parameters are bound by name, like a stage's:

- `ctx`: a `DepCtx` (Config and the resolver's cell).
- Names of static axes: the resolver is called once per combination of the static axes it names. Those axes must precede the dynamic axis in the `foreach` of every stage that uses it. A resolver that names no static axis is called once per command.
- Names of upstream stages: the aligned upstream artifact (a `Path`, or a fan-in `dict`), exactly like a stage parameter.
- Names of `Ref` attributes: the aligned Ref value.

Resources cannot be injected into resolvers. Results are memoized for the duration of one command, keyed by the axis, the cell, and the fingerprints of the upstream artifacts the resolver read.

### Upstream-driven axes

A resolver that reads an upstream stage splits **resolution** from **keying**: the upstream only orders expansion, and its fingerprint does not enter the downstream units' keys. When the resolver returns paths of individual files inside the upstream output, each unit keys on its own file only, so after the upstream reruns, only units whose file bytes changed rerun.

```python
import json
from pathlib import Path

from pydantic import BaseModel

from varve import Axis, Ctx, Pipeline, stage


class Config(BaseModel):
    batch_size: int = 2


DATASET = Axis("dataset", ["alpha", "beta"])


def read_batches(dataset: str, split: Path) -> dict[int, Path]:
    manifest = json.loads((split / "batches.json").read_text())
    return {item["index"]: split / item["file"] for item in manifest}


# Reads the upstream stage `split` (aligned on DATASET) and returns one file
# per batch: each batch unit keys on its own file, not on the whole `split`.
BATCH = Axis("batch", resolver=read_batches)


class Batches(Pipeline):
    Config = Config

    @stage(foreach=DATASET)
    def split(self, ctx: Ctx[Config], dataset: str) -> None:
        rows = [f"{dataset}-{i}" for i in range(5)]
        size = ctx.config.batch_size
        manifest = []
        for index, start in enumerate(range(0, len(rows), size)):
            name = f"batch-{index:03d}.json"
            (ctx.out / name).write_text(json.dumps(rows[start : start + size]))
            manifest.append({"index": index, "file": name})
        (ctx.out / "batches.json").write_text(json.dumps(manifest))

    @stage(foreach=(DATASET, BATCH), produces="part.json")
    def label(self, ctx: Ctx, dataset: str, batch: Path) -> None:
        rows = json.loads(batch.read_text())
        (ctx.out / "part.json").write_text(json.dumps([row.upper() for row in rows]))

    @stage(produces="all.json", publish=True)
    def merge(self, ctx: Ctx, label: dict[tuple[str, int], Path]) -> None:
        merged = {f"{d}/{b}": json.loads(p.read_text()) for (d, b), p in label.items()}
        (ctx.out / "all.json").write_text(json.dumps(merged, indent=1))


if __name__ == "__main__":
    raise SystemExit(Batches.cli())
```

Before the first run, `status` shows `label` and `merge` as `awaiting (split)`: a stage whose resolver reads an upstream that is not completely `hit` cannot be expanded yet, and neither can its dependents. During `run`, the stage is expanded right after the upstream finishes.

If `label` also needed the whole `split` directory in its key, it would add `split` as a parameter or to `needs=`.

### File axes: `Axis.files`

The most common upstream-driven axis has one item per file that an upstream stage wrote. `Axis.files` declares it without writing a resolver. In the example above, if `split` names its files `batch-000.json`, `batch-001.json`, ..., this declaration replaces `read_batches`:

```python
BATCH = Axis.files("batch", "split", "batch-*.json", key=lambda path: int(path.stem[6:]))
```

- `upstream` names a stage of the same pipeline whose axes are all static. The axis is resolved once per upstream unit: it binds the upstream's static axes, which must precede it in the `foreach` of every stage that uses it, so each `dataset` above gets its own batches.
- `pattern` is a POSIX glob (`**` crosses directories) matched against the files of that upstream unit's directory, also when the upstream declares a single-file `produces` (whose file is what stages receive). Matches are ordered by relative path, so zero-padded names keep numeric order.
- Each unit receives its file's `Path`, which enters its `inputs` key segment under the axis name, exactly like a resolver returning `{key: Path}`. An `Axis.files` axis and a resolver that returns the same keys and files give the same input keys; switching from one to the other only changes the source fingerprint of the stages whose closure holds the declaration (`source-changed`, which `reuse` accepts).
- `key(path)` returns the item key (`str` or `int`) from the file's `Path`; the default is `path.stem`, the file name without its last suffix. Two files with the same key are an evaluation error.
- No match is an evaluation error of the consuming stage unless `allow_empty=True`, which allows an empty domain.

### Shrinking domains and orphans

When a dynamic axis resolves to fewer ids than the store holds, the stored units whose static coordinates are still active but whose dynamic id disappeared are marked `orphaned` during `run` (never by a read-only probe, and never because of `axes:`, `--slice`, or `--sample`). The same happens to stored units of a static id that is no longer declared, and to units written under a different `foreach` (other axis names or order). Orphaned units leave the stage, so they no longer appear in downstream fan-ins; `status` reports `orphaned N`, and `clean --orphans` deletes their directories and rows. An orphaned unit whose id comes back is treated as `new`.

## Alignment and fan-in

When a stage depends on another stage, units are matched through the `Axis` objects both declare:

- **Shared axes** align 1-to-1: the downstream unit receives the upstream unit with the same ids, as a `Path`.
- **Upstream-only axes** fan in: the downstream unit receives an ordered `dict` of all upstream units that share its coordinates, in upstream unit order. The key is the axis key for one remaining axis (the declared static value, so an int axis gives int keys and an Enum axis gives members, or the resolver's key) and a tuple of keys, in upstream `foreach` order, for several.
- **Downstream-only axes** broadcast: every downstream unit with the same shared ids receives the same upstream unit or dict.

In the tour pipeline of the README, `score` (`MODEL × SHARD`) receives `tokenize` (`SHARD`) 1-to-1 and broadcasts it over `MODEL`; `total` (`MODEL`) receives `score` as `{shard_key: path}`. Alignment uses hash indexes and stays linear in the number of units. Fan-in dicts may be shared between units; treat them as read-only.

## Outputs: `ctx.out`, `produces`, `publish`

Each unit writes into `ctx.out`, a fresh directory `<storage_root>/.staging/<run_id>/<stage>/<unit_hash>/`. When the method returns:

1. Every `produces` path must exist (and a single-string `produces` must be a regular file).
2. Varve hashes every file under `ctx.out` (the artifact manifest and `artifact_fp`), sets managed files to `0444` (directories stay `0755`), and renames the directory to `<storage_root>/.internal/<branch>/<stage>/<unit_relpath>/`, moving any previous directory aside as `.old-<run_id>` first.
3. The `ok` record is committed to `state.db`.

If the method raises, or any step of finalizing and committing the unit fails, the staging directory is discarded and a `failed` record stores the error type, summary, traceback, duration, and any metrics; the run continues with the other units. Managed trees contain regular files and directories only; symlinks are rejected. Everything in the directory is part of the artifact; `produces` only adds checks and decides what downstream stages receive:

| `produces` | Checked | Downstream receives |
| --- | --- | --- |
| `None` | nothing | the unit directory |
| `"part.json"` | the file exists and is regular | the file |
| `["a.json", "b.json"]` | both exist | the unit directory |

To check a single file but hand downstream stages the unit directory, use a one-item list: `produces=["parts/manifest.json"]`. A stage that only feeds an `Axis.files` axis does not need this, because the axis globs the unit directory whatever `produces` declares.

Never keep references to `ctx.out` after the method returns; the directory is moved.

### `publish`

Managed directories are internal. `publish=` exposes chosen outputs under the branch's publish directory, `<publish_root>/<branch>/` (temporary branches: `<publish_root>/.tmp/<branch>/`), by hardlink:

| Form | Effect |
| --- | --- |
| `publish=True` | Publish every `produces` path under its own name (requires `produces=`). |
| `publish="summary.md"` or `publish=["summary.json", "plots/*.txt"]` | Publish these paths or globs under their own relative paths. In a stage with axes they go under `<unit_relpath>/`. |
| `publish={"total.json": "{model}.json"}` | Map a source path to a target. Targets may use `{axis}` placeholders; in a stage with axes they must use every axis placeholder. |
| `publish={"parts/**/*.parquet": "sources/"}` | A mapping target ending in `/` is a directory: each file a glob source matches is placed below it at its path relative to the glob's literal leading directories (`parts/a.parquet` becomes `sources/a.parquet`, `parts/x/b.parquet` becomes `sources/x/b.parquet`), and a literal source is placed below it under its own name. Directory targets may use placeholders but need not use every axis, so the units of a stage can merge their files into one directory. Glob sources require a directory target. |

Two entries or stages claiming the same literal target are a `GraphError`; templated targets that collide once expanded, and two units or entries of a stage placing a file at the same path through glob or directory targets, fail the stage when it publishes. Directory targets are not checked against other stages. Publishing runs after every unit of the stage is `ok`, including when all were hits, so a deleted published file is restored by the next run. Each file is linked to a temporary name and moved into place with `os.replace`; a directory source is rebuilt as a hardlinked tree and swapped in. The publish directory must be on the same filesystem as the stage's storage: varve never copies, and a cross-filesystem publish fails the stage with a message to set `storage=` to a storage on the publish filesystem. Files a unit's glob entry published earlier but no longer produces are removed at the next publish; varve records what each stage's glob entries published in `.varve-published/<stage>.json` inside the publish directory and never removes anything else. Published files that no longer correspond to a unit or entry are not removed.

## Roots and storage tiers

A pipeline has one **root**, which holds its metadata:

```text
<root>/.varve/state.db                       # the store (SQLite, WAL)
<root>/.varve/lock, .varve/locks/<branch>.lock
<root>/.varve/logs/<run_id>/<stage>/<unit>.log
```

The root is `default_root(config)` (default: the directory of the module that defines the pipeline class, evaluated with `main`'s Config). `--root PATH` overrides it for one command; `VARVE_ROOT_BASE` relocates it (see below).

Managed outputs live in **storage roots**, each with its own `.internal/` and `.staging/`. Stages pick one with `storage=` (or `defaults["storage"]`); the default storage is named `default`. Storage roots are resolved in this order:

1. `Pipeline.storage_roots(config)`, a classmethod returning `{name: path}`; relative paths resolve against the root. It must not depend on branch-specific Config values.
2. The workspace `varve.toml` `[storage]` table: `name = "/base"` maps storage `name` to `/base/<pipeline_name>` for every pipeline that does not declare that name itself.
3. `default` falls back to `<root>/out`.

The publish root is `publish_root(config)` (relative to the root) or, by default, the `default` storage root, which is why the quick start publishes to `out/main/`. Storage root, input root, and source root paths may themselves be or pass through symlinks (for example `data/interim -> /nvme/...`); nothing inside a managed or declared tree may be a symlink.

```python
class Features(Pipeline):
    Config = Config
    defaults = dict(
        storage="scratch",
        depends=Dependencies(values={"schema": 2}),
    )

    @classmethod
    def storage_roots(cls, config):
        return {"scratch": HERE / "scratch", "default": HERE / "out"}
```

Storage roots are recorded per pipeline in the store (`storages` table), shared by all branches. If a run computes a root that differs from the recorded one (because `storage_roots()` depends on branch Config, or the workspace `[storage]` base changed), the run fails; move the data and `clean` first. `clean` deletes under the recorded roots.

`clean --storage scratch` reclaims one tier. Cross-branch hardlink reuse only happens within a storage root.

## Resources

```python
Resource(name, depends=Dependencies(), preflight=None, open=None, scope="stage")
```

A resource bundles a session with what it depends on:

- `name`: the parameter name stages use to receive the session.
- `depends`: `Dependencies` (or a `DepCtx` callable, evaluated per cell) merged into the key of every stage that uses the resource, including `sources` that enter its `source_fp`.
- `preflight(run_ctx)`: a check run once per run per `Resource` object, in the main process, before the first stage that executes a unit needing it. A failure fails that stage.
- `open(run_ctx)`: returns a context manager (sync, or async where allowed) whose value is the session. A resource without `open` injects `None`.
- `scope`: when `open` runs.
  - `"stage"` (default): once in the main process when a stage has units to run, closed when the stage ends, and shared by its units. With the process backend the session is pickled into the workers, so it must be picklable (checked, with a clear error); return a handle such as an endpoint config rather than a live connection. Async context managers are accepted.
  - `"worker"`: once per process-pool worker (pool initializer) and closed when the worker exits; must be a sync context manager. With in-process backends it behaves like `"stage"`.
  - `"unit"`: around each unit, in whatever process runs it; `run_ctx.cell` holds the unit's static coordinates. Async context managers only for `async def` stages.

Nothing is preflighted or opened for a stage whose units are all hits. Resources from `defaults["resources"]` come first; a stage resource with the same name replaces a default. `resources=lambda ctx: [...]` selects resources per cell; two different `Resource` objects with the same name in one cell's declaration are an error.

`Resource` is a frozen dataclass, so `dataclasses.replace(LEXICON, preflight=other_check)` derives a variant with the same name and dependencies; the variant is a distinct object with its own preflight and sessions.

Sessions are keyed by `Resource` object identity, not by name. When a `resources=` callable returns different objects for different cells (for example a new `Resource` built per cell), the stage opens one session per distinct object, and each unit receives the session of its own cell's resource; returning the same module-level object from every cell shares one session.

```python
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel
from scoring import score_word

from varve import Axis, Ctx, Dependencies, Pipeline, Resource, stage

HERE = Path(__file__).parent


class Config(BaseModel):
    lexicon: Path = HERE / "lexicon.txt"
    words: list[str] = ["alpha", "beta", "gamma", "delta"]


def check_lexicon(run) -> None:
    if not run.config.lexicon.is_file():
        raise FileNotFoundError(run.config.lexicon)


@contextmanager
def open_lexicon(run):
    yield set(run.config.lexicon.read_text().split())


LEXICON = Resource(
    "lexicon",
    depends=lambda ctx: Dependencies(
        inputs={"lexicon": ctx.config.lexicon},
        sources=["scoring.py"],
    ),
    preflight=check_lexicon,
    open=open_lexicon,
)

GROUP = Axis("group", ["cheap", "heavy"])
WORD = Axis("word", resolver=lambda ctx: ctx.config.words)


class Scores(Pipeline):
    Config = Config

    @stage(
        foreach=(GROUP, WORD),
        resources=[LEXICON],
        workers=4,
        exclusive="group=heavy",
        max_failures=3,
        produces="score.json",
    )
    async def score(self, ctx: Ctx[Config], group: str, word: str, lexicon: set[str]) -> None:
        await asyncio.sleep(0.01)
        value = score_word(word, lexicon) * (10 if group == "heavy" else 1)
        (ctx.out / "score.json").write_text(json.dumps(value))


if __name__ == "__main__":
    raise SystemExit(Scores.cli())
```

Here `lexicon.txt` enters the key as an input and `scoring.py` (a helper module next to the pipeline) enters the source fingerprint, so editing either reruns `score`; reformatting `scoring.py` or editing its comments does not.

`preflight` and `open` receive a `RunCtx` whose `config` records field reads, like `ctx.config` in a stage. Those fields enter the config access of the units using the resource: for `stage` and `worker` scope, every unit of the stage that uses it; for `unit` scope, that unit. So changing a Config field that only the resource reads invalidates the units that use it, even though the stage body never touches it.

## Dependencies

```python
Dependencies(inputs={}, values={}, sources=())
Dependencies.glob(root, include="**", exclude=(), allow_empty=False)
Dependencies.stat(paths_or_glob)
```

- `inputs` maps names to a path, a list of paths, `Dependencies.glob(...)`, or `Dependencies.stat(...)`. Directories are walked recursively and contribute their regular files; empty directories are ignored. Files are content-hashed (SHA-256) by default; the hash is cached in the store by `(path, inode, size, mtime_ns)`, so unchanged files are not reread. Key material uses paths relative to each declared root (a file contributes its name), never absolute paths, so moving a checkout does not invalidate. An input with more than 32 files stores its file list once in the content-addressed `file_sets` table.
- `values` maps names to JSON values. Pydantic models are dumped, tuples become lists, and `Path` values become POSIX strings; dict keys must be strings. NaN and Infinity are rejected (the stage fails to evaluate).
- `sources` lists Python files, directories (only `*.py` files inside directories count), or `Dependencies.glob(...)` sets (every match must be a `*.py` file) whose normalized AST enters the stage's `source_fp`. Relative paths and glob roots resolve against the pipeline module's directory. Each file enters the fingerprint under its path relative to that directory, so a glob and an explicit list of the files it matches give the same `source_fp`; `Dependencies.stat` cannot declare sources.
- `Dependencies.glob` selects files under `root` with POSIX globs (`**` crosses directories), evaluated whenever the dependency is fingerprinted. A missing root, or no match unless `allow_empty=True`, is an error.
- Symlinks in globs: `root` itself may be or pass through a symlink. Below it, the walk only enters directories where some `include` pattern can still match (pattern segments are compared with directory segments; from a segment containing `**` on, everything below is reachable) and skips directories whose whole subtree an `exclude` pattern removes (`**` or `<dir pattern>/**`). A symlink below `root` is an error only if the glob would select it: it matches `include` and no `exclude`, or it points to a directory, its own path is not excluded, and some `include` pattern could match a non-excluded path below it. Special files follow the same rule. Every other symlink is ignored, so `Dependencies.glob(repo, include="src/**/*.rs")` works next to a symlinked `.venv` or `node_modules/`, while `include="**/*.rs"` needs `exclude=[".venv", "**/node_modules/**"]` (or similar) for them. Plain path inputs (not globs) still reject any symlink below the declared path.
- `Dependencies.stat` fingerprints the given paths or glob by `(relpath, size, mtime_ns)` instead of content, for large read-only inputs. It mixes freely with content-hashed inputs.

```python
    @stage(
        depends=Dependencies(
            inputs={
                "assets": Dependencies.glob(HERE / "assets", include="**/*.txt"),
                "blob": Dependencies.stat(HERE / "big.bin"),
            }
        ),
        version=1,
    )
    def ingest(self, ctx: Ctx[Config]) -> None:
        (ctx.out / "rows.json").write_text(json.dumps([1, 2, 3]))
```

`depends` declarations from `defaults`, the stage, and its resources are merged; the same input or value name declared by two of them is an error, and so is a name equal to one of the stage's dynamic axes. A callable `depends` or `resources`, or a resource whose own `depends` is callable, makes the stage's dependencies evaluated once per cell; otherwise they are evaluated once per stage per command. A missing declared path makes the probe of that stage fail.

## Keys and invalidation

### The input key

Each unit's **input key** hashes five segments, stored with per-segment hashes and summaries in `key_components_json`:

| Segment | Content |
| --- | --- |
| `config` | The Config projected onto the unit's recorded config access. |
| `upstream` | The `artifact_fp` of every aligned upstream stage and Ref (`[[unit_key, fp], ...]` for fan-ins). |
| `inputs` | `Dependencies.inputs` of the stage, its defaults, and its resources, plus `Path` values of dynamic axes. |
| `values` | `Dependencies.values` likewise, plus non-`Path` values of dynamic axes. |
| `version` | The stage's `version` (or null) and its normalized `produces` declaration. |

Static coordinates are the unit's identity (`unit_key`), not key material. Upstream fingerprints are content hashes of the upstream files, so an upstream that reruns and writes identical bytes leaves its dependents hits.

**Config access projection.** A unit's first run keys on the whole Config. When it succeeds, the top-level fields it read through `ctx.config` are recorded, and later probes key only on those fields, so changing an unrelated field is still a hit. Reads in the `preflight`/`open` of the unit's resources are included. When a unit reruns with the same source fingerprint, the new access set is the union of the old and new ones, so a field read only on some executions (for example, only on a cache miss inside the unit) keeps invalidating it; after a source change it starts over. A failed execution's reads are incomplete, so a failed unit keys on the whole Config: after a failure, any Config change (such as fixing a field the edited code now reads) shows as `config-changed`. With the process backend, the worker records accesses and returns them with the result. The recorded set only changes when a unit executes: with `version=` set, or after `reuse`, an edit that makes a stage read a new Config field does not rerun it, so the old set stays in force and changes to the new field go unnoticed. Bump `version` (or `run --force`) when a stage starts reading new Config fields.

### The source fingerprint

Source code is kept out of the input key, in a separate `source_fp`. Without `version=`, it is computed from the stage's **single-file static closure** plus declared `sources`:

- Every top-level name binding in the stage's file is a symbol: `def`, `class`, assignments (plain, annotated, augmented), names bound by top-level `for`/`with`/`try`/`if` statements, and each name of an `import` or `from ... import` line separately.
- The closure starts at the stage method (including its decorators, minus the scheduling keywords) and the class header (bases, keywords, class decorators) and follows referenced module-level symbols transitively.
- Every non-method statement of the class body (`defaults`, class attributes, `Resource`/`Axis`/`Ref` attributes, nested classes) and every dunder method (`__init__`, ...) is in every stage's closure, and so are module-level statements that assign to the class (`Demo.SCALE = 2`). Other methods, including other stages, enter only when reached from the stage or from an included class-body statement through `self.x`, `cls.x`, `ClassName.x`, `type(self).x`, or `self.__class__.x`.
- Top-level statements that bind no name, such as `os.environ.setdefault(...)`, form a shared preamble included in every stage.
- Excluded: the `Config` and `Args` classes (when defined at top level in the same file; Config fields are covered by projection), the `Config = ...`/`Args = ...` class attributes, the `if __name__ == "__main__":` block, the module docstring, and bare constant expressions.
- The AST is compared without positions, so comments and formatting do not matter. Docstrings inside functions and classes do.
- Files that use dynamic lookup (`globals()`, `locals()`, `exec()`, `eval()`, `vars()` without arguments, `sys.modules[__name__]`, `importlib.import_module(__name__)`, or `getattr(self|cls|type(self)|self.__class__, <non-constant name>)` in a class) or `from x import *` fall back to a whole-file AST hash. `inspect --sources` names the reason.
- Code in other modules is only tracked through `Dependencies.sources` (of the stage, its defaults, or its resources), as whole-file normalized ASTs. Varve does not follow imports.

The closure follows names textually: a local variable that shares a module symbol's name pulls that symbol in, which errs on the side of rerunning. Decorator arguments other than `workers`, `exclusive`, `max_failures`, and `backend` are part of the stage symbol, so editing them changes `source_fp`. The same four keys are stripped from a class-body `defaults = {...}` or `defaults = dict(...)` statement, so tuning them in `Pipeline.defaults` does not mark every stage `source-changed` either.

`inspect STAGE --sources` lists the closure's symbol names and external sources; after a source change, `inspect` lists which symbols changed.

### Hit rule

A unit is a hit when its stored record is `ok`, the current input key equals the stored one, every file of the artifact manifest still exists with the recorded size and `mtime_ns` (or content hash, with `--rehash`), and the source rule holds:

- `version` is not set: the current `source_fp` equals the stored one or the `reused_source_fp` that `reuse` recorded (label `reused`). Otherwise the unit is `needs-run · source-changed`.
- `version` is set: the source fingerprint is not compared; a difference only adds the label `code-modified`. Changing `version` produces `version-changed`. Config projection, inputs, values, and upstream artifacts still apply, so declaring an external engine version in `Resource.depends` values still invalidates exactly the stages that use the resource.

Decisions are taken in this order: `--force` (`forced`); no record or an orphaned one (`new`); a record still `running` (`interrupted`); an upstream unit that is not available, so the key cannot be computed (`upstream-pending`); a changed input key (the changed segments, such as `config-changed`); a changed source (`source-changed`); a stored failure with unchanged key and source (`failed`); a missing or modified artifact, including a unit directory that was removed or replaced by a symlink even when the unit wrote no files (`artifact-missing`, or `artifact-changed` with `--rehash`); otherwise `hit`. A unit whose key changed and whose source also changed gets both reasons; `status --expand` and `inspect` list all of a unit's reasons, comma-joined, and stage summaries count each reason.

### `reuse`

`reuse STAGE` accepts the current source of a stage without rerunning it, for edits you know do not change results (logging, refactors, speedups). In one transaction it sets `reused_source_fp` to the current fingerprint on every `ok` unit of the stage whose input key still matches and whose artifact is still intact on disk. Those units become hits with the label `reused`. The exemption ends by itself at the next source change, or when the key changes. Units that cannot be reused are reported by reason (`artifact-missing` for an `ok` unit whose files are gone). Reuse upstream stages first: a unit whose upstream is not a hit has no computable key.

### Cross-branch reuse

Before executing a `needs-run` unit, `run` looks for an `ok` unit of the same stage and `unit_key` on another branch of the same store whose input key equals this unit's key (recomputed under that unit's recorded config access, so branches that differ only in Config fields the stage does not read still match), that satisfies the same source rule, lives in the same storage root, and has an intact manifest (checked again on the hardlinked copy, so a unit that the other branch replaces or cleans meanwhile is executed instead). If one exists, its read-only directory is hardlinked into this branch instead of executing; the unit is labeled `linked@<branch>`. Hardlinks keep the shared inodes alive when either branch is cleaned. `--force` skips linking.

## Statuses, reasons, and labels

| Kind | Values |
| --- | --- |
| Stored unit status (`units.status`) | `running`, `ok`, `failed`, `orphaned` |
| Unit decision | `hit`, `needs-run`, `failed` |
| `needs-run` reasons | `new`, `config-changed`, `upstream-changed`, `input-changed`, `value-changed`, `version-changed`, `source-changed`, `artifact-missing`, `artifact-changed`, `interrupted`, `upstream-pending`, `forced` |
| Labels | `reused`, `code-modified`, `linked@<branch>` |
| Stage status in `status` | `hit`, `needs-run`, `failed`, `awaiting (<upstream>)` |
| Pipeline status in `varve ls` | `uninitialized` (registered in `varve.toml`, no store yet) |
| Stage outcome of `run` | `ok`, `failed`, `interrupted`, `partial`, `blocked`, `skipped` |
| Run status (`runs.status`) | `running`, `ok`, `failed`, `interrupted` |

`status` prints one line per stage, for example `✓ hit (6)`, `2/4 hit · 2 needs-run (upstream-pending 2)`, `0/2 hit · 2 failed`, `awaiting (split)`, followed by labels and `orphaned N`. A `failed` unit reruns on the next `run` like any other non-hit; it stays `failed` (rather than `needs-run`) only while its key and source are unchanged.

In `run`, a stage is `blocked` when it is selected but a dependency outside the selection is not current or cannot be evaluated, or when it is still awaiting; `skipped` when a selected dependency did not complete; `partial` when `--failed-only` left other non-hit units alone; `failed` when a unit failed, a worker process crashed, a resource failed, or publishing failed. A failed stage skips its dependents; independent stages still run.

## Concurrency and observability

Logical stages execute in topological order behind a barrier: every unit of a stage finishes and is committed before its dependents are expanded and keyed. Within a stage, up to `workers` units run in a sliding window, refilled in completion order.

| Backend | Used for | Notes |
| --- | --- | --- |
| inline | `auto`, `def`, one worker | Runs in the main thread. |
| `async` | `auto` or explicit, `async def` | One event loop; `workers` bounds concurrent units. |
| `thread` | explicit only, `def` | A thread pool of `workers` threads. |
| `process` | `auto` with `workers > 1`, or explicit, `def` | One `spawn` pool per stage. Workers import the pipeline class by module and qualname (a script run by path is loaded from its file), so the class must be defined at module level. The setup (Config, Args, picklable stage-scope sessions) and each unit's inputs are pickled to workers; references to classes defined in the entry module, including a package's `__main__.py` run with `python -m package`, resolve to the module the worker imported, so `Config`, `Args`, and Enum axis values may live there. `varve.toml` `[defaults] worker_env` is in effect from each worker's start, before any user module is imported. If a worker dies, no new units are submitted and the stage fails. If the run process itself is killed, its workers exit on their own within a second (worker-scope sessions are not closed then). |

`exclusive` and `max_failures` are described under [The `@stage` decorator](#the-stage-decorator). Ctrl-C once stops submitting new units and lets running units finish and commit (the run ends `interrupted`, exit code 130); Ctrl-C again aborts, terminating process workers. Process workers ignore SIGINT from the moment they start. The notice for the first Ctrl-C is printed by the main loop, so while an inline unit runs it appears when that unit returns.

**Live display.** When stdout is a terminal, `run` shows a `rich.Live` view: pipeline, branch, and overall progress, one line per stage (`hit`, `linked`, `running 142/500`, `awaiting`, `failed`), the active units with rate and ETA, and the last failures. `--no-tui` (or a non-terminal stdout) prints timestamped log lines to stderr instead.

**Output capture.** When the live display is active or `workers > 1`, each unit's stdout and stderr go to `<root>/.varve/logs/<run_id>/<stage>/<unit>.log` (a stage without axes uses `_.log`); the file only exists for units that wrote something. For in-process backends, varve then installs routing proxies for `sys.stdout`/`sys.stderr` (including writes to `sys.stdout.buffer`) for the stage; otherwise the streams are left untouched. Process workers redirect file descriptors. While the live display is active, file-descriptor-level output of the main process (for example from subprocesses) goes to `.varve/logs/<run_id>/process.log`. With one worker and no live display, output goes straight to the terminal. Logs of the 20 most recent runs are kept, plus those of runs still in progress on other branches. `inspect STAGE@... --log` prints a unit's log.

**Metrics.** `ctx.metric(**values)` merges values into the unit's metrics (`units.metrics_json`, shown by `inspect`). Numeric values of successful units are summed per stage into `stage_runs.metrics_summary_json`. By convention `ctx.metric(items=N)` reports the number of items processed, which the web UI turns into items/s throughput (falling back to units/s).

**Progress.** `ctx.progress(iterable, total=None, desc=None)` yields the items and reports progress to the live display (from process workers through a queue). `total` defaults to `len(iterable)` when available. Without the live display and with output not captured, it draws a tqdm bar on stderr when stderr is a terminal; otherwise it only iterates.

**Run bookkeeping.** Every `run` writes a `runs` row (start and end time, status, argv, git commit and dirty flag of the checkout containing the pipeline module, wherever the command runs from, host, wall time) and one `stage_runs` row per executed stage (executed, hit, failed, and total units, workers, wall time, unit p50/p95/max duration, peak RSS, metric sums). Peak RSS is per unit for process workers and the main process's peak otherwise.

```python
    @stage(
        foreach=PART,
        needs="ingest",
        resources=[CLIENT],
        workers=2,
        backend="thread",
        produces="part.json",
    )
    def compute(self, ctx: Ctx[Config], part: int, client: dict) -> None:
        rows = json.loads((ctx.upstream("ingest") / "rows.json").read_text())
        out = [row * part for row in ctx.progress(rows, desc="rows")]
        (ctx.out / "part.json").write_text(json.dumps(out))
        ctx.metric(items=len(out), opened_by=client["opened_by"])
```

## Branches

### `varve.yaml`

An optional `varve.yaml` next to the pipeline module declares branches:

```yaml
main:
  config:
    data_dir: data
small:
  config:
    data_dir: data
  axes:
    model: [small]
only-s1:
  axes:
    shard: [s1]
    model: [large]
  manual: true
```

Each branch may have `config:` (Config values), `axes:` (axis name to a list of active ids), `manual:` (excluded from default top-level `varve run`; explicit targets still run it), and `is_temporary:`. Other keys are an error. `main` always exists, with default Config when undeclared. Branch names match `[A-Za-z0-9][A-Za-z0-9._-]*`. `axes:` restricts static axes and, for dynamic axes, keeps only the listed ids (which the resolver must produce). An unknown axis name or an id that is not a value of a static axis is an error. Each branch has its own records, managed directories (`.internal/<branch>/`), and publish directory (`<publish_root>/<branch>/`). Commands take `--branch NAME` (default `main`).

### Temporary branches

`run` can derive a temporary branch whose Config and active axes are snapshotted in the store, so later commands address it by name alone:

- `--override JSON` deep-merges a JSON object over `main`'s Config. The branch is named by `--branch NEW` if that is a new name, else `main_tmp_<hash>`. Overriding a declared branch other than `main` is an error.
- `--temp` derives `<branch>_tmp_<hash>` from the selected branch's Config and axes.
- `--slice AXIS=ID,ID` or `--slice AXIS=@FILE` (one id per line, `#` comments, relative to the cwd) restricts a static or dynamic axis; the ids must be within the branch's active ids and, for dynamic axes, produced by the resolver.
- `--sample AXIS=N [--seed S]` keeps a deterministic subset of N ids in domain order (dynamic axes are sampled per resolver cell).

`--slice` and `--sample` would change a persistent branch, so they require `--temp` or a new `--branch NAME` (which creates a temporary branch from `main`'s Config). Reusing a temporary branch name with a different snapshot is an error. Temporary branches publish under `<publish_root>/.tmp/<branch>/`, are excluded from top-level macro runs, and are removed with `clean --branch NAME`. Because they share the store, their units are usually linked from `main` rather than recomputed.

## Generated pipeline CLI

`Pipeline.cli()` builds an argparse CLI. `--root PATH` (pipeline root for this command) and `-v/--verbose` are accepted before or after the command. Exit codes: `0` success, `1` a failed or blocked stage or a user error (printed as `error: ...`), `130` interrupted, `2` usage error. When stdout or stderr is a closed pipe (`python demo.py status | head`), the CLI stops printing and exits normally; an inline unit that itself prints to the closed stdout without capture can still fail.

### `run`

```text
run [--branch NAME] [--override JSON] [--temp]
    [--only STAGE | --upto STAGE | --downstream STAGE]
    [--force/-f] [--rehash] [--failed-only]
    [--slice AXIS=IDS]... [--sample AXIS=N]... [--seed S]
    [--no-tui] [Args flags]
```

- `--only STAGE` runs one stage, `--upto STAGE` the stage and its ancestors, `--downstream STAGE` the stage and its descendants. Ancestors outside the selection are probed, never executed; a selected stage whose unselected dependency is not current is `blocked`.
- `--force` reruns every unit of the selected stages and skips cross-branch linking.
- `--rehash` ignores the stat cache: inputs are rehashed and artifacts are verified by content.
- `--failed-only` runs only units whose stored status is `failed`.
- Branch options are described under [Branches](#branches).

Startup recovery runs first (see [Crash recovery](#crash-recovery-and-clean-safety)). At the end, stages that did not end `ok` are listed with their detail.

### `status`

```text
status [STAGE[@AXIS=ID,...]] [--branch NAME] [--expand] [--rehash] [Args flags]
```

One line per stage. `--expand` adds a table of units with decision, reason, and labels. A selector restricts the output to one stage (ancestors are still evaluated) and lists its units, filtered by the given coordinates; omitted axes are wildcards. Stages that read `Ref`s are followed by a Refs table (upstream, committed unit count, last commit time, git commit, root).

### `plan`

```text
plan [--only STAGE | --upto STAGE | --downstream STAGE] [--branch NAME] [--rehash] [Args flags]
```

Draws the stage DAG with each stage's hit count, failures, or `awaiting`. The selection options restrict what is drawn. When the graph cannot be laid out, it prints a list. Refs read by the drawn stages are listed below the DAG as external inputs (`@target:stage`, with `@branch,...` unless only `main` is read).

### `ls`

Lists stage declarations (axes, with `*` marking dynamic ones; dependencies, including Refs as `@target:stage[@branch,...]`; storage; `produces`; publish targets; version; workers) and the stored branches (kind, unit counts by status, last run). CLI tables show timestamps in local time; the store keeps them in UTC.

### `inspect`

```text
inspect STAGE[@AXIS=ID,...] [--branch NAME] [--sources] [--log]
```

With exactly one matching unit: its decision and reason, unit directory, artifact path, stored status, artifact fingerprint, input key, recorded config access, duration, metrics, link source, last run, log path, the key segments that changed (down to Config fields and input files), the source symbols that changed, and the last traceback. With several matches: a table of units. `--sources` lists the stage's closure symbols and external sources (and the fallback reason, if any); `--log` prints the captured log.

### `reuse`

```text
reuse STAGE [--branch NAME]
```

See [`reuse`](#reuse). Prints how many units were reused, how many were already hits, and why others could not be.

### `clean`

```text
clean [--branch NAME] [--storage NAME] [--orphans] [--yes/-y]
```

Prints what would be removed and asks for confirmation unless `--yes` (a closed stdin aborts).

- No option: the whole store, that is every storage's `.internal/` and `.staging/`, the publish directories of known branches and `.tmp/`, `state.db` with its WAL files, and the run logs. This also works when the database is foreign or unreadable.
- `--branch NAME`: that branch's managed directories in every storage, its publish directory, and its records.
- `--storage NAME`: that storage's managed directories and the records stored there (combine with `--branch` to limit it to one branch).
- `--orphans`: directories and records of orphaned units (combine with `--branch` to limit it), plus stat-cache entries of files that are gone or changed.

Every `clean` that keeps the store also drops stored key summaries, source details, and file lists that no remaining unit references; `run` does the same after it executed or linked units.

`clean` removes paths under the storage roots recorded in the store (computed from `main`'s Config only for storages the store does not record); `ls` computes the root from `main`'s Config.

## Cross-pipeline `Ref`

```python
Ref(target, stage, branch="main", selector=None, align_by=None)
```

A `Ref` class attribute makes a committed stage of another pipeline (or another branch) injectable like an upstream stage, by attribute name. It can also be listed in `needs=` or read by a resolver.

```python
import json
from pathlib import Path

from varve import Axis, Ctx, Pipeline, Ref, stage

LANG = Axis("lang", ["en", "de"])


class Report(Pipeline):
    # Committed `count` units of the `corpus` pipeline's `main` branch.
    counts = Ref("corpus", stage="count")

    @stage(foreach=LANG, produces="line.txt")
    def line(self, ctx: Ctx, lang: str, counts: Path) -> None:
        (ctx.out / "line.txt").write_text(f"{lang}: {json.loads(counts.read_text())}\n")

    @stage(produces="report.txt", publish=True)
    def report(self, ctx: Ctx, line: dict[str, Path]) -> None:
        text = "".join(path.read_text() for path in line.values())
        (ctx.out / "report.txt").write_text(text)
```

- **Target.** A `Pipeline` class, or a string selector matched without importing anything: an exact pipeline name, then an exact module or `varve.toml` entry, then an unambiguous dotted suffix, among the `varve.toml` pipelines and the stores found under the scan root (`VARVE_ROOT_BASE`, else the `varve.toml` directory, else the cwd). A class target always uses that class's default (or relocated) root.
- **Reading.** The upstream `state.db` is opened read-only. Only `ok` units of the stage on the named branch are used; each unit's path is its storage root (from the `storages` table) joined with its `artifact_relpath`, and its `artifact_fp` enters the consuming unit's `upstream` key segment. Nothing is imported or rehashed. A missing store, branch, stage, committed unit, or artifact is an error that prints the command to run the upstream (`python -m <module> run --branch B`, or `varve run <name>@B`).
- **Alignment by name.** Unlike in-pipeline stages, Refs align by axis name. Axes the consuming stage shares by name with the upstream stage align 1-to-1, and a requested coordinate without an `ok` upstream unit is an error (upstream domains may be supersets). Upstream-only axes fan in as an ordered `dict` keyed by the upstream id string (a tuple of id strings for several axes); ids stay strings because the upstream is not imported. The entries follow the upstream `foreach` product order, with static axes in their declared id order and dynamic axes in id string order (the upstream resolver's order is not stored), so the dict and the key never depend on the order in which upstream units were committed. Only units inside the upstream branch's active domain are aggregated (its declared static ids restricted by `axes:`, `--slice`, or `--sample`, and explicit dynamic `axes:` lists); orphaned and out-of-domain rows are ignored. If an in-domain unit that a coordinate reads is `failed` or `running`, binding fails with the upstream command rather than leaving it out. `align_by=()` fans in everything, `align_by=("a", "b")` aligns only on the listed names, and `align_by={"ds": "dataset"}` maps a local axis name to an upstream one.
- **Filtering.** `selector="axis=id,..."` keeps only matching upstream units before alignment.
- **Several branches.** `branch=("main", "katex")` wraps each value in an outer `dict` keyed by branch name.
- **Reading outside a pipeline.** `ref.resolve()` returns what a stage without axes would receive: the artifact `Path` when the upstream stage has no axes, else an ordered `dict` of every selected upstream unit keyed by id string (a tuple of id strings for several axes, in upstream `foreach` order), wrapped in a `dict` keyed by branch for several branches. Location, active-domain filtering, and errors are those of a binding: a class target uses its default (or relocated) root, a string target the `varve.toml` found from the cwd and the stores under the scan root, and `VARVE_ROOT_BASE` applies to both. Scripts and notebooks should use it rather than read `.varve/state.db`, whose schema is internal: `Ref("corpus", stage="count").resolve()` gives `{"en": Path, "de": Path}`.

```python
class Summary(Pipeline):
    # Class target; align_by=() fans every upstream unit in, keyed by id string.
    all_counts = Ref(Corpus, stage="count", align_by=())
    # Only the `en` unit of the `long` branch: no shared axis left, so a dict.
    english = Ref("corpus", stage="count", branch="long", selector="lang=en")
```

**"ok" is not "current".** A single-pipeline command reads only the upstream's committed records; it never imports or probes the upstream pipeline, so it cannot tell whether the upstream itself is stale. `status` and `inspect` print the Refs table with each upstream's last commit time and git commit and a reminder that `varve plan` / `varve run` check the whole chain. The top-level `varve run` runs upstream nodes first.

## Workspaces: `varve.toml`

A `varve.toml`, usually at the repository root, declares workspace-level settings. It is parsed without importing any user module:

```toml
[workspace]
pipelines = ["corpus", "report"]   # module or package names, or "pkg.mod:Class"

[storage]
interim = "/data_disk/varve/interim"   # storage "interim" -> /data_disk/varve/interim/<pipeline_name>

[defaults]
worker_env = { OMP_NUM_THREADS = "1" }
```

- `[workspace] pipelines`: registered pipelines, in a meaningful order (it breaks ties in macro ordering). Registered pipelines appear in `varve ls` even before their first run. An entry that names a package is its own selector; a module entry is addressed by its parent package. Without `:Class`, the module (and, for a package, its `__main__` submodule) must define exactly one pipeline class with stages.
- `[storage]`: storage bases; relative paths resolve against the `varve.toml` directory. A pipeline's own `storage_roots()` wins for the names it declares.
- `[defaults] worker_env`: environment variables set in process-pool workers from their start, before user modules are imported.

Unknown tables or keys are an error. Single-pipeline commands use the first `varve.toml` found walking up from the cwd, else from the pipeline module's directory. The top-level `varve` command searches from the cwd only and adds the `varve.toml` directory to `sys.path`, so registered modules import.

## Top-level `varve` CLI

`varve COMMAND`; with no command, `varve ls`. `-v/--verbose` is global.

**Selectors.** A pipeline's selector is its `varve_name`, else the package that contains its defining module (`corpus.run` gives `corpus`; a top-level module gives its own name; a script gives its file stem). `MODULE` arguments accept the selector, the exact module or `varve.toml` entry, or any unambiguous dotted suffix; an importable module that is neither registered nor stored is accepted too. `TARGET` is `MODULE` or `MODULE@BRANCH`.

```text
varve ls
varve status  MODULE [--stage STAGE[@AXIS=ID,...]] [--branch NAME] [--expand] [--rehash]
varve plan    [TARGET...] [--all] [--branch NAME]
varve run     [TARGET...] [--all] [--branch NAME] [--only TARGET]... [--skip TARGET]...
              [--from TARGET] [--set FIELD=VALUE]... [--force/-f] [--rehash] [--no-tui] [--dry-run]
varve reuse   MODULE --stage STAGE [--branch NAME]
varve inspect MODULE --stage STAGE[@AXIS=ID,...] [--branch NAME] [--sources] [--log]
varve clean   MODULE [--branch NAME] [--storage NAME] [--orphans] [--yes/-y]
varve ui      [--port 8765] [--host 127.0.0.1]
```

- `ls` imports nothing. It lists registered pipelines and every store found under the scan root with per-branch unit counts, last run, and root; temporary and manual branches are marked; registered pipelines without a store show `uninitialized`; unreadable stores show their error.
- `status`, `inspect`, `reuse`, and `clean` import the pipeline and run the same code as the generated commands, with default Args. When a discovered store lives at a root other than the class's default root, that root is used.
- `plan` and `run` work on **macro nodes**, `(pipeline, branch)` pairs:
  - Without targets: every registered pipeline with each of its `varve.yaml` branches (`main` first, temporary branches excluded, `manual` branches only with `--all`). With targets: those pipelines' branches, or exactly `MODULE@BRANCH` (which may be manual). `--branch NAME` keeps only that branch of bare targets; registered pipelines without it are skipped.
  - Every `Ref` of a node (class attributes and inline `needs=Ref(...)`) pulls in the upstream `(pipeline, branch)` nodes it reads, recursively, even when they are manual or outside `--branch`. Nodes are ordered topologically, ties broken by `varve.toml` order, then name, then branch declaration order. A cycle is an error.
  - `--only`, `--skip`, and `--from` choose which nodes execute; the others stay in the plan as `skipped`. Each must match a node of the plan.
  - `--set FIELD=VALUE` (repeatable; `-` in names becomes `_`) sets a top-level `Args` field on every selected node whose `Args` declares it. The value is validated against the field type, first as given, then as JSON; for an `Optional` field the token `null` is `None`, as in the generated CLI. A key that no selected pipeline declares, or an invalid value, is an error before anything runs.
- `plan` probes each node in order against its upstreams' committed records and shows per-status stage counts, `waits for X` when the probe fails because upstream `X` is not fresh, and `(after X)` when decisions may change after `X` runs. With exactly one target it also draws that pipeline's stage DAG.
- `run` executes the selected nodes sequentially, in-process, printing a rule before each. `--force`, `--rehash`, and `--no-tui` apply to every node. It stops at the first node that does not end `ok` (later nodes are `not-run`), prints a summary table (status, executed and hit units, wall time), and after a failure prints a `Resume with: varve run ... --from <node>` command. `--dry-run` prints the ordered nodes, why each is included, what they read, whether they run, and the routed `--set` values, without probing or writing anything.
- `ui` serves the web UI over the workspace's stores.

## Relocating roots: `VARVE_ROOT_BASE`

When the environment variable `VARVE_ROOT_BASE=/path` is set and a command has no explicit `--root`, every pipeline's root becomes `/path/<selector>`. Its `default` storage moves to `<root>/out`, other storages to `<root>/storages/<name>`, and the publish root to the default storage root; `storage_roots()`, `publish_root()`, and `[storage]` are ignored. Ref discovery and `varve ls` use the same base. This applies to generated commands and the top-level `varve` alike, so a whole chain can run in a scratch directory without touching real outputs:

```bash
VARVE_ROOT_BASE=/tmp/varve-trial varve run
```

An explicit `--root` disables relocation for that command.

## Web UI

`pip install 'varve[ui]'`, then `varve ui [--port 8765] [--host 127.0.0.1]` serves a local, read-only web UI until you press Ctrl-C. It never imports user modules: it reads the `state.db` files of the workspace (registered pipelines and stores under the scan root, re-evaluated on reload) through read-only SQLite connections, so it can run next to an active `varve run` without blocking it.

- **Workspace page**: every pipeline and branch with status counts, last run, last duration, artifact size, and live progress of active runs; a lineage graph when pipelines are linked by Refs.
- **Pipeline page**: the stage DAG (solid edges for injected data, dashed for `needs=`, dotted for resolver reads) with per-stage state, `ok / total` progress, last wall time, and version; it polls every 1.5 s while units are running.
- **Stage and unit drawer**: labels, counts and timing, metric sums, failures grouped by error type and summary with a sample traceback, a filterable unit table, and per unit the five key segments, config access, source detail, artifact manifest, last run, and captured log.
- **History**: wall time of full runs as a line and partial runs as hollow points labeled `executed/total`, unit p50/p95 for a selected stage, items/s (or units/s) throughput, and a run table with git commit, workers, and per-stage breakdown.

The JSON endpoints are `GET /api/workspace`, `/api/pipelines/{id}/branches/{branch}`, `.../history?stage=&limit=`, `.../stages/{stage}/units?status=&q=&sort=&page=&page_size=`, `.../stages/{stage}/units/{unit_key}` (`_` for a stage without axes), and `/api/runs/{run_id}`.

## Crash recovery and clean safety

- **Atomic units.** A unit's directory is renamed into place before its `ok` record is committed; a replaced directory is moved aside as `.old-<run_id>` first, and only after the unit's `running` record is committed. A unit that already has a record gets its `running` record before the body starts, so after a crash or `kill -9` it probes as `interrupted` and simply reruns; a unit without one that dies before its batched `running` record is written still probes as `new` and reruns.
- **Startup recovery.** Each `run` first restores or drops `.old-*` directories of units still recorded as `running`, removes staging directories of this branch's earlier runs, and marks this branch's earlier `running` runs `interrupted`.
- **Store durability.** `state.db` uses SQLite WAL with `synchronous=NORMAL`, which survives process kills and power loss on local filesystems. Unit writes are batched in short time windows during a run.
- **Locks.** `run`, `reuse`, and `clean --branch` hold `.varve/lock` shared and `.varve/locks/<branch>.lock` exclusively, so different branches can run concurrently. A whole-store, `--storage`, or `--orphans` clean holds `.varve/lock` exclusively. A conflicting command fails immediately with an error instead of waiting.
- **Read-only artifacts.** Managed files are `0444`, which protects them, and inodes shared by cross-branch links, from accidental writes.
- **Clean scope.** `clean` only removes varve-managed paths: `.internal/`, `.staging/`, publish directories of known branches, `.varve/state.db*`, and `.varve/logs/`. Lock files are kept.
- **Schema.** A store written by an incompatible varve, or a foreign SQLite file, is rejected with a message to run `clean`.

## Limitations

- Single machine, POSIX only (`fcntl`, hardlinks, `spawn` process pools). No distributed execution, remote artifact store, or daemon.
- Stages of one pipeline execute one stage at a time; independent stages do not overlap. Macro runs execute nodes sequentially.
- Source tracking covers the stage's own file statically; other modules count only when declared in `Dependencies.sources`, and dynamic dispatch through registries is not followed.
- Config fields read in resolvers, `depends=`/`resources=` callables, `Resource.depends` callables, and `workers=` callables are not recorded in config access (their results are keyed instead, except for `workers`). Reads in `Resource.preflight`/`open` are recorded.
- With `version=` set or after `reuse`, the recorded config access does not grow until the unit executes again.
- Decorator arguments other than the scheduling keywords are part of the stage's source fingerprint.
- `run --only/--upto/--downstream` take stage names, not unit coordinates.
- Published files of removed units or entries are not deleted.
- Ref fan-in keys are id strings, and Ref class targets always use the default (or relocated) root of the class.
- Peak RSS of in-process backends is the main process's lifetime peak. A thread-backend unit that is running at the second Ctrl-C cannot be killed; the process exits when it returns.
- Resolver results are memoized for one command only.
- Source fingerprints hash `ast.dump` output, so a CPython minor-version upgrade may rerun stages.
