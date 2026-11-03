from types import SimpleNamespace

from codeevolution.application.api_explanation_status_service import (
    ApiExplanationStatusService,
)


class StoreStub:
    def __init__(self, current):
        self.current = current

    def get_current_for_repository_snapshot(self, _snapshot_id, api_key):
        return self.current.get(api_key)

    def get_current(self, _repo, _member, api_key):
        return self.current.get(api_key)


class SourceStub:
    def __init__(self, digests):
        self.digests = digests

    def load_many(self, _snapshot_id, specs):
        return [self.digests.get(spec["handler"]) for spec in specs]


def _candidate(status="completed", repository_snapshot_id="old", source="source", graph="graph"):
    return SimpleNamespace(
        id=f"snapshot-{status}",
        status=status,
        repository_snapshot_id=repository_snapshot_id,
        source_revision="rev",
        created_at=1,
        source_digest=source,
        graph_digest=graph,
    )


def _endpoints():
    return [
        {"method": "GET", "path": "/pending", "handler": "pending"},
        {"method": "GET", "path": "/running", "handler": "running"},
        {"method": "GET", "path": "/same", "handler": "same"},
        {"method": "GET", "path": "/stale", "handler": "stale"},
        {"method": "GET", "path": "/fresh", "handler": "fresh"},
    ]


def test_status_projection_covers_pending_running_explained_and_stale():
    store = StoreStub({
        "GET|/running|running": _candidate(status="running"),
        "GET|/same|same": _candidate(repository_snapshot_id="current"),
        "GET|/stale|stale": _candidate(repository_snapshot_id="old"),
        "GET|/fresh|fresh": _candidate(repository_snapshot_id="old"),
    })
    source = SourceStub({
        "stale": {"source_digest": "changed", "graph_digest": "graph"},
        "fresh": {"source_digest": "source", "graph_digest": "graph"},
    })
    service = ApiExplanationStatusService(store)

    report = service.statuses(
        "current", _endpoints(), member_id="shop", source_loader=source
    )

    statuses = {item["handler"]: item["status"] for item in report["endpoints"]}
    assert statuses == {
        "pending": "pending",
        "running": "running",
        "same": "explained",
        "stale": "stale",
        "fresh": "explained",
    }
    assert report["summary"] == {"explained": 2, "stale": 1, "pending": 1, "running": 1}


def test_status_projection_falls_back_to_per_spec_loading():
    class PerSpecSource:
        def load(self, spec):
            return {"source_digest": "changed", "graph_digest": "graph"}

    store = StoreStub({"GET|/stale|stale": _candidate(repository_snapshot_id="old")})
    report = ApiExplanationStatusService(store).statuses(
        "current",
        [{"method": "GET", "path": "/stale", "handler": "stale"}],
        member_id="shop",
        source_loader=PerSpecSource(),
    )

    assert report["endpoints"][0]["status"] == "stale"
