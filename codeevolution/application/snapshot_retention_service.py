"""Reference-aware, recoverable snapshot deletion coordination."""

from __future__ import annotations

from collections.abc import Mapping


class SnapshotRetentionService:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    def preview(self, snapshot_id: str) -> dict:
        snapshot = self.store.get_snapshot(snapshot_id)
        if snapshot is None:
            raise KeyError(snapshot_id)
        refs = self.store.snapshot_references(snapshot_id)
        evidence = self.store.get_evidence(snapshot.evidence_digest)
        return {"snapshot_id": snapshot_id, "protected": bool(refs), "references": refs,
                "estimated_release_bytes": evidence.byte_size if evidence and not refs else 0}

    def delete(self, snapshot_id: str, confirmation: str) -> dict:
        preview = self.preview(snapshot_id)
        if preview["protected"]:
            raise ValueError("snapshot_protected")
        if confirmation != snapshot_id:
            raise ValueError("invalid_deletion_confirmation")
        return self.store.request_snapshot_deletion(snapshot_id)

    def scavenge(self, job_id: str) -> dict:
        job = self.store.get_deletion_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] != "pending":
            return job
        snapshot = self.store.get_snapshot(job["target_id"])
        evidence = self.store.get_evidence(snapshot.evidence_digest) if snapshot else None
        artifact_keys = []
        if evidence:
            artifact_keys.append(evidence.artifact_key)
        if snapshot:
            facts = snapshot.facts if isinstance(snapshot.facts, Mapping) else {}
            summary = facts.get("communication_summary", {})
            if isinstance(summary, Mapping):
                communication_key = summary.get("artifact_key")
                if communication_key and communication_key not in artifact_keys:
                    artifact_keys.append(str(communication_key))
        if artifact_keys:
            trash = None
            for artifact_key in artifact_keys:
                # A prior attempt may have moved one object before failing.  A
                # retry must treat that already-detached object as success.
                digest = str(artifact_key).removeprefix("sha256:")
                existing = self.artifacts.trash_dir / job_id / digest
                if existing.is_dir():
                    trash = existing
                    continue
                trash = self.artifacts.move_to_trash(artifact_key, job_id)
            self.store.mark_deletion_trashed(job_id, str(trash))
            try:
                self.artifacts.purge_trash(job_id)
            except Exception:
                # Leave the job in recoverable trashed state for a later scavenger pass.
                return self.store.get_deletion_job(job_id)
            return self.store.complete_deletion(job_id)
        return self.store.complete_deletion(job_id)

    def plan_member_retention(self, member_id: str, *, keep: int = 10) -> list[dict]:
        """Enqueue deletion jobs for unreferenced snapshots beyond the retention window."""
        jobs = []
        for snapshot_id in self.store.retention_candidates(member_id, keep):
            try:
                jobs.append(self.store.request_snapshot_deletion(snapshot_id))
            except ValueError:
                # A concurrent pin/current publication wins the race and protects it.
                continue
        return jobs

    def scavenge_orphans(self, *, staging_age_seconds: int = 3600,
                         trash_age_seconds: int = 86400) -> dict:
        """Recover interrupted deletion jobs and remove aged filesystem orphans."""
        jobs = []
        for job in self.store.list_deletion_jobs("pending"):
            try:
                jobs.append(self.scavenge(job["id"]))
            except (KeyError, OSError, ValueError):
                continue
        return {
            "deletion_jobs": jobs,
            "staging_removed": self.artifacts.scavenge_staging(
                max_age_seconds=staging_age_seconds,
                protected_names=self.store.active_attempt_ids(),
            ),
            "trash_removed": self.artifacts.scavenge_trash(max_age_seconds=trash_age_seconds),
        }
