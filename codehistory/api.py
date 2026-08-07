"""FastAPI backend for the CodeHistory web dashboard — multi-repo support."""

import json
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .application.chat_service import ChatService
from .application.evolution_service import EvolutionQueryService
from .application.knowledge_service import GroupedKnowledgeService, KnowledgeService
from .application.refactoring_service import RefactoringPlanningService
from .application.ui_recording_service import UiRecordingService
from .infrastructure.audit_store import AuditStore
from .infrastructure.business_rule_store import BusinessRuleStore
from .infrastructure.llm_config_store import LLMConfigStore
from .infrastructure.refactoring_techniques import RefactoringTechniqueCatalog
from .infrastructure.ui_test_store import UiTestStore
from .infrastructure.webbridge_client import WebBridgeClient, WebBridgeError
from .registry import get_repo, list_repos, register_repo, repository_members, unregister_member, unregister_repo
from .store import EvolutionStore

app = FastAPI(title="CodeHistory API")

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
_request_dependencies: ContextVar[dict] = ContextVar("codehistory_dependencies", default={})
_init_tasks: dict[str, dict] = {}
_init_lock = threading.Lock()


class ChatRequest(BaseModel):
    repo: str = ""
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


class RefactoringTechniqueRequest(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=500)
    checks: list[str] = Field(min_length=1, max_length=30)


class LLMConfigRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    api_base: str = Field(default="", max_length=1000)
    api_key: str | None = Field(default=None, max_length=4000)


class BusinessRuleGenerateRequest(BaseModel):
    repo: str = Field(min_length=1, max_length=200)
    handler: str = Field(min_length=1, max_length=500)
    method: str = Field(min_length=1, max_length=10)
    path: str = Field(min_length=1, max_length=1000)
    call_chain_mermaid: str = Field(default="", max_length=10000)
    custom_prompt: str = Field(default="", max_length=5000)


class BusinessRulePromptRequest(BaseModel):
    custom_prompt: str = Field(min_length=1, max_length=5000)


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
        db_path = entry.get("db_path") or str(Path(entry["path"]) / ".codehistory" / "evolution.db")
        _stores[repo] = EvolutionStore(db_path)

    return _stores[repo]


def get_evolution_service(repo: str = "") -> EvolutionQueryService:
    dependencies = _request_dependencies.get()
    if factory := dependencies.get("evolution_service_factory"):
        return factory(repo)
    if injected := dependencies.get("evolution_service"):
        return injected
    return EvolutionQueryService(get_store(repo))


def get_audit_store() -> AuditStore:
    global _audit_store
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("audit_store"):
        return injected
    if _audit_store is None:
        _audit_store = AuditStore(str(codehistory_data_dir() / "assistant-audit.db"))
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


def get_ui_recording_service() -> UiRecordingService:
    global _ui_test_store
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("ui_recording_service"):
        return injected
    if _ui_test_store is None:
        _ui_test_store = UiTestStore(str(codehistory_data_dir() / "ui-tests.db"))
    bridge = dependencies.get("webbridge_client") or WebBridgeClient()
    return UiRecordingService(_ui_test_store, bridge)


def codehistory_data_dir() -> Path:
    return Path(os.environ.get("CODEHISTORY_DATA_DIR", str(Path.home() / ".codehistory")))


def get_llm_config_store() -> LLMConfigStore:
    dependencies = _request_dependencies.get()
    return dependencies.get("llm_config_store") or LLMConfigStore(
        codehistory_data_dir() / "llm-config.json"
    )


def get_business_rule_store() -> BusinessRuleStore:
    global _business_rule_store
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("business_rule_store"):
        return injected
    if _business_rule_store is None:
        _business_rule_store = BusinessRuleStore(
            str(codehistory_data_dir() / "business-rules.db")
        )
    return _business_rule_store


def get_refactoring_member(repo: str, member: str = "") -> tuple[str, str]:
    """Resolve one physical repository inside a registered logical service."""
    if not repo:
        repos = list_repos()
        if not repos:
            raise HTTPException(400, "No repos registered. Register a repo first.")
        repo = repos[0]["name"]
    entry = get_repo(repo)
    if not entry:
        raise HTTPException(404, f"Repo '{repo}' not found")
    members = repository_members(entry)
    selected = next(
        (item for item in members if not member or (item.get("name") or Path(item["path"]).name) == member),
        None,
    )
    if selected is None:
        raise HTTPException(404, f"Repository member '{member}' not found in '{repo}'")
    return selected.get("name") or Path(selected["path"]).name, selected["path"]


def get_refactoring_technique_catalog(
    repo: str = "", member: str = ""
) -> RefactoringTechniqueCatalog:
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("refactoring_technique_catalog"):
        return injected
    _, repo_path = get_refactoring_member(repo, member)
    return RefactoringTechniqueCatalog(
        Path(repo_path) / ".codehistory" / "refactoring-techniques.json"
    )


def get_knowledge_service(repo: str = "") -> tuple[KnowledgeService, bool]:
    """Return a knowledge service and whether the caller owns its lifecycle."""
    dependencies = _request_dependencies.get()
    if factory := dependencies.get("knowledge_service_factory"):
        return factory(repo), True
    if injected := dependencies.get("knowledge_service"):
        return injected, False

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


def get_refactoring_services(
    repo: str = "", member: str = ""
) -> tuple[list[tuple[str, object]], bool]:
    """Resolve planning services for every repository in a logical service."""
    dependencies = _request_dependencies.get()
    if injected := dependencies.get("refactoring_planning_service"):
        return [(repo or "injected", injected)], False

    if not repo:
        repos = list_repos()
        if not repos:
            raise HTTPException(400, "No repos registered. Register a repo first.")
        repo = repos[0]["name"]
    entry = get_repo(repo)
    if not entry:
        raise HTTPException(404, f"Repo '{repo}' not found")

    services = []
    try:
        for member_entry in repository_members(entry):
            member_name = member_entry.get("name") or Path(member_entry["path"]).name
            if member and member_name != member:
                continue
            services.append(
                (member_name, RefactoringPlanningService.from_repository(member_entry["path"]))
            )
    except ValueError as error:
        for _, service in services:
            service.close()
        raise HTTPException(409, str(error)) from error
    if member and not services:
        raise HTTPException(404, f"Repository member '{member}' not found in '{repo}'")
    return services, True


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
        try:
            store = _stores.get(r["name"])
            if not store:
                db_path = r.get("db_path") or str(Path(r["path"]) / ".codehistory" / "evolution.db")
                if Path(db_path).exists():
                    store = EvolutionStore(db_path)
                    _stores[r["name"]] = store
            if store:
                entry["stats"] = store.get_stats()
            else:
                entry["stats"] = None
        except Exception:
            entry["stats"] = None
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
            from .config import Config
            from .application.evolution_command_service import EvolutionCommandService

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
                            ["codegraph", "init", member_path],
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
                        db_path = Path(member_path) / ".codehistory" / "evolution.db"
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
    with _init_lock:
        task = _init_tasks.get(name)
    if not task:
        raise HTTPException(404, f"没有找到服务 '{name}' 的初始化任务")
    return task


# --- Scoped API routes ---


@app.get("/api/stats")
def get_stats(repo: str = Query("")):
    return get_evolution_service(repo).stats()


@app.get("/api/features")
def list_features(
    repo: str = Query(""),
    status: str = Query("all"),
    search: str = Query(""),
    at_commit: str = Query(""),
    limit: int = Query(100),
    offset: int = Query(0),
):
    if at_commit:
        return get_evolution_service(repo).list_features_at_commit(
            at_commit, status, search, limit, offset
        )
    return get_evolution_service(repo).list_features(status, search, limit, offset)


@app.get("/api/commits")
def list_commits(repo: str = Query(""), limit: int = Query(200)):
    return get_evolution_service(repo).commits(limit)


@app.get("/api/features/{stable_id:path}/explain")
def explain_feature(stable_id: str, repo: str = Query("")):
    """Generate LLM explanation for a feature's call chain. Optional: requires OPENAI_API_KEY."""
    from .llm import explain_feature, is_available

    if not is_available():
        return {"available": False, "message": "Set OPENAI_API_KEY to enable AI explanations"}
    context = get_evolution_service(repo).explanation_context(stable_id)
    if not context:
        return {"error": "Feature not found"}
    feature = context["feature"]
    result = explain_feature(
        feature_name=feature["canonical_name"],
        description=feature.get("description", ""),
        description_zh=feature.get("description_zh", ""),
        call_chain=context["call_chain"],
        features_context=context["related_features"],
    )
    return {"available": True, "explanation": result}


@app.get("/api/features/{stable_id:path}")
def get_feature_detail(stable_id: str, repo: str = Query("")):
    feature = get_evolution_service(repo).feature_detail(stable_id)
    if not feature:
        return {"error": "Feature not found"}
    return feature


@app.get("/api/events")
def list_events(
    repo: str = Query(""),
    feature_stable_id: str = Query(""),
    event_type: str = Query(""),
    limit: int = Query(50),
    offset: int = Query(0),
):
    return get_evolution_service(repo).query_events(
        feature_stable_id=feature_stable_id, event_type=event_type, limit=limit, offset=offset
    )


@app.get("/api/capabilities")
def get_capabilities(repo: str = Query("")):
    return {"capabilities": get_evolution_service(repo).capabilities()}


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
        "api_key_configured": bool(effective and effective.get("api_key")),
        "stored_configured": stored is not None,
        "environment_override": environment is not None,
    }


@app.put("/api/llm-config")
def save_llm_settings(request: LLMConfigRequest):
    store = get_llm_config_store()
    current = store.load() or {}
    api_key = (request.api_key or "").strip() or current.get("api_key", "")
    try:
        store.save(
            {"model": request.model, "api_base": request.api_base, "api_key": api_key}
        )
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
    content = OpenAILLMClient(config).complete("Reply with exactly: OK", 8, 0)
    if not content:
        raise HTTPException(502, "LLM 未返回内容，请检查模型与服务地址")
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("error"):
        raise HTTPException(502, f"连接失败：{parsed['error']}")
    return {"ok": True, "message": "连接成功", "model": config["model"]}


@app.get("/api/event-stats")
def event_stats(repo: str = Query("")):
    return {"stats": get_evolution_service(repo).event_stats()}


@app.get("/api/knowledge")
def get_knowledge_report(
    repo: str = Query(""),
    include_llm: bool = Query(False),
):
    """Extract the current knowledge report directly from the repo's CodeGraph index."""
    service, owned = get_knowledge_service(repo)
    try:
        return service.report(include_llm=include_llm)
    finally:
        if owned:
            service.close()


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
def list_business_rules(repo: str = Query("")):
    """List all saved business rules, optionally filtered by repo."""
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

    config = get_llm_config()
    if not config:
        raise HTTPException(409, "请先在 LLM 设置中配置模型和 API Key")

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

        parsed = None
        if content:
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError):
                pass

        if isinstance(parsed, dict) and parsed.get("error"):
            raise RuntimeError(parsed["error"])

        result = content if (parsed is None or not isinstance(parsed, dict) or "business_purpose_en" not in parsed) else json.dumps(parsed, ensure_ascii=False)
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


@app.get("/api/refactor-techniques")
def list_refactoring_techniques(repo: str = Query(""), member: str = Query("")):
    dependencies = _request_dependencies.get()
    if dependencies.get("refactoring_technique_catalog"):
        selected_member = member or repo or "injected"
        members = [selected_member]
    else:
        selected_member, _ = get_refactoring_member(repo, member)
        entry = get_repo(repo) if repo else list_repos()[0]
        members = [item.get("name") or Path(item["path"]).name for item in repository_members(entry)]
    return {
        "repository_member": selected_member,
        "repository_members": members,
        "techniques": get_refactoring_technique_catalog(repo, selected_member).list(),
    }


@app.post("/api/refactor-techniques", status_code=201)
def create_refactoring_technique(
    request: RefactoringTechniqueRequest,
    repo: str = Query(""),
    member: str = Query(""),
):
    try:
        return get_refactoring_technique_catalog(repo, member).create(request.model_dump())
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.put("/api/refactor-techniques/{technique_id}")
def update_refactoring_technique(
    technique_id: str,
    request: RefactoringTechniqueRequest,
    repo: str = Query(""),
    member: str = Query(""),
):
    try:
        return get_refactoring_technique_catalog(repo, member).update(
            technique_id, request.model_dump()
        )
    except ValueError as error:
        status = 404 if "not found" in str(error) else 400
        raise HTTPException(status, str(error)) from error


@app.delete("/api/refactor-techniques/{technique_id}")
def delete_refactoring_technique(
    technique_id: str,
    repo: str = Query(""),
    member: str = Query(""),
):
    """Delete a custom technique, or remove a built-in technique override."""
    try:
        return get_refactoring_technique_catalog(repo, member).delete(technique_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@app.get("/api/refactor-plans")
def get_refactoring_plans(
    repo: str = Query(""),
    member: str = Query(""),
    technique: str = Query("extract-method"),
    window_days: int = Query(7, ge=1, le=3650),
    previous_window_days: int = Query(0, ge=0, le=3649),
    limit: int = Query(5, ge=1, le=50),
    min_tests: int = Query(1, ge=1, le=20),
):
    """Return Agent-ready refactoring scopes and their test-safety gates."""
    services, owned = get_refactoring_services(repo, member)
    plans = []
    try:
        for member_name, service in services:
            member_plans = service.plan(
                technique_id=technique,
                window_days=window_days,
                previous_window_days=previous_window_days,
                limit=limit,
                min_tests=min_tests,
            )
            for plan in member_plans:
                item = plan.to_dict()
                item["repository_member"] = member_name
                plans.append(item)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    finally:
        if owned:
            for _, service in services:
                service.close()

    ranked_plans = sorted(plans, key=lambda item: -item["hotspot"]["score"])
    return {
        "version": "1.0",
        "repository": repo,
        "plan_count": min(len(ranked_plans), limit),
        "plans": ranked_plans[:limit],
    }


@app.post("/api/chat")
def ask_repository(request: ChatRequest):
    """Plan and execute constrained repository queries, recording every attempt."""
    question = request.question.strip()
    if not question:
        raise HTTPException(400, "Question must not be empty")
    try:
        return get_chat_service().ask(request.repo, question)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/api/audit-logs")
def list_assistant_audit_logs(repo: str = Query(""), limit: int = Query(50, ge=1, le=200)):
    return {"logs": get_audit_store().list(repo, limit)}


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
    global _audit_store, _ui_test_store, _business_rule_store
    yield
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


def create_app(dependencies: dict | None = None) -> FastAPI:
    """Create an isolated delivery adapter with injectable dependencies."""
    created = FastAPI(title="CodeHistory API", lifespan=_lifespan)
    created.state.dependencies = dependencies or {}
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
            return {"message": "CodeHistory API running. Frontend not built."}

    uvicorn.run(app, host=host, port=port, log_level="info")
