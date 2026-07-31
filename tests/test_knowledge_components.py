from codehistory.analysis.knowledge.api_contract import ApiContractExtractor
from codehistory.analysis.knowledge.authorization import AuthorizationExtractor
from codehistory.analysis.knowledge.config_usage import ConfigUsageExtractor
from codehistory.analysis.knowledge.core_entities import CoreEntityExtractor
from codehistory.analysis.knowledge.dependencies import DependencyExtractor
from codehistory.analysis.knowledge.heatmap import HeatmapExtractor
from codehistory.analysis.knowledge.layer_rules import LayerRuleExtractor
from codehistory.analysis.knowledge.module_topology import ModuleTopologyExtractor
from codehistory.analysis.knowledge.report_builder import KnowledgeReportBuilder
from codehistory.analysis.knowledge.semantic import SemanticExtractor
from codehistory.analysis.knowledge.test_gaps import TestGapExtractor as GapExtractor


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


def test_every_knowledge_dimension_is_independently_callable():
    dimensions = [
        AuthorizationExtractor,
        ConfigUsageExtractor,
        CoreEntityExtractor,
        DependencyExtractor,
        HeatmapExtractor,
        LayerRuleExtractor,
        ModuleTopologyExtractor,
        SemanticExtractor,
        GapExtractor,
    ]
    for dimension in dimensions:
        assert dimension(lambda: "result").extract() == "result"
