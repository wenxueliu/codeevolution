"""Explicit Graph View artifact generation and read-only cache queries."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from codeevolution.analysis.communication.schema import deserialize_communication_artifact
from codeevolution.analysis.topology.snapshot_builder import TopologyArtifactBuilder
from codeevolution.application.graph_view_resolver import (
    GraphViewResolutionError,
    GraphViewResolver,
)
from codeevolution.domain.topology import SnapshotHandle
from codeevolution.infrastructure.artifact_store_fs import directory_digest


class GraphArtifactRequestError(ValueError):
    """A client supplied artifact request cannot be accepted."""


class GraphArtifactService:
    """Application boundary for the single persisted Topology artifact.

    ``create_job`` is explicit and may be called by Web, CLI or MCP. ``get``
    is deliberately read-only: a cache miss never creates work.
    """

    def __init__(self, store, queries, artifacts=None, *, resolver=None, builder=None):
        self.store, self.queries, self.artifacts = store, queries, artifacts
        self.resolver = resolver or GraphViewResolver(store, self._load_communication)
        self.builder = builder or TopologyArtifactBuilder()

    def topology_request_spec(self, view_id: str):
        view = self._view(view_id)
        if view.scope_id is None:
            raise GraphArtifactRequestError("legacy_mixed_scope_view")
        analyzer_inputs = []
        for member in view.members:
            if member.snapshot_id is None:
                continue
            snapshot = self.store.get_snapshot(member.snapshot_id)
            if snapshot is None or snapshot.deletion_state != "active":
                raise GraphArtifactRequestError("snapshot_unavailable")
            analyzer_inputs.append(
                {
                    "member_id": member.member_id,
                    "snapshot_id": member.snapshot_id,
                    "analyzer_bundle_digest": snapshot.analyzer_bundle_digest,
                }
            )
        from codeevolution.domain.topology import TopologyArtifactRequestSpec, canonical_digest

        return TopologyArtifactRequestSpec(
            view_digest=view.digest,
            scope_id=view.scope_id,
            members=tuple(
                (item.member_id, item.snapshot_id, item.availability.value)
                for item in view.members
            ),
            analyzer_bundle_digest=canonical_digest({"analyzer_inputs": analyzer_inputs}),
            rules_digest=view.topology_rules_digest,
            normalized_params={},
        )

    def create_job(self, view_id: str, artifact_kind: str = "topology", params: dict | None = None) -> dict:
        params = params or {}
        if artifact_kind != "topology" or params:
            raise GraphArtifactRequestError("invalid_artifact_request")
        self._ensure_analyzable(view_id)
        spec = self.topology_request_spec(view_id)
        return self.store.create_artifact_job(
            view_id=view_id, artifact_kind=artifact_kind, cache_key=spec.cache_key,
            request_spec=spec.to_dict(),
        )

    def run_job(self, job_id: str) -> dict:
        job = self.store.get_artifact_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] == "completed":
            return job
        if job["status"] != "pending":
            return job
        self.store.start_artifact_job(job_id)
        try:
            resolved, artifacts = self.resolver.resolve(job["view_id"])
            payload = self.builder.build(resolved, artifacts)
            return self._complete(job_id, payload)
        except GraphViewResolutionError as error:
            return self.store.fail_artifact_job(job_id, str(error), code=str(error))
        except Exception as error:  # the Job retains an auditable failure
            return self.store.fail_artifact_job(job_id, str(error), code="artifact_generation_failed")

    def generate(self, view_id: str, artifact_kind: str = "topology", params: dict | None = None) -> dict:
        """Compatibility helper used by local callers; creates then runs a Job."""
        if artifact_kind == "entities":
            return self._generate_entities(view_id)
        job = self.create_job(view_id, artifact_kind, params)
        if job["status"] == "pending":
            return self.run_job(job["id"])
        return job

    def get(self, view_id: str, artifact_kind: str = "topology", params: dict | None = None) -> dict | None:
        """Read a completed cache entry without creating a Job."""
        params = params or {}
        if artifact_kind == "entities":
            return self._read_cache(view_id, artifact_kind, params)
        if artifact_kind != "topology" or params:
            raise GraphArtifactRequestError("invalid_artifact_request")
        spec = self.topology_request_spec(view_id)
        cache = self.store.get_artifact_cache(spec.cache_key)
        if cache is None:
            return None
        result = dict(cache)
        result["status"] = "completed"
        return result

    def _read_cache(self, view_id: str, artifact_kind: str, params: dict) -> dict | None:
        view = self._view(view_id)
        from hashlib import sha256

        key = sha256(
            json.dumps([view.digest, artifact_kind, params], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cache = self.store.get_artifact_cache(key)
        if cache is None:
            return None
        result = dict(cache)
        result["status"] = "completed"
        return result

    def _generate_entities(self, view_id: str) -> dict:
        view = self._view(view_id)
        from hashlib import sha256

        params: dict[str, object] = {}
        key = sha256(
            json.dumps([view.digest, "entities", params], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        job = self.store.create_artifact_job(view_id=view_id, artifact_kind="entities", cache_key=key)
        if job["status"] != "pending":
            return job
        self.store.start_artifact_job(job["id"])
        try:
            payload = {"view_id": view_id, "view_digest": view.digest, "members": []}
            for member in view.members:
                if member.snapshot_id:
                    facts = self.queries.knowledge(member.snapshot_id)
                    payload["members"].append(
                        {"member_id": member.member_id, "snapshot_id": member.snapshot_id,
                         "entities": facts.get("core_entities", [])}
                    )
            return self.store.complete_artifact_job(job["id"], payload)
        except Exception as error:
            return self.store.fail_artifact_job(job["id"], str(error))

    def _complete(self, job_id: str, payload: dict) -> dict:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if self.artifacts is not None and len(payload_json.encode("utf-8")) >= 1024 * 1024:
            staging = self.artifacts.create_staging(f"artifact-job-{uuid4().hex}")
            (Path(staging) / "payload.json").write_text(payload_json, encoding="utf-8")
            key = self.artifacts.publish(staging, directory_digest(staging))
            return self.store.complete_artifact_job(job_id, payload, artifact_key=key)
        return self.store.complete_artifact_job(job_id, payload)

    def _ensure_analyzable(self, view_id: str) -> None:
        resolved, _ = self.resolver.resolve(view_id)
        if not any(isinstance(item, SnapshotHandle) for item in resolved.members):
            raise GraphArtifactRequestError("view_has_no_analyzable_members")

    def _view(self, view_id: str):
        view = self.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        return view

    def _load_communication(self, snapshot):
        if self.artifacts is None:
            return None
        facts = snapshot.facts if isinstance(snapshot.facts, dict) else {}
        summary = facts.get("communication_summary", {})
        key = summary.get("artifact_key") if isinstance(summary, dict) else None
        if not key:
            return None
        try:
            root = self.artifacts.open(key)
        except Exception:
            return None
        for filename in ("communication.json", "repository-communication.json", "payload.json"):
            candidate = root / filename
            if not candidate.is_file():
                continue
            try:
                return deserialize_communication_artifact(candidate.read_bytes())
            except ValueError:
                return None
        return None


__all__ = ["GraphArtifactRequestError", "GraphArtifactService"]
