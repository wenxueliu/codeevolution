"""Tests for page-to-API attribution (frontend page → HTTP endpoint)."""

from types import SimpleNamespace

from codeevolution.analysis.knowledge.page_api_map import (
    PAGE_API_MAP_VERSION,
    extract_page_api_calls,
)
from codeevolution.application.snapshot_query_service import SnapshotQueryService


FILES = {
    "web/src/api/order.js": (
        "export function fetchOrders() { return http({ url: '/api/orders', method: 'GET' }) }\n"
    ),
    "web/src/api/admin.js": (
        "export function clearCache() { return request({ url: '/api/cache', method: 'DELETE' }) }\n"
    ),
    "web/src/composables/useOrders.js": (
        "import { fetchOrders } from '@/api/order'\n"
        "export function useOrders() { return fetchOrders() }\n"
    ),
    "web/src/views/order/OrderList.vue": (
        "<template><div/></template>\n"
        "<script setup>\n"
        "import { useOrders } from '@/composables/useOrders'\n"
        "import { clearCache } from '../../api/admin.js'\n"
        "</script>\n"
    ),
    "web/src/views/Dashboard.vue": (
        "<template><div/></template>\n"
        "<script setup>\n"
        "import { missing } from '@/utils/nope'\n"
        "</script>\n"
    ),
    "web/src/views/Plain.vue": "<template><div/></template>\n",
}

PAGES = [
    {"name": "OrderManagement", "route": "/orders", "title": "订单管理", "file": "web/src/views/order/OrderList.vue"},
    {"name": "Dashboard", "route": "/dashboard", "title": "", "file": "web/src/views/Dashboard.vue"},
    {"name": "Plain", "route": "/plain", "title": "", "file": "web/src/views/Plain.vue"},
]

ENDPOINTS = [
    {"method": "GET", "path": "/api/orders"},
    {"method": "POST", "path": "/api/other"},
]


class _Source:
    def list_files(self, categories=None, globs=None):
        return [{"path": path} for path in FILES]

    def read_text(self, path):
        return FILES[path]


class _FakeGraph:
    def __init__(self, edges=None, error=False):
        self._edges = edges or []
        self._error = error

    def module_import_edges(self):
        if self._error:
            raise RuntimeError("no graph")
        return self._edges


def _service(facts, graph=None):
    class Handle:
        def __init__(self):
            self.snapshot = SimpleNamespace(facts=facts)
            self.sources = _Source()
            self.graph = graph if graph is not None else _FakeGraph()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

    service = SnapshotQueryService.__new__(SnapshotQueryService)
    service.open = lambda _snapshot_id: Handle()
    return service


def test_extractor_attributes_calls_via_import_reachability():
    result = extract_page_api_calls(_Source(), PAGES, ENDPOINTS)

    assert result["version"] == PAGE_API_MAP_VERSION
    by_page = {page["name"]: page for page in result["pages"]}
    # @/ alias reaches the api wrapper through the composable call site
    order_calls = {(item["function"], item["call_site_file"]) for item in by_page["OrderManagement"]["endpoints"]}
    assert ("fetchOrders", "web/src/composables/useOrders.js") in order_calls
    fetch = next(item for item in by_page["OrderManagement"]["endpoints"] if item["function"] == "fetchOrders")
    assert fetch["method"] == "GET" and fetch["path"] == "/api/orders"
    assert fetch["endpoint_matched"] is True
    # relative import attributed via the definition-file fallback
    clear = next(item for item in by_page["OrderManagement"]["endpoints"] if item["function"] == "clearCache")
    assert clear["definition_file"] == "web/src/api/admin.js"
    assert clear["endpoint_matched"] is False
    # unresolved alias import and import-free pages degrade gracefully
    assert result["unresolved_imports"] >= 1
    assert {page["name"] for page in result["pages_without_calls"]} == {"Dashboard", "Plain"}
    assert result["unmapped_calls"] == [] and result["orphan_call_count"] == 0


def test_extractor_uses_codegraph_import_edges_as_supplement():
    edges = [("web/src/views/Plain.vue", "web/src/api/admin.js")]

    result = extract_page_api_calls(_Source(), PAGES, ENDPOINTS, graph_import_edges=edges)

    by_page = {page["name"]: page for page in result["pages"]}
    assert [item["function"] for item in by_page["Plain"]["endpoints"]] == ["clearCache"]


def test_extractor_returns_empty_result_without_frontend_sources():
    class _Empty:
        def list_files(self, categories=None, globs=None):
            return []

        def read_text(self, path):
            return ""

    assert extract_page_api_calls(_Empty(), PAGES, ENDPOINTS)["page_count"] == 0


def test_knowledge_projects_page_api_map_at_read_boundary():
    facts = {
        "frontend_pages": {"page_count": len(PAGES), "pages": PAGES},
        "api_contract": {"endpoint_count": len(ENDPOINTS), "endpoints": ENDPOINTS},
    }

    full = _service(facts).knowledge("snapshot-1")
    mapped = full["page_api_map"]
    assert mapped["version"] == PAGE_API_MAP_VERSION
    assert {page["name"] for page in mapped["pages"]} == {"OrderManagement"}

    section = _service(facts).knowledge("snapshot-1", section="pages")
    assert section["page_api_map"]["page_count"] == 1


def test_knowledge_respects_persisted_page_api_map_version():
    persisted = {"version": PAGE_API_MAP_VERSION, "pages": [], "marker": True}
    facts = {
        "frontend_pages": {"page_count": len(PAGES), "pages": PAGES},
        "page_api_map": persisted,
    }

    section = _service(facts).knowledge("snapshot-1", section="pages")

    assert section["page_api_map"] is persisted


def test_knowledge_survives_broken_graph_for_page_api_map():
    facts = {
        "frontend_pages": {"page_count": len(PAGES), "pages": PAGES},
        "api_contract": {"endpoint_count": len(ENDPOINTS), "endpoints": ENDPOINTS},
    }

    full = _service(facts, graph=_FakeGraph(error=True)).knowledge("snapshot-1")

    assert {page["name"] for page in full["page_api_map"]["pages"]} == {"OrderManagement"}
