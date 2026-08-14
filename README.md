# EIA Energy Analyst

A FastAPI app that puts a [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)
agent in front of U.S. Energy Information Administration data. Ask an energy
question; the agent pulls the data, analyzes it, charts it, and can package the
result as a workbook, report, or deck.

Live at **https://eia.kerryback.com**.

## How it works

- **Data** comes from the EIA MCP server at `https://eia.rice-business.org/mcp`,
  wired in as a remote HTTP MCP server. The agent is not allowed to source EIA
  numbers any other way.
- **Analysis and output** run in the agent's own workspace via its Bash/Write
  tools — matplotlib for charts, and the vendored `xlsx` / `docx` / `pptx` /
  `pdf` skills in `.claude/skills/` for documents.
- **Each browser session** gets an isolated workspace under `workspaces/`, which
  is the agent's `cwd` and the only place it can write. Files it saves in the
  top level of that workspace are detected after each turn and returned to the
  browser as artifacts. The scan is deliberately shallow: the document skills
  install their own tooling (the `pptx` skill npm-installs `pptxgenjs`), and
  none of that scaffolding should be offered as a download.
- **Permissions**: the agent runs in `dontAsk` mode — the tools in
  `ALLOWED_TOOLS` are auto-approved and everything else is denied. No prompts
  (nobody is at a terminal to answer one) and no permission bypass.
- **Streaming**: `/api/chat` is server-sent events, so the UI shows each step
  (searching routes, querying, charting) while the turn runs.

Model: `claude-sonnet-5`.

## Layout

| Path | What it is |
|---|---|
| `main.py` | FastAPI routes: session, chat (SSE), file download, health |
| `app/agent.py` | Agent SDK options, session lifecycle, artifact detection |
| `static/index.html` | Chat UI with inline charts and download links |
| `.claude/skills/` | Vendored document skills the agent loads on demand |
| `Dockerfile` | Python + Node + Claude Code CLI (the SDK shells out to it) |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code   # the SDK needs the CLI on PATH
uvicorn main:app --reload
```

`.env` holds `ANTHROPIC_API_KEY` and is gitignored. In production the key comes
from the Koyeb secret of the same name.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required |
| `MAX_SESSIONS` | `3` | Concurrent agent sessions; each holds a CLI subprocess |
| `SESSION_IDLE_SECONDS` | `1800` | Idle sessions are disconnected and their workspaces deleted |

## Deployment

Koyeb app `eia` in the `kerrybackapps` organization, built from this repo's
Dockerfile, on an `eco-medium` instance (1 vCPU / 2GB — the Node CLI plus
pandas/matplotlib do not fit in anything smaller).
