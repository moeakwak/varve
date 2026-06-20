"""Command-line interface for Experiment subclasses."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError, create_model
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from varve.cli import argmap
from varve.cli.clean import clean
from varve.engine.runner import run, selected_stages
from varve.experiment import Experiment
from varve.log import configure_cli_logging

_COMMANDS = {"run", "status", "clean", "plan", "list"}
_CONFIG_COMMANDS = {"run", "status", "clean"}
_NEGATIVE_NUMBER_RE = re.compile(r"^-\d+$|^-\d*\.\d+$")
_COMMAND_OPTION_ARITIES = {
    "run": {
        "--only": 1,
        "-s": 1,
        "--downstream": 1,
        "--force": 0,
        "-f": 0,
        "--dry": 0,
        "--config": 1,
    },
    "status": {"--config": 1},
    "clean": {"--yes": 0, "-y": 0, "--config": 1},
}


def _settings_type(config_type: type[BaseModel], yaml_file: Path | None = None):
    class VarveSettings(BaseSettings):
        model_config = SettingsConfigDict(
            env_nested_delimiter="__",
            env_file=".env",
        )

        _yaml_file: ClassVar[Path | None] = yaml_file

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            sources = [init_settings]
            sources.append(env_settings)
            sources.append(dotenv_settings)
            if cls._yaml_file is not None:
                sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=cls._yaml_file))
            sources.append(file_secret_settings)
            return tuple(sources)

    fields = {
        name: (field.annotation, field)
        for name, field in config_type.model_fields.items()
    }
    return create_model(f"{config_type.__name__}VarveSettings", __base__=VarveSettings, **fields)


def _config_from_args(
    config_type: type[BaseModel],
    *,
    init_kwargs: dict[str, Any],
    yaml_file: Path | None,
) -> BaseModel:
    settings_type = _settings_type(config_type, yaml_file)
    settings = settings_type(**init_kwargs)
    return config_type.model_validate(settings.model_dump())


def _clean_config_from_args(
    config_type: type[BaseModel],
    *,
    init_kwargs: dict[str, Any],
    yaml_file: Path | None,
) -> Any:
    """Build a config for clean, tolerating a bare output root.

    clean only needs the output root to locate the store, so when the full
    Config cannot be built (required fields missing) we fall back to a minimal
    object carrying just the output root provided on the CLI.
    """
    try:
        return _config_from_args(config_type, init_kwargs=init_kwargs, yaml_file=yaml_file)
    except ValidationError:
        out = init_kwargs.get("output_root") or init_kwargs.get("out")
        if out is None:
            raise
        out_path = Path(out)
        return SimpleNamespace(out=out_path, output_root=out_path)


def _print_list(experiment: type[Experiment]) -> None:
    for name in experiment.topo_order():
        spec = experiment.stages()[name]
        needs = ",".join(spec.needs) if spec.needs else "-"
        kind = "batch" if spec.kind == "batch" else "stage"
        print(f"{name}\t{kind}\tneeds={needs}")


def _print_plan(
    experiment: type[Experiment],
    *,
    target: str | None,
    mermaid: bool,
    dot: bool,
) -> None:
    stages = experiment.stages()
    selected = selected_stages(experiment, target=target) if target else set(stages)
    if mermaid:
        print("flowchart TD")
        for name, spec in stages.items():
            if name not in selected:
                continue
            if not spec.needs:
                print(f"  {name}[{name}]")
            for upstream in spec.needs:
                if upstream in selected:
                    print(f"  {upstream} --> {name}")
        return
    if dot:
        print("digraph varve {")
        for name, spec in stages.items():
            if name not in selected:
                continue
            if not spec.needs:
                print(f'  "{name}";')
            for upstream in spec.needs:
                if upstream in selected:
                    print(f'  "{upstream}" -> "{name}";')
        print("}")
        return
    print(" -> ".join(name for name in experiment.topo_order() if name in selected))


def _print_status(experiment: type[Experiment], config, target: str | None) -> None:
    outcomes = run(experiment, config, target=target, dry=True)
    for outcome in outcomes:
        print(f"{outcome.stage}\t{outcome.status}\t{outcome.reason}")


def _default_confirm(message: str) -> bool:
    try:
        answer = input(f"{message} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _selected_command_index(argv: list[str]) -> int | None:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"-v", "--verbose"}:
            index += 1
            continue
        if token == "--":
            next_index = index + 1
            return next_index if next_index < len(argv) else None
        if token.startswith("-"):
            return None
        return index
    return None


def _selected_command(argv: list[str]) -> str | None:
    index = _selected_command_index(argv)
    if index is None:
        return None
    return argv[index]


def _option_name(token: str) -> str:
    if token.startswith("--"):
        return token.split("=", 1)[0]
    return token


def _looks_like_option(token: str) -> bool:
    return token.startswith("-") and token != "-" and _NEGATIVE_NUMBER_RE.match(token) is None


def _has_unknown_option_before_config_registration(
    *,
    command: str,
    command_args: list[str],
    config_type: type[BaseModel],
) -> bool:
    option_arities = argmap.config_option_arities(config_type)
    option_arities.update(_COMMAND_OPTION_ARITIES[command])
    # Let argparse handle help instead of failing the strict precheck.
    option_arities.setdefault("--help", 0)
    option_arities.setdefault("-h", 0)

    index = 0
    while index < len(command_args):
        token = command_args[index]
        if token == "--":
            return False
        if not token.startswith("-") or token == "-":
            index += 1
            continue
        option = _option_name(token)
        arity = option_arities.get(option)
        if arity is None:
            return True
        index += 1
        if arity == 1 and "=" not in token:
            if index >= len(command_args) or _looks_like_option(command_args[index]):
                return True
            index += 1
    return False


def main(experiment: type[Experiment], argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    selected_command_index = _selected_command_index(raw_argv)
    selected_command = _selected_command(raw_argv)
    parser = argparse.ArgumentParser(prog=experiment.__name__)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("target", nargs="?")
    run_parser.add_argument("--only", "-s")
    run_parser.add_argument("--downstream")
    run_parser.add_argument("--force", "-f", action="store_true")
    run_parser.add_argument("--dry", action="store_true")
    run_parser.add_argument("--config", type=Path)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("target", nargs="?")
    status_parser.add_argument("--config", type=Path)

    clean_parser = subparsers.add_parser("clean")
    clean_parser.add_argument("target", nargs="?")
    clean_parser.add_argument("--yes", "-y", action="store_true")
    clean_parser.add_argument("--config", type=Path)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("target", nargs="?")
    plan_parser.add_argument("--mermaid", action="store_true")
    plan_parser.add_argument("--dot", action="store_true")

    subparsers.add_parser("list")

    if selected_command in _CONFIG_COMMANDS and selected_command_index is not None:
        command_args = raw_argv[selected_command_index + 1 :]
        if _has_unknown_option_before_config_registration(
            command=selected_command,
            command_args=command_args,
            config_type=experiment.Config,
        ):
            parser.error("unknown option or missing option value")

    if selected_command == "run":
        argmap.register_config_args(run_parser, experiment.Config)
    if selected_command == "status":
        argmap.register_config_args(status_parser, experiment.Config)
    if selected_command == "clean":
        argmap.register_config_args(clean_parser, experiment.Config)

    namespace = parser.parse_args(raw_argv)
    configure_cli_logging(namespace.verbose)

    if namespace.command == "list":
        _print_list(experiment)
        return 0
    if namespace.command == "plan":
        _print_plan(experiment, target=namespace.target, mermaid=namespace.mermaid, dot=namespace.dot)
        return 0

    cli_overrides = argmap.collect_cli_config_namespace(namespace, experiment.Config)
    if namespace.command == "clean":
        config = _clean_config_from_args(
            experiment.Config,
            init_kwargs=cli_overrides,
            yaml_file=namespace.config,
        )
    else:
        config = _config_from_args(
            experiment.Config,
            init_kwargs=cli_overrides,
            yaml_file=namespace.config,
        )
    if namespace.command == "status":
        _print_status(experiment, config, namespace.target)
    elif namespace.command == "clean":
        clean(
            experiment,
            config,
            target=namespace.target,
            yes=namespace.yes,
            allowed_roots=experiment.clean_roots(config),
            confirm=_default_confirm,
        )
    elif namespace.command == "run":
        outcomes = run(
            experiment,
            config,
            target=namespace.target,
            only=namespace.only,
            downstream=namespace.downstream,
            force=namespace.force,
            dry=namespace.dry,
        )
        for outcome in outcomes:
            print(f"{outcome.stage}\t{outcome.status}\t{outcome.reason}")
    return 0
