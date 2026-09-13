"""Execute one repository analysis attempt from live capture to immutable publish."""

from __future__ import annotations

import hashlib
import json
import logging
import ntpath
import os
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeevolution import __version__
from codeevolution.analysis.communication.extractor import CommunicationFactExtractor
from codeevolution.analysis.communication.rules import TIER1_RULES, TIER1_RULES_DIGEST
from codeevolution.analysis.communication.schema import (
    CollectorCoverage,
    CollectorRuleSet,
    CollectorStatus,
    CommunicationArtifactReference,
    RepositoryCommunicationArtifact,
    serialize_communication_artifact,
)
from codeevolution.domain.analysis_snapshot import (
    AttemptStage,
    AttemptStatus,
    EvidenceBundle,
    RepositoryAnalysisSnapshot,
    RepositoryAttempt,
)
from codeevolution.infrastructure.analysis_snapshot_sqlite import (
    AnalysisSnapshotSQLiteStore,
    SnapshotStoreError,
)
from codeevolution.infrastructure.artifact_store_fs import (
    FileSystemArtifactStore,
    directory_digest,
)
from codeevolution.infrastructure.codegraph_capture import (
    CodeGraphCapture,
    CodeGraphCaptureError,
    freeze_sources,
)
from codeevolution.infrastructure.codegraph_command import (
    CodeGraphCommandRunner,
    CodeGraphPreflightError,
    CodeGraphTerminationError,
)
from codeevolution.infrastructure.snapshot_source import SnapshotSourceInventory
from codeevolution.infrastructure.workspace_input_scanner import (
    InputEntry,
    InputObservation,
    ScanPolicy,
    WorkspaceInputScanner,
    WorkspaceScanError,
)
from codeevolution.platform import (
    PlatformCapabilityError,
    manifest_collision_key,
    normalize_manifest_path,
    remove_tree,
    sqlite_readonly_uri,
)


class AttemptExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AttemptCancelledError(RuntimeError):
    pass


ANALYZER_BUNDLE_DIGEST = f"codeevolution:{__version__}:repository-analysis-v2"
logger = logging.getLogger(__name__)


class RepositoryAttemptWorker:
    """Run the strict A/B/C + E/F capture protocol for one member."""

    def __init__(
        self,
        store: AnalysisSnapshotSQLiteStore,
        artifacts: FileSystemArtifactStore,
        *,
        command_runner: CodeGraphCommandRunner | None = None,
        graph_capture: CodeGraphCapture | None = None,
        scan_policy: ScanPolicy | None = None,
        analyzer: Callable[[Path, SnapshotSourceInventory], dict[str, Any]] | None = None,
        codegraph_version: str = "0.9.x",
        analyzer_bundle_digest: str | None = None,
        rules_digest: str | None = None,
        report_schema_version: str = "1",
        options_digest: str = "sha256:default",
    ):
        self.store = store
        self.artifacts = artifacts
        self.command_runner = command_runner or CodeGraphCommandRunner()
        self.graph_capture = graph_capture or CodeGraphCapture()
        self.scan_policy = scan_policy or ScanPolicy()
        self.analyzer = analyzer or _analyze_existing_report
        self.codegraph_version = codegraph_version
        # Bump the algorithm identity when the persisted facts/artifact
        # contract changes.  The package version alone did not invalidate
        # snapshots created before communication artifacts were introduced.
        self.analyzer_bundle_digest = analyzer_bundle_digest or ANALYZER_BUNDLE_DIGEST
        self.rules_digest = rules_digest or TIER1_RULES_DIGEST
        self.report_schema_version = report_schema_version
        self.options_digest = options_digest

    def __call__(self, attempt: RepositoryAttempt) -> None:
        staging: Path | None = None
        try:
            staging = self.artifacts.create_staging(attempt.id)
            self._execute(attempt, staging)
        except AttemptCancelledError:
            self._finish_cancelled(attempt.id)
            if staging is not None:
                _reclaim_staging(staging)
        except SnapshotStoreError as error:
            self._finish_failed(attempt.id, "insufficient_storage", str(error))
            if staging is not None:
                _reclaim_staging(staging)
        except PlatformCapabilityError as error:
            self._finish_failed(attempt.id, "unsupported_storage", str(error))
            if staging is not None:
                _reclaim_staging(staging)
        except CodeGraphCaptureError as error:
            message = str(error)
            lowered = message.lower()
            if "source changed" in lowered:
                code = "source_changed_during_capture"
            elif "unsafe" in lowered or "reparse" in lowered or "unreadable" in lowered:
                code = "unsafe_or_unreadable"
            else:
                code = "capture_failed"
            self._finish_failed(attempt.id, code, message)
            if staging is not None:
                _reclaim_staging(staging)
        except WorkspaceScanError as error:
            message = str(error)
            lowered = message.lower()
            if "unsafe" in lowered or "unreadable" in lowered:
                code = "unsafe_or_unreadable"
            elif "git" in lowered:
                code = "git_error"
            else:
                code = "scan_failed"
            self._finish_failed(attempt.id, code, message)
            if staging is not None:
                _reclaim_staging(staging)
        except AttemptExecutionError as error:
            self._finish_failed(attempt.id, error.code, str(error))
            if staging is not None:
                _reclaim_staging(staging)
        finally:
            # publish_snapshot or any terminal failure releases the reservation.
            self.store.release_storage(attempt.id)

    def _execute(self, attempt: RepositoryAttempt, staging: Path) -> None:
        member = self.store.get_member(attempt.member_id)
        if member is None or member.retired_at is not None:
            raise AttemptExecutionError("member_not_registered", "repository member is unavailable")
        root = Path(member.registered_path).expanduser().resolve()
        if not root.is_dir():
            raise AttemptExecutionError("path_missing", "repository path does not exist")
        if not (root / ".git").exists():
            raise AttemptExecutionError("not_git_repository", "repository path is not a Git repository")
        identity_matches = str(root) == member.path_identity or (
            os.name == "nt" and str(root).casefold() == member.path_identity.casefold()
        )
        if not identity_matches:
            raise AttemptExecutionError("path_identity_changed", "repository path identity changed")
        try:
            self.artifacts.data_dir.relative_to(root)
        except ValueError:
            pass
        else:
            raise AttemptExecutionError(
                "unsupported_storage_layout",
                "analysis data directory must be outside the repository being analyzed",
            )

        scanner = WorkspaceInputScanner(root)
        self._stage(attempt.id, AttemptStage.DIGEST_BEFORE, 5)
        before = scanner.scan(self.scan_policy)
        self._record(attempt.id, "before_sync", before)
        estimate = max(sum(item.size for item in before.entries) * 3, 64 * 1024 * 1024)
        free = shutil.disk_usage(self.artifacts.data_dir).free
        expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
        self.store.reserve_storage(attempt.id, estimate, free, expires)

        graph_db = root / ".codegraph" / "codegraph.db"
        command_stage = AttemptStage.CODEGRAPH_SYNC if graph_db.is_file() else AttemptStage.CODEGRAPH_INIT
        self._stage(attempt.id, command_stage, 15)
        try:
            result = self.command_runner.init_or_sync(
                root, lambda: self._cancel_requested(attempt.id)
            )
        except CodeGraphPreflightError as error:
            raise AttemptExecutionError("codegraph_not_found", str(error)) from error
        except CodeGraphTerminationError as error:
            raise AttemptExecutionError("codegraph_termination_failed", str(error)) from error
        except FileNotFoundError as error:
            raise AttemptExecutionError("codegraph_not_found", str(error)) from error
        if result.cancelled:
            raise AttemptCancelledError()
        if result.timed_out:
            raise AttemptExecutionError("codegraph_timeout", "CodeGraph command timed out")
        if not result.succeeded:
            action = "sync" if command_stage == AttemptStage.CODEGRAPH_SYNC else "init"
            raise AttemptExecutionError(f"codegraph_{action}_failed", _safe_message(result.stderr))

        self._stage(attempt.id, AttemptStage.DIGEST_AFTER, 30)
        after = scanner.scan(self.scan_policy)
        self._record(attempt.id, "after_sync", after)
        if before.source_digest != after.source_digest:
            raise AttemptExecutionError(
                "source_changed_during_capture", "analysis inputs changed while CodeGraph was updating"
            )

        self._stage(attempt.id, AttemptStage.FREEZING_GRAPH, 40)
        evidence_staging = staging / "evidence"
        evidence_staging.mkdir(mode=0o700)
        graph = self.graph_capture.capture(graph_db, evidence_staging / "codegraph.db", root)

        self._stage(attempt.id, AttemptStage.FREEZING_SOURCES, 55)
        expected = _capture_entries(after, graph.path, root)
        frozen = freeze_sources(root, expected, evidence_staging / "sources")

        self._stage(attempt.id, AttemptStage.DIGEST_FINAL, 65)
        final = scanner.scan(self.scan_policy)
        self._record(attempt.id, "after_freeze", final)
        if before.source_digest != final.source_digest:
            raise AttemptExecutionError(
                "source_changed_during_capture", "analysis inputs changed while evidence was freezing"
            )
        if _entry_digest(expected) != _entry_digest(frozen):
            raise AttemptExecutionError(
                "source_changed_during_capture", "frozen evidence does not match expected inputs"
            )

        manifest = _manifest(self.scan_policy, after, frozen, graph)
        manifest_bytes = _canonical_bytes(manifest)
        (evidence_staging / "manifest.json").write_bytes(manifest_bytes)
        evidence_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        inventory = SnapshotSourceInventory(evidence_staging / "sources", manifest)

        self._stage(attempt.id, AttemptStage.ANALYZING, 75)
        facts = self.analyzer(graph.path, inventory)
        snapshot_id = str(uuid4())
        communication = self._communication_artifact(
            graph.path, inventory, snapshot_id, member_id=attempt.member_id
        )
        communication_bytes = serialize_communication_artifact(communication)
        communication_staging = staging / "communication"
        communication_staging.mkdir(mode=0o700)
        (communication_staging / "communication.json").write_bytes(communication_bytes)
        communication_digest = directory_digest(communication_staging)
        communication_key = "sha256:" + communication_digest
        communication_summary = communication.summary(
            CommunicationArtifactReference(
                communication_key, communication.payload_digest(), len(communication_bytes)
            )
        )
        facts = {**facts, "communication_summary": communication_summary}
        facts_bytes = _canonical_bytes(facts)
        facts_digest = "sha256:" + hashlib.sha256(facts_bytes).hexdigest()

        self._stage(attempt.id, AttemptStage.PUBLISHING, 95)
        if self._cancel_requested(attempt.id):
            raise AttemptCancelledError()
        # Evidence identity is the manifest digest.  Keep derived communication
        # output in its own CAS object: it embeds the snapshot ID and therefore
        # must not change the reusable Evidence Bundle identity.
        communication_key = self.artifacts.publish(communication_staging, communication_digest)
        artifact_key = self.artifacts.publish(
            evidence_staging, directory_digest(evidence_staging)
        )
        evidence = EvidenceBundle(
            digest=evidence_digest,
            artifact_key=artifact_key,
            manifest_schema_version="1",
            source_digest=_entry_digest(frozen),
            graph_digest=graph.logical_graph_digest,
            graph_blob_sha256=graph.blob_sha256,
            byte_size=_artifact_size(self.artifacts.open(artifact_key)),
            capture_policy_digest=_policy_digest(self.scan_policy),
            capture_completeness="incomplete" if after.exclusions else "complete",
        )
        snapshot = RepositoryAnalysisSnapshot(
            id=snapshot_id,
            member_id=member.id,
            evidence_digest=evidence.digest,
            captured_at="",
            dirty=bool(final.git_dirty),
            codegraph_version=self.codegraph_version,
            codegraph_schema_digest=graph.schema_fingerprint,
            analyzer_bundle_digest=self.analyzer_bundle_digest,
            rules_digest=self.rules_digest,
            report_schema_version=self.report_schema_version,
            options_digest=self.options_digest,
            facts_digest=facts_digest,
            facts_storage="inline",
            facts=facts,
            analysis_completeness="incomplete" if after.exclusions else "complete",
            observed_branch=final.git_branch,
            observed_head_commit=final.git_head,
        )
        published = self.store.publish_snapshot(attempt.id, evidence, snapshot)
        self._generate_terms(published, facts)
        if published.id != snapshot.id:
            # The immutable snapshot identity already existed.  The retry's
            # communication payload embeds a fresh snapshot ID and is therefore
            # not referenced by the reused facts; reclaim that CAS object.
            cleanup_id = f"orphan-communication-{attempt.id}"
            try:
                self.artifacts.move_to_trash(communication_key, cleanup_id)
                self.artifacts.purge_trash(cleanup_id)
            except Exception:
                # A later retention/scavenger pass can recover an orphan CAS
                # object; publishing the unchanged result must still succeed.
                pass
        # The two published CAS objects were renamed out of this attempt's
        # staging directory; do not leave an empty per-attempt container behind.
        _reclaim_staging(staging)

    def _generate_terms(self, snapshot: RepositoryAnalysisSnapshot, facts: dict[str, Any]) -> None:
        """Materialize derived terms after snapshot publication without blocking it."""
        try:
            from codeevolution.application.term_service import TermRecognitionService
            from codeevolution.infrastructure.term_store import TermStore

            term_store = TermStore(self.artifacts.data_dir / "terms.db")
            TermRecognitionService(term_store).extract(snapshot.id, snapshot.member_id, facts)
        except Exception:
            # Terminology is a derived knowledge projection. A storage or rule
            # failure must be visible in logs but must not invalidate a durable
            # repository snapshot that has already been published.
            logger.exception("automatic terminology generation failed for snapshot %s", snapshot.id)

    def _communication_artifact(
        self,
        graph_path: Path,
        sources: SnapshotSourceInventory,
        snapshot_id: str,
        *,
        member_id: str | None = None,
    ) -> RepositoryCommunicationArtifact:
        """Build communication facts exclusively from the frozen staging inputs."""
        from codeevolution.infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository

        rules = CollectorRuleSet(
            digest=self.rules_digest,
            version=str(TIER1_RULES["version"]),
            budgets=dict(TIER1_RULES["budgets"]),
            options={"support": TIER1_RULES["support"]},
        )
        try:
            with SQLiteCodeGraphRepository(str(graph_path)) as repository:
                return CommunicationFactExtractor().collect(
                    repository, sources, rules, snapshot_id=snapshot_id, member_id=member_id
                )
        except Exception as error:
            # Communication evidence is still explicit and auditable when a
            # language/framework collector cannot inspect a frozen graph.
            return RepositoryCommunicationArtifact(
                snapshot_id=snapshot_id,
                rules_digest=self.rules_digest,
                collector_coverage=(
                    CollectorCoverage(
                        "communication",
                        "communication/v1",
                        CollectorStatus.FAILED,
                        reason=type(error).__name__,
                    ),
                ),
            )

    def _stage(self, attempt_id: str, stage: AttemptStage, percent: int) -> None:
        if self._cancel_requested(attempt_id):
            raise AttemptCancelledError()
        current = self.store.get_attempt(attempt_id)
        if current is None or current.status != AttemptStatus.RUNNING:
            raise AttemptCancelledError()
        self.store.transition_attempt(
            attempt_id,
            expected_status=AttemptStatus.RUNNING,
            status=AttemptStatus.RUNNING,
            stage=stage,
            progress={"percent": percent, "stage": stage.value},
        )

    def _cancel_requested(self, attempt_id: str) -> bool:
        current = self.store.get_attempt(attempt_id)
        return current is None or current.cancellation_requested_at is not None

    def _record(self, attempt_id: str, phase: str, value: InputObservation) -> None:
        self.store.record_observation(
            attempt_id,
            phase,
            input_digest=value.source_digest,
            observed_branch=value.git_branch,
            observed_head_commit=value.git_head,
            dirty=bool(value.git_dirty),
            file_count=len(value.entries),
            total_bytes=sum(item.size for item in value.entries),
        )

    def _finish_cancelled(self, attempt_id: str) -> None:
        current = self.store.get_attempt(attempt_id)
        if current is not None and current.status == AttemptStatus.RUNNING:
            self.store.transition_attempt(
                attempt_id,
                expected_status=AttemptStatus.RUNNING,
                status=AttemptStatus.CANCELLED,
                stage=AttemptStage.FINISHED,
                error_code="cancelled_by_user",
            )

    def _finish_failed(self, attempt_id: str, code: str, message: str) -> None:
        current = self.store.get_attempt(attempt_id)
        if current is not None and current.status == AttemptStatus.RUNNING:
            self.store.transition_attempt(
                attempt_id,
                expected_status=AttemptStatus.RUNNING,
                status=AttemptStatus.FAILED,
                stage=AttemptStage.FINISHED,
                error_code=code,
                error_message=_safe_message(message),
            )


def _capture_entries(
    observation: InputObservation, graph_db: Path, repository_root: Path
) -> tuple[InputEntry, ...]:
    with closing(sqlite3.connect(sqlite_readonly_uri(graph_db), uri=True)) as connection:
        graph_paths = {str(row[0]).replace("\\", "/") for row in connection.execute("SELECT path FROM files")}
    normalized: set[str] = set()
    collision_keys: set[str] = set()
    for value in graph_paths:
        path = Path(value)
        if path.is_absolute() or ntpath.isabs(value) or ntpath.splitdrive(value)[0]:
            try:
                value = path.resolve().relative_to(repository_root).as_posix()
            except ValueError:
                continue
        try:
            value = normalize_manifest_path(value.removeprefix("./"))
            collision = manifest_collision_key(value) if os.name == "nt" else value
        except ValueError:
            continue
        if collision in collision_keys:
            continue
        collision_keys.add(collision)
        normalized.add(value)
    return tuple(
        item
        for item in observation.entries
        if item.path in normalized or "declared" in item.categories
    )


def _reclaim_staging(path: Path) -> None:
    """Best-effort cleanup; Windows scanners may require later scavenging."""
    try:
        remove_tree(path)
    except OSError:
        pass


def _manifest(policy, observation, entries, graph) -> dict[str, Any]:
    return {
        "manifest_schema_version": "1",
        "capture_policy_digest": _policy_digest(policy),
        "codegraph": {
            "path": "codegraph.db",
            "blob_sha256": graph.blob_sha256,
            "logical_graph_digest": graph.logical_graph_digest,
            "byte_size": graph.byte_size,
            "schema_fingerprint": graph.schema_fingerprint,
            "integrity_check": graph.integrity_check,
        },
        "sources": [
            {
                "path": item.path,
                "sha256": item.sha256,
                "size": item.size,
                "category": list(item.categories),
                "encoding_status": "utf8",
            }
            for item in entries
        ],
        "exclusions": [
            {"path": item.path, "reason": item.reason, "required_by_analyzer": "unknown"}
            for item in observation.exclusions
        ],
        "digests": {"source": _entry_digest(entries), "graph": graph.logical_graph_digest},
        "capture_completeness": {
            "status": "incomplete" if observation.exclusions else "complete",
            "reasons": sorted({item.reason for item in observation.exclusions}),
        },
    }


def _analyze_existing_report(graph_db: Path, sources: SnapshotSourceInventory) -> dict[str, Any]:
    from codeevolution.infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository
    from codeevolution.knowledge import KnowledgeExtractor

    repository = SQLiteCodeGraphRepository(str(graph_db))
    try:
        return KnowledgeExtractor(repository, sources).extract_all(include_llm=False)
    finally:
        repository.close()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _entry_digest(entries: tuple[InputEntry, ...]) -> str:
    body = [
        {"category": list(item.categories), "path": item.path, "sha256": item.sha256, "size": item.size}
        for item in sorted(entries, key=lambda value: value.path)
    ]
    return "sha256:" + hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _policy_digest(policy: ScanPolicy) -> str:
    body = {
        "declared_globs": sorted(policy.declared_globs),
        "indexable_extensions": sorted(policy.indexable_extensions),
        "max_file_bytes": policy.max_file_bytes,
        "policy_version": policy.policy_version,
    }
    return "sha256:" + hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _artifact_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _safe_message(value: str) -> str:
    return (value or "operation failed").replace("\n", " ").replace("\r", " ")[:500]
