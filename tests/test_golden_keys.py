"""Golden cache keys: the values below were produced by varve before the
performance rewrite of the keying code (schema 1, commit ca18deb). Any change
here invalidates every user's store, so these must only change on purpose.

Source fingerprints are not pinned (``ast.dump`` differs across Python
versions); the source tests compare cached and fresh analyzers instead.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import textwrap
from pathlib import Path

from pydantic import BaseModel

from varve.branch_config import BranchRequest
from varve.dependencies import Dependencies, InputSpec
from varve.engine.probe import probe
from varve.engine.session import open_session
from varve.keying.fingerprint import FingerprintSession, artifact_fingerprint
from varve.keying.keys import compute_input_key, version_hash

MTIME_NS = 1_700_000_000 * 10**9


def build_tree(base: Path) -> Path:
    data = base / "data"
    files = {
        "single.txt": b"single\n",
        "other.txt": b"other file\n",
        "tree/a.txt": b"tree a\n",
        "tree/b.md": b"tree b\n",
        "tree/sub/c.txt": b"tree c\n",
        "tree/sub/skip1.txt": b"skip\n",
        "shards/s1.txt": b"shard one\n",
        "shards/s2.txt": b"shard two\n",
        "artifact/x.txt": b"x\n",
        "artifact/y/z.txt": b"z\n",
    }
    for index in range(40):
        files[f"big/f{index:02d}.dat"] = f"big file {index}\n".encode() * (index + 1)
    for rel, content in files.items():
        path = data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, ns=(MTIME_NS + len(rel), MTIME_NS + len(rel)))
    (data / "links").mkdir()
    (data / "links" / "file-link").symlink_to("../single.txt")
    (data / "links" / "dir-link").symlink_to("../tree")
    return data


def golden_inputs(data: Path) -> dict[str, InputSpec]:
    return {
        "one": data / "single.txt",
        "as_str": str(data / "single.txt"),
        "pair": [data / "single.txt", data / "other.txt"],
        "tree": data / "tree",
        "stat": Dependencies.stat(data / "single.txt"),
        "stattree": Dependencies.stat([data / "tree", data / "other.txt"]),
        "glob": Dependencies.glob(data / "tree", include=["**/*.txt"], exclude=["sub/skip*"]),
        "globstat": Dependencies.stat(Dependencies.glob(data / "tree", include="*.txt")),
        "big": data / "big",
        "manyroots": sorted((data / "big").glob("*.dat")),
        "empty": Dependencies.glob(data / "tree", include="*.none", allow_empty=True),
        "symfile": data / "links" / "file-link",
        "symdir": data / "links" / "dir-link",
        "dotdot": [data / "tree" / "sub" / ".." / "a.txt", data / "shards" / ".." / "tree"],
    }


class Render(BaseModel):
    dpi: int = 300
    mode: str = "fast"


class GoldConfig(BaseModel):
    prefix: str = "p"
    factor: float = 1.5
    render: Render = Render()
    words: list[str] = ["alpha", "beta"]


def observe_digests(data: Path) -> dict[str, list[object]]:
    session = FingerprintSession()
    out = {}
    for name, spec in golden_inputs(data).items():
        digest = session.input_digest(spec)
        out[name] = [digest.digest, digest.files, digest.bytes]
    return out


def observe_key(data: Path) -> dict[str, object]:
    session = FingerprintSession()
    inputs = {name: session.input_digest(spec) for name, spec in golden_inputs(data).items()}
    key = compute_input_key(
        config=GoldConfig(),
        config_access=["prefix", "render.dpi"],
        upstream={"prepare": "a" * 64, "fan": [["x=1", "b" * 64], ["x=2", "c" * 64]]},
        inputs=inputs,
        values={"label": "ALPHA", "tuple": (1, "x"), "f": 0.1, "nested": {"b": None, "a": [1, 2]}},
        version="2",
        produces=["a.txt", "b/c.txt"],
    )
    return {
        "input_key": key.input_key,
        "hashes": {segment: part["hash"] for segment, part in key.components.items()},
        "file_sets": [row.fileset_hash for row in key.file_sets],
        "whole_config": compute_input_key(
            config=GoldConfig(), config_access=None, upstream={}, inputs={}, values={}, version=None
        ).input_key,
        "version": version_hash(None, "out.json"),
    }


def observe_artifact(data: Path) -> list[object]:
    art = artifact_fingerprint(data / "artifact")
    return [art.fingerprint, art.files, art.bytes]


PIPELINE = """
from pathlib import Path

from pydantic import BaseModel

from varve import Axis, Ctx, Dependencies, Pipeline, stage

DATA = Path(__file__).resolve().parent.parent / "data"


class Config(BaseModel):
    prefix: str = "p"
    words: list[str] = ["alpha", "beta"]
    unused: int = 7


PART = Axis("part", [1, 2])
WORD = Axis("word", resolver=lambda ctx: ctx.config.words)
SHARD = Axis("shard", resolver=lambda ctx: {p.stem: p for p in sorted((DATA / "shards").glob("*"))})
SDIR = Axis("sdir", resolver=lambda ctx: {"tree": DATA / "tree"}, input_fingerprint="stat")


class Golden(Pipeline):
    Config = Config

    @stage(
        foreach=(PART, WORD, SHARD),
        depends=lambda ctx: Dependencies(
            inputs={"tree": DATA / "tree", "one": DATA / "single.txt"},
            values={"part": ctx.cell["part"] * 10, "pair": (1, 2.5)},
        ),
        produces="w.txt",
    )
    def words(self, ctx: Ctx[Config], part: int, word: str, shard: Path) -> None:
        (ctx.out / "w.txt").write_text(ctx.config.prefix + word + shard.read_text())

    @stage(foreach=SDIR, version="3")
    def dirs(self, ctx: Ctx, sdir: Path) -> None:
        (ctx.out / "n.txt").write_text(str(len(list(sdir.iterdir()))))
"""


def observe_probe(base: Path, module: str) -> dict[str, object]:
    """Non-source segment hashes of every unit of a fresh probe."""

    src = base / "src"
    src.mkdir(exist_ok=True)
    (src / f"{module}.py").write_text(textwrap.dedent(PIPELINE))
    sys.path.insert(0, str(src))
    try:
        pipeline = importlib.import_module(module).Golden
        session = open_session(pipeline, BranchRequest("main"), root=base / "root")
        try:
            plans = probe(session)
        finally:
            session.close()
    finally:
        sys.path.remove(str(src))
        sys.modules.pop(module, None)
    return {
        f"{name}@{unit.unit_key}": unit.hashes
        for name, plan in plans.items()
        for unit in plan.units
    }


def observe(base: Path, module: str) -> dict[str, object]:
    data = build_tree(base)
    return {
        "digests": observe_digests(data),
        "key": observe_key(data),
        "artifact": observe_artifact(data),
        "probe": observe_probe(base, module),
    }


GOLDEN: dict[str, object] = {
    "artifact": ["978bcf558ac086d5067ffde0f192dd4db000698bc062141b6285f4ae85674ddd", 2, 4],
    "digests": {
        "as_str": ["525a1941492ebf4281a97f71ee79f2ecbe9035d08a2d12acf76153649a4564b3", 1, 7],
        "big": ["3e248f6dd7cc3b8d61989eb50643b55a2dfe13c7919c3bb00d4c513a60849f1e", 40, 9785],
        "dotdot": ["5325335b0895de96f8a0d295dba28b028cffd8980c6ca300e39411845bd464ce", 5, 33],
        "empty": ["e8e4db6f04ae5373ddbffb29a44846fb6f2e386a69324c2eabc8bd4bf1f62198", 0, 0],
        "glob": ["2a774c021fbda97c98cd87e4154623b7403323d40b04e55590cfcd2deb9f69cc", 2, 14],
        "globstat": ["b1d8568a1b689dc4a16810f93622a405e2651d044818a3bbe99bbbb68b155791", 1, 7],
        "manyroots": ["f3ed4208f7dbc55c4175f5958294dfad541101c8235f0fff47a2e4a291e1524c", 40, 9785],
        "one": ["525a1941492ebf4281a97f71ee79f2ecbe9035d08a2d12acf76153649a4564b3", 1, 7],
        "pair": ["0030a663756c80e0efefb7665f7cef803cc1638c4e93d50b100c04fe082600e0", 2, 18],
        "stat": ["fab4f82ef9a4e223b1f3c0a26d81ce836ef8f043f11c602e66ba318cf9275a08", 1, 7],
        "stattree": ["33ed33a6f0e328e82c918c9a01e91af15c0244a4ba8950652dd21d823a8421d6", 5, 37],
        "symdir": ["1a770068140b5bf29039e06d52e0a082b6e421910256e33a8fc977623d621348", 4, 26],
        "symfile": ["525a1941492ebf4281a97f71ee79f2ecbe9035d08a2d12acf76153649a4564b3", 1, 7],
        "tree": ["1a770068140b5bf29039e06d52e0a082b6e421910256e33a8fc977623d621348", 4, 26],
    },
    "key": {
        "file_sets": [
            "3e248f6dd7cc3b8d61989eb50643b55a2dfe13c7919c3bb00d4c513a60849f1e",
            "f3ed4208f7dbc55c4175f5958294dfad541101c8235f0fff47a2e4a291e1524c",
        ],
        "hashes": {
            "config": "2afebc23931bf9cee7ff95379130c8a089351432f0120cf5329c7d1354369d7f",
            "inputs": "5935e95658d0661f4d4de9c1d6dd250efda1aef8bd78f4903adab898d7706e1f",
            "upstream": "d1496fb46bbf83314ccfe9762d91d63efa7f0f5d6183c5fb9ad6fa5ecfe3920a",
            "values": "ba2c4528b57a863e750972767bd13f1068c6f3aa9d06d2f22a13e69890cfaf9c",
            "version": "ab55b0935d01ab952df0ba055ec11779c36e173ccf63866617d2f2a325cc9c69",
        },
        "input_key": "5ff878bf40cc366ba9c95c343bd862f193eae583c47f30f6a9006b503005bbd4",
        "version": "fa52fad3c23a729cb97a20b3364aefb8b6c9fbdbc526ead5eccb9e80cdd4a892",
        "whole_config": "75a27eaff96341fe8ff42951ca737c24e8561a3a841a0c7654f453925a87bfce",
    },
    "probe": {
        "dirs@sdir=tree": {
            "config": "a24443d520ae926b4661f011dcdfa8652655ebabcf8e9d70407f8497aa2f8ef0",
            "inputs": "baaf5a6aac28f89fe4f25250972e4a87c432cf6d9e69aafe581052ee7dd55e41",
            "upstream": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "values": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "version": "a4aab3f1f08004e907d2357fafe74cab56359bcb32e23f52e7eb1d3a9c0a2ad3",
        },
        "words@part=1,word=alpha,shard=s1": {
            "config": "a24443d520ae926b4661f011dcdfa8652655ebabcf8e9d70407f8497aa2f8ef0",
            "inputs": "748155bfbc5c177089bf51dac5c5703fd0e4501a5da77f04cc7c43a6ca5b6243",
            "upstream": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "values": "734affe69168cbf609ae8e54939c8500507a17eaad41066a35de52048afd7960",
            "version": "78bc1f290391414742ef01d2d8177d82565830154d82149c038a82da427fb2a6",
        },
        "words@part=1,word=alpha,shard=s2": {
            "config": "a24443d520ae926b4661f011dcdfa8652655ebabcf8e9d70407f8497aa2f8ef0",
            "inputs": "928a8a382ddf765ce1dd5dbc5f5a33dc9425d1824ccd39709464c009c82edef5",
            "upstream": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "values": "734affe69168cbf609ae8e54939c8500507a17eaad41066a35de52048afd7960",
            "version": "78bc1f290391414742ef01d2d8177d82565830154d82149c038a82da427fb2a6",
        },
        "words@part=1,word=beta,shard=s1": {
            "config": "a24443d520ae926b4661f011dcdfa8652655ebabcf8e9d70407f8497aa2f8ef0",
            "inputs": "748155bfbc5c177089bf51dac5c5703fd0e4501a5da77f04cc7c43a6ca5b6243",
            "upstream": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "values": "dd21bcf25176d5dea371048a01cf806786b3021a7be5cc8841dc8c07db28fdc2",
            "version": "78bc1f290391414742ef01d2d8177d82565830154d82149c038a82da427fb2a6",
        },
        "words@part=1,word=beta,shard=s2": {
            "config": "a24443d520ae926b4661f011dcdfa8652655ebabcf8e9d70407f8497aa2f8ef0",
            "inputs": "928a8a382ddf765ce1dd5dbc5f5a33dc9425d1824ccd39709464c009c82edef5",
            "upstream": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "values": "dd21bcf25176d5dea371048a01cf806786b3021a7be5cc8841dc8c07db28fdc2",
            "version": "78bc1f290391414742ef01d2d8177d82565830154d82149c038a82da427fb2a6",
        },
        "words@part=2,word=alpha,shard=s1": {
            "config": "a24443d520ae926b4661f011dcdfa8652655ebabcf8e9d70407f8497aa2f8ef0",
            "inputs": "748155bfbc5c177089bf51dac5c5703fd0e4501a5da77f04cc7c43a6ca5b6243",
            "upstream": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "values": "e7600b000576b1eeb5ff473bd5a80517960d09b2d85f93b4fac68bd503d41408",
            "version": "78bc1f290391414742ef01d2d8177d82565830154d82149c038a82da427fb2a6",
        },
        "words@part=2,word=alpha,shard=s2": {
            "config": "a24443d520ae926b4661f011dcdfa8652655ebabcf8e9d70407f8497aa2f8ef0",
            "inputs": "928a8a382ddf765ce1dd5dbc5f5a33dc9425d1824ccd39709464c009c82edef5",
            "upstream": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "values": "e7600b000576b1eeb5ff473bd5a80517960d09b2d85f93b4fac68bd503d41408",
            "version": "78bc1f290391414742ef01d2d8177d82565830154d82149c038a82da427fb2a6",
        },
        "words@part=2,word=beta,shard=s1": {
            "config": "a24443d520ae926b4661f011dcdfa8652655ebabcf8e9d70407f8497aa2f8ef0",
            "inputs": "748155bfbc5c177089bf51dac5c5703fd0e4501a5da77f04cc7c43a6ca5b6243",
            "upstream": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "values": "f1a06cbe2c6f948de0ca62fca316c372ebb945be78213e687b6edcb292e73708",
            "version": "78bc1f290391414742ef01d2d8177d82565830154d82149c038a82da427fb2a6",
        },
        "words@part=2,word=beta,shard=s2": {
            "config": "a24443d520ae926b4661f011dcdfa8652655ebabcf8e9d70407f8497aa2f8ef0",
            "inputs": "928a8a382ddf765ce1dd5dbc5f5a33dc9425d1824ccd39709464c009c82edef5",
            "upstream": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "values": "f1a06cbe2c6f948de0ca62fca316c372ebb945be78213e687b6edcb292e73708",
            "version": "78bc1f290391414742ef01d2d8177d82565830154d82149c038a82da427fb2a6",
        },
    },
}


def test_cache_keys_match_the_pinned_values(tmp_path) -> None:
    assert observe(tmp_path, f"varve_golden_{os.getpid()}") == GOLDEN


def test_cached_source_analysis_matches_a_fresh_analyzer(tmp_path) -> None:
    from varve.keying.source import SourceAnalyzer

    module = f"varve_golden_src_{os.getpid()}"
    src = tmp_path / "src"
    src.mkdir()
    (src / f"{module}.py").write_text(textwrap.dedent(PIPELINE))
    sys.path.insert(0, str(src))
    try:
        pipeline = importlib.import_module(module).Golden
        shared = SourceAnalyzer()
        for _ in range(2):
            for name in pipeline.stages():
                assert (
                    shared.stage_source(pipeline, name).fingerprint
                    == SourceAnalyzer().stage_source(pipeline, name).fingerprint
                )
    finally:
        sys.path.remove(str(src))
        sys.modules.pop(module, None)


if __name__ == "__main__":  # print the observations (to regenerate GOLDEN deliberately)
    print(json.dumps(observe(Path(sys.argv[1]), sys.argv[2]), indent=1, sort_keys=True))
