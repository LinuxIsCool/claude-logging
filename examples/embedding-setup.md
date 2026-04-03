# Enabling Semantic Search

Semantic search lets you find conversations by meaning, not just keywords.

## Install

```bash
cd /path/to/claude-logging
uv sync --extra embeddings
```

This installs `sentence-transformers` and `numpy`. The default model
(`all-MiniLM-L6-v2`) is 22MB and runs on CPU at ~5000 sentences/sec.

## Generate Embeddings

```bash
uv run scripts/embed_backfill.py --project-path /home/youruser
```

This creates `db/embeddings.db` alongside your existing `db/logging.db`.

## Use

Semantic search activates automatically when `embeddings.db` exists:

```python
from pathlib import Path
from lib.storage import StorageManager

sm = StorageManager(Path("~/.claude/local/logging/-home-youruser").expanduser())
svc = sm.get_search_service()
results, ms = svc.hybrid_search("how did we handle auth?", use_semantic=True)
for r in results:
    print(f"[{r.timestamp[:10]}] {r.content[:120]}")
```

Or via the REST API:

```bash
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "how did we handle auth?", "use_semantic": true}'
```
