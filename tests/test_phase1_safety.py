import ast
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from codehistory.analyzer import EvolutionAnalyzer
from codehistory.engine import EvolutionEngine
from codehistory.matcher import FeatureMatcher
from codehistory.store import EvolutionStore
from codehistory.walker import CommitInfo


def _commit(store: EvolutionStore, hash_: str) -> int:
    return store.insert_commit(hash_, None, 1, "tester", "test: fixture")


def _feature(store: EvolutionStore, commit_id: int) -> int:
    return store.insert_feature("api.py::handler", "handler", "http", "handler", commit_id)


def _snapshot(store: EvolutionStore, feature_id: int, commit_id: int):
    store.insert_snapshot(
        feature_id,
        commit_id,
        {
            "call_tree_nodes": 2,
            "call_tree_edges": 1,
            "call_tree_depth": 1,
            "file_path": "api.py",
            "line_start": 1,
            "line_end": 3,
        },
    )


def test_store_transaction_rolls_back_all_commit_writes(tmp_path):
    store = EvolutionStore(str(tmp_path / "evolution.db"))

    with pytest.raises(RuntimeError):
        with store.transaction():
            commit_id = _commit(store, "abc")
            _feature(store, commit_id)
            raise RuntimeError("analysis failed")

    assert store.get_stats()["total_commits"] == 0
    assert store.get_stats()["total_features"] == 0


def test_matcher_is_hydrated_from_active_features(tmp_path):
    store = EvolutionStore(str(tmp_path / "evolution.db"))
    commit_id = _commit(store, "abc")
    _feature(store, commit_id)
    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.store = store
    engine.matcher = FeatureMatcher()

    engine._hydrate_matcher()

    match = engine.matcher.match("http", "handler")
    assert match.matched_feature_id == "api.py::handler"


def test_matcher_uses_call_tree_and_content_fallbacks_for_renames():
    matcher = FeatureMatcher()
    matcher.register_feature(
        "api.py::create_order",
        "http",
        "post /orders",
        call_tree=["validate_order", "save_order", "publish_event"],
        content="validate order save order publish event",
    )

    l2 = matcher.match(
        "http",
        "post /purchases",
        call_tree=["validate_order", "save_order", "publish_event", "audit_order"],
    )
    l3 = matcher.match(
        "http", "post /renamed-again", content="validate order save order publish event v2"
    )

    assert (l2.matched_feature_id, l2.match_level) == ("api.py::create_order", "L2")
    assert (l3.matched_feature_id, l3.match_level) == ("api.py::create_order", "L3")
    assert l2.evidence["rule"] == "call-tree-structure"


def test_missing_entry_point_generates_died_event(tmp_path):
    store = EvolutionStore(str(tmp_path / "evolution.db"))
    first_commit = _commit(store, "first")
    feature_id = _feature(store, first_commit)
    _snapshot(store, feature_id, first_commit)

    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.store = store
    engine.matcher = FeatureMatcher()
    engine.matcher.register_feature("api.py::handler", "http", "handler")
    engine.analyzer = EvolutionAnalyzer()
    engine._reader = SimpleNamespace(get_entry_points=lambda: [])

    with store.transaction():
        second_commit = store.insert_commit("second", "first", 2, "tester", "feat: remove")
        engine._process_entry_points(second_commit)

    assert store.get_feature("api.py::handler")["status"] == "removed"
    assert [event["event_type"] for event in store.get_feature_timeline("api.py::handler")] == [
        "DIED"
    ]
    assert engine.matcher.match("http", "handler").matched_feature_id is None


def test_removed_feature_can_reappear_without_changing_identity(tmp_path):
    store = EvolutionStore(str(tmp_path / "evolution.db"))
    first_commit = _commit(store, "first")
    original_id = _feature(store, first_commit)
    _snapshot(store, original_id, first_commit)
    store.mark_feature_removed(original_id)

    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.store = store
    engine.matcher = FeatureMatcher()
    engine.analyzer = EvolutionAnalyzer()
    engine._reader = SimpleNamespace(
        get_call_tree=lambda _node_id: ["handler", "callee"],
        get_call_tree_depth=lambda _node_id: 1,
        get_call_chain=lambda _node_id: [],
        get_callees=lambda _node_id: ["callee"],
    )
    endpoint = SimpleNamespace(
        node_id="handler",
        name="handler",
        qualified_name="api.py::handler",
        file_path="api.py",
        params=[],
        entry_type="http",
        start_line=1,
        end_line=3,
    )

    with store.transaction():
        second_commit = store.insert_commit("second", "first", 2, "tester", "feat: restore")
        stable_id = engine._process_one_entry_point(endpoint, second_commit)

    feature = store.get_feature(stable_id)
    assert feature["id"] == original_id
    assert feature["status"] == "active"
    assert store.get_latest_snapshot(original_id) is not None


def test_backfill_refuses_to_mix_existing_database_with_old_history(tmp_path):
    store = EvolutionStore(str(tmp_path / "evolution.db"))
    _commit(store, "existing")
    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.store = store
    engine.walker = SimpleNamespace(count_commits=lambda: 1)

    with pytest.raises(RuntimeError, match="database is not empty"):
        engine.backfill()


def test_failed_commit_restores_database_and_matcher(tmp_path):
    store = EvolutionStore(str(tmp_path / "evolution.db"))
    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.store = store
    engine.matcher = FeatureMatcher()
    engine.matcher.register_feature("existing", "http", "existing")
    engine._reader = None
    engine._checkout = lambda _hash: None
    engine._sync_codegraph = lambda: True

    def fail_after_mutation(commit_id):
        store.insert_feature("new", "new", "http", "new", commit_id)
        engine.matcher.register_feature("new", "http", "new")
        raise RuntimeError("extractor failed")

    engine._process_entry_points = fail_after_mutation
    commit = CommitInfo("abc", None, 1, "tester", "feat: fail")

    with pytest.raises(RuntimeError):
        engine._process_commit(commit)

    assert store.get_stats()["total_commits"] == 0
    assert store.get_stats()["total_features"] == 0
    assert engine.matcher.match("http", "existing").matched_feature_id == "existing"
    assert engine.matcher.match("http", "new").matched_feature_id is None


def test_isolated_worktree_preserves_source_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    source = repo / "tracked.txt"
    source.write_text("committed\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Tester",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    source.write_text("local change\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codegraph = bin_dir / "codegraph"
    codegraph.write_text("#!/bin/sh\nexit 0\n")
    codegraph.chmod(codegraph.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])

    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.config = SimpleNamespace(repo_path=str(repo))
    engine._analysis_repo_path = str(repo)
    engine._reader = None

    with engine._isolated_worktree():
        assert engine._analysis_repo_path != str(repo)
        assert Path(engine._analysis_repo_path, "tracked.txt").read_text() == "committed\n"

    assert source.read_text() == "local change\n"
    assert engine._analysis_repo_path == str(repo)
    worktrees = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "codehistory-worktree-" not in worktrees


def test_llm_module_parses_as_python_310():
    source = Path("codehistory/llm.py").read_text()
    ast.parse(source, filename="codehistory/llm.py", feature_version=(3, 10))
