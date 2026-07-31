"""FastAPI backend for the CodeHistory web dashboard — multi-repo support."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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


def get_store(repo: str = "") -> EvolutionStore:
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
    store = get_store(repo)
    return store.get_stats()


@app.get("/api/features")
def list_features(
    repo: str = Query(""),
    status: str = Query("all"),
    search: str = Query(""),
    at_commit: str = Query(""),
    limit: int = Query(100),
    offset: int = Query(0),
):
    store = get_store(repo)
    if at_commit:
        all_features = store.get_features_at_commit(at_commit)
    else:
        all_features = store.get_all_features()

    if status != "all":
        all_features = [f for f in all_features if f["status"] == status]
    if search:
        s = search.lower()
        all_features = [
            f
            for f in all_features
            if s in f["canonical_name"].lower() or s in f["entry_signature"].lower()
        ]

    total = len(all_features)
    features = all_features[offset : offset + limit]
    for f in features:
        timeline = store.get_feature_timeline(f["stable_id"])
        f["event_count"] = len(timeline)
        if not at_commit:
            snapshot = store.get_latest_snapshot(f["id"])
            if snapshot:
                f["call_chain"] = snapshot.get("call_chain", [])
                f["call_tree_nodes"] = snapshot.get("call_tree_nodes", 0)
            else:
                f["call_chain"] = []
                f["call_tree_nodes"] = 0

    return {"total": total, "features": features}


@app.get("/api/commits")
def list_commits(repo: str = Query(""), limit: int = Query(200)):
    store = get_store(repo)
    commits = store.get_commits(limit=limit)
    return {"total": len(commits), "commits": commits}


@app.get("/api/features/{stable_id:path}/explain")
def explain_feature(stable_id: str, repo: str = Query("")):
    """Generate LLM explanation for a feature's call chain. Optional: requires OPENAI_API_KEY."""
    from .llm import explain_feature, is_available

    if not is_available():
        return {"available": False, "message": "Set OPENAI_API_KEY to enable AI explanations"}
    store = get_store(repo)
    feature = store.get_feature(stable_id)
    if not feature:
        return {"error": "Feature not found"}
    snapshot = store.get_latest_snapshot(feature["id"])
    call_chain = snapshot.get("call_chain", []) if snapshot else []
    callee_names = set()
    for edge in call_chain:
        to_name = edge.get("to", "").replace("self.", "")
        if to_name:
            callee_names.add(to_name)
    features_context = []
    for f in store.get_all_features():
        if f["canonical_name"] in callee_names:
            features_context.append(f)
    result = explain_feature(
        feature_name=feature["canonical_name"],
        description=feature.get("description", ""),
        description_zh=feature.get("description_zh", ""),
        call_chain=call_chain,
        features_context=features_context,
    )
    return {"available": True, "explanation": result}


@app.get("/api/features/{stable_id:path}")
def get_feature_detail(stable_id: str, repo: str = Query("")):
    store = get_store(repo)
    feature = store.get_feature(stable_id)
    if not feature:
        return {"error": "Feature not found"}
    timeline = store.get_feature_timeline(stable_id)
    feature["timeline"] = timeline
    feature["event_count"] = len(timeline)
    snapshot = store.get_latest_snapshot(feature["id"])
    if snapshot:
        feature["call_chain"] = snapshot.get("call_chain", [])
        feature["call_tree_nodes"] = snapshot.get("call_tree_nodes", 0)
        feature["call_tree_depth"] = snapshot.get("call_tree_depth", 0)
    else:
        feature["call_chain"] = []
    return feature


@app.get("/api/events")
def list_events(
    repo: str = Query(""),
    feature_stable_id: str = Query(""),
    event_type: str = Query(""),
    limit: int = Query(50),
    offset: int = Query(0),
):
    store = get_store(repo)
    conn = store.conn
    conditions = []
    params = []
    if feature_stable_id:
        conditions.append("f.stable_id = ?")
        params.append(feature_stable_id)
    if event_type:
        conditions.append("e.event_type = ?")
        params.append(event_type)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    rows = conn.execute(
        f"""SELECT e.id, e.event_type, e.detail, e.feature_id, e.commit_id,
                   c.hash, c.timestamp, c.author, c.message,
                   f.canonical_name, f.stable_id
            FROM evolution_events e
            JOIN commits c ON e.commit_id = c.id
            JOIN features f ON e.feature_id = f.id
            {where}
            ORDER BY c.timestamp DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()

    count_result = conn.execute(
        f"""SELECT COUNT(*) FROM evolution_events e
            JOIN features f ON e.feature_id = f.id {where}""",
        params,
    ).fetchone()

    events = [
        {
            "id": r[0],
            "event_type": r[1],
            "detail": r[2],
            "feature_id": r[3],
            "commit_id": r[4],
            "commit_hash": r[5],
            "timestamp": r[6],
            "author": r[7],
            "message": r[8],
            "canonical_name": r[9],
            "stable_id": r[10],
        }
        for r in rows
    ]

    return {"total": count_result[0] if count_result else 0, "events": events}


@app.get("/api/capabilities")
def get_capabilities(repo: str = Query("")):
    store = get_store(repo)
    return {"capabilities": store.get_capabilities()}


@app.get("/api/llm-status")
def llm_status():
    from .llm import is_available

    return {"available": is_available()}


@app.get("/api/event-stats")
def event_stats(repo: str = Query("")):
    store = get_store(repo)
    rows = store.conn.execute(
        "SELECT event_type, COUNT(*) as cnt FROM evolution_events GROUP BY event_type ORDER BY cnt DESC"
    ).fetchall()
    return {"stats": [{"event_type": r[0], "count": r[1]} for r in rows]}


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
