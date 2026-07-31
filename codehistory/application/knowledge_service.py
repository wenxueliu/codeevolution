"""Knowledge extraction use cases."""


class KnowledgeService:
    def __init__(self, extractor):
        self.extractor = extractor

    def report(self, include_llm: bool = False) -> dict:
        return self.extractor.extract_all(include_llm=include_llm)
