"""External dependency inventory."""

import json
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Protocol

PATTERNS = {
    "requests.": ("http-client", "Python requests"),
    "httpx.": ("http-client", "Python httpx"),
    "axios.": ("http-client", "JS axios"),
    "fetch(": ("http-client", "JS fetch"),
    "sqlalchemy": ("database", "SQLAlchemy"),
    "psycopg": ("database", "PostgreSQL driver"),
    "pymongo": ("database", "MongoDB driver"),
    "redis.": ("database", "Redis client"),
    "kafka": ("message-queue", "Apache Kafka"),
    "rabbitmq": ("message-queue", "RabbitMQ"),
    "amqp.": ("message-queue", "AMQP"),
    "celery": ("message-queue", "Celery task queue"),
    "memcached": ("cache", "Memcached"),
    "boto3": ("cloud", "AWS SDK (boto3)"),
    "google-cloud": ("cloud", "Google Cloud SDK"),
    "elasticsearch": ("search", "Elasticsearch"),
    "opentelemetry": ("observability", "OpenTelemetry"),
    "sentry": ("observability", "Sentry error tracking"),
    "oauth": ("auth", "OAuth"),
    "jwt.": ("auth", "JWT"),
    "bcrypt": ("auth", "bcrypt"),
}


class _Source(Protocol):
    def query(self, sql: str, params=None) -> list[dict[str, Any]]: ...


class DependencyExtractor:
    def __init__(self, source: _Source | Callable[[], list[dict]]):
        self.source = source

    def extract(self) -> list[dict]:
        if callable(self.source) and not hasattr(self.source, "query"):
            return self.source()
        categories: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        rows = self.source.query(
            "SELECT name AS import_name, file_path AS importer_file, start_line, signature FROM nodes WHERE kind = 'import'"
        )  # type: ignore[union-attr]
        evidence = [
            (
                row.get("signature") or row["import_name"],
                row["importer_file"],
                row["start_line"],
                "import",
            )
            for row in rows
        ]
        decorators = self.source.query(
            "SELECT DISTINCT decorators, file_path, start_line FROM nodes WHERE decorators IS NOT NULL"
        )  # type: ignore[union-attr]
        for row in decorators:
            try:
                values = (
                    json.loads(row["decorators"])
                    if isinstance(row["decorators"], str)
                    else row["decorators"]
                )
            except (json.JSONDecodeError, TypeError):
                continue
            evidence.extend(
                (value, row["file_path"], row["start_line"], "decorator") for value in values
            )
        for name, path, line, origin in evidence:
            category, label = self.classify(str(name))
            if category:
                categories[category][label].append({"file": path, "line": line, "source": origin})
        return [
            {
                "category": category,
                "dependency_count": len(items),
                "dependencies": [
                    {
                        "label": label,
                        "file_count": len(set(use["file"] for use in uses)),
                        "files": sorted(set(use["file"] for use in uses))[:10],
                    }
                    for label, uses in items.items()
                ],
            }
            for category, items in sorted(categories.items())
        ]

    @staticmethod
    def classify(name: str) -> tuple[str | None, str | None]:
        lowered = name.lower()
        for pattern, result in PATTERNS.items():
            if pattern.lower() in lowered:
                return result
        return None, None
