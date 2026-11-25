"""API integration coverage for LLM-backed ADR generation.

The model is the only decision source, so the tests inject a deterministic
completer and assert the whole chain: source input -> rendered prompt ->
stored trace -> MADR draft -> export into the repository.
"""

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from codeevolution.adr.service import AdrService
from codeevolution.adr.store import AdrStore
from codeevolution.api import create_app
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore

DECISION = {
    "is_decision": True,
    "reason": "引入新的外部契约",
    "title": "引入支付网关适配器",
    "slug": "add-payment-gateway-adapter",
    "context": "支付渠道需要独立演进。",
    "drivers": ["可扩展", "隔离故障"],
    "options": [{"name": "适配器", "pros": ["解耦"], "cons": ["多一层抽象"]}],
    "chosen_option": "适配器",
    "outcome": "渠道差异被隔离在适配器内",
    "consequences": {"good": ["新增渠道成本低"], "bad": ["调试链路变长"]},
    "confirmation": "集成测试覆盖适配器契约",
    "confidence": 0.82,
    "evidence": [{"path": "app/payments.py", "line": 1, "note": "网关入口"}],
}


class SyncScheduler:
    """Run queued generations inline so the HTTP response is deterministic."""

    def __init__(self, service):
        self.service = service

    def start(self):
        pass

    def close(self):
        pass

    def submit(self, generation_id):
        self.service.run(generation_id)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=dev", *args],
        check=True,
        capture_output=True,
    )


def _setup(tmp_path, decision=None):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "payments.py").write_text("def pay():\n    pass\n", encoding="utf-8")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "app/payments.py", "README.md")
    _git(repo, "commit", "-m", "feat: add payment gateway")
    (repo / "README.md").write_text("hello world\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "docs: update readme")

    prompts: list[str] = []

    def completer(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(decision or DECISION, ensure_ascii=False)

    store = AdrStore(tmp_path / "data" / "adr.db")
    member = SimpleNamespace(
        id="member-1", display_name="shop", registered_path=str(repo)
    )
    service = AdrService(store, lambda mid: member if mid == member.id else None, completer)

    snapshot_store = SimpleNamespace(
        get_snapshot=lambda snapshot_id: SimpleNamespace(member_id="member-1")
    )
    runtime = SimpleNamespace(store=snapshot_store, start=lambda: None, close=lambda: None)
    app = create_app(
        {
            "adr_store": store,
            "adr_service": service,
            "adr_scheduler": SyncScheduler(service),
            "snapshot_runtime": runtime,
        }
    )
    return repo, store, service, prompts, TestClient(app)


def _generate(client, source):
    response = client.post(
        "/api/adr/generations", json={"member_id": "member-1", "source": source}
    )
    assert response.status_code == 202, response.text
    generation_id = response.json()["generation"]["id"]
    return client.get(f"/api/adr/generations/{generation_id}").json()


def test_text_generation_persists_draft_and_debug_trace(tmp_path):
    _repo, _store, _service, prompts, client = _setup(tmp_path)

    with client:
        detail = _generate(
            client, {"kind": "text", "text": "我们决定用适配器隔离支付渠道"}
        )

    trace = detail["generation"]
    assert trace["status"] == "succeeded"
    assert trace["source_kind"] == "text"
    assert trace["source_payload"]["text"]["text"] == "我们决定用适配器隔离支付渠道"
    assert trace["prompt_digest"].startswith("sha256:")
    assert trace["prompt_text"]
    assert "我们决定用适配器隔离支付渠道" in trace["prompt_rendered"]
    assert "shop" in trace["prompt_rendered"]
    assert trace["raw_response"]
    assert trace["parsed"]["title"] == DECISION["title"]
    assert trace["duration_ms"] >= 0
    assert len(prompts) == 1
    assert "我们决定用适配器隔离支付渠道" in prompts[0]

    draft = detail["draft"]
    assert draft["status"] == "proposed"
    assert draft["markdown"].startswith("---\nstatus: proposed")
    assert "# 引入支付网关适配器" in draft["markdown"]
    assert "source: text:" in draft["markdown"]


def test_commit_generation_feeds_diff_and_skips_noise(tmp_path):
    _repo, _store, _service, prompts, client = _setup(tmp_path)

    with client:
        significant = _generate(client, {"kind": "commit", "ref": "HEAD~1"})["generation"]
        assert significant["status"] == "succeeded"
        assert "app/payments.py" in prompts[0]
        assert "feat: add payment gateway" in prompts[0]
        assert "shop" in prompts[0]

        noisy = _generate(client, {"kind": "commit", "ref": "HEAD"})["generation"]
        assert noisy["status"] == "skipped"
        assert "docs/tests/lockfiles" in noisy["skip_reason"]
        assert len(prompts) == 1  # the noise commit never reaches the model

        forced = client.post(
            "/api/adr/generations",
            json={
                "member_id": "member-1",
                "source": {"kind": "commit", "ref": "HEAD"},
                "force": True,
            },
        ).json()["generation"]
        assert forced["status"] == "queued"
        forced_detail = client.get(f"/api/adr/generations/{forced['id']}").json()
        assert forced_detail["generation"]["status"] == "succeeded"


def test_prompts_are_versioned_per_service_and_source_kind(tmp_path):
    _repo, _store, _service, prompts, client = _setup(tmp_path)

    with client:
        custom = "CUSTOM {{service}} PROMPT\n{{text}}\n{{schema}}"
        saved = client.put(
            "/api/adr/prompts",
            json={"member_id": "member-1", "source_kind": "text", "prompt_text": custom},
        )
        assert saved.status_code == 200
        assert saved.json()["prompt"]["version"] == 1

        detail = _generate(client, {"kind": "text", "text": "决策内容"})
        assert detail["generation"]["prompt_version"] == 1
        assert "CUSTOM shop PROMPT" in prompts[-1]
        assert "决策内容" in prompts[-1]

        listing = client.get("/api/adr/prompts", params={"member_id": "member-1"}).json()
        by_kind = {item["source_kind"]: item for item in listing["kinds"]}
        assert by_kind["text"]["prompt"]["version"] == 1
        assert by_kind["commit"]["prompt"] is None  # untouched kinds keep defaults
        assert by_kind["text"]["prompt"]["prompt_text"] == custom

        reset = client.delete(
            "/api/adr/prompts",
            params={"member_id": "member-1", "source_kind": "text"},
        )
        assert reset.status_code == 200
        assert reset.json()["kinds"][2]["prompt"] is None


def test_file_generation_supports_repo_path_and_uploaded_content(tmp_path):
    _repo, _store, _service, prompts, client = _setup(tmp_path)

    with client:
        from_repo = _generate(client, {"kind": "file", "path": "app/payments.py"})["generation"]
        assert from_repo["status"] == "succeeded"
        assert "def pay():" in prompts[-1]
        assert "app/payments.py" in prompts[-1]

        uploaded = _generate(
            client,
            {"kind": "file", "filename": "design.md", "content": "# 设计\n使用事件总线。"},
        )["generation"]
        assert uploaded["status"] == "succeeded"
        assert "使用事件总线。" in prompts[-1]


def test_history_batch_groups_commits_and_reports_progress(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        response = client.post(
            "/api/adr/history",
            json={"member_id": "member-1", "limit": 10},
        )
        assert response.status_code == 202
        batch = response.json()
        assert batch["batch_id"]
        progress = client.get(f"/api/adr/batches/{batch['batch_id']}").json()
        assert progress["counts"].get("succeeded", 0) >= 1
        assert progress["counts"].get("skipped", 0) >= 1
        assert all(item["batch_id"] == batch["batch_id"] for item in progress["generations"])


def test_export_writes_madr_file_and_refreshes_index(tmp_path):
    repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        draft = _generate(client, {"kind": "text", "text": "导出测试"})["draft"]
        exported = client.post(f"/api/adr/drafts/{draft['id']}/export").json()

        target = Path(exported["path"])
        assert target.is_file()
        assert target.parent == repo / "docs" / "adr"
        assert target.name == "0001-add-payment-gateway-adapter.md"
        content = target.read_text(encoding="utf-8")
        assert "status: proposed" in content
        index = Path(exported["index_path"]).read_text(encoding="utf-8")
        assert "* [ADR-0001](0001-add-payment-gateway-adapter.md)" in index

        again = client.post(f"/api/adr/drafts/{draft['id']}/export").json()
        assert Path(again["path"]).name == "0001-add-payment-gateway-adapter.md"
        assert client.get(f"/api/adr/drafts/{draft['id']}").json()["draft"][
            "exported_path"
        ].endswith("0001-add-payment-gateway-adapter.md")


def test_source_mapping_links_sources_prompts_and_latest_draft(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        _generate(client, {"kind": "text", "text": "映射测试"})
        mapping = client.get("/api/adr/mapping", params={"member_id": "member-1"}).json()

    rows = {row["source_kind"]: row for row in mapping["sources"]}
    assert set(rows) == {"commit", "file", "text"}
    assert rows["text"]["generation_count"] == 1
    assert rows["text"]["latest_draft"]["title"] == DECISION["title"]
    assert rows["text"]["prompt_origin"] == "default"
    assert rows["commit"]["latest_generation"] is None


def test_snapshot_id_resolves_the_service_for_prompt_edits(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        response = client.get(
            "/api/adr/prompts", params={"snapshot_id": "snapshot-1"}
        )
    assert response.status_code == 200
    assert response.json()["member_id"] == "member-1"


def test_cli_lists_prompts_and_exports_a_draft(tmp_path):
    data_root = tmp_path / "data"
    store = AnalysisSnapshotSQLiteStore(data_root / "analysis-snapshots.db")
    scope = store.create_scope("shop")
    member = store.create_member(scope.id, "shop", str(tmp_path), str(tmp_path), member_id="member-1")
    # AnalysisSnapshotSQLiteStore opens short-lived connections.

    adr_store = AdrStore(data_root / "adr.db")
    adr_store.create_draft(
        generation_id="gen-1",
        member_id=member.id,
        source_kind="text",
        source_ref="cli",
        title="CLI 导出决策",
        slug="cli-export-decision",
        markdown="# CLI 导出决策\n\n## Context and Problem Statement\n\nx\n",
        status="proposed",
        confidence=0.5,
    )
    draft_id = adr_store.list_drafts(member.id)[0]["id"]
    adr_store.close()

    env = {**os.environ, "CODEEVOLUTION_DATA_DIR": str(data_root)}
    prompts = subprocess.run(
        ["python", "-m", "codeevolution.cli", "adr", "prompts", "list", "--member-id", "member-1"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert prompts.returncode == 0, prompts.stderr
    assert "commit" in prompts.stdout and "default" in prompts.stdout

    listing = subprocess.run(
        ["python", "-m", "codeevolution.cli", "adr", "list", "--member-id", "member-1"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert listing.returncode == 0, listing.stderr
    assert "CLI 导出决策" in listing.stdout

    exported = subprocess.run(
        ["python", "-m", "codeevolution.cli", "adr", "export", draft_id, "--json"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert exported.returncode == 0, exported.stderr
    payload = json.loads(exported.stdout)
    assert Path(payload["path"]).is_file()
    assert Path(payload["path"]).parent == tmp_path / "docs" / "adr"
