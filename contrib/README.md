# Optional Integrations

These scripts demonstrate how to extend claude-logging with external services.
They are **not required** for core functionality.

| Script | Requires | Purpose |
|--------|----------|---------|
| `bridge_to_hippo.py` | `redis` | Bridge session entities to a FalkorDB knowledge graph |
| `heartbeat_check.py` | `pyyaml`, `psycopg2-binary` | Monitor pipeline health across multiple services |

## Usage

```bash
cd <plugin-root>
uv run contrib/bridge_to_hippo.py --dry-run
uv run contrib/heartbeat_check.py --json
```

Both scripts declare their dependencies via PEP 723 inline metadata,
so `uv run` will install them automatically in an isolated environment.
