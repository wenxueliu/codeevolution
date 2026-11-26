"""API integration coverage for LLM-backed ADR generation.

The model is the only decision source, so the tests inject a deterministic
completer and assert the whole chain: source input -> rendered prompt ->
stored trace -> MADR draft -> export into the repository.
"""

import json
import os
import re
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
        if "代码仓变更分诊器" in prompt:
            shas = re.findall(r'"sha":\s*"([0-9a-f]+)"', prompt)
            return json.dumps(
                [
                    {
                        "sha": sha,
                        "candidate": True,
                        "reason": "涉及支付网关契约",
                        "change_type": "architecture",
                    }
                    for sha in shas
                ],
                ensure_ascii=False,
            )
        if "ADR 回测器" in prompt:
            return json.dumps(
                {
                    "verdict": "partial",
                    "confidence": 0.66,
                    "reasons": ["适配器接口仍存在", "但缺少部分渠道实现"],
                    "evidence": [{"path": "app/payments.py", "line": 1, "note": "入口仍在"}],
                    "recommendation": "补全渠道实现后重新回测",
                },
                ensure_ascii=False,
            )
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
        denied = client.post(f"/api/adr/drafts/{draft['id']}/export")
        assert denied.status_code == 409
        client.post(
            f"/api/adr/drafts/{draft['id']}/review",
            json={"action": "approve", "rating": 4, "comment": "ok"},
        )
        exported = client.post(f"/api/adr/drafts/{draft['id']}/export").json()

        target = Path(exported["path"])
        assert target.is_file()
        assert target.parent == repo / "docs" / "adr"
        assert target.name == "0001-add-payment-gateway-adapter.md"
        content = target.read_text(encoding="utf-8")
        assert "status: accepted" in content
        index = Path(exported["index_path"]).read_text(encoding="utf-8")
        assert "* [ADR-0001](0001-add-payment-gateway-adapter.md)" in index

        again = client.post(f"/api/adr/drafts/{draft['id']}/export").json()
        assert Path(again["path"]).name == "0001-add-payment-gateway-adapter.md"
        assert client.get(f"/api/adr/drafts/{draft['id']}").json()["draft"][
            "exported_path"
        ].endswith("0001-add-payment-gateway-adapter.md")


def test_triage_and_backtest_prompts_are_editable(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        for kind, text in (
            ("triage", "TRIAGE {{commits}}"),
            ("backtest", "BACKTEST {{adr}} {{code}}"),
        ):
            saved = client.put(
                "/api/adr/prompts",
                json={"member_id": "member-1", "source_kind": kind, "prompt_text": text},
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["prompt"]["source_kind"] == kind

        listing = client.get("/api/adr/prompts", params={"member_id": "member-1"}).json()
        by_kind = {item["source_kind"]: item for item in listing["kinds"]}
        assert set(by_kind) == {"commit", "file", "text", "triage", "backtest"}
        assert by_kind["triage"]["prompt"]["prompt_text"] == "TRIAGE {{commits}}"
        assert by_kind["backtest"]["prompt"]["version"] == 1

        reset = client.delete(
            "/api/adr/prompts",
            params={"member_id": "member-1", "source_kind": "backtest"},
        )
        assert reset.status_code == 200
        assert reset.json()["kinds"][4]["prompt"] is None


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


def test_commit_table_triages_noise_and_caches_results(tmp_path):
    _repo, _store, _service, prompts, client = _setup(tmp_path)

    with client:
        listing = client.get("/api/adr/commits", params={"member_id": "member-1"}).json()
        assert listing["total"] == 2
        by_subject = {item["subject"]: item for item in listing["items"]}
        docs = by_subject["docs: update readme"]
        feature = by_subject["feat: add payment gateway"]
        assert docs["candidate"] is False and docs["scanned"] is False
        assert feature["rule_candidate"] is True
        assert docs["stage"] == "skipped"
        assert feature["stage"] == "pending"
        assert listing["counts"]["pending"] == 1
        assert listing["counts"]["skipped"] == 1
        assert listing["progress"]["total"] == 2
        assert listing["progress"]["processed"] == 1
        assert listing["progress"]["percent"] == 50

        pending_only = client.get(
            "/api/adr/commits", params={"member_id": "member-1", "stage": "pending"}
        ).json()
        assert [item["subject"] for item in pending_only["items"]] == ["feat: add payment gateway"]
        assert pending_only["counts"]["skipped"] == 1  # counts stay global

        scanned = client.post("/api/adr/commits/scan", json={"member_id": "member-1"}).json()
        assert scanned["scanned"] == 2
        by_subject = {item["subject"]: item for item in scanned["items"]}
        assert by_subject["feat: add payment gateway"]["candidate"] is True
        assert by_subject["feat: add payment gateway"]["change_type"] == "architecture"
        assert by_subject["docs: update readme"]["candidate"] is False

        calls = len(prompts)
        again = client.post("/api/adr/commits/scan", json={"member_id": "member-1"}).json()
        assert again["scanned"] == 2
        assert len(prompts) == calls  # cached triage is not re-sent to the model


def test_commit_stage_progress_follows_generation_and_review(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    def stage_of(sha):
        result = client.get("/api/adr/commits", params={"member_id": "member-1"}).json()
        return next(item for item in result["items"] if item["sha"] == sha)

    with client:
        scanned = client.post("/api/adr/commits/scan", json={"member_id": "member-1"}).json()
        feature_sha = next(
            item["sha"] for item in scanned["items"] if item["subject"] == "feat: add payment gateway"
        )

        detail = _generate(client, {"kind": "commit", "ref": feature_sha})
        item = stage_of(feature_sha)
        assert item["stage"] == "generated"
        assert item["generation_status"] == "succeeded"

        client.post(f"/api/adr/drafts/{detail['draft']['id']}/backtest")
        assert stage_of(feature_sha)["stage"] == "backtested"

        client.post(
            f"/api/adr/drafts/{detail['draft']['id']}/review",
            json={"action": "approve"},
        )
        assert stage_of(feature_sha)["stage"] == "approved"

        client.post(f"/api/adr/drafts/{detail['draft']['id']}/export")
        assert stage_of(feature_sha)["stage"] == "exported"

        listing = client.get("/api/adr/commits", params={"member_id": "member-1"}).json()
        assert listing["counts"]["exported"] == 1
        assert listing["counts"]["skipped"] == 1
        assert listing["progress"]["processed"] == 2
        assert listing["progress"]["percent"] == 100
        assert listing["progress"]["active"] == 0


def test_batch_generation_from_selected_commits(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        scanned = client.post("/api/adr/commits/scan", json={"member_id": "member-1"}).json()
        shas = {item["subject"]: item["sha"] for item in scanned["items"]}
        response = client.post(
            "/api/adr/generations/batch",
            json={
                "member_id": "member-1",
                "commits": [shas["feat: add payment gateway"], shas["docs: update readme"]],
            },
        )
        assert response.status_code == 202
        batch = response.json()
        assert batch["total"] == 2
        assert batch["progress"]["total"] == 2
        assert batch["progress"]["skipped"] == 1
        assert batch["progress"]["queued"] == 1
        assert batch["progress"]["percent"] == 50

        progress = client.get(f"/api/adr/batches/{batch['batch_id']}").json()
        statuses = {item["source_ref"]: item["status"] for item in progress["generations"]}
        assert statuses[shas["feat: add payment gateway"]] == "succeeded"
        assert statuses[shas["docs: update readme"]] == "skipped"
        # Progress tracks how many of the selected commits finished executing.
        assert progress["progress"] == {
            "total": 2,
            "completed": 1,
            "failed": 0,
            "skipped": 1,
            "running": 0,
            "queued": 0,
            "percent": 100,
        }


def test_output_template_is_customizable_per_service(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        default = client.get("/api/adr/template", params={"member_id": "member-1"}).json()
        assert "{{title}}" in default["default_template"]
        assert default["template"] is None

        custom = "# {{title}}\n\nSERVICE={{service}}\n\n{{context}}\n"
        saved = client.put(
            "/api/adr/template",
            json={"member_id": "member-1", "template_text": custom},
        )
        assert saved.status_code == 200
        assert saved.json()["template"]["version"] == 1

        detail = _generate(client, {"kind": "text", "text": "模板测试"})
        markdown = detail["draft"]["markdown"]
        assert markdown.startswith("# 引入支付网关适配器")
        assert "SERVICE=shop" in markdown

        reset = client.delete("/api/adr/template", params={"member_id": "member-1"})
        assert reset.status_code == 200
        assert reset.json()["reset"] is True
        assert reset.json()["default_template"] == default["default_template"]

        invalid = client.put(
            "/api/adr/template",
            json={"member_id": "member-1", "template_text": "no title placeholder"},
        )
        assert invalid.status_code == 400


def test_backtest_reports_confidence_and_reasons(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        draft = _generate(client, {"kind": "text", "text": "回测测试"})["draft"]
        result = client.post(f"/api/adr/drafts/{draft['id']}/backtest").json()["backtest"]
        assert result["status"] == "ready"
        assert result["verdict"] == "partial"
        assert result["confidence"] == 0.66
        assert result["reasons"]
        assert result["prompt_rendered"]

        detail = client.get(f"/api/adr/drafts/{draft['id']}").json()
        assert detail["backtest"]["verdict"] == "partial"
        listing = client.get("/api/adr/drafts", params={"member_id": "member-1"}).json()
        assert listing["drafts"][0]["backtest"]["confidence"] == 0.66


def test_review_records_rating_and_gates_export(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        draft = _generate(client, {"kind": "text", "text": "审批测试"})["draft"]
        blocked = client.post(f"/api/adr/drafts/{draft['id']}/export")
        assert blocked.status_code == 409

        reviewed = client.post(
            f"/api/adr/drafts/{draft['id']}/review",
            json={"action": "approve", "rating": 4, "comment": "同意"},
        ).json()
        updated = reviewed["draft"]
        assert updated["status"] == "accepted"
        assert updated["review_status"] == "approved"
        assert updated["rating"] == 4
        assert "status: accepted" in updated["markdown"]

        exported = client.post(f"/api/adr/drafts/{draft['id']}/export")
        assert exported.status_code == 200

        rejected_draft = _generate(client, {"kind": "text", "text": "拒绝测试"})["draft"]
        rejected = client.post(
            f"/api/adr/drafts/{rejected_draft['id']}/review",
            json={"action": "reject", "comment": "证据不足"},
        ).json()["draft"]
        assert rejected["status"] == "rejected"
        assert rejected["review_status"] == "rejected"


def test_cli_lists_prompts_and_exports_a_draft(tmp_path):
    data_root = tmp_path / "data"
    store = AnalysisSnapshotSQLiteStore(data_root / "analysis-snapshots.db")
    scope = store.create_scope("shop")
    member = store.create_member(scope.id, "shop", str(tmp_path), str(tmp_path), member_id="member-1")
    # AnalysisSnapshotSQLiteStore opens short-lived connections.

    adr_store = AdrStore(data_root / "adr.db")
    draft = adr_store.create_draft(
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
    adr_store.update_draft(
        draft["id"],
        status="accepted",
        review_status="approved",
        markdown="---\nstatus: accepted\n---\n\n# CLI 导出决策\n",
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


def test_edit_draft_creates_revision_and_resets_review(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        draft = _generate(client, {"kind": "text", "text": "精修测试"})["draft"]
        client.post(
            f"/api/adr/drafts/{draft['id']}/review",
            json={"action": "approve", "rating": 5, "comment": "初版可用", "reviewer": "alice"},
        )
        client.post(f"/api/adr/drafts/{draft['id']}/backtest")
        detail = client.get(f"/api/adr/drafts/{draft['id']}").json()
        assert detail["draft"]["review_status"] == "approved"
        assert detail["backtest"]["status"] == "ready"

        edited_text = detail["draft"]["markdown"].replace(
            "# 引入支付网关适配器", "# 引入支付网关适配器（精修）"
        )
        response = client.put(
            f"/api/adr/drafts/{draft['id']}",
            json={"markdown": edited_text, "editor": "bob", "note": "精简措辞"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        updated = payload["draft"]
        assert updated["revision"] == 2
        assert updated["status"] == "proposed"
        assert updated["review_status"] == "pending"
        assert updated["rating"] is None
        assert updated["editor"] == "bob"
        assert updated["reviewer"] == ""
        assert "# 引入支付网关适配器（精修）" in updated["markdown"]
        assert "status: proposed" in updated["markdown"]
        assert payload["backtest"] is None
        assert [item["revision"] for item in payload["revisions"]] == [2, 1]
        assert "# 引入支付网关适配器（精修）" in payload["revisions"][0]["markdown"]
        assert "# 引入支付网关适配器" in payload["revisions"][1]["markdown"]

        after = client.get(f"/api/adr/drafts/{draft['id']}").json()
        assert after["backtest"] is None
        assert len(after["revisions"]) == 2

        reviewed = client.post(
            f"/api/adr/drafts/{draft['id']}/review",
            json={"action": "approve", "rating": 4, "reviewer": "carol"},
        ).json()["draft"]
        exported = client.post(f"/api/adr/drafts/{draft['id']}/export").json()
        content = Path(exported["path"]).read_text(encoding="utf-8")
        assert 'reviewer: "carol"' in content
        assert "revision: 2" in content

        blocked_edit = client.put(
            f"/api/adr/drafts/{draft['id']}", json={"markdown": "# 再改一次\n"}
        )
        assert blocked_edit.status_code == 409
        blocked_review = client.post(
            f"/api/adr/drafts/{draft['id']}/review", json={"action": "reject"}
        )
        assert blocked_review.status_code == 409


def test_export_records_provenance_and_replaces_it_idempotently(tmp_path):
    _repo, _store, _service, _prompts, client = _setup(tmp_path)

    with client:
        draft = _generate(client, {"kind": "text", "text": "溯源测试"})["draft"]
        client.post(
            f"/api/adr/drafts/{draft['id']}/review",
            json={"action": "approve", "reviewer": "alice", "comment": "同意"},
        )
        exported = client.post(f"/api/adr/drafts/{draft['id']}/export").json()
        target = Path(exported["path"])
        assert target.read_bytes().startswith(b"\xef\xbb\xbf")  # Windows-friendly BOM
        content = target.read_text(encoding="utf-8-sig")
        assert content.startswith("---\n")
        assert "provenance:" in content
        assert 'draft_id: "' + draft["id"] + '"' in content
        assert 'reviewer: "alice"' in content
        assert 'source: "text:' in content
        assert 'prompt_digest: "sha256:' in content
        assert "app/payments.py" in content
        assert "status: accepted" in content
        assert content.count("provenance:") == 1

        client.post(f"/api/adr/drafts/{draft['id']}/export")
        assert target.read_bytes().count(b"\xef\xbb\xbf") == 1
        again = target.read_text(encoding="utf-8-sig")
        assert again.count("provenance:") == 1
        assert again.count("status: accepted") == 1
        assert again.count("exported_at:") == 1
        assert "app/payments.py" in again
