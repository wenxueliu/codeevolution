"""FastAPI backend for the CodeHistory web dashboard — multi-repo support."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .application.evolution_service import EvolutionQueryService
from .registry import get_repo, list_repos, register_repo
from .store import EvolutionStore

app = FastAPI(title="CodeHistory API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_stores: dict[str, EvolutionStore] = {}
_request_dependencies: ContextVar[dict] = ContextVar("codehistory_dependencies", default={})


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


# --- Repo management ---


@app.get("/api/repos")
def api_list_repos():
    repos = list_repos()
    result = []
    for r in repos:
        entry = {"name": r["name"], "path": r["path"]}
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
    from .llm import is_available

    return {"available": is_available()}


@app.get("/api/event-stats")
def event_stats(repo: str = Query("")):
    return {"stats": get_evolution_service(repo).event_stats()}


_route_app = app


@asynccontextmanager
async def _lifespan(application):
    yield
    for store in list(_stores.values()):
        store.close()
    _stores.clear()


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
