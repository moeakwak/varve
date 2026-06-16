"""Command-line interface for Experiment subclasses."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import ClassVar, get_args, get_origin

from pydantic import BaseModel, create_model
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from varve.clean import clean
from varve.experiment import Experiment
from varve.log import configure_cli_logging
from varve.runner import run, selected_stages


def _settings_type(config_type: type[BaseModel], yaml_file: Path | None = None):
    class VarveSettings(BaseSettings):
        model_config = SettingsConfigDict(
            cli_implicit_flags="dual",
            cli_kebab_case=True,
            cli_ignore_unknown_args=True,
            env_nested_delimiter="__",
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
    argv: list[str],
    yaml_file: Path | None,
) -> BaseModel:
    settings_type = _settings_type(config_type, yaml_file)
    settings = settings_type(_cli_parse_args=argv)
    return config_type.model_validate(settings.model_dump())


def _extract_option(args: list[str], names: set[str]) -> str | None:
    for index, token in enumerate(args):
        if token in names and index + 1 < len(args):
            return args[index + 1]
        for name in names:
            prefix = name + "="
            if token.startswith(prefix):
                return token[len(prefix) :]
    return None


def _field_type(annotation):
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    return _field_type(args[0]) if args else annotation


def _is_model_type(annotation) -> bool:
    value_type = _field_type(annotation)
    return isinstance(value_type, type) and issubclass(value_type, BaseModel)


def _config_option_sets(
    config_type: type[BaseModel],
    *,
    prefix: str = "",
) -> tuple[set[str], set[str]]:
    value_options: set[str] = set()
    flag_options: set[str] = set()
    for name, field in config_type.model_fields.items():
        option_name = f"{prefix}{name.replace('_', '-')}"
        option = "--" + option_name
        field_type = _field_type(field.annotation)
        if _is_model_type(field.annotation):
            nested_values, nested_flags = _config_option_sets(field_type, prefix=f"{option_name}.")
            value_options |= nested_values
            flag_options |= nested_flags
        elif field_type is bool:
            flag_options.add(option)
            flag_options.add("--no-" + option_name)
        else:
            value_options.add(option)
    return value_options, flag_options


def _has_flag(args: list[str], names: set[str]) -> bool:
    return any(token in names for token in args)


def _target_from_args(args: list[str], known_value_options: set[str], known_flags: set[str]) -> str | None:
    index = 0
    while index < len(args):
        token = args[index]
        if token in known_flags:
            index += 1
            continue
        if token.startswith("-") and "=" in token:
            index += 1
            continue
        if token in known_value_options:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


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


def main(experiment: type[Experiment], argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
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

    namespace, _unknown = parser.parse_known_args(raw_argv)
    command_args = []
    if raw_argv is not None and namespace.command in raw_argv:
        command_args = raw_argv[raw_argv.index(namespace.command) + 1 :]
    configure_cli_logging(namespace.verbose)

    if namespace.command == "list":
        _print_list(experiment)
        return 0
    if namespace.command == "plan":
        _print_plan(experiment, target=namespace.target, mermaid=namespace.mermaid, dot=namespace.dot)
        return 0

    config_value_options, config_flags = _config_option_sets(experiment.Config)
    target = _target_from_args(
        command_args,
        known_value_options={"--only", "-s", "--downstream", "--config"} | config_value_options,
        known_flags={"--force", "-f", "--dry", "--yes", "-y"} | config_flags,
    )
    if namespace.command == "run":
        only = _extract_option(command_args, {"--only", "-s"})
        downstream = _extract_option(command_args, {"--downstream"})
        force = _has_flag(command_args, {"--force", "-f"})
        dry = _has_flag(command_args, {"--dry"})
    else:
        only = None
        downstream = None
        force = False
        dry = False
    if namespace.command == "clean":
        yes = _has_flag(command_args, {"--yes", "-y"})
    else:
        yes = False
    config_path = _extract_option(command_args, {"--config"})

    config = _config_from_args(
        experiment.Config,
        argv=raw_argv or [],
        yaml_file=Path(config_path) if config_path else None,
    )
    if namespace.command == "status":
        _print_status(experiment, config, target)
    elif namespace.command == "clean":
        clean(experiment, config, target=target, yes=yes)
    elif namespace.command == "run":
        outcomes = run(
            experiment,
            config,
            target=target,
            only=only,
            downstream=downstream,
            force=force,
            dry=dry,
        )
        for outcome in outcomes:
            print(f"{outcome.stage}\t{outcome.status}\t{outcome.reason}")
    return 0
