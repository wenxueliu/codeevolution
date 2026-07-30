---
name: codehistory-exploring
description: Explore code structure via direct CodeGraph SQLite queries. Use when user asks "how does X work?", "who calls this?", "what does this function call?", "show me the call chain", "find functions by name", "what's in this file?", or "what cross-service calls exist?". Works on any repo with CodeGraph initialized.
---

# Code Exploring via CodeGraph SQLite

Directly query CodeGraph's knowledge graph (SQLite) for code navigation. Also works across multiple registered services for cross-repo queries.

## Prerequisites

```bash
cd /path/to/repo && codegraph init   # one-time per repo
```

## Single-Repo Queries

All queries read `.codegraph/codegraph.db` in the target repo.

### Find a function/class by name
```bash
python -c "
import sqlite3
db = sqlite3.connect('.codegraph/codegraph.db')
rows = db.execute(\"\"\"
  SELECT name, kind, qualified_name, file_path, start_line, signature
  FROM nodes WHERE name LIKE ? AND kind IN ('function','method','class')
  LIMIT 20
\"\"\", ('%<term>%',)).fetchall()
for r in rows: print(f'[{r[1]}] {r[0]} @ {r[3]}:{r[4]}  {r[5] or \"\"}')
db.close()
"
```

### Who calls this function?
```bash
python -c "
import sqlite3
db = sqlite3.connect('.codegraph/codegraph.db')
target = db.execute('SELECT id FROM nodes WHERE name LIKE ? AND kind IN (\"function\",\"method\") LIMIT 1', ('%<term>%',)).fetchone()
if target:
    rows = db.execute(\"\"\"
        SELECT n.name, n.kind, n.file_path, n.start_line, e.line as call_line
        FROM edges e JOIN nodes n ON n.id = e.source
        WHERE e.target = ? AND e.kind = 'calls'
        ORDER BY n.file_path, n.start_line
    \"\"\", (target[0],)).fetchall()
    for r in rows: print(f'  {r[0]} ({r[1]}) @ {r[2]}:{r[3]}  call_line={r[4]}')
else: print('Not found')
db.close()
"
```

### What does this function call?
```bash
python -c "
import sqlite3
db = sqlite3.connect('.codegraph/codegraph.db')
target = db.execute('SELECT id FROM nodes WHERE name LIKE ? AND kind IN (\"function\",\"method\") LIMIT 1', ('%<term>%',)).fetchone()
if target:
    rows = db.execute(\"\"\"
        SELECT n.name, n.kind, n.file_path, n.start_line, e.line as call_line
        FROM edges e JOIN nodes n ON n.id = e.target
        WHERE e.source = ? AND e.kind = 'calls'
        ORDER BY n.file_path, n.start_line
    \"\"\", (target[0],)).fetchall()
    for r in rows: print(f'  {r[0]} ({r[1]}) @ {r[2]}:{r[3]}  call_line={r[4]}')
else: print('Not found')
db.close()
"
```

### Call chain (BFS, 5 hops)
```bash
python -c "
import sqlite3
db = sqlite3.connect('.codegraph/codegraph.db')
start = db.execute('SELECT id FROM nodes WHERE name LIKE ? LIMIT 1', ('%<term>%',)).fetchone()
if not start: print('Not found'); exit()
visited = {start[0]}
queue = [(start[0], 0)]
while queue:
    cur, depth = queue.pop(0)
    if depth >= 5: continue
    name = db.execute('SELECT name, qualified_name FROM nodes WHERE id = ?', (cur,)).fetchone()
    prefix = '  ' * depth
    print(f'{prefix}{name[1] if name else cur}')
    callees = db.execute('SELECT target FROM edges WHERE source = ? AND kind = \"calls\"', (cur,)).fetchall()
    for (t,) in callees:
        if t not in visited:
            visited.add(t)
            queue.append((t, depth + 1))
db.close()
"
```

### What symbols are in a file?
```bash
python -c "
import sqlite3
db = sqlite3.connect('.codegraph/codegraph.db')
rows = db.execute(\"\"\"
  SELECT kind, name, start_line, signature, visibility
  FROM nodes WHERE file_path = ? AND kind IN ('class','function','method')
  ORDER BY start_line
\"\"\", ('<filepath>',)).fetchall()
for r in rows: print(f'[{r[0]:10s}] L{r[2]:4d}  {r[3] or \"\"}  {r[1]}')
db.close()
"
```

### Full-text search
```bash
python -c "
import sqlite3
db = sqlite3.connect('.codegraph/codegraph.db')
rows = db.execute(\"\"\"
  SELECT n.name, n.kind, n.file_path, n.start_line, n.signature
  FROM nodes_fts f JOIN nodes n ON n.rowid = f.rowid
  WHERE f.nodes_fts MATCH ?
  LIMIT 20
\"\"\", ('<term>',)).fetchall()
for r in rows: print(f'[{r[1]:10s}] {r[0]:30s} @ {r[2]}:{r[3]}')
db.close()
"
```

### Find all HTTP endpoints
```bash
python -m codehistory.cli knowledge -r . -s api
```

## Multi-Repo Cross-Service Queries

Use the `codehistory` CLI for cross-service analysis:

### Service dependency graph
```bash
cd /home/chengnanfeng/code/harness/services/codehistory
python -m codehistory.cli topology
```

### Cross-service impact (what breaks if I change X?)
```bash
python -m codehistory.cli impact -s <service-name>
```

### End-to-end HTTP call trace
```bash
python -m codehistory.cli trace -s <service-name>
```

### Full flow trace (HTTP + MQ + gRPC)
```bash
python -m codehistory.cli flow -s <service-name>
```

### Cross-service entity mapping (same concept, different names)
```bash
python -m codehistory.cli entities
python -m codehistory.cli entities --llm    # with LLM verification
```

## When to use which skill

| Question | Use |
|----------|-----|
| "Who calls X?" / "What does X call?" | **codehistory-exploring** (direct SQL) |
| "What's in file Y?" / "Find function Z" | **codehistory-exploring** (direct SQL) |
| "What APIs does this project have?" | **codehistory-knowledge** (`-s api`) |
| "How are services connected?" | **codehistory-knowledge** (`topology`) |
| "What breaks if I change service X?" | **codehistory-knowledge** (`impact -s X`) |
| "Trace the complete order flow" | **codehistory-knowledge** (`flow -s order-svc`) |
| "Which entities are shared across services?" | **codehistory-knowledge** (`entities --llm`) |
