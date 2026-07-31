from codehistory.analysis.knowledge.api_contract import ApiContractExtractor
from codehistory.analysis.knowledge.report_builder import KnowledgeReportBuilder


def test_report_builder_runs_dimensions_in_declared_order():
    calls = []
    builder = KnowledgeReportBuilder(
        {
            "api_contract": ApiContractExtractor(lambda: calls.append("api") or {"count": 1}),
            "layers": ApiContractExtractor(lambda: calls.append("layers") or []),
        }
    )

    assert builder.build() == {"api_contract": {"count": 1}, "layers": []}
    assert calls == ["api", "layers"]
