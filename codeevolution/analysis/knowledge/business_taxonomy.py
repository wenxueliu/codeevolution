"""LLM-backed business taxonomy for module topology.

The deterministic classifier in :mod:`module_topology` only recognises a
``PmsProductController`` style prefix.  Repositories that name files in
snake_case, or that mix architectural layers with capabilities
(``application/knowledge_service.py``), carry no such prefix and fall back to
the directory skeleton.

Business classification is a semantic question, so this module asks a model for
it.  The task is split so a small model can still do it reliably:

1. **entities** are derived deterministically from file names (strip the
   architectural layer suffix, keep the business stem);
2. the model proposes the *domains* from a directory summary;
3. the model assigns each entity to a domain in small chunks.

The model never enumerates file paths.  Every entity it misses, and every
domain it invents, is surfaced instead of being silently forced.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import PurePosixPath
from typing import Any, Iterable

TAXONOMY_PROMPT_VERSION = "business-taxonomy/v3"

# Trailing words that describe *how* a file works, not what it is about.
LAYER_WORDS = frozenset(
    {
        "adapter",
        "adapters",
        "api",
        "apis",
        "app",
        "application",
        "base",
        "builder",
        "capture",
        "client",
        "command",
        "commands",
        "component",
        "components",
        "config",
        "controller",
        "controllers",
        "dao",
        "db",
        "domain",
        "dto",
        "entities",
        "entity",
        "extractor",
        "handler",
        "handlers",
        "helper",
        "helpers",
        "impl",
        "infra",
        "infrastructure",
        "json",
        "loader",
        "mapper",
        "model",
        "models",
        "page",
        "pages",
        "parser",
        "reader",
        "renderer",
        "renderers",
        "repository",
        "repositories",
        "resolver",
        "router",
        "routers",
        "schema",
        "schemas",
        "scheduler",
        "service",
        "services",
        "settings",
        "spec",
        "specs",
        "sqlite",
        "store",
        "stores",
        "test",
        "tests",
        "types",
        "util",
        "utils",
        "view",
        "views",
        "web",
        "worker",
        "writer",
    }
)

_SHARED_DOMAIN = "shared"
_UNCLASSIFIED = "unclassified"
_TECHNICAL_DOMAIN_WORDS = frozenset(
    {
        "api",
        "application",
        "controller",
        "dao",
        "domain",
        "infrastructure",
        "model",
        "repository",
        "service",
        "store",
        "util",
        "utils",
        "web",
    }
)


def derive_entities(source_records: Iterable[tuple[str, str]]) -> "OrderedDict[str, list[str]]":
    """Group source files into business entities by file name.

    ``PmsProductServiceImpl`` -> ``PmsProduct`` and
    ``snapshot_query_service`` -> ``snapshot_query``.  Test files join the
    entity they exercise instead of forming a parallel tree.
    """
    entities: dict[str, list[str]] = {}
    for path, _language in source_records:
        key = entity_key(path)
        entities.setdefault(key, []).append(path)
    return OrderedDict(sorted(entities.items(), key=lambda item: item[0].lower()))


def entity_key(path: str) -> str:
    stem = PurePosixPath(path).stem
    if stem.startswith("test_"):
        stem = stem[5:]
    if "_" not in stem and "-" not in stem:
        # CamelCase identifier: peel the longest trailing layer word.
        peeled = _peel_camel(stem)
        return peeled or stem
    tokens = [token for token in re.split(r"[_\-]+", stem) if token]
    while len(tokens) > 1 and tokens[-1].lower() in LAYER_WORDS:
        tokens.pop()
    return "_".join(tokens) or stem


def _peel_camel(stem: str) -> str:
    tokens = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", stem)
    if len(tokens) <= 1:
        return ""
    for size in (3, 2, 1):
        if len(tokens) <= size:
            continue
        if "".join(tokens[-size:]).lower() in LAYER_WORDS:
            return "".join(tokens[:-size])
    return ""


def build_domain_prompt(source_records: Iterable[tuple[str, str]], *, repository: str, context: str = "") -> str:
    """Phase one: propose business domains from the directory shape."""
    directories: dict[str, list[str]] = {}
    for path, _language in source_records:
        directories.setdefault(str(PurePosixPath(path).parent), []).append(PurePosixPath(path).name)
    summary = "\n".join(
        f"- {directory}/ ({len(files)} 个): {', '.join(sorted(files)[:10])}"
        for directory, files in sorted(directories.items())
    )
    context_block = context.strip() or "（无额外背景）"
    forbidden = "、".join(sorted(_TECHNICAL_DOMAIN_WORDS))
    return f"""你是软件架构分析师。请把一个代码仓库划分成 6-12 个「业务能力域」。

规则：
1. 域是业务能力，是用户/产品视角能说清的一件事（如"快照管理""知识提取""解释生成"）。
2. 禁止用技术分层词作为域：{forbidden}。
3. 域要能覆盖仓库的主要部分，不要为单个文件造域。
4. `id` 用英文小写加连字符。

仓库：{repository}
背景：{context_block}

目录摘要：
{summary}

只输出 JSON：
{{"domains":[{{"id":"kebab-case","name":"中文名","name_en":"English Name","description":"一句话"}}]}}
"""


def build_assignment_prompt(entities: list[str], domain_ids: list[str]) -> str:
    """Phase two: map a chunk of entities onto the fixed domain list."""
    choices = ", ".join(domain_ids + [_SHARED_DOMAIN])
    return f"""把每个实体映射到一个已有的业务域 id。

可选域：{choices}

实体：
{', '.join(entities)}

只输出一个 JSON 对象（实体名 -> 域 id），不要遗漏，共 {len(entities)} 个。示例：
{{"snapshot_query": "snapshot-management", "config_loader": "shared"}}
"""


def parse_domains(payload: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate the phase-one answer."""
    issues: list[str] = []
    domains: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_domains = payload.get("domains") if isinstance(payload, dict) else None
    for index, raw in enumerate(raw_domains if isinstance(raw_domains, list) else []):
        if not isinstance(raw, dict):
            continue
        domain_id = _slug(raw.get("id") or raw.get("name_en") or raw.get("name"), f"domain-{index + 1}")
        if domain_id in seen:
            issues.append(f"重复域已忽略：{domain_id}")
            continue
        seen.add(domain_id)
        if domain_id in _TECHNICAL_DOMAIN_WORDS:
            issues.append(f"域使用了架构层词：{domain_id}")
        domains.append(
            {
                "id": domain_id,
                "name": _label(raw.get("name"), domain_id),
                "name_en": _label(raw.get("name_en"), domain_id),
                "description": _label(raw.get("description"), ""),
            }
        )
    return domains, issues


def parse_assignments(
    payload: dict[str, Any] | None,
    entities: list[str],
    domain_ids: set[str],
) -> dict[str, str]:
    """Keep only mappings for known entities and known domains."""
    if not isinstance(payload, dict):
        return {}
    allowed = {entity.lower(): entity for entity in entities}
    mapping: dict[str, str] = {}
    for raw_entity, raw_domain in payload.items():
        entity = allowed.get(str(raw_entity).strip().lower())
        if entity is None:
            continue
        domain = _slug(raw_domain, "")
        if domain not in domain_ids and domain != _SHARED_DOMAIN:
            continue
        mapping[entity] = domain
    return mapping


def assemble_taxonomy(
    source_records: Iterable[tuple[str, str]],
    entities: "OrderedDict[str, list[str]]",
    domains: list[dict[str, Any]],
    assignment: dict[str, str],
) -> dict[str, Any]:
    """Expand the entity assignment into the stored per-file taxonomy."""
    issues: list[str] = []
    buckets: dict[str, list[str]] = {domain["id"]: [] for domain in domains}
    shared: dict[str, list[str]] = {}
    unclassified: list[str] = []

    for entity, files in entities.items():
        domain_id = assignment.get(entity)
        if domain_id in buckets:
            buckets[domain_id].append(entity)
        elif domain_id == _SHARED_DOMAIN:
            shared.setdefault(_SHARED_DOMAIN, []).append(entity)
        else:
            unclassified.append(entity)

    known_files = {path for files in entities.values() for path in files}
    emitted = 0
    payload_domains: list[dict[str, Any]] = []
    for domain in domains:
        entity_ids = buckets.get(domain["id"]) or []
        if not entity_ids:
            continue
        payload_entities = []
        for entity in entity_ids:
            files = entities[entity]
            payload_entities.append(
                {
                    "id": _slug(entity, "entity"),
                    "name": entity,
                    "name_en": entity,
                    "files": files,
                }
            )
            emitted += len(files)
        payload_domains.append({**domain, "entities": payload_entities})

    payload_shared = [
        {
            "id": _SHARED_DOMAIN,
            "name": "公共与基础设施",
            "name_en": "Shared",
            "files": [path for entity in shared.get(_SHARED_DOMAIN, []) for path in entities[entity]],
        }
    ]
    payload_shared = [entry for entry in payload_shared if entry["files"]]
    if unclassified:
        payload_shared.append(
            {
                "id": _UNCLASSIFIED,
                "name": "未归类",
                "name_en": "Unclassified",
                "files": [path for entity in unclassified for path in entities[entity]],
            }
        )
        issues.append(f"{len(unclassified)} 个实体未被归类，已放入 shared/unclassified")

    if not payload_domains:
        raise ValueError("模型没有给出任何可用于这个仓库的业务域")

    missing = known_files - {path for domain in payload_domains for entity in domain["entities"] for path in entity["files"]} - {
        path for entry in payload_shared for path in entry["files"]
    }
    if missing:
        payload_shared.append(
            {
                "id": _UNCLASSIFIED,
                "name": "未归类",
                "name_en": "Unclassified",
                "files": sorted(missing),
            }
        )
        issues.append(f"{len(missing)} 个文件未被覆盖，已放入 shared/unclassified")

    total = len(known_files)
    unclassified_count = sum(
        len(entry["files"]) for entry in payload_shared if entry["id"] == _UNCLASSIFIED
    )
    return {
        "domains": payload_domains,
        "shared": payload_shared,
        "stats": {
            "file_count": total,
            "entity_count": len(entities),
            "domain_count": len(payload_domains),
            "assigned_count": total - unclassified_count,
            "unclassified_count": unclassified_count,
        },
        "issues": issues[:20],
    }


def _slug(value: Any, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text[:60] or fallback


def _label(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback
