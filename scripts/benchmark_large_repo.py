#!/usr/bin/env python3
"""Deterministic large-repository query benchmark with time and memory gates."""

import argparse
import tempfile
import time
import tracemalloc
from pathlib import Path

from codehistory.store import EvolutionStore


def run_benchmark(feature_count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="codehistory-benchmark-") as directory:
        store = EvolutionStore(str(Path(directory) / "evolution.db"))
        with store.transaction():
            commit_id = store.insert_commit(
                "benchmark", None, 1, "benchmark", "benchmark fixture"
            )
            for index in range(feature_count):
                feature_id = store.insert_feature(
                    f"feature-{index}",
                    f"Feature {index:06d}",
                    "http",
                    f"GET /features/{index}",
                    commit_id,
                )
                store.insert_snapshot(
                    feature_id,
                    commit_id,
                    {
                        "call_tree_nodes": index % 20,
                        "call_tree_edges": index % 19,
                        "call_tree_depth": index % 5,
                        "file_path": f"api/{index}.py",
                        "line_start": 1,
                        "line_end": 5,
                        "call_chain": [],
                    },
                )

        statements = []
        store.conn.set_trace_callback(
            lambda sql: statements.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
        )
        tracemalloc.start()
        started = time.perf_counter()
        result = store.query_features(search="feature", limit=100, offset=feature_count // 2)
        elapsed = time.perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        store.close()
    return {
        "features": feature_count,
        "returned": len(result["features"]),
        "total": result["total"],
        "select_statements": len(statements),
        "seconds": elapsed,
        "peak_mib": peak_bytes / 1024 / 1024,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", type=int, default=10_000)
    parser.add_argument("--max-seconds", type=float, default=5.0)
    parser.add_argument("--max-mib", type=float, default=64.0)
    args = parser.parse_args()
    result = run_benchmark(args.features)
    print(
        "features={features} returned={returned} total={total} selects={select_statements} "
        "seconds={seconds:.4f} peak_mib={peak_mib:.2f}".format(**result)
    )
    if result["total"] != args.features or result["select_statements"] > 2:
        return 1
    if result["seconds"] > args.max_seconds or result["peak_mib"] > args.max_mib:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
