"""Deterministic, snapshot-bound Graph View artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4


class GraphArtifactService:
    def __init__(self, store, queries, artifacts=None):
        self.store, self.queries, self.artifacts = store, queries, artifacts

    def generate(self, view_id: str, artifact_kind: str, params: dict | None = None) -> dict:
        view = self.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        params = params or {}
        key = hashlib.sha256(json.dumps([view.digest, artifact_kind, params], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
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
        key = hashlib.sha256(json.dumps([view.digest, artifact_kind, params], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        job = self.store.create_artifact_job(view_id=view_id, artifact_kind=artifact_kind, cache_key=key)
        return job
