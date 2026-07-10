"""Tests for bounded source dependency discovery."""

from __future__ import annotations

import importlib
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

from varve.keying import dependencies
from varve.keying.dependencies import default_packages, discover_source_dependencies


@contextmanager
def loaded_project(
    tmp_path: Path,
    files: dict[str, str],
    *,
    entry: str = "sample_project.pipeline",
) -> Iterator[ModuleType]:
    names: set[str] = set()
    for module_name, source in files.items():
        parts = module_name.split(".")
        for index in range(1, len(parts)):
            package_name = ".".join(parts[:index])
            package_path = tmp_path.joinpath(*parts[:index])
            package_path.mkdir(parents=True, exist_ok=True)
            init_path = package_path / "__init__.py"
            if not init_path.exists():
                init_path.write_text("", encoding="utf-8")
            names.add(package_name)
        path = tmp_path.joinpath(*parts).with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        names.add(module_name)

    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    try:
        yield importlib.import_module(entry)
    finally:
        sys.path.remove(str(tmp_path))
        for name in sorted(names, key=len, reverse=True):
            sys.modules.pop(name, None)
        importlib.invalidate_caches()


def test_discovers_supported_function_references(tmp_path: Path) -> None:
    with loaded_project(
        tmp_path,
        {
            "sample_project.helpers": """
                def imported_alias(value):
                    return value
            """,
            "sample_project.pipeline": """
                from sample_project.helpers import imported_alias

                GLOBAL_VALUE = {"mode": "strict", "limit": 3}

                def local_helper(value):
                    return imported_alias(value)

                def make_stage():
                    captured_helper = imported_alias
                    captured_value = ["closure"]

                    def stage(value, transform=local_helper, mode="strict"):
                        captured = GLOBAL_VALUE
                        def nested(item):
                            return imported_alias(item)
                        return (
                            transform(captured_helper(nested(value))),
                            captured,
                            captured_value,
                            mode,
                        )

                    return stage

                stage = make_stage()
            """,
        },
    ) as module:
        result = discover_source_dependencies(
            module.stage,
            explicit_uses=(),
            auto_uses=True,
            packages=("sample_project",),
        )

        assert result.component_names() == {
            "auto.function.sample_project.helpers.imported_alias",
            "auto.function.sample_project.pipeline.local_helper",
            "auto.value.sample_project.pipeline.make_stage.<locals>.stage.closure.captured_value",
            "auto.value.sample_project.pipeline.make_stage.<locals>.stage.default.mode",
            "auto.value.sample_project.pipeline.make_stage.<locals>.stage.global.GLOBAL_VALUE",
        }
        assert result.diagnostics == ()


def test_simple_module_attributes_resolve_functions_classes_and_values(tmp_path: Path) -> None:
    with loaded_project(
        tmp_path,
        {
            "sample_project.helpers": """
                LIMIT = 4

                def normalize(value):
                    return value

                class Renderer:
                    def render(self, value):
                        return normalize(value)
            """,
            "sample_project.pipeline": """
                import sample_project.helpers as helpers

                def stage(value):
                    return helpers.normalize(value), helpers.Renderer, helpers.LIMIT
            """,
        },
    ) as module:
        result = discover_source_dependencies(
            module.stage,
            explicit_uses=(),
            auto_uses=True,
            packages=("sample_project",),
        )

        assert "auto.function.sample_project.helpers.normalize" in result.components
        assert "auto.class.sample_project.helpers.Renderer" in result.components
        assert "auto.value.sample_project.helpers.attr.LIMIT" in result.components


def test_none_and_empty_package_scopes(tmp_path: Path) -> None:
    with loaded_project(
        tmp_path,
        {
            "sample_project.helpers": """
                def helper(value):
                    return value
            """,
            "sample_project.pipeline": """
                from sample_project.helpers import helper

                def stage(value):
                    return helper(value)
            """,
        },
    ) as module:
        inferred = discover_source_dependencies(
            module.stage,
            explicit_uses=(),
            auto_uses=True,
            packages=None,
        )
        disabled = discover_source_dependencies(
            module.stage,
            explicit_uses=(),
            auto_uses=True,
            packages=(),
        )
        explicit = discover_source_dependencies(
            module.stage,
            explicit_uses=(module.helper,),
            auto_uses=True,
            packages=(),
        )

        assert "auto.function.sample_project.helpers.helper" in inferred.components
        assert disabled.nodes == {}
        assert explicit.component_names() == {"uses.function.sample_project.helpers.helper"}


def test_explicit_root_recurses_only_when_auto_uses_is_enabled(tmp_path: Path) -> None:
    with loaded_project(
        tmp_path,
        {
            "outside_pkg.helpers": """
                def child(value):
                    return value

                def root(value):
                    return child(value)
            """,
            "sample_project.pipeline": """
                def stage(value):
                    return value
            """,
        },
    ) as module:
        outside = importlib.import_module("outside_pkg.helpers")
        result = discover_source_dependencies(
            module.stage,
            explicit_uses=(outside.root,),
            auto_uses=True,
            packages=("outside_pkg",),
        )
        no_auto = discover_source_dependencies(
            module.stage,
            explicit_uses=(outside.root,),
            auto_uses=False,
            packages=("outside_pkg",),
        )

        assert "uses.function.outside_pkg.helpers.root" in result.components
        assert "auto.function.outside_pkg.helpers.child" in result.components
        assert no_auto.component_names() == {"uses.function.outside_pkg.helpers.root"}


def test_main_without_spec_defaults_to_main_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def main_function() -> None:
        return None

    original_module = main_function.__module__
    main_function.__module__ = "__main__"
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None)
    try:
        assert default_packages(main_function) == ("__main__",)
    finally:
        main_function.__module__ = original_module


def test_dynamic_calls_are_ignored_without_diagnostics() -> None:
    registry: dict[str, object] = {}

    def dynamic_stage(value, transform, backend, method_name):
        return (
            transform(value),
            backend.normalize(value),
            getattr(backend, method_name)(value),
            registry[method_name],
        )

    result = discover_source_dependencies(
        dynamic_stage,
        explicit_uses=(),
        auto_uses=True,
        packages=(__name__.split(".", 1)[0],),
    )

    assert result.diagnostics == ()
    assert all("backend.normalize" not in edge.reason for edge in result.edges)


def test_recursive_stage_root_does_not_become_a_dependency_node() -> None:
    def recursive_stage(value: int) -> int:
        return value if value == 0 else recursive_stage(value - 1)

    result = discover_source_dependencies(
        recursive_stage,
        explicit_uses=(),
        auto_uses=True,
        packages=(__name__.split(".", 1)[0],),
    )

    assert result.components == {}
    assert result.nodes == {}


def test_class_is_hashed_whole_and_discovers_owned_methods_and_base(tmp_path: Path) -> None:
    with loaded_project(
        tmp_path,
        {
            "sample_project.helpers": """
                def ordinary(value): return value
                def static(value): return value
                def classed(value): return value
                def property_get(value): return value
                def base_helper(value): return value
            """,
            "sample_project.render": """
                from sample_project.helpers import (
                    base_helper, classed, ordinary, property_get, static
                )

                class Base:
                    def base(self, value):
                        return base_helper(value)

                class Renderer(Base):
                    def render(self, value):
                        return ordinary(value)

                    @staticmethod
                    def clean(value):
                        return static(value)

                    @classmethod
                    def build(cls, value):
                        return classed(value)

                    @property
                    def label(self):
                        return property_get("label")
            """,
            "sample_project.pipeline": """
                from sample_project.render import Renderer

                def stage(value):
                    return Renderer().render(value)
            """,
        },
    ) as module:
        result = discover_source_dependencies(
            module.stage,
            explicit_uses=(),
            auto_uses=True,
            packages=("sample_project",),
        )

        assert "auto.class.sample_project.render.Renderer" in result.components
        assert "auto.class.sample_project.render.Base" in result.components
        assert not any("Renderer.render" in name for name in result.components)
        renderer = result.find("sample_project.render.Renderer")
        assert renderer is not None
        assert renderer.scope == "whole class"
        for name in ("ordinary", "static", "classed", "property_get", "base_helper"):
            assert result.find(f"sample_project.helpers.{name}") is not None


def test_whole_class_digest_changes_for_unrelated_method(tmp_path: Path) -> None:
    sources = {
        "sample_project.render": """
            class Renderer:
                def used(self): return 1
                def unrelated(self): return 2
        """,
        "sample_project.pipeline": """
            from sample_project.render import Renderer
            def stage(): return Renderer().used()
        """,
    }
    with loaded_project(tmp_path, sources) as module:
        first = discover_source_dependencies(
            module.stage,
            explicit_uses=(),
            auto_uses=True,
            packages=("sample_project",),
        ).components["auto.class.sample_project.render.Renderer"]

    sources["sample_project.render"] = sources["sample_project.render"].replace(
        "return 2", "return 3"
    )
    with loaded_project(tmp_path, sources) as module:
        second = discover_source_dependencies(
            module.stage,
            explicit_uses=(),
            auto_uses=True,
            packages=("sample_project",),
        ).components["auto.class.sample_project.render.Renderer"]

    assert first != second


def test_module_fallback_is_narrow_and_reexports_use_actual_object(tmp_path: Path) -> None:
    with loaded_project(
        tmp_path,
        {
            "sample_project.implementation": """
                def exported(value): return value
            """,
            "sample_project.registry": """
                from sample_project.implementation import exported
                REGISTRY = object()
            """,
            "sample_project.pipeline": """
                import sample_project.registry as registry

                def bare(): return registry
                def unsupported(): return registry.REGISTRY
                def reexport(value): return registry.exported(value)
            """,
        },
    ) as module:
        bare = discover_source_dependencies(
            module.bare,
            explicit_uses=(),
            auto_uses=True,
            packages=("sample_project",),
        )
        unsupported = discover_source_dependencies(
            module.unsupported,
            explicit_uses=(),
            auto_uses=True,
            packages=("sample_project",),
        )
        reexport = discover_source_dependencies(
            module.reexport,
            explicit_uses=(),
            auto_uses=True,
            packages=("sample_project",),
        )

        assert bare.component_names() == {"auto.module.sample_project.registry"}
        assert unsupported.component_names() == {"auto.module.sample_project.registry"}
        node = unsupported.find("sample_project.registry")
        assert node is not None
        assert node.scope == "module file"
        assert reexport.component_names() == {
            "auto.function.sample_project.implementation.exported"
        }


def test_inferred_source_failure_skips_only_that_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with loaded_project(
        tmp_path,
        {
            "sample_project.pipeline": """
                def good(value): return value
                def bad(value): return value
                def stage(value): return good(value), bad(value)
            """,
        },
    ) as module:
        original = dependencies.source_hash

        def selective_failure(value):
            if value is module.bad:
                raise ValueError("cannot inspect")
            return original(value)

        monkeypatch.setattr(dependencies, "source_hash", selective_failure)
        result = discover_source_dependencies(
            module.stage,
            explicit_uses=(),
            auto_uses=True,
            packages=("sample_project",),
        )

        assert result.find("sample_project.pipeline.good") is not None
        assert result.find("sample_project.pipeline.bad") is None


def test_unexpected_discovery_failure_preserves_explicit_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with loaded_project(
        tmp_path,
        {
            "sample_project.pipeline": """
                def explicit(value): return value
                def stage(value): return value
            """,
        },
    ) as module:
        monkeypatch.setattr(
            dependencies._Builder,
            "add_inferred_from",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken analyzer")),
        )
        result = discover_source_dependencies(
            module.stage,
            explicit_uses=(module.explicit,),
            auto_uses=True,
            packages=None,
        )

        assert result.component_names() == {"uses.function.sample_project.pipeline.explicit"}


def test_explicit_source_failure_is_strict() -> None:
    with pytest.raises(TypeError):
        discover_source_dependencies(
            lambda: None,
            explicit_uses=(len,),
            auto_uses=False,
            packages=(),
        )
