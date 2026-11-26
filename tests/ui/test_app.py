from __future__ import annotations

import hashlib
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

pytest.importorskip("starlette", reason="the UI tests need the varve[ui] extra")
pytest.importorskip("httpx", reason="starlette's TestClient needs httpx")

with warnings.catch_warnings():
    # Starlette 1.x prefers httpx2, which needs Python >= 3.11; httpx still works.
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from varve.store.db import connect, write_meta
from varve.ui import create_app, discover_state_dbs
from varve.ui.queries import assign_ids

API = "/api/pipelines/demo.corpus/branches/main"


@pytest.fixture
def client(workspace: dict[str, Path]) -> TestClient:
    return TestClient(create_app(list(workspace.values())))


def test_workspace_overview(client: TestClient, workspace: dict[str, Path]) -> None:
    data = client.get("/api/workspace").json()
    pipes = {p["id"]: p for p in data["pipelines"]}
    assert set(pipes) == {"demo.corpus", "demo.eval"}
    corpus = pipes["demo.corpus"]
    assert corpus["has_graph"] and corpus["stage_count"] == 3 and corpus["error"] is None
    main = next(b for b in corpus["branches"] if b["name"] == "main")
    assert main["counts"] == {"ok": 7, "failed": 3, "running": 1, "orphaned": 1}
    assert main["total_units"] == 11
    assert main["artifact_bytes"] == 10 + 6 * 100  # orphaned bytes excluded
    assert main["last_run"]["run_id"] == "r3"
    assert main["last_duration_ms"] == 3000
    assert main["active"] == {
        "run_id": "r3",
        "branch_id": "main",
        "started_at": "2026-11-03T00:00:00",
        "done": 3,
        "running": 1,
    }
    dev = next(b for b in corpus["branches"] if b["name"] == "dev")
    assert dev["last_run"] is None and dev["active"] is None
    assert data["edges"] == [
        {
            "source": "demo.corpus",
            "source_name": "corpus",
            "source_stage": "merge",
            "branches": ["main"],
            "target": "demo.eval",
            "attr": "corpus",
        }
    ]


def test_workspace_reports_broken_dbs(tmp_path: Path, workspace: dict[str, Path]) -> None:
    junk = tmp_path / "junk" / ".varve" / "state.db"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"not a database at all" * 100)
    missing = tmp_path / "gone" / ".varve" / "state.db"
    client = TestClient(create_app([workspace["corpus"], junk, missing]))
    pipes = client.get("/api/workspace").json()["pipelines"]
    errors = {p["db_path"]: p["error"] for p in pipes}
    assert errors[str(workspace["corpus"])] is None
    assert errors[str(junk)] and errors[str(missing)]
    assert "FileNotFoundError" in errors[str(missing)]


def test_branch_detail_with_graph(client: TestClient) -> None:
    data = client.get(API).json()
    assert [s["name"] for s in data["stages"]] == ["prepare", "shard", "merge"]
    assert data["edges"] == [
        {"from": "prepare", "to": "shard", "kind": "data"},
        {"from": "shard", "to": "merge", "kind": "data"},
        {"from": "prepare", "to": "merge", "kind": "needs"},
    ]
    shard = data["stages"][1]
    assert (shard["ok"], shard["failed"], shard["running"], shard["orphaned"]) == (6, 3, 1, 1)
    assert shard["total"] == 10 and shard["state"] == "running"
    assert shard["reused_units"] == 1 and shard["linked_units"] == 1
    assert shard["linked_from"] == ["dev"]
    assert shard["version"] == "2"
    assert shard["last"]["run_id"] == "r2" and shard["last"]["executed_units"] == 2
    merge = data["stages"][2]
    assert merge["total"] == 0 and merge["state"] == "empty" and merge["last"]["run_id"] == "r1"
    assert data["stages"][0]["state"] == "ok"
    assert data["running_units"] == 1
    assert data["active_run"]["run_id"] == "r3"
    assert data["branch"]["config"] == {"lang": "en"}
    assert set(data["storages"]) == {"default"}


def test_branch_detail_lists_stages_removed_from_the_code(client: TestClient) -> None:
    data = client.get("/api/pipelines/demo.corpus/branches/dev").json()
    assert [(s["name"], s["declared"]) for s in data["stages"]] == [
        ("prepare", True),
        ("shard", True),
        ("merge", True),
        ("retired", False),
    ]
    assert data["stages"][3]["ok"] == 1 and len(data["edges"]) == 3


def test_unknown_things_are_404(client: TestClient) -> None:
    assert client.get("/api/pipelines/nope/branches/main").status_code == 404
    assert client.get("/api/pipelines/demo.corpus/branches/nope").status_code == 404
    assert client.get(f"{API}/stages/nope/units").status_code == 404
    assert client.get(f"{API}/stages/shard/units/part=99").status_code == 404
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get(f"{API}/stages/shard/units?page=x").status_code == 400


def test_units_pagination_and_filters(client: TestClient) -> None:
    first = client.get(f"{API}/stages/shard/units", params={"page_size": 4}).json()
    assert first["total"] == 11 and first["pages"] == 3 and first["page"] == 1
    assert [u["unit_key"] for u in first["units"]] == [f"part={i}" for i in range(4)]
    assert first["counts"] == {"ok": 6, "failed": 3, "running": 1, "orphaned": 1}
    last = client.get(f"{API}/stages/shard/units", params={"page_size": 4, "page": 3}).json()
    assert [u["unit_key"] for u in last["units"]] == ["part=8", "part=9", "part=10"]
    reused = first["units"][1]
    assert reused["reused"] is True and reused["linked_from"] is None
    assert first["units"][2]["linked_from"] == "dev"

    failed = client.get(f"{API}/stages/shard/units", params={"status": "failed"}).json()
    assert failed["total"] == 3 and {u["status"] for u in failed["units"]} == {"failed"}
    group = client.get(
        f"{API}/stages/shard/units",
        params={"error_type": "ValueError", "error_summary": "bad row"},
    ).json()
    assert [u["unit_key"] for u in group["units"]] == ["part=6", "part=7"]
    search = client.get(f"{API}/stages/shard/units", params={"q": "=1"}).json()
    assert [u["unit_key"] for u in search["units"]] == ["part=1", "part=10"]
    by_duration = client.get(
        f"{API}/stages/shard/units", params={"sort": "duration", "page_size": 2}
    ).json()
    assert [u["unit_key"] for u in by_duration["units"]] == ["part=5", "part=4"]
    clamped = client.get(f"{API}/stages/shard/units", params={"page_size": 100000}).json()
    assert clamped["page_size"] == 500


def test_units_error_groups_and_metrics(client: TestClient) -> None:
    data = client.get(f"{API}/stages/shard/units").json()
    groups = [(g["error_type"], g["error_summary"], g["count"]) for g in data["error_groups"]]
    assert groups == [("ValueError", "bad row", 2), ("KeyError", "'x'", 1)]
    first = data["error_groups"][0]
    assert first["sample_unit_key"] == "part=6"
    assert first["sample_traceback"].endswith("ValueError: bad row")
    assert data["metrics"] == [
        {"name": "items", "units": 6, "sum": 600, "min": 100, "max": 100, "mean": 100.0}
    ]
    assert data["summary"]["artifact_bytes"] == 600
    assert data["summary"]["max_ms"] == 1005


def test_unit_detail(client: TestClient) -> None:
    data = client.get(f"{API}/stages/shard/units/part=6").json()
    assert data["unit"]["status"] == "failed"
    assert data["unit"]["error_traceback"].startswith("Traceback")
    assert data["unit"]["source_detail"]["symbols"] == {"Pipe.shard": "1" * 16, "helper": "2" * 16}
    assert [s["name"] for s in data["segments"]] == [
        "config",
        "upstream",
        "inputs",
        "values",
        "version",
    ]
    assert data["log"]["available"] and data["log"]["text"] == "hello from shard 6\n"
    assert data["run"]["run_id"] == "r3"

    reused = client.get(f"{API}/stages/shard/units/part%3D1").json()
    assert [label["text"] for label in reused["labels"]] == ["version=2", "reused"]
    linked = client.get(f"{API}/stages/shard/units/part=2").json()
    assert {"kind": "linked", "text": "linked@dev"} in linked["labels"]
    assert linked["log"] == {
        "available": False,
        "path": linked["log"]["path"],
    }

    prepare = client.get(f"{API}/stages/prepare/units/_").json()
    assert prepare["unit"]["unit_key"] == ""
    assert prepare["file_sets"] == {"f" * 64: {"file_count": 40, "total_bytes": 4096}}
    assert prepare["artifact_path"].endswith("out/.internal/main/prepare")
    assert prepare["unit"]["peak_rss_mb"] is None


def test_history_full_vs_partial(client: TestClient) -> None:
    data = client.get(f"{API}/history").json()
    assert data["stages"] == ["prepare", "shard", "merge"]
    assert data["throughput_unit"] == "items/s"
    runs = {r["run_id"]: r for r in data["runs"]}
    assert [r["run_id"] for r in data["runs"]] == ["r3", "r2", "r1"]
    assert runs["r1"]["kind"] == "full" and runs["r1"]["executed"] == 12
    assert runs["r1"]["wall_ms"] == 12000 and runs["r1"]["unit_p50_ms"] is None
    assert runs["r2"]["kind"] == "partial"
    assert (runs["r2"]["executed"], runs["r2"]["total"], runs["r2"]["failed"]) == (2, 11, 3)
    assert runs["r2"]["git_dirty"] is True and runs["r2"]["argv"] == ["run"]
    assert runs["r3"]["kind"] == "none" and runs["r3"]["executed"] is None
    assert runs["r1"]["items_per_s"] == pytest.approx(900 / 12)

    shard = client.get(f"{API}/history", params={"stage": "shard", "limit": 1}).json()
    assert [r["run_id"] for r in shard["runs"]] == ["r2"]
    row = shard["runs"][0]
    assert row["unit_p50_ms"] == 900 and row["unit_p95_ms"] == 1000
    assert row["items_per_s"] == pytest.approx(100.0)
    assert row["units_per_s"] == pytest.approx(1.0)
    shard_all = client.get(f"{API}/history", params={"stage": "shard"}).json()
    assert [r["kind"] for r in shard_all["runs"]] == ["partial", "full"]
    assert shard_all["runs"][1]["peak_rss_mb"] == 512.0


def test_history_units_per_second_fallback(client: TestClient) -> None:
    data = client.get(f"{API}/history", params={"stage": "prepare"}).json()
    assert data["throughput_unit"] == "units/s"
    assert data["runs"][-1]["units_per_s"] == pytest.approx(1.0)


def test_run_detail(client: TestClient) -> None:
    data = client.get("/api/runs/r1").json()
    assert data["pipeline"]["id"] == "demo.corpus"
    assert data["run"]["branch"] == "main" and data["run"]["git_commit"] == "abc1234"
    assert [s["stage_name"] for s in data["stages"]] == ["prepare", "shard", "merge"]
    shard = data["stages"][1]
    assert shard["kind"] == "full" and shard["items_per_s"] == pytest.approx(100.0)
    assert shard["metrics_summary"] == {"items": 900}
    assert data["stages"][0]["peak_rss_mb"] is None
    assert data["totals"]["executed"] == 12
    scoped = client.get("/api/runs/r1", params={"pipeline": "demo.eval"})
    assert scoped.status_code == 404


def test_static_index(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200 and "<title>varve</title>" in index.text
    for asset in ("app.js", "style.css"):
        response = client.get(f"/static/{asset}")
        assert response.status_code == 200 and len(response.content) > 1000
    assert "http://" not in index.text and "https://" not in index.text


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hit_everything(client: TestClient) -> None:
    urls = [
        "/api/workspace",
        API,
        f"{API}/stages/shard/units?status=failed",
        f"{API}/stages/shard/units/part=6",
        f"{API}/stages/prepare/units/_",
        f"{API}/history",
        f"{API}/history?stage=shard",
        "/api/runs/r2",
    ]
    for url in urls:
        assert client.get(url).status_code == 200, url


def test_never_writes(workspace: dict[str, Path]) -> None:
    before = {name: _digest(path) for name, path in workspace.items()}
    files_before = {
        name: sorted(p.name for p in path.parent.iterdir()) for name, path in workspace.items()
    }
    _hit_everything(TestClient(create_app(list(workspace.values()))))
    assert {name: _digest(path) for name, path in workspace.items()} == before
    for name, path in workspace.items():
        # A read-only reader must not leave a WAL with content behind.
        wal = path.with_name("state.db-wal")
        assert not wal.exists() or wal.stat().st_size == 0, files_before[name]


def test_discovery_callable_is_reevaluated(tmp_path: Path, workspace: dict[str, Path]) -> None:
    found = discover_state_dbs(workspace["corpus"].parents[2])
    assert found == sorted(workspace.values())
    client = TestClient(create_app(lambda: discover_state_dbs(tmp_path)))
    assert len(client.get("/api/workspace").json()["pipelines"]) == 2
    conn = connect(tmp_path / "late" / ".varve" / "state.db")
    write_meta(conn, {"pipeline_name": "demo.late"})
    conn.close()
    assert client.get("/api/pipelines/demo.late/branches/main").status_code == 404  # no branch
    ids = {p["id"] for p in client.get("/api/workspace").json()["pipelines"]}
    assert "demo.late" in ids and len(ids) == 3


def test_discovery_skips_noise(tmp_path: Path, workspace: dict[str, Path]) -> None:
    for noise in (".git/x", "node_modules/y", ".venv/z", ".worktrees/w", "corpus/out/q"):
        db = tmp_path / noise / ".varve" / "state.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"")
    (tmp_path / "corpus" / "out" / ".internal").mkdir(parents=True)
    assert discover_state_dbs(tmp_path) == sorted(workspace.values())


def test_assign_ids_disambiguates_duplicates() -> None:
    paths = [Path("/b/.varve/state.db"), Path("/a/.varve/state.db"), Path("/c/.varve/state.db")]
    metas = [{"pipeline_name": "x.y"}, {"pipeline_name": "x.y"}, {}]
    ids = assign_ids(paths, metas)
    assert ids[1] == "x.y" and ids[0].startswith("x.y~") and ids[2].startswith("db-")
    assert assign_ids(paths, metas) == ids


def test_core_import_has_no_web_dependencies() -> None:
    code = (
        "import sys, varve, varve.ui\n"
        "from varve.ui import discover_state_dbs\n"
        "assert 'starlette' not in sys.modules and 'uvicorn' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_missing_extra_names_varve_ui() -> None:
    code = (
        "import sys\n"
        "sys.modules['starlette'] = None\n"
        "try:\n"
        "    from varve.ui import create_app\n"
        "except ImportError as exc:\n"
        "    assert 'varve[ui]' in str(exc), exc\n"
        "else:\n"
        "    raise SystemExit('expected ImportError')\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
