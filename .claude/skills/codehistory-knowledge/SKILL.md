---
name: codehistory-knowledge
description: Extract business knowledge from code. Use when user asks about API design, module structure, core entities, test coverage, architecture violations, external dependencies, authorization model, or wants LLM-powered business descriptions/rules/error catalogs/state machines. Examples: "What APIs does this project expose?", "Find core domain entities", "Show test coverage gaps", "Detect layer violations", "Generate business descriptions for this codebase"
---

# CodeHistory Knowledge Extraction

Extracts 13-dimension business knowledge from code via CodeGraph's graph database.

## Prerequisites

```bash
# One-time setup per repo
cd /path/to/repo && codegraph init
```

## Quick Reference

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

# Phase 3 — LLM-powered (requires OPENAI_API_KEY or ANTHROPIC_API_KEY)
python -m codehistory.cli knowledge -r <repo_path> -s business --llm   # Business descriptions
python -m codehistory.cli knowledge -r <repo_path> -s rules --llm      # Business rules
python -m codehistory.cli knowledge -r <repo_path> -s errors --llm     # Error catalog
python -m codehistory.cli knowledge -r <repo_path> -s states --llm     # State machines

# Full report
python -m codehistory.cli knowledge -r <repo_path>                       # Phase 1+2 (all non-LLM)
python -m codehistory.cli knowledge -r <repo_path> --llm                 # Phase 1+2+3 (with LLM)
python -m codehistory.cli knowledge -r <repo_path> -o report.json        # Export JSON
python -m codehistory.cli knowledge -r <repo_path> -o report.json --llm  # Full JSON with LLM
```

## Section guide — which section to use for which question

| User asks about | Use `-s` |
|-----------------|----------|
| API design, REST endpoints, request/response shapes | `api` |
| Project structure, module boundaries, coupling | `modules` |
| Most important classes/functions, domain core | `entities` |
| Test coverage, untested production code | `tests` |
| Architecture violations (e.g., controller calling DB directly) | `layers` |
| What config keys affect which functions | `config` |
| External services the code depends on (DB, MQ, cache, cloud) | `deps` |
| Permission model, role-based access, auth middleware | `auth` |
| Which functions are most/least frequently called | `heatmap` |
| Business-level explanation of what functions do | `business --llm` |
| Business validation rules, constraints, guard clauses | `rules --llm` |
| Error handling patterns, exception types, failure modes | `errors --llm` |
| State machines, status transitions, workflow states | `states --llm` |

## Interpreting output

### API Contract (`-s api`)
```
GET    /api/users/:id          → src/controllers/user_controller.py::get_user
POST   /api/orders             → src/controllers/order_controller.py::create_order
```
Method, path, handler function — ready to feed to product or testing teams.

### Module Topology (`-s modules`)
```
mod-1: "src/controllers" (12 files, .py) → deps: ['mod-2', 'mod-3']
mod-2: "src/services"    (8 files,  .py) → deps: ['mod-3']
```
Shows module boundaries detected by Louvain community detection. Coupling score near 1.0 = highly coupled.

### Core Entities (`-s entities`)
```
0.0523  OrderService    in=15 out=3  [application]
0.0411  UserRepository  in=12 out=1  [infrastructure]
```
PageRank centrality — higher = more important in the call graph. in = callers, out = callees.

### Heat Map (`-s heatmap`)
```
HOT   (top 10%):  core infrastructure, called by many
WARM  (10-60%):   regular business logic
COLD  (bottom 40%): leaf functions, rarely called
```
Hot functions are the most risky to change. Cold functions are the safest.

### Business Descriptions (`-s business --llm`)
```
[Entry Point] create_order
  EN: Creates a new order after validating inventory and calculating shipping
  ZH: 验证库存并计算运费后创建新订单
  Domain: Order Processing
```
LLMs produce business-level summaries accessible to non-engineers.

### Business Rules (`-s rules --llm`)
```
[validation] OrderService.place_order
  EN: Order total must be greater than 0
  Condition: if order.total <= 0
  On failure: raise ValidationError("Order total must be positive")
```

### State Machines (`-s states --llm`)
```
Entity: Order
States: [PENDING, CONFIRMED, SHIPPED, DELIVERED, CANCELLED]
Transitions:
  PENDING → CONFIRMED: payment verified
  CONFIRMED → SHIPPED: warehouse dispatch
  SHIPPED → DELIVERED: customer receipt confirmation
  PENDING → CANCELLED: user cancels or payment timeout
```

## Tool Selection

Run the right section for the user's question. If unsure, start with `-s entities` and `-s modules` for architecture overview, or `-s api` for API questions. Add `--llm` only when the user asks for business semantics.

## LLM Configuration

```bash
export OPENAI_API_KEY=sk-...      # or ANTHROPIC_API_KEY
export CODEHISTORY_LLM_MODEL=gpt-4o-mini  # default, override for different model
```
