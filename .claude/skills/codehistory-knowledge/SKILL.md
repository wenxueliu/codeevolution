---
name: codehistory-knowledge
description: Extract business knowledge from code and analyze multi-service microservice topologies. Use when user asks about API design, module structure, core entities, test coverage, architecture violations, external dependencies, authorization model, service dependencies, cross-service impact, end-to-end flow tracing, entity alignment, or wants LLM-powered business descriptions/rules/error catalogs/state machines. Examples: "What APIs does this project expose?", "Find core domain entities", "Show me the service dependency graph", "What's the impact of changing user-service?", "Trace the order flow end-to-end", "Which entities are shared across services?", "Generate business descriptions for this codebase"
---

# CodeHistory Knowledge Extraction

Two subsystems: **single-repo knowledge** (13 dimensions via `knowledge` command) and **multi-repo topology** (cross-service analysis via `topology`/`impact`/`trace`/`flow`/`entities`/`check` commands).

## Prerequisites

```bash
# Per-repo: initialize CodeGraph
cd /path/to/repo && codegraph init

# Multi-repo: register services
cd /home/chengnanfeng/code/harness/services/codehistory
python -m codehistory.cli register -n <service-name> -r /path/to/repo
python -m codehistory.cli discover -d /path/to/repos   # scan directory
python -m codehistory.cli check                         # health check
```

## Single-Repo Knowledge Extraction

```bash
cd /home/chengnanfeng/code/harness/services/codehistory

# Phase 1 — Pure graph analysis (fast, no LLM)
python -m codehistory.cli knowledge -r <repo_path> -s api       # API contract
python -m codehistory.cli knowledge -r <repo_path> -s modules    # Module topology
python -m codehistory.cli knowledge -r <repo_path> -s entities   # Core entities (PageRank)
python -m codehistory.cli knowledge -r <repo_path> -s tests      # Test coverage gaps
python -m codehistory.cli knowledge -r <repo_path> -s layers     # Layer violations

# Phase 2 — Graph + rules (fast, no LLM)
python -m codehistory.cli knowledge -r <repo_path> -s config     # Config consumption
python -m codehistory.cli knowledge -r <repo_path> -s deps       # External dependencies
python -m codehistory.cli knowledge -r <repo_path> -s auth       # Authorization model
python -m codehistory.cli knowledge -r <repo_path> -s heatmap    # Heat map (hot/warm/cold)

# Phase 3 — LLM-powered (requires OPENAI_API_KEY)
python -m codehistory.cli knowledge -r <repo_path> -s business --llm   # Business descriptions
python -m codehistory.cli knowledge -r <repo_path> -s rules --llm      # Business rules
python -m codehistory.cli knowledge -r <repo_path> -s errors --llm     # Error catalog
python -m codehistory.cli knowledge -r <repo_path> -s states --llm     # State machines

# Export
python -m codehistory.cli knowledge -r <repo_path> -o report.json        # Phase 1+2
python -m codehistory.cli knowledge -r <repo_path> -o report.json --llm  # Full
```

## Multi-Repo Cross-Service Analysis

```bash
cd /home/chengnanfeng/code/harness/services/codehistory

# Service management
python -m codehistory.cli register -n <name> -r /path/to/repo   # Register with auto-detection
python -m codehistory.cli discover -d /path/to/repos              # Scan for git repos
python -m codehistory.cli check                                   # Health check all services

# Topology (cached after first build)
python -m codehistory.cli topology                                # Unified service graph
python -m codehistory.cli impact -s <service>                     # Cross-service change impact
python -m codehistory.cli trace -s <service>                      # HTTP call chain trace
python -m codehistory.cli flow -s <service>                       # Full flow (HTTP+MQ+gRPC+DB)
python -m codehistory.cli entities [--llm]                        # Cross-service entity alignment
```

## Section guide

| Question | Command |
|----------|---------|
| What APIs does this service have? | `knowledge -s api` |
| Module boundaries and coupling? | `knowledge -s modules` |
| Most important classes/functions? | `knowledge -s entities` |
| Test coverage gaps? | `knowledge -s tests` |
| Architecture layer violations? | `knowledge -s layers` |
| What config keys affect what? | `knowledge -s config` |
| External services the code uses? | `knowledge -s deps` |
| Permission model? | `knowledge -s auth` |
| Hot/warm/cold function map? | `knowledge -s heatmap` |
| What does this function do (business terms)? | `knowledge -s business --llm` |
| Business validation rules? | `knowledge -s rules --llm` |
| Error handling patterns? | `knowledge -s errors --llm` |
| State machines? | `knowledge -s states --llm` |
| How are services connected? | `topology` |
| What breaks if I change service X? | `impact -s <service>` |
| Complete end-to-end flow? | `flow -s <service>` |
| Same entity across services? | `entities [--llm]` |
| Which repos are discoverable? | `discover -d /path` |
| Are all services healthy? | `check` |
