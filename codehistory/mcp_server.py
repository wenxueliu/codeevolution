"""MCP server — exposes evolution analysis tools for AI agents."""

import json

from fastmcp import FastMCP

from .application.evolution_service import EvolutionQueryService
from .config import Config
from .store import EvolutionStore

mcp = FastMCP("codehistory")


def get_store() -> EvolutionStore | None:
    """Get the store for the configured repo."""
    # Store is injected by CLI when starting the server
    return getattr(mcp, "_store", None)


def get_config() -> Config | None:
    return getattr(mcp, "_config", None)


def set_context(store: EvolutionStore, config: Config):
    """Set the store and config for this MCP server instance."""
    mcp._store = store
    mcp._config = config


# --- Tools ---


@mcp.tool()
def get_feature_timeline(feature_name: str) -> str:
    """Get the full evolution timeline of a feature.

    Returns all evolution events (BORN, GROWN, SHRUNK, DIED, etc.)
    with commit info, ordered chronologically.

    Args:
        feature_name: Name or partial name of the feature to look up.
                      Matches against canonical_name and entry_signature.
    """
    store = get_store()
    if not store:
        return json.dumps({"error": "No store configured"})

    # Search by name (prefix match)
    features = EvolutionQueryService(store).list_features()["features"]
    matched = None
    for f in features:
        if feature_name.lower() in f["canonical_name"].lower():
            matched = f
            break
        if feature_name.lower() in f["entry_signature"].lower():
            matched = f
            break

    if not matched:
        return json.dumps(
            {
                "error": f"Feature not found: {feature_name}",
                "available_features": [f["canonical_name"] for f in features[:20]],
            }
        )

    timeline = store.get_feature_timeline(matched["stable_id"])
    return json.dumps(
        {
            "feature": {
                "stable_id": matched["stable_id"],
                "canonical_name": matched["canonical_name"],
                "entry_type": matched["entry_type"],
                "status": matched["status"],
            },
            "timeline": timeline,
        },
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def list_features() -> str:
    """List all tracked features with their current status.

    Returns each feature's name, type, status, and first/last seen info.
    """
    store = get_store()
    if not store:
        return json.dumps({"error": "No store configured"})

    features = store.get_all_features()
    return json.dumps(
        {
            "total": len(features),
            "features": [
                {
                    "stable_id": f["stable_id"],
                    "canonical_name": f["canonical_name"],
                    "entry_type": f["entry_type"],
                    "status": f["status"],
                }
                for f in features
            ],
        },
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def get_stats() -> str:
    """Get overall evolution statistics for the analyzed repository.

    Returns total commits, features, events, and active feature count.
    """
    store = get_store()
    if not store:
        return json.dumps({"error": "No store configured"})
    return json.dumps(store.get_stats(), indent=2)


@mcp.tool()
def search_feature_history(query: str) -> str:
    """Search for features whose evolution events match a keyword.

    Searches commit messages, event types, and feature names.

    Args:
        query: Keyword to search for (e.g., "login", "auth", "GROWN").
    """
    store = get_store()
    if not store:
        return json.dumps({"error": "No store configured"})

    # Search in features
    features = store.get_all_features()
    results = []
    for f in features:
        if (
            query.lower() in f["canonical_name"].lower()
            or query.lower() in f["entry_signature"].lower()
        ):
            timeline = store.get_feature_timeline(f["stable_id"])
            results.append(
                {
                    "stable_id": f["stable_id"],
                    "canonical_name": f["canonical_name"],
                    "entry_type": f["entry_type"],
                    "status": f["status"],
                    "event_count": len(timeline),
                }
            )

    return json.dumps(
        {
            "query": query,
            "results": results[:20],
            "total": len(results),
        },
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def get_feature_summary(feature_name: str) -> str:
    """Get a human-readable summary of a feature's evolution.

    Includes: birth, current status, growth trend, key events.

    Args:
        feature_name: Name or partial name of the feature.
    """
    store = get_store()
    if not store:
        return json.dumps({"error": "No store configured"})

    features = store.get_all_features()
    matched = None
    for f in features:
        if feature_name.lower() in f["canonical_name"].lower():
            matched = f
            break

    if not matched:
        return json.dumps({"error": f"Feature not found: {feature_name}"})

    timeline = store.get_feature_timeline(matched["stable_id"])

    # Summarize
    event_types = {}
    for ev in timeline:
        event_types[ev["event_type"]] = event_types.get(ev["event_type"], 0) + 1

    growth_events = sum(
        v for k, v in event_types.items() if k in ("GROWN", "EXTENDED", "DEP_CREATED")
    )
    shrink_events = sum(
        v for k, v in event_types.items() if k in ("SHRUNK", "CONTRACTED", "DEP_REMOVED")
    )

    trend = "stable"
    if growth_events > shrink_events:
        trend = "growing"
    elif shrink_events > growth_events:
        trend = "shrinking"

    first_event = timeline[0] if timeline else None
    last_event = timeline[-1] if timeline else None

    return json.dumps(
        {
            "feature": matched["canonical_name"],
            "stable_id": matched["stable_id"],
            "type": matched["entry_type"],
            "status": matched["status"],
            "trend": trend,
            "total_events": len(timeline),
            "event_breakdown": event_types,
            "first_event": first_event,
            "last_event": last_event,
        },
        indent=2,
        ensure_ascii=False,
    )


def run_server(store: EvolutionStore, config: Config, transport: str = "stdio"):
    """Start the MCP server."""
    set_context(store, config)
    mcp.run(transport=transport)
