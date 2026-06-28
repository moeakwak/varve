# varve

`varve` is a small Python library for serial experiment orchestration with a materialized, content-addressed cache. It is intentionally thin: experiments own their output formats and may override special output-base policies, while varve owns the default module-local `out/` base, branch-aware output-root resolution, `ctx.out`, and the store that records which stage successfully produced which durable artifacts for a given content key.

For maintainers, see [ARCHITECTURE.md](ARCHITECTURE.md) for the current package layout and cache model, and [AGENTS.md](AGENTS.md) for development rules and dependency boundaries.

```python
from pydantic import BaseModel
from varve import Experiment, stage

class Args(BaseModel):
    workers: int = 1

class Config(BaseModel):
    seed: int = 1

class Demo(Experiment):
    Args = Args
    Config = Config

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(str(ctx.config.seed))

if __name__ == "__main__":
    raise SystemExit(Demo.cli())
```

Commands:

- `run [--branch NAME] [--override JSON] [--upto STAGE | --downstream STAGE] [--force] [--out PATH]`: run the selected stage set, using cached artifacts when valid.
- `status [--branch NAME] [--upto STAGE | --downstream STAGE] [--out PATH]`: show cache state without executing stages.
- `plan [--upto STAGE | --downstream STAGE]`: print the selected stage order.
- `list`: list declared stages.
- `clean [--branch NAME] [--downstream STAGE] [--out PATH] [--yes]`: remove store records and artifacts.

Top-level dashboard:

- `varve ls [--root DIR]`: scan a directory tree for varve stores and print an overview.
- `varve show <experiment_id> [--root DIR] [--branch NAME]`: print one discovered store's stage details and recorded dependency edges.
- `varve refresh [--root DIR]`: run discovered experiment branches whose dashboard status is `stale`.

`varve ls` and `varve show` are read-only. The dashboard discovers experiments by looking for `.varve/manifest.json` under the scan root, imports the experiment module recorded in the manifest, resolves the selected branch, and uses the same engine state evaluator as the experiment `status` command. `varve refresh` executes stale experiments in discovery order. For colocated outputs shaped like `<experiment>/out/<branch>`, the `experiment_id` is the experiment path with the trailing `out/<branch>` removed, and the branch is tracked separately. Stores outside that layout are not shown.

Dashboard overview uses a `STATUS` column. Each experiment + branch status is one of the engine `Status` values, or `error` when manifest parsing, experiment import, branch/config resolution, or engine evaluation fails. `DURATION` is the sum of the latest persisted stage execution durations when every stage has one. `varve show` prints the diagnostic phase and message for those failures, and includes each stage's engine `STATUS` and `REASON`.

## Output paths

Stage outputs are anchored at the experiment output root (`ctx.out`). Static `@stage(produces=...)` entries are interpreted relative to `ctx.out`.

The output root is not a `Config` field. `run`, `status`, and `clean` first pick an output base from explicit `--out PATH` or `Experiment.default_output_root(config)`. The default `Experiment.default_output_root(config)` is the experiment module's sibling `out/` directory. varve then appends the selected branch: `base/<branch>` for persistent branches and `base/.tmp/<branch>` for temporary override branches. Stage code should write through `ctx.out`; helper functions that create sidecars should receive `ctx.out` from their stage instead of reading an output path from `ctx.config`.

Experiments that need a special output location may still override `default_output_root(config)`. Workspace studies that only repeated the module-local `out/` default no longer need that override.

Batch stages record the paths they yield. Yield either an absolute path under `ctx.out`, or a path relative to `ctx.out`. Relative batch output paths are not interpreted relative to the current working directory.

Batch stages get one overall `tqdm` progress bar for the resumed iterable by default. The bar is labeled with the stage name and seeds its initial count from already-completed indexes, so resumed runs do not restart from zero:

```python
async for index, item in ctx.resume(items):
    ...
```

Pass `progress=False` to disable the bar, `desc=...` to override the label, `unit=...` to change the counted noun, `total=...` when the iterable has no `len()`, and `postfix=lambda item: ...` to annotate the bar with per-item context.

## Configuration sources

Experiments may define two models:

- `Args`: execution options exposed as CLI flags and available at `ctx.args`.
- `Config`: semantic configuration loaded from `varve.yaml` and available at `ctx.config`.

`varve.yaml` lives next to the experiment module by default. It maps branch names to Config values. If the file is missing, the `main` branch uses the Config model defaults:

```yaml
main:
  seed: 1
smoke:
  is_temporary: true
  seed: 2
```

`--branch NAME` is the only branch selector. `--override '{"seed": 3}'` is only accepted by `run`; it deep-merges JSON over `main` and creates or reuses a temporary branch under `.tmp/`. Without an explicit non-main `--branch`, the temporary branch is named from the canonical JSON of the fully validated Config, such as `main_override_<hash>`. With `--branch quick --override ...`, `quick` is a named temporary branch, and later `status --branch quick` or `clean --branch quick` can locate it without repeating the override.

If a temporary branch was created with a custom `--out PATH`, later `status` or `clean` calls must pass the same `--out PATH`.

Config values still receive environment and `.env` fallback for fields not supplied by the selected branch:

```text
branch or override value > env > dotenv (.env) > field default
```

Nested environment variables use `__` as the delimiter, for example `INNER__NAME` for `inner.name`.

The CLI is strict: unknown options fail instead of being ignored. Args flags are generated at runtime from the experiment `Args`, while `--out`, `--branch`, and `--override` are built-in command options owned by varve. Config fields are not generated as CLI flags.

## Known limitations

- varve hashes the whole Config, declared file/value inputs, and source ASTs. Same-module helper functions directly called by a stage or helper must be listed in `uses`; aliases, methods, indirect calls, closures, and decorator wrappers are not detected by that guard. If an output is changed outside varve, use `clean` to reset the affected stages.
- Source AST fingerprints are derived from `ast.dump`, whose output format can change between CPython minor versions. Upgrading the Python interpreter may therefore invalidate every stage's source hash at once, forcing a full rebuild. Run `clean` to reset after such an upgrade.
