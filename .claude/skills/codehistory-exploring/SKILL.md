---
name: codehistory-exploring
description: Explore code structure via direct CodeGraph SQLite queries. Use when user asks "how does X work?", "who calls this?", "what does this function call?", "show me the call chain", "find functions by name", or "what's in this file?". Works on any repo with CodeGraph initialized. Faster than grep/read for structural questions."
---

# Code Exploring via CodeGraph SQLite

Directly query CodeGraph's knowledge graph (SQLite) for code navigation. No need to grep or read files for structural questions.

## Prerequisites

```bash
cd /path/to/repo && codegraph init   # one-time per repo
```

## Quick Queries

Run these from the target repo directory. Replace `<term>` with the user's search term.

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

### Call chain (BFS, N hops)
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

### Find all HTTP endpoints (routes)
```bash
python -c "
import sqlite3, json
db = sqlite3.connect('.codegraph/codegraph.db')
# Route nodes
routes = db.execute(\"SELECT name, file_path, start_line FROM nodes WHERE kind = 'route'\").fetchall()
for r in routes: print(f'  {r[0]:40s} @ {r[1]}:{r[2]}')
# Decorated endpoints
decos = db.execute(\"\"\"
  SELECT name, qualified_name, file_path, start_line, decorators
  FROM nodes WHERE kind IN ('function','method') AND decorators IS NOT NULL
\"\"\").fetchall()
http_decos = {'get','post','put','delete','patch','getmapping','postmapping','putmapping','deletemapping','patchmapping'}
for r in decos:
    try:
        ds = json.loads(r[4]) if isinstance(r[4], str) else r[4]
        for d in ds:
            name = d.lstrip('@').split('.')[-1].lower()
            if name in http_decos:
                print(f'  {name.upper():6s} {r[1]:45s} @ {r[2]}:{r[3]}')
    except: pass
db.close()
"
```

## When to use this skill vs codehistory-knowledge

| Question | Use |
|----------|-----|
| "Who calls X?" / "What does X call?" | **codehistory-exploring** (this skill) |
| "What's in file Y?" / "Find function Z" | **codehistory-exploring** (this skill) |
| "What APIs does this project have?" | **codehistory-knowledge** (`-s api`) |
| "What's the module structure?" | **codehistory-knowledge** (`-s modules`) |
| "What are the core business entities?" | **codehistory-knowledge** (`-s entities`) |
| "What tests cover this?" | **codehistory-knowledge** (`-s tests`) |
| "What does this function do in business terms?" | **codehistory-knowledge** (`-s business --llm`) |
