from __future__ import annotations

import importlib.util
import itertools
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from varve.keying import fingerprint as fingerprint_module
from varve.keying.fingerprint import FingerprintSession, StatCache
from varve.keying.source import PREAMBLE, SourceAnalyzer

_counter = itertools.count()

BASE = '''\
"""Module docstring."""
import json
from os.path import join as pjoin

from pydantic import BaseModel
from varve import Pipeline, stage

SCHEMA = {"a": 1}
SHARED = 10


def helper_a():
    return SCHEMA


def helper_b():
    return pjoin("x", "y")


class Config(BaseModel):
    size: int = 1


class Args(BaseModel):
    workers: int = 1


class Demo(Pipeline):
    Config = Config
    Args = Args

    @stage()
    def a(self, ctx):
        self._util()
        return helper_a(), SHARED

    @stage()
    def b(self, ctx):
        return helper_b(), json.dumps(SHARED)

    def _util(self):
        return 1


if __name__ == "__main__":
    raise SystemExit(Demo.cli())
'''


def load(directory: Path, source: str) -> type[Any]:
    path = directory / "demo_pipeline.py"
    path.write_text(source)
    name = f"varve_source_demo_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.Demo


def fps(directory: Path, source: str, **sources: list[Any]) -> dict[str, str]:
    pipeline = load(directory, source)
    analyzer = SourceAnalyzer()
    return {
        name: analyzer.stage_source(pipeline, name, sources.get(name, ())).fingerprint
        for name in ("a", "b")
    }


def changed(tmp_path: Path, old: str, new: str, *, count: int = 1) -> set[str]:
    before = fps(tmp_path, BASE)
    edited = BASE.replace(old, new, count)
    assert edited != BASE
    after = fps(tmp_path, edited)
    return {name for name in before if before[name] != after[name]}


def test_closure_detail_lists_reached_symbols(tmp_path: Path) -> None:
    pipeline = load(tmp_path, BASE)
    detail = SourceAnalyzer().stage_source(pipeline, "a").detail
    assert detail["mode"] == "closure" and detail["fallback_reason"] is None
    assert set(detail["symbols"]) == {
        "Demo.a",
        "Demo._util",
        "Demo.<header>",
        "helper_a",
        "SCHEMA",
        "SHARED",
        "Pipeline",
        "stage",
    }
    b = SourceAnalyzer().stage_source(pipeline, "b").detail["symbols"]
    assert {"helper_b", "pjoin", "json", "SHARED"} <= set(b)
    assert "Config" not in b and "Demo.a" not in b


def test_comments_docstring_and_formatting_do_not_matter(tmp_path: Path) -> None:
    assert changed(tmp_path, '"""Module docstring."""', '"""Edited."""\n# comment\n') == set()
    assert (
        changed(tmp_path, "return helper_a(), SHARED", "return (helper_a(),   SHARED)  # x")
        == set()
    )


def test_helper_edit_only_affects_stages_that_reach_it(tmp_path: Path) -> None:
    assert changed(tmp_path, 'pjoin("x", "y")', 'pjoin("x", "z")') == {"b"}
    assert changed(tmp_path, 'SCHEMA = {"a": 1}', 'SCHEMA = {"a": 2}') == {"a"}
    assert changed(tmp_path, "return 1", "return 2") == {"a"}  # self._util
    assert changed(tmp_path, "SHARED = 10", "SHARED = 11") == {"a", "b"}


def test_stage_body_edit_does_not_affect_other_stages(tmp_path: Path) -> None:
    assert changed(tmp_path, "json.dumps(SHARED)", "json.dumps(SHARED, indent=1)") == {"b"}
    assert changed(tmp_path, "    @stage()\n    def a", "    @stage(version=2)\n    def a") == {"a"}


def test_import_change_only_affects_stages_using_it(tmp_path: Path) -> None:
    assert changed(
        tmp_path, "from os.path import join as pjoin", "from posixpath import join as pjoin"
    ) == {"b"}
    assert changed(tmp_path, "import json\n", "import json as json\n") == {"b"}
    assert (
        changed(
            tmp_path, "from varve import Pipeline, stage", "from varve import Ctx, Pipeline, stage"
        )
        == set()
    )


def test_unused_bindings_and_main_block_change_nothing(tmp_path: Path) -> None:
    assert changed(tmp_path, "import json\n", "import json\nimport csv\n") == set()
    assert changed(tmp_path, "SHARED = 10\n", "SHARED = 10\nUNUSED = [1, 2]\n") == set()
    assert changed(tmp_path, "raise SystemExit(Demo.cli())", "Demo.cli()") == set()


def test_config_and_args_bodies_affect_nobody(tmp_path: Path) -> None:
    assert (
        changed(tmp_path, "    size: int = 1", "    size: int = 2\n    extra: str = 'x'") == set()
    )
    assert changed(tmp_path, "    workers: int = 1", "    workers: int = 8") == set()


def test_preamble_statements_affect_every_stage(tmp_path: Path) -> None:
    with_preamble = BASE.replace("SHARED = 10\n", "SHARED = 10\nprint('setup')\n")
    before = fps(tmp_path, with_preamble)
    after = fps(tmp_path, with_preamble.replace("print('setup')", "print('changed')"))
    assert all(before[name] != after[name] for name in before)
    detail = SourceAnalyzer().stage_source(load(tmp_path, with_preamble), "a").detail
    assert PREAMBLE in detail["symbols"]


@pytest.mark.parametrize(
    ("snippet", "reason"),
    [
        ("globals()", "globals()"),
        ("eval('1')", "eval()"),
        ("vars()", "vars()"),
        ("sys.modules[__name__]", "sys.modules[__name__]"),
        ("importlib.import_module(__name__)", "importlib.import_module(__name__)"),
    ],
)
def test_dynamic_lookup_falls_back_to_whole_file(tmp_path: Path, snippet: str, reason: str) -> None:
    dynamic = BASE.replace("def helper_b():\n", f"def helper_b():\n    {snippet}\n")
    pipeline = load(tmp_path, dynamic)
    detail = SourceAnalyzer().stage_source(pipeline, "a").detail
    assert detail["mode"] == "whole-file" and detail["fallback_reason"] == reason
    before = fps(tmp_path, dynamic)
    after = fps(tmp_path, dynamic.replace('pjoin("x", "y")', 'pjoin("x", "z")'))
    assert before["a"] != after["a"] and before["b"] != after["b"]


def test_cross_file_sources_enter_the_fingerprint(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "render.py").write_text("def render():\n    return 1\n")
    (shared / "notes.txt").write_text("not python")
    single = tmp_path / "extra.py"
    single.write_text("X = 1\n")

    before = fps(tmp_path, BASE, a=["shared"], b=[single])
    pipeline = load(tmp_path, BASE)
    detail = SourceAnalyzer().stage_source(pipeline, "a", ["shared", single]).detail
    assert list(detail["external"]) == ["extra.py", "shared/render.py"]

    (shared / "render.py").write_text("def render():\n    # comment only\n    return 1\n")
    assert fps(tmp_path, BASE, a=["shared"], b=[single]) == before
    (shared / "render.py").write_text("def render():\n    return 2\n")
    after = fps(tmp_path, BASE, a=["shared"], b=[single])
    assert after["a"] != before["a"] and after["b"] == before["b"]

    with pytest.raises(ValueError, match="must end in .py"):
        SourceAnalyzer().stage_source(pipeline, "a", [shared / "notes.txt"])


def test_glob_sources_equal_the_explicit_files_they_match(tmp_path: Path) -> None:
    from dataclasses import replace

    from varve import Dependencies

    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    for rel in ("a.py", "sub/b.py", "sub/test_b.py", "sub/data.json"):
        (pkg / rel).write_text("X = 1\n" if rel.endswith(".py") else "{}")
    explicit = [pkg / "a.py", pkg / "sub/b.py"]
    glob = Dependencies.glob(pkg, include="**/*.py", exclude="**/test_*.py")
    relative = Dependencies.glob("pkg", include="**/*.py", exclude="**/test_*.py")
    assert fps(tmp_path, BASE, a=[glob]) == fps(tmp_path, BASE, a=explicit)
    assert fps(tmp_path, BASE, a=[relative]) == fps(tmp_path, BASE, a=explicit)
    pipeline = load(tmp_path, BASE)
    detail = SourceAnalyzer().stage_source(pipeline, "a", [glob]).detail
    assert list(detail["external"]) == ["pkg/a.py", "pkg/sub/b.py"]

    with pytest.raises(ValueError, match="must end in .py"):
        SourceAnalyzer().stage_source(pipeline, "a", [Dependencies.glob(pkg, "sub/*")])
    with pytest.raises(FileNotFoundError, match="matched no files"):
        SourceAnalyzer().stage_source(pipeline, "a", [Dependencies.glob(pkg, "*.pyx")])
    with pytest.raises(ValueError, match="cannot declare sources"):
        SourceAnalyzer().stage_source(pipeline, "a", [replace(glob, mode="stat")])


def test_checkout_location_does_not_enter_the_fingerprint(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    for directory in (first, second):
        (directory / "lib").mkdir(parents=True)
        (directory / "lib" / "mod.py").write_text("Y = 2\n")
    assert fps(first, BASE, a=["lib"]) == fps(second, BASE, a=["lib"])


def test_symlinked_source_root_prefix_is_allowed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "mod.py").write_text("Z = 3\n")
    (tmp_path / "link").symlink_to(real)
    assert fps(tmp_path, BASE, a=["link"])["a"]
    (real / "inner").symlink_to(real / "mod.py")
    with pytest.raises(ValueError, match="Symlinks are not allowed"):
        fps(tmp_path, BASE, a=["link"])


def test_external_ast_hash_uses_the_stat_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ext.py").write_text("A = 1\n")
    pipeline = load(tmp_path, BASE)
    cache = StatCache()
    first = SourceAnalyzer(FingerprintSession(cache)).stage_source(pipeline, "a", ["ext.py"])
    rows = cache.drain()
    assert any(row.ast_hash for row in rows.values())

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("external file was re-parsed")

    monkeypatch.setattr(fingerprint_module, "normalized_ast", fail)
    second = SourceAnalyzer(FingerprintSession(StatCache(rows))).stage_source(
        pipeline, "a", ["ext.py"]
    )
    assert second.fingerprint == first.fingerprint


def test_copy_of_pipeline_file_has_same_fingerprint(tmp_path: Path) -> None:
    other = tmp_path / "copy"
    other.mkdir()
    before = fps(tmp_path, BASE)
    shutil.copy(tmp_path / "demo_pipeline.py", other / "demo_pipeline.py")
    assert fps(other, BASE) == before


def test_class_body_members_enter_every_stage(tmp_path: Path) -> None:
    with_attrs = BASE.replace(
        "    Config = Config\n",
        "    Config = Config\n    defaults = {'depends': SIZE}\n    SCALE = 1\n\n"
        "    def __init__(self):\n        self.k = 0\n",
    ).replace("SHARED = 10\n", "SHARED = 10\nSIZE = 2\n")

    def edit(old: str, new: str) -> set[str]:
        before = fps(tmp_path, with_attrs)
        after = fps(tmp_path, with_attrs.replace(old, new))
        return {name for name in before if before[name] != after[name]}

    assert edit("SCALE = 1", "SCALE = 2") == {"a", "b"}
    assert edit("self.k = 0", "self.k = 1") == {"a", "b"}  # dunder methods run for every stage
    assert edit("SIZE = 2", "SIZE = 3") == {"a", "b"}  # followed from `defaults`
    # A stage's private helper still only affects that stage.
    assert edit('pjoin("x", "y")', 'pjoin("x", "z")') == {"b"}


def test_module_level_class_mutation_enters_the_closure(tmp_path: Path) -> None:
    mutated = BASE.replace(
        'if __name__ == "__main__":', 'Demo.SCALE = 1\n\n\nif __name__ == "__main__":'
    )
    before = fps(tmp_path, mutated)
    after = fps(tmp_path, mutated.replace("Demo.SCALE = 1", "Demo.SCALE = 2"))
    assert before["a"] != after["a"] and before["b"] != after["b"]


def test_type_self_attribute_reaches_the_member(tmp_path: Path) -> None:
    source = BASE.replace("        return helper_b(),", "        return type(self).Y, helper_b(),")
    source = source.replace(
        "    def _util(self):", "    def Y(self):\n        return 5\n\n    def _util(self):"
    )
    before = fps(tmp_path, source)
    after = fps(tmp_path, source.replace("return 5", "return 6"))
    assert before["b"] != after["b"]


@pytest.mark.parametrize("owner", ["self", "cls", "type(self)", "self.__class__", "Demo"])
def test_dynamic_class_getattr_falls_back_to_whole_file(tmp_path: Path, owner: str) -> None:
    source = BASE.replace("        self._util()\n", f"        getattr({owner}, ctx)\n")
    pipeline = load(tmp_path, source)
    detail = SourceAnalyzer().stage_source(pipeline, "b").detail
    assert detail["mode"] == "whole-file"
    assert detail["fallback_reason"].startswith(f"getattr({owner}, <non-constant>)")
    constant = BASE.replace("        self._util()\n", f"        getattr({owner}, 'x')\n")
    detail = SourceAnalyzer().stage_source(load(tmp_path, constant), "b").detail
    assert detail["mode"] == "closure"


def test_scheduling_keywords_do_not_enter_the_fingerprint(tmp_path: Path) -> None:
    for old, new in [
        ("    @stage()\n    def a", "    @stage(workers=4, backend='thread')\n    def a"),
        ("    @stage()\n    def a", "    @stage(exclusive='x=1', max_failures=2)\n    def a"),
    ]:
        assert changed(tmp_path, old, new) == set()
    assert changed(tmp_path, "    @stage()\n    def a", "    @stage(storage='x')\n    def a") == {
        "a"
    }
    aliased = BASE.replace(
        "from varve import Pipeline, stage", "import varve\nfrom varve import Pipeline"
    )
    aliased = aliased.replace("@stage()", "@varve.stage()")
    before = fps(tmp_path, aliased)
    after = fps(
        tmp_path, aliased.replace("@varve.stage()\n    def a", "@varve.stage(workers=3)\n    def a")
    )
    assert before == after


def test_helper_methods_enter_only_the_stages_that_reach_them(tmp_path: Path) -> None:
    source = BASE.replace(
        "        return helper_b(), json.dumps(SHARED)",
        "        return self._h(), helper_b(), json.dumps(SHARED)",
    ).replace(
        "    def _util(self):",
        "    def _h(self):\n        return type(self)._deep()\n\n"
        "    @classmethod\n    def _deep(cls):\n        return 'h1'\n\n"
        "    def _picked(self):\n        return 'p1'\n\n"
        "    defaults = {'depends': _picked}\n\n"
        "    def _util(self):",
    )

    def edit(old: str, new: str) -> set[str]:
        before = fps(tmp_path, source)
        after = fps(tmp_path, source.replace(old, new))
        return {name for name in before if before[name] != after[name]}

    assert edit("return type(self)._deep()", "return type(self)._deep() + 'x'") == {"b"}
    assert edit("return 'h1'", "return 'h2'") == {"b"}  # transitively via type(self)
    assert edit("return 'p1'", "return 'p2'") == {"a", "b"}  # referenced by `defaults`


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("{'storage': 'x', 'workers': 2}", "{'storage': 'x', 'workers': 8, 'backend': 'thread'}"),
        ("dict(storage='x', workers=2)", "dict(storage='x', max_failures=3, exclusive='a=1')"),
        ("dict(storage='x', workers=2)", "dict(storage='x', workers=WORKERS)"),
    ],
)
def test_scheduling_keys_of_class_defaults_do_not_enter_the_fingerprint(
    tmp_path: Path, old: str, new: str
) -> None:
    source = BASE.replace("    Config = Config\n", f"    Config = Config\n    defaults = {old}\n")
    source = source.replace("SHARED = 10\n", "SHARED = 10\nWORKERS = 4\n")
    before = fps(tmp_path, source)
    assert fps(tmp_path, source.replace(old, new)) == before
    # Other keys still enter every stage.
    changed_storage = fps(
        tmp_path,
        source.replace("storage='x'", "storage='y'").replace("'storage': 'x'", "'storage': 'y'"),
    )
    assert all(changed_storage[name] != before[name] for name in before)
