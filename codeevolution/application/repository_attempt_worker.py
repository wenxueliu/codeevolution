"""Execute one repository analysis attempt from live capture to immutable publish."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeevolution import __version__
from codeevolution.domain.analysis_snapshot import (
    AttemptStage,
    AttemptStatus,
    EvidenceBundle,
    RepositoryAnalysisSnapshot,
    RepositoryAttempt,
)
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore, SnapshotStoreError
from codeevolution.infrastructure.artifact_store_fs import (
    FileSystemArtifactStore,
    directory_digest,
)
from codeevolution.infrastructure.codegraph_capture import CodeGraphCapture, freeze_sources
from codeevolution.infrastructure.codegraph_command import CodeGraphCommandRunner
from codeevolution.infrastructure.snapshot_source import SnapshotSourceInventory
from codeevolution.infrastructure.workspace_input_scanner import (
    InputEntry,
    InputObservation,
    ScanPolicy,
    WorkspaceInputScanner,
)


class AttemptExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AttemptCancelledError(RuntimeError):
    pass


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
        rules_digest: str = "rules:phase1",
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
        self.analyzer_bundle_digest = analyzer_bundle_digest or f"codeevolution:{__version__}"
        self.rules_digest = rules_digest
        self.report_schema_version = report_schema_version
        self.options_digest = options_digest

    def __call__(self, attempt: RepositoryAttempt) -> None:
        staging: Path | None = None
        reserved = False
        try:
            staging = self.artifacts.create_staging(attempt.id)
            self._execute(attempt, staging)
            reserved = True
        except AttemptCancelledError:
            self._finish_cancelled(attempt.id)
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
        except SnapshotStoreError as error:
            self._finish_failed(attempt.id, "insufficient_storage", str(error))
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
        except AttemptExecutionError as error:
            self._finish_failed(attempt.id, error.code, str(error))
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
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
        if str(root) != member.path_identity and str(root).casefold() != member.path_identity.casefold():
            raise AttemptExecutionError("path_identity_changed", "repository path identity changed")

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
        result = self.command_runner.init_or_sync(root, lambda: self._cancel_requested(attempt.id))
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
        graph = self.graph_capture.capture(graph_db, staging / "codegraph.db", root)

        self._stage(attempt.id, AttemptStage.FREEZING_SOURCES, 55)
        expected = _capture_entries(after, graph.path, root)
        frozen = freeze_sources(root, expected, staging / "sources")

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
        (staging / "manifest.json").write_bytes(manifest_bytes)
        evidence_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        inventory = SnapshotSourceInventory(staging / "sources", manifest)

        self._stage(attempt.id, AttemptStage.ANALYZING, 75)
        facts = self.analyzer(graph.path, inventory)
        facts_bytes = _canonical_bytes(facts)
        facts_digest = "sha256:" + hashlib.sha256(facts_bytes).hexdigest()

        self._stage(attempt.id, AttemptStage.PUBLISHING, 95)
        if self._cancel_requested(attempt.id):
            raise AttemptCancelledError()
        artifact_key = self.artifacts.publish(staging, directory_digest(staging))
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
            id=str(uuid4()),
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
        self.store.publish_snapshot(attempt.id, evidence, snapshot)

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
    with sqlite3.connect(f"file:{graph_db.as_posix()}?mode=ro", uri=True) as connection:
        graph_paths = {str(row[0]).replace("\\", "/") for row in connection.execute("SELECT path FROM files")}
    normalized: set[str] = set()
    for value in graph_paths:
        path = Path(value)
        if path.is_absolute():
            try:
                value = path.resolve().relative_to(repository_root).as_posix()
            except ValueError:
                continue
        normalized.add(value.removeprefix("./"))
    return tuple(
        item
        for item in observation.entries
        if item.path in normalized or "declared" in item.categories
    )


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
