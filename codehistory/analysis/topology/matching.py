"""Pure cross-service matching rules."""

import re


class PathMatcher:
    @staticmethod
    def extract(url: str) -> str:
        url = url.strip().strip("\"'`")
        if "://" in url:
            match = re.search(r''':\/\/[^/]+(/[^\s'",;?#]*)''', url)
            if match:
                return match.group(1)
        path = re.sub(r"\{[^}]+\}", ":param", url)
        return path.split("?")[0] if path.startswith("/") else ""

    @staticmethod
    def matches(actual: str, template: str) -> bool:
        actual_parts = actual.strip("/").split("/")
        template_parts = template.strip("/").split("/")
        if len(actual_parts) != len(template_parts):
            return False
        return all(
            left.startswith((":", "{"))
            or right.startswith((":", "{"))
            or left.lower() == right.lower()
            for left, right in zip(actual_parts, template_parts)
        )


class TopicMatcher:
    @staticmethod
    def matches(left: str, right: str) -> bool:
        if not left or not right:
            return True
        def normalize(value: str) -> str:
            return value.lower().strip().replace("-", "").replace("_", "")

        left_clean, right_clean = normalize(left), normalize(right)
        return (
            left_clean == right_clean
            or left_clean in right_clean
            or right_clean in left_clean
        )


class EntitySimilarity:
    _suffixes = {
        "service": "svc",
        "repository": "repo",
        "controller": "ctrl",
        "manager": "mgr",
        "configuration": "config",
    }

    @classmethod
    def score(cls, left: str, right: str) -> float:
        if left == right:
            return 1.0

        def words(value: str) -> set[str]:
            tokens = re.findall(r"[A-Z]?[a-z0-9]+", value.replace("_", " "))
            normalized = {token.lower() for token in tokens}
            return {cls._suffixes.get(token, token) for token in normalized}

        left_words, right_words = words(left), words(right)
        if not left_words or not right_words:
            return 0.0
        intersection = left_words & right_words
        union = left_words | right_words
        score = len(intersection) / len(union)
        if left_words <= right_words or right_words <= left_words:
            score = max(score, 0.7)
        return score
