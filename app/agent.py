"""Agent SDK session management.

Each browser session gets its own workspace directory, which is the agent's cwd
and the only place it writes. Charts and documents the agent produces there are
picked up after each turn and handed back to the browser as artifacts.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACES = REPO_ROOT / "workspaces"
SKILLS_SOURCE = REPO_ROOT / ".claude"

MODEL = "claude-sonnet-5"
EIA_MCP_URL = "https://eia.kerryback.com/mcp"

# Each live session holds a `claude` subprocess plus whatever Python the agent
# runs, so this is a memory cap as much as anything. Tune per instance size.
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "3"))
SESSION_IDLE_SECONDS = int(os.environ.get("SESSION_IDLE_SECONDS", str(30 * 60)))

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
DOC_SUFFIXES = {".xlsx", ".docx", ".pptx", ".pdf", ".csv", ".md"}
ARTIFACT_SUFFIXES = IMAGE_SUFFIXES | DOC_SUFFIXES

# Tooling the agent may install while working (the pptx skill npm-installs
# pptxgenjs, for one) leaves scaffolding behind that is not a deliverable.
IGNORED_NAMES = {"package.json", "package-lock.json", "requirements.txt", "README.md"}

SYSTEM_PROMPT = """\
You are an energy data analyst for Rice Business. You answer questions using \
U.S. Energy Information Administration (EIA) data and turn that data into \
charts and documents.

## Getting data

Use the `eia` MCP tools — never invent numbers, and never fetch EIA data any \
other way. The workflow is:

1. `eia_search_routes` or `eia_browse_routes` to find a leaf route.
2. `eia_describe_route` on that route to get its facets, valid facet values, \
   and data columns. Facet values come only from describe, so always describe \
   before querying.
3. `eia_query_route` with facet-ID filters, a date range, and a frequency.

Values come back as strings — convert them before doing arithmetic. Large \
results spill to a dataframe you can query with SQL via `eia_dataframe_query`.

## Producing output

Save every deliverable directly in your working directory (the current \
directory), not in a subdirectory — only files at the top level are returned \
to the user. Never write outside the working directory.

- Charts: matplotlib, saved as PNG at 150 dpi. Label axes, include units, and \
  title the chart. Save with a short descriptive filename.
- Excel: use the `xlsx` skill. Word: use the `docx` skill. PowerPoint: use the \
  `pptx` skill. PDF: use the `pdf` skill. Read the skill before using it.
- Put the underlying data in the workbook or an appendix so figures can be \
  checked.

Default to a chart when a question is about a trend, a comparison, or a \
series over time. Build a document only when the user asks for one.

## Answering

State the finding first, in prose, with the numbers that support it and the \
period they cover. Name the EIA route the data came from so it can be traced. \
Keep it brief — the charts and documents carry the detail. If the data does \
not answer the question, say so plainly rather than substituting a proxy \
without flagging it.
"""

ALLOWED_TOOLS = [
    "mcp__eia__eia_search_routes",
    "mcp__eia__eia_browse_routes",
    "mcp__eia__eia_describe_route",
    "mcp__eia__eia_query_route",
    "mcp__eia__eia_dataframe_describe",
    "mcp__eia__eia_dataframe_query",
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Skill",
]


def _log_cli_stderr(line: str) -> None:
    line = line.strip()
    if line:
        print(f"[claude-cli] {line}", flush=True)


@dataclass
class Artifact:
    name: str
    kind: str  # "image" or "file"


@dataclass
class Session:
    id: str
    workspace: Path
    client: ClaudeSDKClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    seen_files: set[str] = field(default_factory=set)


class SessionManager:
    """Owns the live agent sessions and their workspaces."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    def _options(self, workspace: Path) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=MODEL,
            cwd=str(workspace),
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={"eia": {"type": "http", "url": EIA_MCP_URL}},
            allowed_tools=ALLOWED_TOOLS,
            # Nobody is at a terminal to answer a prompt, so the allowlist is
            # the policy: "dontAsk" auto-approves the tools above and denies
            # everything else outright.
            permission_mode="dontAsk",
            # "project" lets the CLI discover the vendored docx/xlsx/pptx/pdf
            # skills from the .claude directory linked into the workspace.
            setting_sources=["project"],
            # Surface CLI-level failures (bad flags, auth, MCP connect) in the
            # container logs; without this they collapse into "exit code 1".
            stderr=_log_cli_stderr,
        )

    async def create(self) -> str:
        await self._evict_idle()

        async with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise RuntimeError(
                    "The app is at capacity right now. Try again in a few minutes."
                )
            session_id = uuid.uuid4().hex[:16]
            workspace = WORKSPACES / session_id
            workspace.mkdir(parents=True, exist_ok=True)

            # Skills are discovered relative to cwd; link rather than copy so
            # every session shares one 3.7 MB set.
            link = workspace / ".claude"
            if not link.exists():
                link.symlink_to(SKILLS_SOURCE, target_is_directory=True)

            client = ClaudeSDKClient(options=self._options(workspace))
            await client.connect()
            self._sessions[session_id] = Session(
                id=session_id, workspace=workspace, client=client
            )
            return session_id

    def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.last_used = time.monotonic()
        return session

    def new_artifacts(self, session: Session) -> list[Artifact]:
        """Deliverables the agent created in this workspace since the last turn.

        Deliberately shallow: only the workspace root is scanned, because the
        agent is told to save deliverables there. Anything the agent installs
        while working (node_modules, virtualenvs, caches) lands in
        subdirectories and is ignored rather than offered as a download.
        """
        found: list[Artifact] = []
        for path in sorted(session.workspace.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.name in IGNORED_NAMES:
                continue
            suffix = path.suffix.lower()
            if suffix not in ARTIFACT_SUFFIXES:
                continue
            if path.name in session.seen_files:
                continue
            session.seen_files.add(path.name)
            kind = "image" if suffix in IMAGE_SUFFIXES else "file"
            found.append(Artifact(name=path.name, kind=kind))
        return found

    async def close(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            await session.client.disconnect()
        except Exception:  # a dead subprocess must not block cleanup
            pass
        shutil.rmtree(session.workspace, ignore_errors=True)

    async def _evict_idle(self) -> None:
        cutoff = time.monotonic() - SESSION_IDLE_SECONDS
        stale = [sid for sid, s in self._sessions.items() if s.last_used < cutoff]
        for session_id in stale:
            await self.close(session_id)

    async def shutdown(self) -> None:
        for session_id in list(self._sessions):
            await self.close(session_id)


async def run_turn(session: Session, message: str):
    """Run one agent turn, yielding UI events as they happen."""
    await session.client.query(message)

    async for msg in session.client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    yield {"type": "text", "text": block.text}
                elif isinstance(block, ToolUseBlock):
                    yield {"type": "tool", "name": _tool_label(block.name)}
        elif isinstance(msg, ResultMessage):
            if msg.is_error:
                yield {"type": "error", "text": "The agent hit an error on that turn."}

    for artifact in SESSIONS.new_artifacts(session):
        yield {
            "type": "artifact",
            "name": artifact.name,
            "kind": artifact.kind,
            "url": f"/api/files/{session.id}/{artifact.name}",
        }

    yield {"type": "done"}


def _tool_label(name: str) -> str:
    """Turn a raw tool name into something worth showing a user."""
    labels = {
        "mcp__eia__eia_search_routes": "Searching EIA datasets",
        "mcp__eia__eia_browse_routes": "Browsing EIA datasets",
        "mcp__eia__eia_describe_route": "Inspecting dataset fields",
        "mcp__eia__eia_query_route": "Querying EIA data",
        "mcp__eia__eia_dataframe_describe": "Listing pulled data",
        "mcp__eia__eia_dataframe_query": "Running SQL on pulled data",
        "Bash": "Running analysis",
        "Write": "Writing a file",
        "Edit": "Editing a file",
        "Read": "Reading a file",
        "Glob": "Looking for files",
        "Grep": "Searching files",
        "Skill": "Loading a skill",
    }
    return labels.get(name, name)


SESSIONS = SessionManager()
