"""LLM-backed semantic knowledge extraction."""

from ... import llm


class SemanticExtractor:
    def __init__(self, graph, source_provider=None, core_entities=None, classify_layer=None):
        self.graph = graph
        self.source = source_provider
        self.core_entities = core_entities
        self.classify_layer = classify_layer or (lambda _path: "")

    def extract(self):
        if callable(self.graph) and self.source is None:
            return self.graph()
        return {
            "business_descriptions": self.business_descriptions(),
            "business_rules": self.business_rules(),
            "error_catalog": self.error_catalog(),
            "state_machines": self.state_machines(),
        }

    def snippet(self, function, context_lines=5):
        return self.source.snippet(
            function.file_path,
            max(1, function.start_line - context_lines),
            function.end_line + context_lines,
        )

    def _functions(self, names, limit, predicate=lambda _function: True):
        if names:
            return [
                function for name in names if (function := self.graph.get_function_by_qname(name))
            ]
        return [function for function in self.graph.get_all_functions() if predicate(function)][
            :limit
        ]

    def _central_functions(self, limit):
        return [
            function
            for entity in self.core_entities(limit)
            if (function := self.graph.get_function_by_id(entity.node_id))
        ]

    def business_descriptions(self, names=None, limit=20):
        if not llm.is_available():
            return [{"error": "LLM not configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY."}]
        functions = self._functions(names, limit) if names else self._central_functions(limit)
        batch = []
        for function in functions:
            batch.append(
                {
                    "name": function.name,
                    "qualified_name": function.qualified_name,
                    "signature": function.signature,
                    "docstring": None,
                    "decorators": function.decorators,
                    "file_path": function.file_path,
                    "source_snippet": self.snippet(function),
                    "callee_names": [
                        item.callee_name for item in self.graph.get_callees(function.node_id)[:10]
                    ],
                    "caller_names": [
                        item.callee_name for item in self.graph.get_callers(function.node_id)[:5]
                    ],
                }
            )
        return llm.batch_explain_functions(batch) if batch else []

    def business_rules(self, names=None, limit=15):
        if not llm.is_available():
            return [{"error": "LLM not configured."}]
        def predicate(function):
            return (
                not function.name.startswith(("get_", "set_", "__"))
                and self.classify_layer(function.file_path) in ("application", "domain", "")
                and not function.is_test
            )
        functions = self._functions(names, limit * 2, predicate)
        results = []
        for function in functions[:limit]:
            if not (source := self.snippet(function)):
                continue
            for rule in llm.extract_business_rules(
                function.qualified_name, source, function.file_path
            ):
                results.append(
                    {
                        "function": rule.function_name,
                        "rule_type": rule.rule_type,
                        "description_en": rule.description_en,
                        "description_zh": rule.description_zh,
                        "condition": rule.condition,
                        "failure_mode": rule.failure_mode,
                    }
                )
        return results

    def error_catalog(self, names=None, limit=20):
        if not llm.is_available():
            return [{"error": "LLM not configured."}]
        functions = (
            self._functions(names, limit)
            if names
            else [
                function
                for function in self._central_functions(30)
                if not function.is_test and not function.name.startswith("_")
            ]
        )
        results = []
        for function in functions[:limit]:
            if not (source := self.snippet(function)):
                continue
            for scenario in llm.extract_error_scenarios(
                function.qualified_name, source, function.file_path
            ):
                results.append(
                    {
                        "function": scenario.function_name,
                        "error_type": scenario.error_type,
                        "trigger_condition": scenario.trigger_condition,
                        "handling": scenario.handling,
                        "user_facing": scenario.user_facing,
                    }
                )
        return results

    def state_machines(self):
        if not llm.is_available():
            return [{"error": "LLM not configured."}]
        enums = self.graph.enum_nodes()
        results = []
        for enum in enums:
            members = self.graph.enum_members(enum["id"])
            names = [member["name"] for member in members]
            if len(names) < 2 or not any(
                word in enum["name"].lower()
                for word in ("status", "state", "stage", "phase", "type")
            ):
                continue
            references = []
            for name in names[:20]:
                references.extend(self.graph.functions_named_like(name))
            unique = {item["name"]: item for item in references}
            for item in list(unique.values())[:8]:
                source = self.source.snippet(
                    item["file_path"], item["start_line"], item["end_line"]
                )
                item["relevant_lines"] = self.enum_usage(source, names)
            machine = llm.detect_state_machine(
                enum["name"], enum["qualified_name"], names, list(unique.values())
            )
            if machine and machine.states:
                results.append(
                    {
                        "entity": machine.entity,
                        "states": machine.states,
                        "initial_state": machine.initial_state,
                        "terminal_states": machine.terminal_states,
                        "transitions": machine.transitions,
                    }
                )
        return results

    @staticmethod
    def enum_usage(source, members):
        lines = [
            line.strip()
            for line in (source or "").splitlines()
            if any(member in line for member in members)
        ]
        return "\n".join(lines[:15])
