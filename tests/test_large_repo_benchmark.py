from scripts.benchmark_large_repo import run_benchmark


def test_large_repo_query_is_batched_and_memory_bounded():
    result = run_benchmark(250)

    assert result["total"] == 250
    assert result["returned"] == 100
    assert result["select_statements"] <= 2
    assert result["peak_mib"] < 16
