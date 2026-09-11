import pytest

from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore


@pytest.fixture
def store(tmp_path):
    value = AnalysisSnapshotSQLiteStore(tmp_path / "analysis.db")
    scope = value.create_scope("scope", scope_id="scope")
    value.create_member(scope.id, "orders", "/repos/orders", "path:orders", member_id="orders")
    return value


def test_rule_candidate_reference_and_current_are_atomic(store):
    from codeevolution.infrastructure.analysis_snapshot_sqlite import utc_now
    with store.connection() as db, db:
        db.execute("INSERT INTO evidence_bundles VALUES ('ev','a','1','s','g','b',0,'p','complete','now','active')")
        db.execute("""INSERT INTO repository_analysis_snapshots
          (id,member_id,evidence_digest,captured_at,dirty,codegraph_version,codegraph_schema_digest,
           analyzer_bundle_digest,rules_digest,report_schema_version,options_digest,facts_digest,
           facts_storage,facts_json,analysis_completeness) VALUES
          ('snap','orders','ev','now',0,'cg','schema','a','r','1','o','f','inline','{}','complete')""")
        db.execute("INSERT INTO repository_snapshot_metadata(snapshot_id,updated_at) VALUES ('snap','now')")
    candidate = store.create_rule_candidate(rule_kind="node", snapshot_id="snap", subject_key="n1",
                                            prompt="p", input_digest="d", now=utc_now())
    with store.connection() as db:
        assert db.execute("SELECT state FROM snapshot_references WHERE owner_id=?", (candidate["id"],)).fetchone()[0] == "temporary"
    store.finish_rule_candidate(candidate["id"], status="completed", result="ok", now=utc_now())
    assert store.get_current_rule(rule_kind="node", snapshot_id="snap", subject_key="n1")["result"] == "ok"
    with store.connection() as db:
        assert db.execute("SELECT state FROM snapshot_references WHERE owner_id=?", (candidate["id"],)).fetchone()[0] == "permanent"
