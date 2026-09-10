"""FastAPI backend for the CodeEvolution web dashboard — multi-repo support."""

import json
import hashlib
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analysis.knowledge.call_tree import CallTreeService
from .analysis.knowledge.node_rule import NodeRuleService
from .application.chat_service import ChatService, SnapshotChatService
from .application.knowledge_service import GroupedKnowledgeService, KnowledgeService
from .application.snapshot_runtime import SnapshotRuntime
from .application.snapshot_query_service import SnapshotQueryService
from .application.ui_recording_service import UiRecordingService
from .infrastructure.audit_store import AuditStore
from .infrastructure.business_rule_store import BusinessRuleStore
from .infrastructure.explanation_snapshot_store import (
    ExplanationSnapshotStore,
    SnapshotStateError,
)
from .infrastructure.llm_config_store import LLMConfigStore
from .infrastructure.node_rule_store import NodeRuleStore
from .infrastructure.analysis_snapshot_sqlite import utc_now
from .infrastructure.ui_test_store import UiTestStore
from .infrastructure.webbridge_client import WebBridgeClient, WebBridgeError
from .paths import analysis_data_dir, data_dir, repo_data_file
from .registry import (
    REGISTRY_FILE,
    get_repo,
    list_repos,
    register_repo,
    repository_members,
    unregister_member,
    unregister_repo,
)
from .store import EvolutionStore

app = FastAPI(title="CodeEvolution API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_stores: dict[str, EvolutionStore] = {}
_audit_store: AuditStore | None = None
_ui_test_store: UiTestStore | None = None
_business_rule_store: BusinessRuleStore | None = None
_node_rule_store: NodeRuleStore | None = None
_explanation_snapshot_store: ExplanationSnapshotStore | None = None
_snapshot_runtime: SnapshotRuntime | None = None
_request_dependencies: ContextVar[dict] = ContextVar("codeevolution_dependencies", default={})
_init_tasks: dict[str, dict] = {}
_init_lock = threading.Lock()
_explanation_generation_lock = threading.Lock()


class ChatRequest(BaseModel):
    repo: str = ""
    snapshot_id: str = Field(default="", max_length=200)
    question: str = Field(min_length=1, max_length=2000)


class UiTargetRequest(BaseModel):
    repo: str
    name: str = Field(min_length=1, max_length=100)
    base_url: str
    allowed_origins: list[str] = Field(default_factory=list)


class UiRecordingRequest(BaseModel):
    repo: str
    target_id: int
    name: str = Field(min_length=1, max_length=150)
    start_url: str


class UiCheckpointRequest(BaseModel):
    action: str
    target: dict = Field(default_factory=dict)
    payload: dict = Field(default_factory=dict)
    page_url: str = ""


class LLMConfigRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    api_base: str = Field(default="", max_length=1000)
    api_key: str | None = Field(default=None, max_length=4000)
    disable_thinking: bool | None = Field(default=None)
    context_window: int | None = Field(default=None, ge=1, le=1_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)


class BusinessRuleGenerateRequest(BaseModel):
    repository_snapshot_id: str = Field(default="", max_length=200)
    view_id: str = Field(default="", max_length=200)
    repo: str = Field(min_length=1, max_length=200)
    handler: str = Field(min_length=1, max_length=500)
    method: str = Field(min_length=1, max_length=10)
    path: str = Field(min_length=1, max_length=1000)
    call_chain_mermaid: str = Field(default="", max_length=10000)
    custom_prompt: str = Field(default="", max_length=5000)


class BusinessRulePromptRequest(BaseModel):
    custom_prompt: str = Field(min_length=1, max_length=5000)


class NodeRuleGenerateRequest(BaseModel):
    repository_snapshot_id: str = Field(default="", max_length=200)
    view_id: str = Field(default="", max_length=200)
    repo: str = Field(min_length=1, max_length=200)
    member: str = Field(default="", max_length=200)
    node_type: str = Field(default="func", pattern="^(func|cross)$")
    node_id: str = Field(default="", max_length=200)
    handler: str = Field(default="", max_length=500)
    custom_prompt: str = Field(default="", max_length=5000)


class ApiExplanationGenerateRequest(BaseModel):
    repository_snapshot_id: str = Field(default="", max_length=200)
    repo: str = Field(min_length=1, max_length=200)
    member: str = Field(default="", max_length=200)
    method: str = Field(min_length=1, max_length=16)
    path: str = Field(min_length=1, max_length=1000)
    handler: str = Field(min_length=1, max_length=500)
    file: str = Field(default="", max_length=2000)
    line: int | None = Field(default=None, ge=1)


class AnalysisRunCreateRequest(BaseModel):
    member_ids: list[str] = Field(min_length=1)
    external_context: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    idempotency_key: str | None = Field(default=None, max_length=200)


class AnalysisRunRetryRequest(BaseModel):
    member_ids: list[str] | None = None


class RepositoryMembersCheckRequest(BaseModel):
    member_ids: list[str] = Field(min_length=1)


class ScopeCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ScopeUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class RepositoryMemberCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    registered_path: str = Field(min_length=1, max_length=4000)


class RepositoryMemberUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    registered_path: str | None = Field(default=None, min_length=1, max_length=4000)


class SnapshotMetadataRequest(BaseModel):
    label: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=5000)
    pinned: bool = False
    metadata_version: int = Field(ge=1)


class CurrentGraphViewRequest(BaseModel):
    scope_ids: list[str] | None = None
    member_ids: list[str] | None = None


class ExplicitGraphViewMemberRequest(BaseModel):
    member_id: str
    snapshot_id: str | None = None
    availability: str = Field(pattern="^(available|unparsed|retired)$")


class ExplicitGraphViewRequest(BaseModel):
    members: list[ExplicitGraphViewMemberRequest] = Field(min_length=1)


class GraphViewPinRequest(BaseModel):
    label: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=5000)


class GraphArtifactJobCreateRequest(BaseModel):
    artifact_kind: str = Field(default="topology", min_length=1, max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)


def get_store(repo: str = "") -> EvolutionStore:
    dependencies = _request_dependencies.get()
    if factory := dependencies.get("store_factory"):
        return factory(repo)
    if injected := dependencies.get("store"):
        return injected
    if not repo:
        repos = list_repos()
        if repos:
            repo = repos[0]["name"]
        else:
            raise HTTPException(400, "No repos registered. Register a repo first.")

    if repo not in _stores:
        entry = get_repo(repo)
        if not entry:
            raise HTTPException(404, f"Repo '{repo}' not found")
        db_path = entry.get("db_path") or str(repo_data_file(entry["path"], "evolution.db"))
        _stores[repo] = EvolutionStore(db_path)

    return _stores[repo]


def get_audit_store() -> AuditStore:
    global _audit_store
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("audit_store"):
        return injected
    if _audit_store is None:
        _audit_store = AuditStore(str(codeevolution_data_dir() / "assistant-audit.db"))
    return _audit_store


def get_chat_service() -> ChatService:
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("chat_service"):
        return injected
    llm_client = dependencies.get("llm_client")
    if llm_client is None:
        from .semantic.client import OpenAILLMClient
        from .semantic.config import get_llm_config

        config = get_llm_config()
        llm_client = OpenAILLMClient(config) if config else None

    def resolve_members(repo: str):
        entry = get_repo(repo)
        if not entry:
            raise ValueError(f"Repo '{repo}' not found")
        return repository_members(entry)

    return ChatService(get_audit_store(), resolve_members, get_store, llm_client)


def get_snapshot_chat_service() -> SnapshotChatService:
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("snapshot_chat_service"):
        return injected
    return SnapshotChatService(get_audit_store(), get_snapshot_query_service(), dependencies.get("llm_client"))


def get_ui_recording_service() -> UiRecordingService:
    global _ui_test_store
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("ui_recording_service"):
        return injected
    if _ui_test_store is None:
        _ui_test_store = UiTestStore(str(codeevolution_data_dir() / "ui-tests.db"))
    bridge = dependencies.get("webbridge_client") or WebBridgeClient()
    return UiRecordingService(_ui_test_store, bridge)


def codeevolution_data_dir() -> Path:
    return data_dir()


def get_snapshot_runtime() -> SnapshotRuntime:
    global _snapshot_runtime
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("snapshot_runtime"):
        return injected
    if _snapshot_runtime is None:
        _snapshot_runtime = SnapshotRuntime(
            analysis_data_dir(),
            legacy_registry=REGISTRY_FILE,
            concurrency=int(os.environ.get("CODEEVOLUTION_ANALYSIS_CONCURRENCY", "2")),
        )
    return _snapshot_runtime


def get_catalog_service():
    dependencies = _request_dependencies.get()
    return dependencies.get("catalog_service") or get_snapshot_runtime().catalog


def get_analysis_run_service():
    dependencies = _request_dependencies.get()
    return dependencies.get("analysis_run_service") or get_snapshot_runtime().runs


def get_repository_snapshot_service():
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("repository_snapshot_service"):
        return injected
    from .application.repository_snapshot_service import RepositorySnapshotService

    return RepositorySnapshotService(get_snapshot_runtime().store)


def get_graph_view_service():
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("graph_view_service"):
        return injected
    from .application.repository_snapshot_service import GraphViewService

    return GraphViewService(get_snapshot_runtime().store)


def get_graph_artifact_service():
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("graph_artifact_service"):
        return injected
    runtime = get_snapshot_runtime()
    return runtime.graph_artifacts


def get_graph_artifact_scheduler():
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("graph_artifact_scheduler"):
        return injected
    if dependencies.get("graph_artifact_service") is not None:
        return None
    return get_snapshot_runtime().graph_artifact_scheduler


def get_snapshot_topology_service():
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("snapshot_topology_service"):
        return injected
    from .application.snapshot_topology_service import SnapshotTopologyService
    runtime = get_snapshot_runtime()
    return SnapshotTopologyService(runtime.store, get_snapshot_query_service())


def get_topology_query_service():
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("topology_query_service"):
        return injected
    from .application.topology_query_service import TopologyQueryService

    return TopologyQueryService()


def get_graph_artifact_store():
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("graph_artifact_store"):
        return injected
    if injected := dependencies.get("graph_artifact_service"):
        return injected.store
    return get_snapshot_runtime().store


def get_snapshot_query_service() -> SnapshotQueryService:
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("snapshot_query_service"):
        return injected
    return get_snapshot_runtime().snapshot_queries


def get_snapshot_retention_service():
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("snapshot_retention_service"):
        return injected
    from .application.snapshot_retention_service import SnapshotRetentionService
    runtime = get_snapshot_runtime()
    return SnapshotRetentionService(runtime.store, runtime.artifacts)


def _jsonable(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _resource(value) -> dict:
    return _jsonable(asdict(value))


def _run_resource(service, run) -> dict:
    attempts = {item.id: _resource(item) for item in service.attempts(run.id)}
    result = _resource(run)
    for member in result["members"]:
        attempt_id = member.get("attempt_id")
        member["attempt"] = attempts.get(attempt_id)
    result["counts"] = {
        "total": len(result["members"]),
        "queued": sum(item["disposition"] == "queued" for item in result["members"]),
        "already_running": sum(
            item["disposition"] == "already_running" for item in result["members"]
        ),
    }
    return result


def _notify_analysis_scheduler() -> None:
    dependencies = _request_dependencies.get()
    if notify := dependencies.get("analysis_scheduler_notify"):
        notify()
    elif "analysis_run_service" not in dependencies:
        get_snapshot_runtime().notify()


# --- Repository snapshot runtime ---


@app.get("/api/scopes")
def list_analysis_scopes():
    return {"scopes": [_resource(item) for item in get_catalog_service().list_scopes()]}


@app.post("/api/scopes", status_code=201)
def create_analysis_scope(request: ScopeCreateRequest):
    try:
        return {"scope": _resource(get_catalog_service().create_scope(request.name))}
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.patch("/api/scopes/{scope_id}")
def update_analysis_scope(scope_id: str, request: ScopeUpdateRequest):
    try:
        return {"scope": _resource(get_catalog_service().rename_scope(scope_id, request.name))}
    except KeyError as error:
        raise HTTPException(404, "scope not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.delete("/api/scopes/{scope_id}")
def retire_analysis_scope(scope_id: str):
    try:
        return {"scope": _resource(get_catalog_service().retire_scope(scope_id))}
    except KeyError as error:
        raise HTTPException(404, "scope not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/scopes/{scope_id}/members")
def list_analysis_scope_members(scope_id: str):
    try:
        members = get_catalog_service().list_members(scope_id)
    except KeyError as error:
        raise HTTPException(404, "scope not found") from error
    return {"members": [_resource(item) for item in members]}


@app.post("/api/scopes/{scope_id}/members", status_code=201)
def create_repository_member(scope_id: str, request: RepositoryMemberCreateRequest):
    try:
        member = get_catalog_service().add_member(
            scope_id, request.display_name, request.registered_path
        )
        return {"member": _resource(member)}
    except KeyError as error:
        raise HTTPException(404, "scope not found") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.patch("/api/repository-members/{member_id}")
def update_repository_member(member_id: str, request: RepositoryMemberUpdateRequest):
    try:
        member = get_catalog_service().update_member(
            member_id,
            display_name=request.display_name,
            registered_path=request.registered_path,
        )
        return {"member": _resource(member)}
    except KeyError as error:
        raise HTTPException(404, "repository member not found") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.post("/api/repository-members/check", include_in_schema=False)
def check_repository_members(request: RepositoryMembersCheckRequest):
    """Check selected workspaces without syncing or creating a Snapshot."""
    from .infrastructure.workspace_input_scanner import WorkspaceInputScanner

    store = get_snapshot_runtime().store
    results = []
    for member_id in request.member_ids:
        member = store.get_member(member_id)
        if member is None:
            raise HTTPException(404, f"repository member not found: {member_id}")
        current = store.get_current_snapshot(member_id)
        if current is None:
            results.append({"member_id": member_id, "status": "unparsed", "source_changed": False,
                            "version_drift": False})
            continue
        try:
            observed = WorkspaceInputScanner(member.registered_path).scan()
            evidence = store.get_evidence(current.evidence_digest)
            changed = evidence is None or observed.source_digest != evidence.source_digest
            status = "source_changed" if changed else "unchanged"
            results.append({"member_id": member_id, "status": status, "source_changed": changed,
                            "version_drift": False, "snapshot_id": current.id,
                            "input_digest": observed.source_digest})
        except Exception as error:
            results.append({"member_id": member_id, "status": "check_failed",
                            "source_changed": False, "version_drift": False,
                            "error_code": type(error).__name__, "error_message": str(error)[:500]})
    return {"items": results}


@app.delete("/api/repository-members/{member_id}")
def retire_repository_member(member_id: str):
    try:
        member = get_catalog_service().retire_member(member_id)
        return {"member": _resource(member), "deleted_data": False}
    except KeyError as error:
        raise HTTPException(404, "repository member not found") from error


@app.post("/api/analysis-runs", status_code=202)
def create_analysis_run(request: AnalysisRunCreateRequest, response: Response):
    service = get_analysis_run_service()
    try:
        run = service.create_run(
            request.member_ids,
            external_context=request.external_context,
            tags=request.tags,
            idempotency_key=request.idempotency_key,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    response.headers["Location"] = f"/api/analysis-runs/{run.id}"
    _notify_analysis_scheduler()
    return {"run": _run_resource(service, run)}


@app.get("/api/analysis-runs/{run_id}")
def get_analysis_run(run_id: str):
    service = get_analysis_run_service()
    try:
        return {"run": _run_resource(service, service.get_run(run_id))}
    except KeyError as error:
        raise HTTPException(404, "analysis run not found") from error


@app.post("/api/analysis-runs/{run_id}/retry", status_code=202)
def retry_analysis_run(run_id: str, request: AnalysisRunRetryRequest, response: Response):
    service = get_analysis_run_service()
    try:
        run = service.retry_run(run_id, request.member_ids)
    except KeyError as error:
        raise HTTPException(404, "analysis run not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    response.headers["Location"] = f"/api/analysis-runs/{run.id}"
    _notify_analysis_scheduler()
    return {"run": _run_resource(service, run)}


@app.post("/api/analysis-runs/{run_id}/cancel")
def cancel_analysis_run(run_id: str):
    service = get_analysis_run_service()
    try:
        return {"run": _run_resource(service, service.cancel_run(run_id))}
    except KeyError as error:
        raise HTTPException(404, "analysis run not found") from error


@app.post("/api/analysis-runs/{run_id}/members/{member_id}/cancel")
def cancel_analysis_run_member(run_id: str, member_id: str):
    service = get_analysis_run_service()
    try:
        return {"run": _run_resource(service, service.cancel_member(run_id, member_id))}
    except KeyError as error:
        raise HTTPException(404, "analysis run or member not found") from error


@app.get("/api/repository-members/{member_id}/snapshots")
def list_repository_snapshots(member_id: str, cursor: str = Query(""), limit: int = Query(50, ge=1, le=200)):
    try:
        items = get_repository_snapshot_service().list_snapshots(member_id)
    except KeyError as error:
        raise HTTPException(404, "repository member not found") from error
    try:
        offset = int(cursor) if cursor else 0
    except ValueError as error:
        raise HTTPException(400, "invalid cursor") from error
    page = items[offset:offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(items) else None
    return {"items": [_resource(item) for item in page], "next_cursor": next_cursor}


@app.get("/api/repository-snapshots/{snapshot_id}")
def get_repository_snapshot(snapshot_id: str, include: str = Query(default="")):
    try:
        item = _resource(get_repository_snapshot_service().get_snapshot(snapshot_id))
    except KeyError as error:
        raise HTTPException(404, "repository snapshot not found") from error
    except RuntimeError as error:
        raise HTTPException(410 if str(error) == "snapshot_gone" else 409, str(error)) from error
    if include != "facts":
        item.pop("facts", None)
    return {"snapshot": item}


@app.patch("/api/repository-snapshots/{snapshot_id}/metadata")
def update_repository_snapshot_metadata(snapshot_id: str, request: SnapshotMetadataRequest):
    try:
        item = get_repository_snapshot_service().update_metadata(
            snapshot_id,
            label=request.label,
            note=request.note,
            pinned=request.pinned,
            expected_version=request.metadata_version,
        )
    except KeyError as error:
        raise HTTPException(404, "repository snapshot not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"snapshot": _resource(item)}


@app.post("/api/repository-snapshots/{snapshot_id}/deletion-preview", include_in_schema=False)
def preview_repository_snapshot_deletion(snapshot_id: str):
    try:
        return get_snapshot_retention_service().preview(snapshot_id)
    except KeyError as error:
        raise HTTPException(404, "repository snapshot not found") from error


@app.delete("/api/repository-snapshots/{snapshot_id}", status_code=202, include_in_schema=False)
def delete_repository_snapshot(
    snapshot_id: str,
    response: Response,
    deletion_confirmation: str = Query(""),
    deletion_header: str = Header("", alias="X-Deletion-Confirmation"),
):
    try:
        result = get_snapshot_retention_service().delete(snapshot_id, deletion_header or deletion_confirmation)
        response.headers["Location"] = f"/api/repository-snapshots/{snapshot_id}/deletion"
        return {"job": result}
    except KeyError as error:
        raise HTTPException(404, "repository snapshot not found") from error
    except ValueError as error:
        status = 409 if str(error) in {"snapshot_protected", "invalid_deletion_confirmation"} else 400
        raise HTTPException(status, str(error)) from error


@app.post("/api/snapshot-retention/scavenge", include_in_schema=False)
def scavenge_snapshot_retention():
    return get_snapshot_retention_service().scavenge_orphans()


@app.post("/api/graph-views/current", status_code=201)
def create_current_graph_view(request: CurrentGraphViewRequest):
    try:
        view = get_graph_view_service().create_current(
            scope_ids=request.scope_ids, member_ids=request.member_ids
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {"view": _resource(view)}


@app.post("/api/graph-views", status_code=201)
def create_explicit_graph_view(request: ExplicitGraphViewRequest):
    from .domain.analysis_snapshot import GraphViewMember, ViewAvailability

    members = [
        GraphViewMember(
            member_id=item.member_id,
            ordinal=index,
            snapshot_id=item.snapshot_id,
            availability=ViewAvailability(item.availability),
        )
        for index, item in enumerate(request.members)
    ]
    try:
        view = get_graph_view_service().create_explicit(members)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {"view": _resource(view)}


@app.get("/api/graph-views/{view_id}")
def get_graph_view(view_id: str):
    from .infrastructure.analysis_snapshot_sqlite import ViewExpiredError

    try:
        return {"view": _resource(get_graph_view_service().get(view_id))}
    except ViewExpiredError as error:
        raise HTTPException(410, "graph view expired") from error
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error


@app.post("/api/graph-views/{view_id}/pin")
def pin_graph_view(view_id: str, request: GraphViewPinRequest):
    from .infrastructure.analysis_snapshot_sqlite import ViewExpiredError
    try:
        view = get_graph_view_service().pin(view_id, label=request.label, note=request.note)
    except ViewExpiredError as error:
        raise HTTPException(410, "graph view expired") from error
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error
    except ValueError as error:
        raise HTTPException(410, str(error)) from error
    return {"view": _resource(view)}


@app.post("/api/graph-views/{view_id}/refresh", status_code=201, include_in_schema=False)
def refresh_graph_view(view_id: str):
    from .infrastructure.analysis_snapshot_sqlite import ViewExpiredError
    try:
        return {"view": _resource(get_graph_view_service().refresh(view_id))}
    except ViewExpiredError as error:
        raise HTTPException(410, "graph view expired") from error
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error


@app.get("/api/graph-views/{view_id}/export", include_in_schema=False)
def export_graph_view(view_id: str):
    from .infrastructure.analysis_snapshot_sqlite import ViewExpiredError
    try:
        return get_graph_view_service().export(view_id)
    except ViewExpiredError as error:
        raise HTTPException(410, "graph view expired") from error
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/graph-views/{view_id}/artifacts/{artifact_kind}/generate", status_code=202, include_in_schema=False)
def generate_graph_artifact(view_id: str, artifact_kind: str):
    try:
        job = get_graph_artifact_service().generate(view_id, artifact_kind)
        return {"job": job}
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error


@app.post("/api/graph-views/{view_id}/artifact-jobs")
def create_graph_artifact_job(
    view_id: str, request: GraphArtifactJobCreateRequest, response: Response
):
    from .application.graph_artifact_service import GraphArtifactRequestError

    if request.artifact_kind != "topology" or request.params:
        raise HTTPException(422, "invalid_artifact_request")
    try:
        job = get_graph_artifact_service().create_job(
            view_id, request.artifact_kind, request.params
        )
    except KeyError as error:
        raise HTTPException(404, "view_not_found") from error
    except GraphArtifactRequestError as error:
        code = str(error)
        status = 409 if code in {"view_has_no_analyzable_members", "legacy_mixed_scope_view"} else 424 if code in {"snapshot_unavailable", "snapshot_artifact_corrupt"} else 422
        raise HTTPException(status, code) from error
    response.headers["Location"] = f"/api/graph-artifact-jobs/{job['id']}"
    scheduler = get_graph_artifact_scheduler()
    if scheduler is not None and hasattr(scheduler, "submit") and job.get("status") == "pending":
        scheduler.submit(job["id"])
    if job.get("status") == "completed":
        response.status_code = 200
    else:
        response.status_code = 202
        response.headers["Retry-After"] = "2"
    return {"job": job}


@app.get("/api/graph-views/{view_id}/artifacts/topology")
def get_topology_artifact(view_id: str):
    from .application.graph_artifact_service import GraphArtifactRequestError

    try:
        cached = get_graph_artifact_service().get(view_id, "topology", {})
    except KeyError as error:
        raise HTTPException(404, "view_not_found") from error
    except GraphArtifactRequestError as error:
        code = str(error)
        raise HTTPException(409 if code == "legacy_mixed_scope_view" else 424, code) from error
    if cached is None:
        raise HTTPException(404, "artifact_not_generated")
    payload_json = cached.get("payload_json")
    if not payload_json and cached.get("payload_storage") == "artifact":
        try:
            root = get_snapshot_runtime().artifacts.open(cached["artifact_key"])
            payload_json = (root / "payload.json").read_text(encoding="utf-8")
        except (OSError, KeyError, ValueError) as error:
            raise HTTPException(424, "snapshot_artifact_corrupt") from error
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError as error:
        raise HTTPException(424, "snapshot_artifact_corrupt") from error
    return {
        "view_id": view_id,
        "artifact_url": f"/api/graph-views/{view_id}/artifacts/topology",
        "payload_digest": cached.get("payload_digest"),
        "artifact": payload,
    }


@app.get("/api/graph-views/{view_id}/artifacts/{artifact_kind}", include_in_schema=False)
def get_graph_artifact(view_id: str, artifact_kind: str):
    try:
        job = get_graph_artifact_service().get(view_id, artifact_kind)
        if job is None or job.get("status") != "completed":
            return Response(
                status_code=202,
                content=json.dumps({"status": job.get("status", "pending") if job else "pending",
                                     "job": job}, ensure_ascii=False),
                media_type="application/json",
            )
        payload_json = job.get("payload_json")
        if not payload_json:
            cached = get_snapshot_runtime().store.get_artifact_cache(job["cache_key_digest"])
            if cached and cached.get("payload_storage") == "artifact":
                artifact = get_snapshot_runtime().artifacts.open(cached["artifact_key"])
                payload_json = (artifact / "payload.json").read_text(encoding="utf-8")
        payload = json.loads(payload_json) if payload_json else job
        return payload
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error


@app.get("/api/graph-artifact-jobs/{job_id}", include_in_schema=False)
def get_graph_artifact_job(job_id: str):
    job = get_snapshot_runtime().store.get_artifact_job(job_id)
    if job is None:
        raise HTTPException(404, "artifact job not found")
    return {"job": job}


@app.get("/api/graph-artifact-jobs/{job_id}")
def get_graph_artifact_job_contract(job_id: str):
    job = get_graph_artifact_store().get_artifact_job(job_id)
    if job is None:
        raise HTTPException(404, "job_not_found")
    return {"job": job}


@app.post("/api/graph-artifact-jobs/{job_id}/cancel", include_in_schema=False)
def cancel_graph_artifact_job(job_id: str):
    try:
        return {"job": get_snapshot_runtime().store.cancel_artifact_job(job_id)}
    except KeyError as error:
        raise HTTPException(404, "artifact job not found") from error


@app.post("/api/graph-artifact-jobs/{job_id}/retry", status_code=202, include_in_schema=False)
def retry_graph_artifact_job(job_id: str):
    try:
        return {"job": get_snapshot_runtime().store.retry_artifact_job(job_id)}
    except KeyError as error:
        raise HTTPException(404, "artifact job not found") from error


@app.get("/api/graph-views/{view_id}/impact")
def query_graph_impact(
    view_id: str,
    member_id: str = Query(...),
    direction: str = Query("both"),
    max_depth: int = Query(5, ge=0, le=20),
    channels: str = Query("http,message,grpc"),
    include_resources: bool = Query(True),
    include_shared_resource_risks: bool = Query(False),
    include_candidates: bool = Query(False),
):
    from .application.graph_artifact_service import GraphArtifactRequestError
    from .application.topology_query_service import TopologyQueryError

    try:
        payload = get_topology_artifact(view_id)["artifact"]
        return get_topology_query_service().impact(
            payload,
            member_id,
            direction=direction,
            max_depth=max_depth,
            channels=tuple(item for item in channels.split(",") if item),
            include_resources=include_resources,
            include_shared_resource_risks=include_shared_resource_risks,
            include_candidates=include_candidates,
            view_id=view_id,
        )
    except HTTPException:
        raise
    except (GraphArtifactRequestError, TopologyQueryError) as error:
        code = str(error)
        raise HTTPException(422, code) from error


@app.get("/api/graph-views/{view_id}/flow")
def query_graph_flow(
    view_id: str,
    member_id: str = Query(...),
    entry_id: str = Query(""),
    method: str = Query(""),
    path: str = Query(""),
    max_depth: int = Query(8, ge=0, le=20),
    max_nodes: int = Query(500, ge=1, le=5000),
    max_edges: int = Query(1000, ge=1, le=10000),
    channels: str = Query("http,message,grpc"),
    include_resources: bool = Query(False),
    include_candidates: bool = Query(False),
):
    from .application.topology_query_service import TopologyQueryError

    try:
        payload = get_topology_artifact(view_id)["artifact"]
        return get_topology_query_service().flow(
            payload,
            member_id,
            entry_id=entry_id or None,
            method=method or None,
            path=path or None,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            channels=tuple(item for item in channels.split(",") if item),
            include_resources=include_resources,
            include_candidates=include_candidates,
            view_id=view_id,
        )
    except HTTPException:
        raise
    except TopologyQueryError as error:
        raise HTTPException(422, str(error)) from error


@app.get("/api/topology", include_in_schema=False)
def snapshot_topology(view_id: str = Query(...)):
    try:
        return get_snapshot_topology_service().topology(view_id)
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error


@app.get("/api/impact", include_in_schema=False)
def snapshot_impact(view_id: str = Query(...), service: str = Query(...)):
    try:
        return get_snapshot_topology_service().impact(view_id, service)
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error


@app.get("/api/flow", include_in_schema=False)
def snapshot_flow(view_id: str = Query(...), service: str = Query(...), path: str = Query("")):
    try:
        return get_snapshot_topology_service().flow(view_id, service, path)
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error


@app.get("/api/entities", include_in_schema=False)
def snapshot_entities(view_id: str = Query(...)):
    try:
        return get_snapshot_topology_service().entities(view_id)
    except KeyError as error:
        raise HTTPException(404, "graph view not found") from error


def get_llm_config_store() -> LLMConfigStore:
    dependencies = _request_dependencies.get()
    return dependencies.get("llm_config_store") or LLMConfigStore(
        codeevolution_data_dir() / "llm-config.json"
    )


def get_business_rule_store() -> BusinessRuleStore:
    global _business_rule_store
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("business_rule_store"):
        return injected
    if _business_rule_store is None:
        _business_rule_store = BusinessRuleStore(
            str(codeevolution_data_dir() / "business-rules.db")
        )
    return _business_rule_store


def get_node_rule_store() -> NodeRuleStore:
    global _node_rule_store
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("node_rule_store"):
        return injected
    if _node_rule_store is None:
        _node_rule_store = NodeRuleStore(
            str(codeevolution_data_dir() / "node-rules.db")
        )
    return _node_rule_store


def get_explanation_snapshot_store() -> ExplanationSnapshotStore:
    global _explanation_snapshot_store
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("explanation_snapshot_store"):
        return injected
    if _explanation_snapshot_store is None:
        _explanation_snapshot_store = ExplanationSnapshotStore(
            codeevolution_data_dir() / "api-explanations.db"
        )
    return _explanation_snapshot_store


def get_knowledge_service(repo: str = "") -> tuple[KnowledgeService, bool]:
    """Return a knowledge service and whether the caller owns its lifecycle."""
    dependencies = _request_dependencies.get()
    if factory := dependencies.get("knowledge_service_factory"):
        return factory(repo), True
    if injected := dependencies.get("knowledge_service"):
        return injected, False

    raise HTTPException(400, "snapshot_id or view_id is required")

    if not repo:
        repos = list_repos()
        if not repos:
            raise HTTPException(400, "No repos registered. Register a repo first.")
        repo = repos[0]["name"]

    entry = get_repo(repo)
    if not entry:
        raise HTTPException(404, f"Repo '{repo}' not found")
    services = []
    missing = []
    for member in repository_members(entry):
        codegraph_db = Path(member["path"]) / ".codegraph" / "codegraph.db"
        if not codegraph_db.exists():
            missing.append(member.get("name") or Path(member["path"]).name)
            continue
        services.append(
            (
                member.get("name") or Path(member["path"]).name,
                KnowledgeService.from_codegraph(str(codegraph_db)),
                member["path"],
            )
        )
    if missing:
        for _, service, _path in services:
            service.close()
        raise HTTPException(
            409,
            f"CodeGraph database not found for: {', '.join(missing)}. Run codegraph init first.",
        )
    if len(services) == 1:
        return services[0][1], True
    return GroupedKnowledgeService(services), True


# --- Repo management ---


@app.get("/api/repos")
def api_list_repos():
    repos = list_repos()
    result = []
    for r in repos:
        entry = {
            "name": r["name"],
            "path": r["path"],
            "repositories": repository_members(r),
        }
        result.append(entry)
    return {"repos": result}


@app.post("/api/repos/register")
def api_register_repo(name: str = Query(...), path: str = Query(...)):
    try:
        entry = register_repo(name, path)
        return {"ok": True, "repo": entry}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/repos/{name}")
def api_unregister_repo(name: str):
    """Remove a logical service registration without deleting repository data."""
    if not get_repo(name):
        raise HTTPException(404, f"Repo '{name}' not found")
    if store := _stores.pop(name, None):
        store.close()
    unregister_repo(name)
    return {"ok": True, "name": name, "deleted_data": False}


@app.get("/api/repos/{name}/members")
def api_list_members(name: str):
    """List all physical repo members under a logical service."""
    entry = get_repo(name)
    if not entry:
        raise HTTPException(404, f"Repo '{name}' not found")
    return {"members": repository_members(entry)}


class AddMemberRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2000)


@app.post("/api/repos/{name}/members")
def api_add_member(name: str, request: AddMemberRequest):
    """Add a physical repo to an existing logical service."""
    entry = get_repo(name)
    if not entry:
        raise HTTPException(404, f"Repo '{name}' not found")
    try:
        updated = register_repo(name, request.path)
        return {"ok": True, "repo": updated}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/repos/{name}/members")
def api_remove_member(name: str, path: str = Query(..., min_length=1)):
    """Remove a physical repo from a logical service."""
    if not get_repo(name):
        raise HTTPException(404, f"Repo '{name}' not found")
    try:
        result = unregister_member(name, path)
        if result is None:
            # Last member removed → service deleted
            if store := _stores.pop(name, None):
                store.close()
            return {"ok": True, "name": name, "deleted_service": True}
        return {"ok": True, "repo": result}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/repos/{name}/init")
def api_init_repo(name: str):
    """Start one-click init: codegraph init + backfill for all member repos."""
    raise HTTPException(410, "legacy init removed; create an analysis run for repository members")
    entry = get_repo(name)
    if not entry:
        raise HTTPException(404, f"Repo '{name}' not found")

    with _init_lock:
        task = _init_tasks.get(name)
        if task and task.get("status") in ("pending", "running"):
            raise HTTPException(409, f"服务 '{name}' 正在初始化中，请等待完成")
        _init_tasks[name] = {
            "status": "pending",
            "progress": [],
            "started_at": time.time(),
            "service": name,
        }

    def _run_init():
        try:
            from .application.evolution_command_service import EvolutionCommandService
            from .config import Config

            members = repository_members(entry)
            if not members:
                _init_tasks[name]["status"] = "failed"
                _init_tasks[name]["error"] = "该服务下没有代码仓成员"
                return

            with _init_lock:
                _init_tasks[name]["status"] = "running"
                _init_tasks[name]["total"] = len(members)

            for i, member in enumerate(members):
                member_name = member.get("name") or Path(member["path"]).name
                member_path = str(member["path"])

                # Step 1: codegraph init
                step1 = {"member": member_name, "step": "codegraph_init", "status": "running"}
                with _init_lock:
                    _init_tasks[name]["progress"].append(step1)

                cg_db = Path(member_path) / ".codegraph" / "codegraph.db"
                if not cg_db.exists():
                    try:
                        result = subprocess.run(
                            ["codegraph.cmd" if os.name == "nt" else "codegraph", "init", member_path],
                            capture_output=True, text=True, timeout=300,
                        )
                        if result.returncode != 0:
                            with _init_lock:
                                step1["status"] = "failed"
                                step1["error"] = (result.stderr or result.stdout)[:500]
                            continue
                    except subprocess.TimeoutExpired:
                        with _init_lock:
                            step1["status"] = "failed"
                            step1["error"] = "codegraph init 超时"
                        continue
                    except FileNotFoundError:
                        with _init_lock:
                            _init_tasks[name]["status"] = "failed"
                            _init_tasks[name]["error"] = "未安装 codegraph CLI (npm i -g @colbymchenry/codegraph)"
                        return

                with _init_lock:
                    step1["status"] = "completed"

                # Step 2: backfill
                step2 = {"member": member_name, "step": "backfill", "status": "running"}
                with _init_lock:
                    _init_tasks[name]["progress"].append(step2)

                try:
                    config = Config(repo_path=str(member_path))
                    service = EvolutionCommandService.from_config(config)
                    try:
                        db_path = repo_data_file(member_path, "evolution.db")
                        if db_path.exists():
                            stats = service.update()
                        else:
                            stats = service.backfill()
                    finally:
                        service.close()
                    with _init_lock:
                        step2["status"] = "completed"
                        step2["stats"] = stats
                except Exception as exc:
                    with _init_lock:
                        step2["status"] = "failed"
                        step2["error"] = str(exc)[:500]

            # Determine overall status
            with _init_lock:
                any_failed = any(
                    s.get("status") == "failed"
                    for s in _init_tasks[name].get("progress", [])
                )
                _init_tasks[name]["status"] = "completed" if not any_failed else "partial"
                _init_tasks[name]["finished_at"] = time.time()

        except Exception as exc:
            with _init_lock:
                _init_tasks[name]["status"] = "failed"
                _init_tasks[name]["error"] = str(exc)[:500]

    threading.Thread(target=_run_init, daemon=True).start()
    return {"ok": True, "task": _init_tasks[name]}


@app.get("/api/repos/{name}/init/status")
def api_init_repo_status(name: str):
    """Poll the status of an init task."""
    raise HTTPException(410, "legacy init removed; query /api/analysis-runs/{run_id}")
    with _init_lock:
        task = _init_tasks.get(name)
    if not task:
        raise HTTPException(404, f"没有找到服务 '{name}' 的初始化任务")
    return task


# --- Scoped API routes ---


@app.get("/api/llm-status")
def llm_status():
    from .semantic.config import get_llm_config_status

    return get_llm_config_status()


@app.get("/api/llm-config")
def get_llm_settings():
    from .semantic.config import get_environment_llm_config

    environment = get_environment_llm_config()
    stored = get_llm_config_store().load()
    effective = environment or stored
    return {
        "available": effective is not None,
        "source": "environment" if environment else ("page" if stored else "none"),
        "model": effective.get("model", "") if effective else "",
        "api_base": effective.get("api_base", "") if effective else "",
        "disable_thinking": bool(effective and effective.get("disable_thinking", True)),
        "context_window": (effective or {}).get("context_window"),
        "max_output_tokens": (effective or {}).get("max_output_tokens"),
        "api_key_configured": bool(effective and effective.get("api_key")),
        "stored_configured": stored is not None,
        "environment_override": environment is not None,
    }


@app.put("/api/llm-config")
def save_llm_settings(request: LLMConfigRequest):
    store = get_llm_config_store()
    current = store.load() or {}
    api_key = (request.api_key or "").strip() or current.get("api_key", "")

    # 字段省略 => 保留现值；显式传 null => 清除（context_window / max_output_tokens）。
    provided = request.model_fields_set

    def keep_or_take(field: str, fallback):
        return getattr(request, field) if field in provided else fallback

    try:
        payload = {
                "model": request.model,
                "api_base": request.api_base,
                "api_key": api_key,
        }
        for field in ("disable_thinking", "context_window", "max_output_tokens"):
            if field in provided or field in current:
                payload[field] = keep_or_take(field, current.get(field))
        store.save(payload)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, "api_key_configured": True}


@app.delete("/api/llm-config")
def delete_llm_settings():
    return {"ok": True, "deleted": get_llm_config_store().delete()}


@app.post("/api/llm-config/test")
def test_llm_settings():
    from .semantic.client import OpenAILLMClient
    from .semantic.config import get_llm_config

    config = get_llm_config()
    if not config:
        raise HTTPException(409, "请先保存 LLM 配置")
    # Reasoning models spend tokens on reasoning_content before emitting
    # content; an 8-token budget is consumed entirely by reasoning, yielding
    # an empty content. 128 tokens gives the probe headroom to actually answer.
    content = OpenAILLMClient(config).complete("Reply with exactly: OK", 128, 0)
    if not content:
        raise HTTPException(502, "LLM 未返回内容，请检查模型与服务地址")
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("error"):
        raise HTTPException(502, f"连接失败：{parsed['error']}")
    return {"ok": True, "message": "连接成功", "model": config["model"]}


@app.get("/api/knowledge")
def get_knowledge_report(
    snapshot_id: str = Query("", min_length=0),
    view_id: str = Query("", min_length=0),
    section: str | None = Query(None),
    # Compatibility is deliberately limited to an explicitly injected test/
    # embedding service; production never falls back to a live repo.
    repo: str = Query(""),
    include_llm: bool = Query(False),
):
    """Project immutable knowledge facts; never access a registered checkout."""
    if not isinstance(snapshot_id, str):  # direct Python compatibility calls
        snapshot_id = ""
    injected = _request_dependencies.get().get("knowledge_service")
    if not snapshot_id and injected is not None:
        return injected.report(include_llm=include_llm)
    if not snapshot_id:
        if view_id:
            try:
                return get_snapshot_query_service().view_knowledge(view_id)
            except KeyError as error:
                raise HTTPException(404, "graph view not found") from error
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
        raise HTTPException(400, "snapshot_id is required")
    try:
        return get_snapshot_query_service().knowledge(snapshot_id, section=section)
    except KeyError as error:
        raise HTTPException(404, "repository snapshot not found") from error
    except RuntimeError as error:
        raise HTTPException(424, str(error)) from error


# ── Call-chain tree (lazy per-node expansion) ──


@app.get("/api/call-tree/children")
def call_tree_children(
    snapshot_id: str = Query(..., min_length=1),
    node_id: str = Query(..., min_length=1),
    view_id: str = Query("", min_length=0),
):
    """Expand one frozen graph node from the selected repository snapshot."""
    try:
        if view_id:
            get_snapshot_query_service().view_snapshot(view_id, snapshot_id)
        return get_snapshot_query_service().call_tree_children(snapshot_id, node_id)
    except KeyError as error:
        raise HTTPException(404, "repository snapshot not found") from error
    except RuntimeError as error:
        raise HTTPException(424, str(error)) from error


# ── Business Rules (LLM-generated API business explanations) ──

DEFAULT_BUSINESS_RULE_PROMPT = """You are a senior software architect explaining API business logic to product managers and developers.

Below is the call chain sequence diagram (Mermaid sequenceDiagram) for an API endpoint. Analyze it and explain:

1. **Business purpose**: What business function does this API serve? (1-2 sentences in English)
2. **Business flow**: Walk through the call chain step by step, explaining what each step does in business terms (not code).
3. **Key business rules**: What business constraints, validations, or decisions are embedded in this flow?
4. **Side effects**: What external systems, databases, or services are affected?

Flowchart:
```
{flowchart}
```

API endpoint: {method} {path}
Handler: {handler}

Output JSON:
{{
  "business_purpose_en": "English business purpose",
  "business_purpose_zh": "Chinese business purpose",
  "business_flow_en": ["Step 1: ...", "Step 2: ...", ...],
  "business_flow_zh": ["步骤1: ...", "步骤2: ...", ...],
  "business_rules": ["Rule 1: ...", "Rule 2: ...", ...],
  "side_effects": ["Effect 1: ...", "Effect 2: ...", ...]
}}

JSON:"""


@app.get("/api/business-rules")
def list_business_rules(repo: str = Query(""), repository_snapshot_id: str = Query("")):
    """List all saved business rules, optionally filtered by repo."""
    if repository_snapshot_id:
        try:
            runtime = get_snapshot_runtime()
            if runtime.store.get_snapshot(repository_snapshot_id) is None:
                raise HTTPException(404, "repository snapshot not found")
            return {"rules": runtime.store.list_current_rules(
                rule_kind="business", snapshot_id=repository_snapshot_id
            )}
        except HTTPException:
            raise
    store = get_business_rule_store()
    if repo:
        return {"rules": store.list_by_repo(repo)}
    repos = list_repos()
    all_rules: list[dict] = []
    for r in repos:
        all_rules.extend(store.list_by_repo(r["name"]))
    return {"rules": all_rules}


@app.post("/api/business-rules/generate")
def generate_business_rule(request: BusinessRuleGenerateRequest):
    """Generate or regenerate a business rule for an API endpoint via LLM."""
    from .semantic.client import OpenAILLMClient
    from .semantic.config import get_llm_config
    from .semantic.json_parser import parse_json

    config = get_llm_config()
    if not config:
        raise HTTPException(409, "请先在 LLM 设置中配置模型和 API Key")

    if request.repository_snapshot_id:
        try:
            if request.view_id:
                get_snapshot_query_service().view_snapshot(request.view_id, request.repository_snapshot_id)
            facts = get_snapshot_query_service().knowledge(request.repository_snapshot_id)
        except (KeyError, RuntimeError) as error:
            raise HTTPException(424, str(error)) from error
        endpoint = next((item for item in facts.get("api_contract", {}).get("endpoints", [])
                         if item.get("handler") == request.handler and item.get("method", "").upper() == request.method.upper()), None)
        if endpoint is None:
            raise HTTPException(404, "endpoint not found in snapshot facts")
        prompt = request.custom_prompt or DEFAULT_BUSINESS_RULE_PROMPT.format(
            flowchart="sequenceDiagram\n    participant API as " + request.handler,
            method=endpoint.get("method", request.method), path=endpoint.get("path", request.path), handler=request.handler,
        )
        snapshot_store = get_snapshot_runtime().store
        subject_key = f"{endpoint.get('method', request.method).upper()} {endpoint.get('path', request.path)} {request.handler}"
        candidate = snapshot_store.create_rule_candidate(
            rule_kind="business", snapshot_id=request.repository_snapshot_id, subject_key=subject_key,
            prompt=prompt, input_digest=hashlib.sha256((request.repository_snapshot_id + request.view_id + subject_key + prompt).encode()).hexdigest(), now=utc_now(), view_id=request.view_id or None,
        )
        try:
            content = OpenAILLMClient(config).complete(prompt, max_tokens=1200, temperature=0.3)
            if not content:
                raise RuntimeError("LLM returned empty response")
            parsed = parse_json(content)
            if parsed is not None and parsed.get("error"):
                raise RuntimeError(parsed["error"])
            result = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else content
            completed = snapshot_store.finish_rule_candidate(candidate["id"], status="completed", result=result, now=utc_now())
            return {"id": completed["id"], "status": "completed", "result": result, "prompt": prompt,
                    "repository_snapshot_id": request.repository_snapshot_id}
        except Exception as exc:
            snapshot_store.finish_rule_candidate(candidate["id"], status="failed", error=str(exc)[:1000], now=utc_now())
            raise HTTPException(502, f"LLM 生成失败：{exc}") from exc

    raise HTTPException(400, "repository_snapshot_id is required")

    store = get_business_rule_store()
    prompt = request.custom_prompt or DEFAULT_BUSINESS_RULE_PROMPT.format(
        flowchart=request.call_chain_mermaid or f"sequenceDiagram\n    participant N0 as {request.handler}\n    Note over N0: No downstream calls",
        method=request.method,
        path=request.path,
        handler=request.handler,
    )

    # Mark as running
    rule_id = store.upsert(
        repo_name=request.repo,
        handler=request.handler,
        method=request.method,
        path=request.path,
        custom_prompt=request.custom_prompt,
        status="running",
    )

    try:
        client = OpenAILLMClient(config)
        content = client.complete(prompt, max_tokens=1200, temperature=0.3)
        if not content:
            raise RuntimeError("LLM returned empty response")

        # parse_json 兼容 LLM 用 ```json 围栏包裹的返回；仅当解析出完整对象时才落结构化 JSON。
        parsed = parse_json(content)
        if parsed is not None and parsed.get("error"):
            raise RuntimeError(parsed["error"])

        result = (
            json.dumps(parsed, ensure_ascii=False)
            if isinstance(parsed, dict) and "business_purpose_en" in parsed
            else content
        )
        store.update_status(rule_id, status="completed", result=result)
        return {
            "id": rule_id,
            "status": "completed",
            "result": result,
            "prompt": prompt,
        }
    except Exception as exc:
        store.update_status(rule_id, status="failed", error=str(exc)[:1000])
        raise HTTPException(502, f"LLM 生成失败：{exc}")


@app.put("/api/business-rules/{rule_id}/prompt")
def update_business_rule_prompt(rule_id: int, request: BusinessRulePromptRequest):
    """Update the custom prompt for a business rule without regenerating."""
    store = get_business_rule_store()
    store.update_prompt(rule_id, request.custom_prompt)
    return {"ok": True, "id": rule_id}


# ── Per-node business rules (call-chain tree right rail) ──


def _default_repo(repo: str) -> str:
    """Resolve an empty repo to the first registered logical service."""
    if repo:
        return repo
    repos = list_repos()
    if not repos:
        raise HTTPException(400, "No repos registered. Register a repo first.")
    return repos[0]["name"]


def _node_rule_http_error(message: str) -> HTTPException:
    """Map a NodeRuleService resolution message to an HTTP status."""
    if "CodeGraph" in message or "缺少" in message:
        return HTTPException(409, message)
    return HTTPException(404, message)


def _resolve_node_rule(repo: str, member: str, node_type: str, node_id: str, handler: str) -> dict:
    """Resolve a node context + stored rule, raising on bad input."""
    repo = _default_repo(repo)
    if node_type == "func" and not node_id:
        raise HTTPException(400, "函数节点需要 node_id 参数")
    if node_type == "cross" and not handler:
        raise HTTPException(400, "跨服务节点需要 handler 参数")
    ctx, error = NodeRuleService().resolve(
        repo, member or None, node_type=node_type, node_id=node_id or None, handler=handler or None
    )
    if ctx is None:
        raise _node_rule_http_error(error or "节点不可解析")
    return ctx


@app.get("/api/call-tree/rule")
def call_tree_node_rule(
    snapshot_id: str = Query(..., min_length=1),
    node_id: str = Query(..., min_length=1),
    view_id: str = Query("", min_length=0),
):
    """Describe a node using only graph/source content frozen in its snapshot."""
    try:
        if view_id:
            get_snapshot_query_service().view_snapshot(view_id, snapshot_id)
        ctx = get_snapshot_query_service().node_rule_context(snapshot_id, node_id)
    except KeyError as error:
        raise HTTPException(404, "repository snapshot not found") from error
    except RuntimeError as error:
        raise HTTPException(424, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if ctx is None:
        raise HTTPException(404, "snapshot node not found")
    svc = NodeRuleService()
    rule = get_snapshot_runtime().store.get_current_rule(
        rule_kind="node", snapshot_id=snapshot_id, subject_key=ctx["node_key"], view_id=view_id or None
    )
    return {
        "node": {
            "type": ctx["node_type"],
            "repository_snapshot_id": snapshot_id,
            "member_id": ctx["member_id"],
            "node_id": ctx["node_id"],
            "qualified_name": ctx["qualified_name"],
            "name": ctx["name"],
            "file": ctx["file"],
            "line": ctx["line"],
        },
        "default_prompt": svc.default_prompt(ctx),
        "rule": rule,
    }


@app.post("/api/call-tree/rule/generate")
def generate_call_tree_node_rule(request: NodeRuleGenerateRequest):
    """Generate or regenerate the business-rule explanation for one call-chain node."""
    from .semantic.client import OpenAILLMClient
    from .semantic.config import get_llm_config
    from .semantic.json_parser import parse_json

    config = get_llm_config()
    if not config:
        raise HTTPException(409, "请先在 LLM 设置中配置模型和 API Key")

    if request.repository_snapshot_id:
        try:
            if request.view_id:
                get_snapshot_query_service().view_snapshot(request.view_id, request.repository_snapshot_id)
            ctx = get_snapshot_query_service().node_rule_context(
                request.repository_snapshot_id, request.node_id
            )
        except KeyError as error:
            raise HTTPException(404, "repository snapshot not found") from error
        if ctx is None:
            raise HTTPException(404, "snapshot node not found")
        prompt = request.custom_prompt or NodeRuleService().default_prompt(ctx)
        subject_key = ctx["node_key"]
        digest = hashlib.sha256((request.repository_snapshot_id + request.view_id + subject_key + prompt).encode()).hexdigest()
        snapshot_store = get_snapshot_runtime().store
        candidate = snapshot_store.create_rule_candidate(
            rule_kind="node", snapshot_id=request.repository_snapshot_id,
            subject_key=subject_key, prompt=prompt, input_digest=digest, now=utc_now(), view_id=request.view_id or None,
        )
        try:
            content = OpenAILLMClient(config).complete(prompt, max_tokens=1200, temperature=0.3)
            if not content:
                raise RuntimeError("LLM returned empty response")
            parsed = parse_json(content)
            if parsed is not None and parsed.get("error"):
                raise RuntimeError(parsed["error"])
            result = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else content
            completed = snapshot_store.finish_rule_candidate(
                candidate["id"], status="completed", result=result, now=utc_now()
            )
            return {"id": completed["id"], "status": "completed", "result": result,
                    "prompt": prompt, "repository_snapshot_id": request.repository_snapshot_id}
        except Exception as exc:
            snapshot_store.finish_rule_candidate(candidate["id"], status="failed", error=str(exc)[:1000], now=utc_now())
            raise HTTPException(502, f"LLM 生成失败：{exc}") from exc

    raise HTTPException(400, "repository_snapshot_id is required")

    ctx = _resolve_node_rule(request.repo, request.member, request.node_type, request.node_id, request.handler)
    svc = NodeRuleService()
    prompt = request.custom_prompt or svc.default_prompt(ctx)
    store = get_node_rule_store()

    rule_id = store.upsert(
        repo_name=ctx["service"],
        member=ctx["member"],
        node_type=ctx["node_type"],
        node_key=ctx["node_key"],
        custom_prompt=request.custom_prompt,
        status="running",
    )

    try:
        client = OpenAILLMClient(config)
        content = client.complete(prompt, max_tokens=1200, temperature=0.3)
        if not content:
            raise RuntimeError("LLM returned empty response")

        parsed = parse_json(content)
        if parsed is not None and parsed.get("error"):
            raise RuntimeError(parsed["error"])

        result = (
            json.dumps(parsed, ensure_ascii=False)
            if isinstance(parsed, dict) and "business_purpose_en" in parsed
            else content
        )
        store.update_status(rule_id, status="completed", result=result)
        return {"id": rule_id, "status": "completed", "result": result, "prompt": prompt}
    except Exception as exc:
        store.update_status(rule_id, status="failed", error=str(exc)[:1000])
        raise HTTPException(502, f"LLM 生成失败：{exc}")


def _snapshot_payload(snapshot, *, include_nodes: bool = False) -> dict:
    payload = asdict(snapshot)
    if include_nodes:
        payload["nodes"] = [
            asdict(node) for node in get_explanation_snapshot_store().list_nodes(snapshot.id)
        ]
    return payload


def _explanation_member(repo: str, member: str) -> str:
    if member:
        return member
    entry = get_repo(repo)
    if not entry:
        raise HTTPException(404, f"Repo '{repo}' not found")
    members = repository_members(entry)
    if len(members) != 1:
        raise HTTPException(400, "多成员仓库必须明确指定 member")
    item = members[0]
    return item.get("name") or Path(item["path"]).name


def _build_explanation_service():
    dependencies = _request_dependencies.get()
    if factory := dependencies.get("explanation_generation_service_factory"):
        return factory()
    from .application.explanation_generation_service import ExplanationGenerationService
    from .infrastructure.explanation_source import SnapshotExplanationSource
    from .semantic.client import OpenAILLMClient
    from .semantic.config import get_llm_config
    from .semantic.explanation_service import ExplanationSemanticService

    config = get_llm_config()
    if not config:
        raise HTTPException(409, "请先在 LLM 设置中配置模型和 API Key")
    return ExplanationGenerationService(
        get_explanation_snapshot_store(),
        SnapshotExplanationSource(get_snapshot_query_service()),
        ExplanationSemanticService(OpenAILLMClient(config)),
        config["model"],
    )


def _submit_explanation_generation(service, snapshot_id: str, frozen: dict) -> None:
    def run_and_finalize() -> None:
        try:
            service.generate(snapshot_id, frozen)
        finally:
            source_snapshot_id = frozen.get("_generation_spec", {}).get("repository_snapshot_id")
            if source_snapshot_id:
                central = get_snapshot_runtime().store
                generated = get_explanation_snapshot_store().get_snapshot(snapshot_id)
                if generated and generated.status in {"completed", "partial"}:
                    central.make_snapshot_reference_permanent(source_snapshot_id, "api_explanation", snapshot_id)
                else:
                    central.remove_snapshot_reference(source_snapshot_id, "api_explanation", snapshot_id)

    submit = _request_dependencies.get().get("background_submit")
    if submit:
        submit(run_and_finalize)
        return
    threading.Thread(
        target=run_and_finalize,
        name=f"api-explanation-{snapshot_id[:8]}",
        daemon=True,
    ).start()


@app.post("/api/api-explanations/generate", status_code=202)
def generate_api_explanation(request: ApiExplanationGenerateRequest):
    """Manually start one endpoint-scoped explanation candidate."""
    from .infrastructure.explanation_source import api_key

    store = get_explanation_snapshot_store()
    if request.repository_snapshot_id:
        try:
            with get_snapshot_query_service().open(request.repository_snapshot_id) as handle:
                member = handle.snapshot.member_id
        except KeyError as error:
            raise HTTPException(404, "repository snapshot not found") from error
        except RuntimeError as error:
            raise HTTPException(424, str(error)) from error
    elif _request_dependencies.get().get("explanation_generation_service_factory"):
        # Compatibility for an explicitly injected embedding/test generator.
        member = _explanation_member(request.repo, request.member)
    else:
        raise HTTPException(400, "repository_snapshot_id is required")
    key = api_key(request.method, request.path, request.handler)
    with _explanation_generation_lock:
        active = next(
            (
                item
                for item in store.list_snapshots(request.repo, member, key)
                if item.status in {"pending", "running", "validating"}
            ),
            None,
        )
        if active:
            raise HTTPException(409, f"该 API 已有生成任务：{active.id}")
        service = _build_explanation_service()
        try:
            spec = request.model_dump()
            spec["member"] = member
            # Never permit a caller-selected live repository identity to affect
            # frozen evidence resolution.
            if request.repository_snapshot_id:
                spec["repo"] = member
            snapshot_id, frozen = service.prepare(spec)
        except ValueError as error:
            message = str(error)
            status = 409 if "CodeGraph" in message else 404
            raise HTTPException(status, message) from error
        snapshot = store.get_snapshot(snapshot_id)
        if request.repository_snapshot_id:
            get_snapshot_runtime().store.add_snapshot_reference(
                request.repository_snapshot_id, "api_explanation", snapshot_id
            )
        _submit_explanation_generation(service, snapshot_id, frozen)
    return {"snapshot": _snapshot_payload(snapshot)}


@app.get("/api/api-explanations/current")
def current_api_explanation(
    repo: str = Query(""), member: str = Query(""), api_key: str = Query(...),
    repository_snapshot_id: str = Query(""),
):
    if repository_snapshot_id:
        try:
            with get_snapshot_query_service().open(repository_snapshot_id) as handle:
                repo = handle.snapshot.member_id
                member = handle.snapshot.member_id
        except (KeyError, RuntimeError) as error:
            raise HTTPException(404, "repository snapshot not found") from error
    elif not (_request_dependencies.get().get("explanation_source_loader")
               or _request_dependencies.get().get("explanation_generation_service_factory")):
        raise HTTPException(400, "repository_snapshot_id is required")
    else:
        member = _explanation_member(repo, member)
    explanation_store = get_explanation_snapshot_store()
    snapshot = (
        explanation_store.get_current_for_repository_snapshot(repository_snapshot_id, api_key)
        if repository_snapshot_id
        else explanation_store.get_current(repo, member, api_key)
    )
    if snapshot is None:
        return {"status": "missing", "snapshot": None}
    freshness = "unknown"
    try:
        dependencies = _request_dependencies.get()
        source = dependencies.get("explanation_source_loader")
        if source is None and repository_snapshot_id:
            from .infrastructure.explanation_source import SnapshotExplanationSource
            source = SnapshotExplanationSource(get_snapshot_query_service())
        if source is None:
            raise ValueError("snapshot source loader is unavailable")
        spec = {"repo": snapshot.repo_name, "member": snapshot.member_name,
                "method": snapshot.method, "path": snapshot.path, "handler": snapshot.handler}
        if repository_snapshot_id:
            spec["repository_snapshot_id"] = repository_snapshot_id
        current = source.load(spec)
        freshness = (
            "current"
            if current["source_digest"] == snapshot.source_digest
            and current["graph_digest"] == snapshot.graph_digest
            else "outdated"
        )
    except (OSError, ValueError, KeyError):
        pass
    return {
        "freshness": freshness,
        "snapshot": _snapshot_payload(snapshot, include_nodes=True),
    }


@app.get("/api/api-explanations/snapshots")
def list_api_explanation_snapshots(
    repo: str = Query(""), member: str = Query(""), api_key: str = Query(...),
    repository_snapshot_id: str = Query("")
):
    if repository_snapshot_id:
        if get_snapshot_runtime().store.get_snapshot(repository_snapshot_id) is None:
            raise HTTPException(404, "repository snapshot not found")
        snapshots = get_explanation_snapshot_store().list_snapshots_for_repository_snapshot(
            repository_snapshot_id, api_key
        )
    elif (_request_dependencies.get().get("explanation_source_loader")
          or _request_dependencies.get().get("explanation_generation_service_factory")):
        member = _explanation_member(repo, member)
        snapshots = get_explanation_snapshot_store().list_snapshots(repo, member, api_key)
    else:
        raise HTTPException(400, "repository_snapshot_id is required")
    return {"snapshots": [_snapshot_payload(item) for item in snapshots]}


@app.get("/api/api-explanations/snapshots/{snapshot_id}")
def get_api_explanation_snapshot(snapshot_id: str):
    store = get_explanation_snapshot_store()
    snapshot = store.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(404, "解释快照不存在")
    payload = _snapshot_payload(snapshot, include_nodes=True)
    payload["edges"] = [asdict(edge) for edge in store.list_edges(snapshot_id)]
    return {"snapshot": payload}


@app.get("/api/api-explanations/snapshots/{snapshot_id}/nodes/{node_key}")
def get_api_explanation_node(snapshot_id: str, node_key: str):
    store = get_explanation_snapshot_store()
    node = store.get_node(snapshot_id, node_key)
    if node is None:
        raise HTTPException(404, "节点解释不存在")
    return {
        "node": asdict(node),
        "chunks": [asdict(chunk) for chunk in store.list_chunks(snapshot_id, node_key)],
    }


@app.get("/api/api-explanations/snapshots/{snapshot_id}/nodes/{node_key}/chunks")
def get_api_explanation_node_chunks(snapshot_id: str, node_key: str):
    store = get_explanation_snapshot_store()
    if store.get_node(snapshot_id, node_key) is None:
        raise HTTPException(404, "节点解释不存在")
    return {
        "chunks": [asdict(chunk) for chunk in store.list_chunks(snapshot_id, node_key)]
    }


@app.post("/api/api-explanations/snapshots/{snapshot_id}/cancel")
def cancel_api_explanation_snapshot(snapshot_id: str):
    try:
        snapshot = get_explanation_snapshot_store().cancel(snapshot_id)
    except KeyError as error:
        raise HTTPException(404, "解释快照不存在") from error
    except SnapshotStateError as error:
        raise HTTPException(409, str(error)) from error
    return {"snapshot": _snapshot_payload(snapshot)}


@app.delete("/api/api-explanations/snapshots/{snapshot_id}")
def delete_api_explanation_snapshot(
    snapshot_id: str, confirm_current: bool = Query(False)
):
    try:
        get_explanation_snapshot_store().delete(
            snapshot_id, confirm_current=confirm_current
        )
    except KeyError as error:
        raise HTTPException(404, "解释快照不存在") from error
    except SnapshotStateError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True, "deleted": snapshot_id}


@app.post("/api/chat")
def ask_repository(request: ChatRequest):
    """Plan and execute constrained repository queries, recording every attempt."""
    question = request.question.strip()
    if not question:
        raise HTTPException(400, "Question must not be empty")
    if not request.snapshot_id and _request_dependencies.get().get("chat_service"):
        return get_chat_service().ask(request.repo, question)
    if not request.snapshot_id:
        raise HTTPException(400, "snapshot_id is required")
    try:
        return get_snapshot_chat_service().ask(request.snapshot_id, question)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/api/audit-logs")
def list_assistant_audit_logs(repo: str = Query(""), limit: int = Query(50, ge=1, le=200)):
    return {"logs": get_audit_store().list(repo, limit)}


@app.get("/api/metrics/snapshots", include_in_schema=False)
def snapshot_metrics():
    return get_snapshot_runtime().store.metrics()


@app.post("/api/ui-test-targets")
def create_ui_test_target(request: UiTargetRequest):
    try:
        return get_ui_recording_service().add_target(
            request.repo, request.name, request.base_url, request.allowed_origins
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/api/ui-test-targets")
def list_ui_test_targets(repo: str = Query(...)):
    return {"targets": get_ui_recording_service().store.list_targets(repo)}


@app.post("/api/ui-recordings/start")
def start_ui_recording(request: UiRecordingRequest):
    try:
        return get_ui_recording_service().start(
            request.repo, request.target_id, request.name, request.start_url
        )
    except (ValueError, WebBridgeError) as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/ui-recordings/{recording_id}/collect")
def collect_ui_recording(recording_id: int):
    try:
        return get_ui_recording_service().collect(recording_id)
    except (ValueError, WebBridgeError) as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/ui-recordings/{recording_id}/stop")
def stop_ui_recording(recording_id: int):
    try:
        return get_ui_recording_service().stop(recording_id)
    except (ValueError, WebBridgeError) as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/ui-recordings/{recording_id}/checkpoints")
def add_ui_recording_checkpoint(recording_id: int, request: UiCheckpointRequest):
    try:
        return get_ui_recording_service().add_checkpoint(
            recording_id, request.action, request.target, request.payload, request.page_url
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/api/ui-recordings")
def list_ui_recordings(repo: str = Query(...)):
    return {"recordings": get_ui_recording_service().store.list_recordings(repo)}


@app.post("/api/ui-recordings/{recording_id}/run")
def run_ui_recording(recording_id: int):
    try:
        return get_ui_recording_service().replay(recording_id)
    except (ValueError, WebBridgeError) as error:
        raise HTTPException(400, str(error)) from error


_route_app = app


@asynccontextmanager
async def _lifespan(application):
    global _audit_store, _ui_test_store, _business_rule_store, _node_rule_store
    global _explanation_snapshot_store
    runtime = application.state.dependencies.get("snapshot_runtime")
    if runtime is None and application.state.use_default_snapshot_runtime:
        runtime = get_snapshot_runtime()
    if runtime is not None:
        runtime.start()
    yield
    if runtime is not None:
        runtime.close()
    for store in list(_stores.values()):
        store.close()
    _stores.clear()
    if _audit_store is not None:
        _audit_store.close()
        _audit_store = None
    if _ui_test_store is not None:
        _ui_test_store.close()
        _ui_test_store = None
    if _business_rule_store is not None:
        _business_rule_store.close()
        _business_rule_store = None
    if _node_rule_store is not None:
        _node_rule_store.close()
        _node_rule_store = None
    if _explanation_snapshot_store is not None:
        _explanation_snapshot_store.close()
        _explanation_snapshot_store = None


def create_app(dependencies: dict | None = None) -> FastAPI:
    """Create an isolated delivery adapter with injectable dependencies."""
    created = FastAPI(title="CodeEvolution API", lifespan=_lifespan)
    created.state.dependencies = dependencies or {}
    created.state.use_default_snapshot_runtime = dependencies is None
    created.add_middleware(
        CORSMiddleware,
        allow_origins=created.state.dependencies.get("cors_origins", ["*"]),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @created.middleware("http")
    async def bind_dependencies(request, call_next):
        token = _request_dependencies.set(request.app.state.dependencies)
        try:
            return await call_next(request)
        finally:
            _request_dependencies.reset(token)

    for route in _route_app.router.routes:
        if getattr(route, "path", "").startswith("/api/"):
            created.router.routes.append(route)
    return created


app = create_app()


def serve(host: str = "0.0.0.0", port: int = 8765):
    """Start the web API server. Uses registry for multi-repo support."""
    import uvicorn

    web_dir = Path(__file__).parent.parent / "web" / "dist"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")
    else:

        @app.get("/")
        def root():
            return {"message": "CodeEvolution API running. Frontend not built."}

    uvicorn.run(app, host=host, port=port, log_level="info")
