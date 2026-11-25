"""Everything one command needs: pipeline, branch, Config, Args, roots, store, caches."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from varve.axis import Axis, AxisDomain
from varve.branch import load_branches
from varve.branch_config import BranchRequest, ResolvedBranch, build_config, resolve_branch
from varve.engine.refs import RefResolver
from varve.graph import DomainKey, StageGraph, static_domains
from varve.keying.fingerprint import FingerprintSession, StatCache
from varve.keying.keys import config_data
from varve.keying.source import SourceAnalyzer
from varve.pipeline import Pipeline
from varve.ref import Ref
from varve.store import layout
from varve.store.db import connect
from varve.store.repo import Repo
from varve.workspace.config import WorkspaceSettings, settings_for


@dataclass
class Session:
    pipeline: type[Pipeline]
    graph: StageGraph
    branch: ResolvedBranch
    args: Any
    root: Path
    storage_roots: dict[str, Path]
    publish_root: Path
    repo: Repo | None
    refs: RefResolver
    workspace: WorkspaceSettings
    rehash: bool = False
    fingerprints: FingerprintSession = field(init=False)
    sources: SourceAnalyzer = field(init=False)
    statics: dict[Axis, AxisDomain] = field(init=False)
    resolved: dict[tuple[Any, ...], AxisDomain] = field(default_factory=dict, init=False)
    """Resolver memo: ``(axis, cell ids, upstream fps)`` -> full (unrestricted) domain."""
    full_domains: dict[DomainKey, AxisDomain] = field(default_factory=dict, init=False)
    """Latest full domain per resolver request (orphan detection)."""

    def __post_init__(self) -> None:
        cache = StatCache(self.repo.load_stat_cache() if self.repo is not None else None)
        self.fingerprints = FingerprintSession(cache, rehash=self.rehash)
        self.sources = SourceAnalyzer(self.fingerprints)
        static_active = {
            name: ids
            for name, ids in self.branch.active.items()
            if name in self.graph.axes and self.graph.axes[name].is_static
        }
        self.statics = static_domains(self.graph, static_active)
        for name, sample in self.branch.samples.items():
            axis = self.graph.axes[name]
            if axis.is_static:
                domain = self.statics[axis]
                self.statics[axis] = domain.restrict(sample.pick(domain.ids, name))
        self._config_data = config_data(self.config)

    @property
    def config(self) -> Any:
        return self.branch.config

    @property
    def config_dump(self) -> dict[str, Any]:
        return self._config_data

    @property
    def branch_id(self) -> str:
        return self.branch.name

    @property
    def publish_dir(self) -> Path:
        return layout.publish_dir(
            self.publish_root, self.branch.name, temporary=self.branch.is_temp
        )

    def storage_root(self, name: str) -> Path:
        return self.storage_roots[name]

    def unit_dir(self, stage: str, storage: str, relpath: str) -> Path:
        return layout.internal_dir(self.storage_roots[storage], self.branch.name, stage, relpath)

    def close(self) -> None:
        if self.repo is not None:
            self.repo.conn.close()
            self.repo = None


def main_config(pipeline: type[Pipeline]) -> Any:
    """The ``main`` branch Config (used to locate the pipeline root)."""

    branches = load_branches(pipeline.varve_config_path())
    main = branches.get("main")
    return build_config(pipeline, main.config if main is not None else {})


def pipeline_name(pipeline: type[Pipeline], workspace: WorkspaceSettings | None = None) -> str:
    """Name used for workspace storage subdirectories and ``VARVE_ROOT_BASE``."""

    return (workspace.pipeline_name if workspace else None) or pipeline.selector()


def pipeline_root(
    pipeline: type[Pipeline], root: Path | None, workspace: WorkspaceSettings | None = None
) -> Path:
    """Explicit ``root``, else ``<root_base>/<name>`` under relocation, else ``default_root``."""

    if root is not None:
        return Path(root).expanduser().absolute()
    if workspace is not None and workspace.root_base is not None:
        return workspace.root_base / pipeline_name(pipeline, workspace)
    return Path(pipeline.default_root(main_config(pipeline))).expanduser().absolute()


def resolve_roots(
    pipeline: type[Pipeline],
    config: Any,
    root: Path | None,
    workspace: WorkspaceSettings,
) -> tuple[Path, dict[str, Path], Path]:
    """(pipeline root, storage roots, publish root) for one command.

    Under ``VARVE_ROOT_BASE`` (and no explicit ``root``) every storage lives
    below the relocated root (``default`` at ``<root>/out``, others at
    ``<root>/storages/<name>``) and publishing uses the default storage root,
    so nothing outside the base is touched.
    """

    resolved_root = pipeline_root(pipeline, root, workspace)
    declared = pipeline.storage_roots(config)
    if root is None and workspace.root_base is not None:
        names = {*declared, *workspace.storage, *pipeline.graph().storage_names()}
        relocated = {
            name: Path("out") if name == layout.DEFAULT_STORAGE else Path("storages") / name
            for name in names
        }
        storage_roots = layout.resolve_storage_roots(relocated, resolved_root)
        return resolved_root, storage_roots, storage_roots[layout.DEFAULT_STORAGE]
    storage_roots = layout.resolve_storage_roots(
        declared,
        resolved_root,
        workspace=workspace.storage or None,
        pipeline_name=pipeline_name(pipeline, workspace),
    )
    publish_root = layout.resolve_publish_root(
        pipeline.publish_root(config), storage_roots, resolved_root
    )
    return resolved_root, storage_roots, publish_root


def build_args(pipeline: type[Pipeline], args: BaseModel | Mapping[str, Any] | None) -> Any:
    if isinstance(args, BaseModel):
        return args
    return pipeline.Args.model_validate(dict(args or {}))


def open_session(
    pipeline: type[Pipeline],
    request: BranchRequest,
    *,
    root: Path | None = None,
    args: BaseModel | Mapping[str, Any] | None = None,
    readonly: bool = True,
    refs: RefResolver | None = None,
    workspace: WorkspaceSettings | None = None,
    rehash: bool = False,
) -> Session:
    """Resolve the branch and open the store (read-only opens tolerate a missing db).

    ``workspace`` defaults to the ``varve.toml`` found from the cwd (else the
    pipeline module's directory) plus ``VARVE_ROOT_BASE``; ``refs`` defaults
    to the workspace Ref resolver over those settings.
    """

    graph = pipeline.graph()
    workspace = workspace if workspace is not None else settings_for(pipeline)
    if refs is None:
        from varve.workspace.refs import WorkspaceRefResolver

        refs = WorkspaceRefResolver(workspace)
    resolved_root = pipeline_root(pipeline, root, workspace)
    db_path = layout.state_db_path(resolved_root)
    repo: Repo | None = None
    if not readonly or db_path.exists():
        repo = Repo(connect(db_path, readonly=readonly))
    try:
        branch = resolve_branch(
            pipeline, request, stored=repo.get_branch if repo is not None else None
        )
        _, storage_roots, publish_root = resolve_roots(pipeline, branch.config, root, workspace)
        graph.validate_storages(storage_roots)
        return Session(
            pipeline=pipeline,
            graph=graph,
            branch=branch,
            args=build_args(pipeline, args),
            root=resolved_root,
            storage_roots=storage_roots,
            publish_root=publish_root,
            repo=repo,
            refs=refs,
            workspace=workspace,
            rehash=rehash,
        )
    except BaseException:
        if repo is not None:
            repo.conn.close()
        raise


def graph_document(graph: StageGraph) -> dict[str, Any]:
    """Zero-import description of the logical graph stored in ``meta.graph_json``."""

    stages = []
    for name, node in graph.nodes.items():
        data = {p.name for p in node.params if p.kind == "upstream"}
        deps = []
        for dep in node.deps:
            kind = "data" if dep in data else "needs" if dep in node.upstream else "resolver"
            deps.append({"stage": dep, "kind": kind})
        injected_refs = {p.name for p in node.params if p.kind == "ref"}
        ref_edges = [
            {"attr": ref, "kind": "data" if ref in injected_refs else "needs"} for ref in node.refs
        ]
        ref_edges += [
            {"attr": ref, "kind": "resolver"} for ref in node.resolver_refs if ref not in node.refs
        ]
        spec = node.spec
        stages.append(
            {
                "name": name,
                "deps": deps,
                "refs": ref_edges,
                "axes": [_axis_document(axis) for axis in node.axes],
                "storage": node.storage,
                "produces": list(spec.produces) if spec.produces is not None else None,
                "produces_file": spec.produces_file,
                "publish": [
                    {"src": entry.src, "dst": entry.dst, "templated": entry.templated}
                    for entry in spec.publish
                ],
                "version": spec.version,
                "workers_static": node.workers if isinstance(node.workers, int) else None,
                "is_async": spec.is_async,
            }
        )
    refs = [_ref_document(attr, ref) for attr, ref in graph.refs.items()]
    return {"stages": stages, "refs": refs}


def _axis_document(axis: Axis) -> dict[str, Any]:
    """Name and kind; static axes also record their declared ids (in order) so Ref
    resolution can tell rows outside the current domain without importing the module."""

    if axis.is_static:
        return {"name": axis.name, "kind": "static", "ids": list(axis.static_domain().ids)}
    return {"name": axis.name, "kind": "dynamic"}


def _ref_document(attr: str, ref: Ref) -> dict[str, Any]:
    target = ref.target
    if not isinstance(target, str):
        target = f"{target.__module__}:{target.__qualname__}"
    align_by: Any = ref.align_by
    if isinstance(align_by, Mapping):
        align_by = dict(align_by)
    elif align_by is not None:
        align_by = list(align_by)
    return {
        "attr": attr,
        "target": target,
        "stage": ref.stage,
        "branches": list(ref.branches),
        "selector": ref.selector,
        "align_by": align_by,
    }


def identity_meta(session: Session) -> dict[str, str]:
    pipeline = session.pipeline
    return {
        "pipeline_name": pipeline_name(pipeline, session.workspace),
        "module": pipeline.import_module_name(),
        "qualname": pipeline.__qualname__,
        "root": str(session.root),
        "graph_json": json.dumps(graph_document(session.graph), separators=(",", ":")),
    }
