"""Explicit Graph View artifact generation and read-only cache queries."""

from __future__ import annotations

import hashlib
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
from codeevolution.infrastructure.analysis_snapshot_sqlite import (
    DEFAULT_TOPOLOGY_CACHE_RETENTION_SECONDS,
    DEFAULT_TOPOLOGY_JOB_RETENTION_SECONDS,
    ViewExpiredError,
)
from codeevolution.infrastructure.artifact_store_fs import directory_digest

MAX_TOPOLOGY_PAYLOAD_BYTES = 64 * 1024 * 1024


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

        registry_digest = getattr(self.builder, "known_external_registry_digest", "")
        topology_rules_digest = getattr(view, "topology_rules_digest", "")
        rules_digest = canonical_digest({
            "topology_rules_digest": topology_rules_digest,
            "known_external_registry_digest": registry_digest,
        }) if registry_digest else topology_rules_digest

        return TopologyArtifactRequestSpec(
            view_digest=view.digest,
            scope_id=view.scope_id,
            members=tuple(
                (item.member_id, item.snapshot_id, item.availability.value)
                for item in view.members
            ),
            analyzer_bundle_digest=canonical_digest({"analyzer_inputs": analyzer_inputs}),
            rules_digest=rules_digest,
            normalized_params={},
        )

    def create_job(self, view_id: str, artifact_kind: str = "topology", params: dict | None = None) -> dict:
        params = params or {}
        if artifact_kind != "topology" or params:
            raise GraphArtifactRequestError("invalid_artifact_request")
        try:
            self._ensure_analyzable(view_id)
        except GraphViewResolutionError as error:
            raise GraphArtifactRequestError(str(error)) from error
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
        running = self.store.start_artifact_job(job_id)
        if running.get("status") != "running" or not running.get("lease_token"):
            return running
        lease_token = running["lease_token"]
        try:
            self.store.heartbeat_artifact_job(job_id, lease_token)
            resolved, artifacts = self.resolver.resolve(job["view_id"])
            payload = self.builder.build(resolved, artifacts)
            self.store.heartbeat_artifact_job(job_id, lease_token)
            return self._complete(job_id, payload, lease_token=lease_token)
        except GraphViewResolutionError as error:
            return self.store.fail_artifact_job(job_id, str(error), code=str(error), lease_token=lease_token)
        except Exception as error:  # the Job retains an auditable failure
            return self.store.fail_artifact_job(job_id, str(error), code="artifact_generation_failed", lease_token=lease_token)

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
        self.store.touch_artifact_cache(spec.cache_key)
        result = dict(cache)
        result["status"] = "completed"
        return result

    def scavenge_cache(
        self,
        *,
        max_age_seconds: int = DEFAULT_TOPOLOGY_CACHE_RETENTION_SECONDS,
        limit: int = 100,
    ) -> list[dict]:
        """Apply bounded TTL/LRU cleanup to cache metadata.

        The store excludes active Jobs and reader leases.  CAS payloads are
        intentionally left recoverable for a separate, reference-aware sweep.
        """
        return self.store.scavenge_artifact_cache(max_age_seconds=max_age_seconds, limit=limit)

    def scavenge_jobs(self, *, max_age_seconds: int = DEFAULT_TOPOLOGY_JOB_RETENTION_SECONDS,
                      limit: int = 100) -> list[dict]:
        """Apply the bounded retention window to terminal Job metadata."""
        return self.store.scavenge_artifact_jobs(max_age_seconds=max_age_seconds, limit=limit)

    def get_job(self, job_id: str) -> dict:
        """Read a Job only through its still-authorized Graph View.

        Job IDs are opaque and are not an authorization capability.  Resolving
        the owning View here prevents a guessed ID from exposing a completed
        payload or retrying work after an ephemeral View expired.
        """
        job = self.store.get_artifact_job(job_id)
        if job is None:
            raise KeyError(job_id)
        view_id = job.get("view_id")
        if not view_id:
            raise GraphArtifactRequestError("artifact_view_unavailable")
        self._view(str(view_id))
        return self.store.public_artifact_job(job)

    def retry_job(self, job_id: str) -> dict:
        """Retry a failed Job only while its owning View remains valid."""
        job = self.store.get_artifact_job(job_id)
        if job is None:
            raise KeyError(job_id)
        view_id = job.get("view_id")
        if not view_id:
            raise GraphArtifactRequestError("artifact_view_unavailable")
        self._view(str(view_id))
        return self.store.public_artifact_job(self.store.retry_artifact_job(job_id))

    def cancel_job(self, job_id: str, *, administrator: bool = False) -> dict:
        """Internal administrative cancel operation.

        The shared HTTP/MCP surfaces deliberately do not expose cancellation.
        Keeping the authorization check here prevents a future caller from
        accidentally turning the storage primitive into an unguarded route.
        """
        if not administrator:
            raise GraphArtifactRequestError("artifact_cancel_requires_admin")
        job = self.store.get_artifact_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if not job.get("view_id"):
            raise GraphArtifactRequestError("artifact_view_unavailable")
        self._view(str(job["view_id"]))
        return self.store.public_artifact_job(self.store.cancel_artifact_job(job_id))

    def history(self, view_id: str, artifact_kind: str = "topology", *, limit: int = 20) -> list[dict]:
        """Return generation attempts for audit/progress display, newest first."""
        if artifact_kind != "topology":
            raise GraphArtifactRequestError("invalid_artifact_request")
        self._view(view_id)
        return [
            self.store.public_artifact_job(item)
            for item in self.store.list_artifact_jobs(view_id, artifact_kind=artifact_kind, limit=limit)
        ]

    def _read_cache(self, view_id: str, artifact_kind: str, params: dict) -> dict | None:
        view = self._view(view_id)
        from hashlib import sha256

        key = sha256(
            json.dumps([view.digest, artifact_kind, params], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cache = self.store.get_artifact_cache(key)
        if cache is None:
            return None
        self.store.touch_artifact_cache(key)
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

    def _complete(self, job_id: str, payload: dict, *, lease_token: str | None = None) -> dict:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(payload_json.encode("utf-8")) > MAX_TOPOLOGY_PAYLOAD_BYTES:
            raise ValueError("artifact_payload_too_large")
        if self.artifacts is not None and len(payload_json.encode("utf-8")) >= 1024 * 1024:
            staging = self.artifacts.create_staging(f"artifact-job-{uuid4().hex}")
            (Path(staging) / "payload.json").write_text(payload_json, encoding="utf-8")
            key = self.artifacts.publish(staging, directory_digest(staging))
            return self.store.complete_artifact_job(job_id, payload, artifact_key=key, lease_token=lease_token)
        return self.store.complete_artifact_job(job_id, payload, lease_token=lease_token)

    def _ensure_analyzable(self, view_id: str) -> None:
        resolved, _ = self.resolver.resolve(view_id)
        if not any(isinstance(item, SnapshotHandle) for item in resolved.members):
            raise GraphArtifactRequestError("view_has_no_analyzable_members")

    def _view(self, view_id: str):
        try:
            view = self.store.get_view(view_id)
        except ViewExpiredError as error:
            raise GraphArtifactRequestError("view_expired") from error
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
        try:
            expected_tree = str(key).removeprefix("sha256:")
            if directory_digest(root) != expected_tree:
                return None
        except Exception:
            return None
        for filename in ("communication.json", "repository-communication.json", "payload.json"):
            candidate = root / filename
            if not candidate.is_file():
                continue
            try:
                raw = candidate.read_bytes()
                expected_size = summary.get("byte_size") if isinstance(summary, dict) else None
                expected_payload = summary.get("payload_digest") if isinstance(summary, dict) else None
                if expected_size is not None and int(expected_size) != len(raw):
                    return None
                if expected_payload and str(expected_payload) != "sha256:" + hashlib.sha256(raw).hexdigest():
                    return None
                return deserialize_communication_artifact(raw)
            except ValueError:
                return None
        return None


__all__ = ["GraphArtifactRequestError", "GraphArtifactService"]
