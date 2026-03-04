---
name: web-ui
description: Launch the logging web interface for visual search and exploration
allowed-tools: Bash
---

# Web UI Skill

You are helping the user launch the logging web interface for visual exploration of conversation history.

## Prerequisites

- The logging plugin must be active
- Node.js and npm/pnpm must be installed
- Dependencies must be installed in the web directory

## Launching the Web Interface

The web interface consists of two servers:

1. **API Server** (FastAPI on port 3001): Provides REST endpoints and SSE streaming
2. **Web Server** (Next.js on port 3002): Provides the React frontend

### First-time Setup

```bash
cd ${CLAUDE_PLUGIN_ROOT}/web
npm install  # or pnpm install
```

### Starting the Servers

Start both servers (in separate terminals or using &):

```bash
# Terminal 1: API Server
cd ${CLAUDE_PLUGIN_ROOT}
uv run python -m uvicorn api.server:app --host 127.0.0.1 --port 3001

# Terminal 2: Web Server
cd ${CLAUDE_PLUGIN_ROOT}/web
npm run dev
```

Or use the convenience script:

```bash
cd ${CLAUDE_PLUGIN_ROOT}
./scripts/start-web.sh
```

## Response Format

After launching:
```
🌐 Logging Web Interface

API Server: http://127.0.0.1:3001
Web Server: http://127.0.0.1:3002

Features:
• Hybrid search (keyword + semantic)
• Session browser with timeline
• 2D embedding visualization
• Real-time event streaming
• Statistics dashboard

Open http://127.0.0.1:3002 in your browser.
```

## Troubleshooting

- **Port in use**: Change ports via LOGGING_API_PORT env var or next.config.js
- **CORS errors**: The Next.js rewrites proxy API requests automatically
- **No data**: Ensure the API server is running and has synced JSONL files
