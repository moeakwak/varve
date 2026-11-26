"""Unit commits: store rows for running/ok/failed units and cross-branch hardlink reuse."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import replace

from varve.engine.events import EventBus, UnitFinished
from varve.engine.probe import StageEvaluator, StageState, UnitPlan
from varve.engine.session import Session
from varve.engine.unit import UnitJob, UnitOutcome, link_tree, promote, remove_tree
from varve.engine.writer import Writer
from varve.graph import StageNode
from varve.keying.config_access import merge_access
from varve.keying.fingerprint import verify_manifest
from varve.keying.keys import InputKey, combine_hashes, compute_input_key
from varve.store import layout
from varve.store.repo import LinkCandidate, UnitRow


class Committer:
    """Builds and writes unit rows for one run; promotes staging dirs before ``ok`` rows."""

    def __init__(
        self,
        session: Session,
        run_id: str,
        writer: Writer,
        evaluator: StageEvaluator,
        bus: EventBus,
    ) -> None:
        self.session = session
        self.run_id = run_id
        self.writer = writer
        self.evaluator = evaluator
        self.bus = bus
        self._links: tuple[str, dict[str, list[LinkCandidate]]] | None = None

    def input_key(self, node: StageNode, unit_plan: UnitPlan, access: list[str] | None) -> InputKey:
        """The unit's key under ``access``, reusing the probe's other segment hashes."""

        assert unit_plan.upstream_keys is not None and unit_plan.hashes is not None
        access_json = None if access is None else json.dumps(access, separators=(",", ":"))
        hashes = {**unit_plan.hashes, "config": self.evaluator.config_hash(access_json)}
        return compute_input_key(
            config=self.session.config_dump,
            config_access=access,
            upstream=unit_plan.upstream_keys,
            inputs=unit_plan.inputs(),
            values=unit_plan.values(),
            version=node.spec.version,
            produces=node.spec.produces_key,
            hashes=hashes,
        )

    def _link_index(self, node: StageNode) -> dict[str, list[LinkCandidate]]:
        """Other branches' ``ok`` units of the stage (light columns), loaded once per stage."""

        if self._links is None or self._links[0] != node.name:
            repo = self.session.repo
            assert repo is not None
            index = (
                repo.link_index(node.name, exclude_branch_id=self.session.branch_id)
                if repo.branch_count() > 1
                else {}
            )
            self._links = (node.name, index)
        return self._links[1]

    def artifact_relpath(self, node: StageNode, relpath: str) -> str:
        base = layout.internal_relpath(self.session.branch.name, node.name, relpath)
        produces = node.spec.produces_file
        return f"{base}/{produces}" if produces else base

    def row(
        self,
        node: StageNode,
        unit_plan: UnitPlan,
        key: InputKey,
        access: list[str] | None,
        status: str,
    ) -> UnitRow:
        source = unit_plan.cell.source
        return UnitRow(
            branch_id=self.session.branch_id,
            stage_name=node.name,
            unit_key=unit_plan.unit_key,
            storage_name=node.storage,
            input_key=key.input_key,
            key_components=key.components,
            source_fp=source.fingerprint,
            source_detail=source.detail,
            status=status,
            config_access=access,
            last_run_id=self.run_id,
        )

    def commit_ok(
        self,
        node: StageNode,
        unit_plan: UnitPlan,
        job: UnitJob,
        result: UnitOutcome,
        state: StageState,
    ) -> None:
        session = self.session
        assert result.artifact is not None
        access = result.config_access
        previous = unit_plan.row
        if (
            previous is not None
            and previous.status == "ok"
            and previous.source_fp == unit_plan.cell.source.fingerprint
        ):
            access = merge_access(access, previous.config_access)
        key = self.input_key(node, unit_plan, access)
        final = session.unit_dir(node.name, node.storage, unit_plan.unit.relpath)
        if final.exists():
            # Replacing a unit dir goes through `.old-*`; recovery finds such
            # half-finished promotions through the unit's `running` row.
            self.writer.before_promote(node.name, unit_plan.unit_key)
        promote(job.staging, final, self.run_id)
        relpath = self.artifact_relpath(node, unit_plan.unit.relpath)
        row = self.row(node, unit_plan, key, access, "ok")
        artifact = result.artifact
        row.artifact_relpath = relpath
        row.artifact_fp = artifact.fingerprint
        row.artifact_manifest = artifact.manifest
        row.artifact_files = artifact.files
        row.artifact_bytes = artifact.bytes
        row.metrics = result.metrics or None
        row.duration_ms = result.duration_ms
        row.peak_rss_mb = result.peak_rss_mb
        self.writer.put(row, key.file_sets)
        state.artifacts[unit_plan.unit_key] = (
            artifact.fingerprint,
            session.storage_roots[node.storage] / relpath,
        )

    def link(self, node: StageNode, unit_plan: UnitPlan, state: StageState) -> bool:
        """Cross-branch hardlink reuse of an identical ``ok`` unit (spec §7.3)."""

        session = self.session
        repo = session.repo
        assert repo is not None
        if unit_plan.hashes is None:
            return False
        candidates = self._link_index(node).get(unit_plan.unit_key)
        if not candidates:
            return False
        source_fp = unit_plan.cell.source.fingerprint
        storage_root = session.storage_roots[node.storage]
        for candidate in candidates:
            if candidate.storage_name != node.storage or not candidate.has_manifest:
                continue
            if node.spec.version is None and source_fp not in (
                candidate.source_fp,
                candidate.reused_source_fp,
            ):
                continue
            config_hash = self.evaluator.config_hash(candidate.config_access_json)
            if combine_hashes({**unit_plan.hashes, "config": config_hash}) != candidate.input_key:
                continue
            # The index is a snapshot and other branches commit concurrently, so the
            # full row is checked again by the same rules.
            row = repo.get_unit(candidate.branch_id, node.name, unit_plan.unit_key)
            if row is None or row.status != "ok" or row.storage_name != node.storage:
                continue
            if row.artifact_manifest is None:
                continue
            if node.spec.version is None and source_fp not in (row.source_fp, row.reused_source_fp):
                continue
            access_json = (
                None
                if row.config_access is None
                else json.dumps(row.config_access, separators=(",", ":"))
            )
            config_hash = self.evaluator.config_hash(access_json)
            if combine_hashes({**unit_plan.hashes, "config": config_hash}) != row.input_key:
                continue
            other = candidate.branch_name
            relpath = unit_plan.unit.relpath
            source_dir = layout.internal_dir(storage_root, other, node.name, relpath)
            if not verify_manifest(source_dir, row.artifact_manifest):
                continue
            staging = layout.staging_dir(storage_root, self.run_id, node.name, unit_plan.unit_key)
            try:
                if staging.exists():
                    remove_tree(staging)
                link_tree(source_dir, staging)
            except OSError:
                remove_tree(staging)
                continue
            # The other branch may replace or clean its unit dir while we link it
            # (different branches run concurrently); hardlinks share inodes, so
            # re-checking the copy by stat catches any file that changed meanwhile.
            if not verify_manifest(staging, row.artifact_manifest):
                remove_tree(staging)
                continue
            linked = UnitRow(
                branch_id=session.branch_id,
                stage_name=node.name,
                unit_key=unit_plan.unit_key,
                storage_name=node.storage,
                input_key=row.input_key,
                key_components=row.key_components,
                source_fp=source_fp,
                source_detail=unit_plan.cell.source.detail,
                status="running",
                config_access=row.config_access,
                last_run_id=self.run_id,
            )
            final = session.unit_dir(node.name, node.storage, relpath)
            self.writer.put(linked)
            try:
                if final.exists():
                    self.writer.before_promote(node.name, unit_plan.unit_key)
                promote(staging, final, self.run_id)
            except OSError:
                # Fall back to executing the unit (its `running` row stays until then).
                with suppress(OSError):
                    remove_tree(staging)
                return False
            artifact_relpath = self.artifact_relpath(node, relpath)
            committed = replace(
                linked,
                status="ok",
                artifact_relpath=artifact_relpath,
                artifact_fp=row.artifact_fp,
                artifact_manifest=row.artifact_manifest,
                artifact_files=row.artifact_files,
                artifact_bytes=row.artifact_bytes,
                metrics=row.metrics,
                duration_ms=row.duration_ms,
                linked_from=other,
            )
            self.writer.put(committed)
            assert row.artifact_fp is not None
            state.artifacts[unit_plan.unit_key] = (row.artifact_fp, storage_root / artifact_relpath)
            self.bus.publish(
                UnitFinished(node.name, unit_plan.unit_key, True, 0, linked_from=other)
            )
            return True
        return False
