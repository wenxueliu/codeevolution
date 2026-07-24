"""FastAPI backend for the CodeHistory web dashboard."""

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .store import EvolutionStore
from .config import Config

app = FastAPI(title="CodeHistory API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_store: EvolutionStore | None = None


def init_store(db_path: str):
    global _store
    _store = EvolutionStore(db_path)


def get_store() -> EvolutionStore:
    if _store is None:
        raise RuntimeError("Store not initialized")
    return _store


# --- API Routes ---

@app.get("/api/stats")
def get_stats():
    store = get_store()
    return store.get_stats()


@app.get("/api/features")
def list_features(
    status: str = Query("all"),
    search: str = Query(""),
    at_commit: str = Query(""),
    limit: int = Query(100),
    offset: int = Query(0),
):
    store = get_store()

    if at_commit:
        all_features = store.get_features_at_commit(at_commit)
    else:
        all_features = store.get_all_features()

    if status != "all":
        all_features = [f for f in all_features if f["status"] == status]
    if search:
        s = search.lower()
        all_features = [
            f for f in all_features
            if s in f["canonical_name"].lower() or s in f["entry_signature"].lower()
        ]

    total = len(all_features)
    features = all_features[offset : offset + limit]
    for f in features:
        timeline = store.get_feature_timeline(f["stable_id"])
        f["event_count"] = len(timeline)

    return {"total": total, "features": features}


@app.get("/api/commits")
def list_commits(limit: int = Query(200)):
    store = get_store()
    commits = store.get_commits(limit=limit)
    return {"total": len(commits), "commits": commits}


@app.get("/api/features/{stable_id:path}")
def get_feature_detail(stable_id: str):
    store = get_store()
    feature = store.get_feature(stable_id)
    if not feature:
        return {"error": "Feature not found"}

    timeline = store.get_feature_timeline(stable_id)
    feature["timeline"] = timeline
    feature["event_count"] = len(timeline)
    return feature


@app.get("/api/events")
def list_events(
    feature_stable_id: str = Query(""),
    event_type: str = Query(""),
    limit: int = Query(50),
    offset: int = Query(0),
):
    store = get_store()
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

    # Count total
    count_result = conn.execute(
        f"""SELECT COUNT(*)
            FROM evolution_events e
            JOIN features f ON e.feature_id = f.id
            {where}""",
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


@app.get("/api/event-stats")
def event_stats():
    store = get_store()
    rows = store.conn.execute(
        "SELECT event_type, COUNT(*) as cnt FROM evolution_events GROUP BY event_type ORDER BY cnt DESC"
    ).fetchall()
    return {"stats": [{"event_type": r[0], "count": r[1]} for r in rows]}


def serve(repo_path: str, host: str = "0.0.0.0", port: int = 8765):
    """Start the web API server and serve the Vue frontend."""
    import uvicorn

    config = Config(repo_path=repo_path)
    init_store(config.db_path)

    # Serve Vue build if exists
    web_dir = Path(__file__).parent.parent / "web" / "dist"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")
    else:
        @app.get("/")
        def root():
            return {"message": "CodeHistory API running. Frontend not built. Run: cd web && npm run build"}

    uvicorn.run(app, host=host, port=port, log_level="info")
