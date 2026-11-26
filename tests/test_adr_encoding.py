"""Windows encoding coverage for ADR inputs, facts and exports.

Windows checkouts are not uniformly UTF-8: Notepad saves Chinese Markdown as
GB18030 or UTF-16, and commits authored in a cp936 console carry GBK bytes.
These integration tests drive the real API, git and filesystem so that a
regression shows up again as mojibake (``U+FFFD``) in the ADR workbench.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from codeevolution.adr.service import AdrService
from codeevolution.adr.store import AdrStore
from codeevolution.analysis.knowledge.adr import AdrExtractor
from codeevolution.api import create_app

DECISION = {
    "is_decision": True,
    "reason": "引入新的外部契约",
    "title": "引入支付网关适配器",
    "slug": "add-payment-gateway-adapter",
    "context": "支付渠道需要独立演进。",
    "drivers": ["可扩展"],
    "options": [{"name": "适配器", "pros": ["解耦"], "cons": ["多一层抽象"]}],
    "chosen_option": "适配器",
    "outcome": "渠道差异被隔离在适配器内",
    "consequences": {"good": ["新增渠道成本低"], "bad": ["调试链路变长"]},
    "confidence": 0.8,
    "evidence": [{"path": "app/payments.py", "line": 1, "note": "网关入口"}],
}

REPLACEMENT_CHAR = "\ufffd"


class SyncScheduler:
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


def _commit_with_raw_message(repo, message: bytes) -> str:
    """Write a commit whose message bytes are stored verbatim.

    ``git commit -F`` re-encodes non-UTF-8 input (and warns), so a real legacy
    commit is built through ``hash-object`` instead.
    """
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], capture_output=True, check=True
    ).stdout.strip()
    payload = (
        b"tree "
        + tree
        + b"\nauthor dev <a@b> 1700000000 +0800\ncommitter dev <a@b> 1700000000 +0800\n\n"
        + message
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
        input=payload,
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    branch = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    _git(repo, "update-ref", f"refs/heads/{branch}", sha)
    return sha


def _client_for(tmp_path, repo):
    prompts = []

    def completer(prompt):
        prompts.append(prompt)
        return json.dumps(DECISION, ensure_ascii=False)

    store = AdrStore(tmp_path / "data" / "adr.db")
    member = SimpleNamespace(id="member-1", display_name="shop", registered_path=str(repo))
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
    return TestClient(app), prompts


def _start_generation(client, source):
    response = client.post(
        "/api/adr/generations", json={"member_id": "member-1", "source": source}
    )
    assert response.status_code == 202, response.text
    generation_id = response.json()["generation"]["id"]
    return client.get(f"/api/adr/generations/{generation_id}").json()["generation"]


def test_gb18030_commit_message_survives_adr_import(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "gateway.py").write_text("def route():\n    pass\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "app/gateway.py")
    _git(repo, "commit", "-m", "chore: bootstrap")
    message = "feat: 网关从 Node 迁移到 Rust\n\n降低尾延迟。\n"
    _commit_with_raw_message(repo, message.encode("gb18030"))

    # Fixture guard: git must really emit the GBK bytes, not UTF-8.
    raw = subprocess.run(
        ["git", "-C", str(repo), "show", "-s", "--format=%s"], capture_output=True
    ).stdout
    assert raw.strip() == "feat: 网关从 Node 迁移到 Rust".encode("gb18030")

    client, prompts = _client_for(tmp_path, repo)
    with client:
        commits = client.get(
            "/api/adr/commits", params={"snapshot_id": "snap-1", "member_id": "member-1"}
        ).json()["items"]
        subjects = [item["subject"] for item in commits]
        assert "feat: 网关从 Node 迁移到 Rust" in subjects
        assert not any(REPLACEMENT_CHAR in subject for subject in subjects)

        generation = _start_generation(client, {"kind": "commit", "ref": "HEAD"})

    assert generation["status"] == "succeeded", generation
    assert "网关从 Node 迁移到 Rust" in prompts[-1]
    assert REPLACEMENT_CHAR not in prompts[-1]


def test_gb18030_repository_file_is_decoded_for_generation(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "payments.py").write_bytes(
        "def 支付网关():\n    return '中文说明'\n".encode("gb18030")
    )

    client, prompts = _client_for(tmp_path, repo)
    with client:
        generation = _start_generation(
            client, {"kind": "file", "path": "app/payments.py"}
        )

    assert generation["status"] == "succeeded", generation
    assert "支付网关" in prompts[-1]
    assert "中文说明" in prompts[-1]
    assert REPLACEMENT_CHAR not in prompts[-1]


def test_gb18030_and_utf16_adr_files_render_as_chinese(tmp_path):
    repo = tmp_path / "repo"
    adr_dir = repo / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    madr = (
        "---\nstatus: accepted\ndate: 2026-02-01\n---\n\n"
        "# {title}\n\n## Context and Problem Statement\n\n尾延迟过高。\n\n"
        '## Decision Outcome\n\nChosen option: "Rust", because 更快。\n'
    )
    (adr_dir / "0001-gateway.md").write_bytes(
        madr.format(title="网关从 Node 迁移到 Rust").encode("gb18030")
    )
    (adr_dir / "0002-notepad.md").write_bytes(
        madr.format(title="记事本保存的决策").encode("utf-16")
    )

    report = AdrExtractor(repo).extract()
    titles = {record["title"] for record in report["records"]}
    assert "网关从 Node 迁移到 Rust" in titles
    assert "记事本保存的决策" in titles
    assert not any("missing title" in item["message"] for item in report["violations"])
    assert not any("missing status" in item["message"] for item in report["violations"])


def test_export_writes_bom_and_keeps_index_fresh(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "payments.py").write_text("def pay():\n    pass\n", encoding="utf-8")

    client, _prompts = _client_for(tmp_path, repo)
    with client:
        generation = _start_generation(client, {"kind": "text", "text": "导出编码测试"})
        draft = client.get(f"/api/adr/drafts/{generation['draft_id']}").json()["draft"]
        client.post(f"/api/adr/drafts/{draft['id']}/review", json={"action": "approve"})
        exported = client.post(f"/api/adr/drafts/{draft['id']}/export").json()

        assert Path(exported["path"]).read_bytes().startswith(b"\xef\xbb\xbf")
        assert Path(exported["index_path"]).read_bytes().startswith(b"\xef\xbb\xbf")

        report = AdrExtractor(repo).extract()
        assert report["available"] is True
        assert report["index_stale"] is False
        assert any(record["title"] == DECISION["title"] for record in report["records"])
