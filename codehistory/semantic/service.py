from .client import LLMClient
from .json_parser import complete_json


class SemanticService:
    def __init__(self, client: LLMClient):
        self.client = client

    def complete_json(self, prompt: str, **options) -> dict | None:
        return complete_json(self.client, prompt, **options)
