"""Deterministic, snapshot-bound Graph View artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from codeevolution.domain.topology import TopologyArtifactRequestSpec, canonical_digest


class GraphArtifactService:
    def __init__(self, store, queries, artifacts=None):
        self.store, self.queries, self.artifacts = store, queries, artifacts

    def topology_request_spec(self, view_id: str) -> TopologyArtifactRequestSpec:
        """Build the persisted identity for the only cross-service artifact.

        The builder itself lands in Phase 2.  Establishing this identity now
        prevents a later implementation from accidentally caching topology by
        display filters or a mutable View id.
        """
        view = self.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        if view.scope_id is None:
            raise ValueError("legacy_mixed_scope_view")
        analyzer_inputs = []
        for member in view.members:
            if member.snapshot_id is None:
                continue
            snapshot = self.store.get_snapshot(member.snapshot_id)
            if snapshot is None:
                # A frozen available snapshot missing from storage is not a
                # partial view; the Phase 2 resolver reports it as 424.
                raise RuntimeError("snapshot_unavailable")
            analyzer_inputs.append(
                {"member_id": member.member_id, "snapshot_id": member.snapshot_id,
                 "analyzer_bundle_digest": snapshot.analyzer_bundle_digest}
            )
        return TopologyArtifactRequestSpec(
            view_digest=view.digest,
            scope_id=view.scope_id,
            members=tuple(
                (member.member_id, member.snapshot_id, member.availability.value)
                for member in view.members
            ),
            analyzer_bundle_digest=canonical_digest({"analyzer_inputs": analyzer_inputs}),
            rules_digest=view.topology_rules_digest,
            normalized_params={},
        )

    def _cache_key(self, view_id: str, artifact_kind: str, params: dict) -> str:
        if artifact_kind == "topology":
            if params:
                raise ValueError("invalid_artifact_request")
            return self.topology_request_spec(view_id).cache_key
        view = self.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        return hashlib.sha256(
            json.dumps([view.digest, artifact_kind, params], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def generate(self, view_id: str, artifact_kind: str, params: dict | None = None) -> dict:
        view = self.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        params = params or {}
        key = self._cache_key(view_id, artifact_kind, params)
        job = self.store.create_artifact_job(view_id=view_id, artifact_kind=artifact_kind, cache_key=key)
        if job["status"] == "completed":
            return job
        if job["status"] != "pending":
            return job
        job = self.store.start_artifact_job(job["id"])
        if artifact_kind == "entities":
            try:
                payload = {"view_id": view_id, "view_digest": view.digest, "members": []}
                for member in view.members:
                    if member.snapshot_id:
                        facts = self.queries.knowledge(member.snapshot_id)
                        payload["members"].append({"member_id": member.member_id, "snapshot_id": member.snapshot_id,
                                                   "entities": facts.get("core_entities", [])})
            except Exception as error:
                return self.store.fail_artifact_job(job["id"], str(error))
        else:
            payload = {"view_id": view_id, "view_digest": view.digest, "artifact_kind": artifact_kind,
                       "status": "not_generated"}
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if self.artifacts is not None and len(payload_json.encode()) > 64 * 1024:
            staging = self.artifacts.create_staging(f"artifact-job-{uuid4().hex}")
            (Path(staging) / "payload.json").write_text(payload_json, encoding="utf-8")
            from codeevolution.infrastructure.artifact_store_fs import directory_digest
            key = self.artifacts.publish(staging, directory_digest(staging))
            return self.store.complete_artifact_job(job["id"], payload, artifact_key=key)
        return self.store.complete_artifact_job(job["id"], payload)

    def get(self, view_id: str, artifact_kind: str, params: dict | None = None) -> dict | None:
        view = self.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        params = params or {}
        key = self._cache_key(view_id, artifact_kind, params)
        job = self.store.create_artifact_job(view_id=view_id, artifact_kind=artifact_kind, cache_key=key)
        return job
