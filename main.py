"""EIA Energy Analyst — a FastAPI front end over a Claude Agent SDK session."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent import SESSIONS, run_turn

load_dotenv()

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await SESSIONS.shutdown()


app = FastAPI(title="EIA Energy Analyst", lifespan=lifespan)


class NewSession(BaseModel):
    session_id: str


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.post("/api/session", response_model=NewSession)
async def new_session() -> NewSession:
    try:
        return NewSession(session_id=await SESSIONS.create())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    session = SESSIONS.get(request.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session expired — reload the page.")
    if session.lock.locked():
        raise HTTPException(status_code=409, detail="That session is still working.")

    async def stream():
        async with session.lock:
            try:
                async for event in run_turn(session, request.message):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception as exc:  # surface failures in the transcript
                yield f"data: {json.dumps({'type': 'error', 'text': str(exc)})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/files/{session_id}/{filename:path}")
async def get_file(session_id: str, filename: str) -> FileResponse:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session expired.")

    # Resolve and confine to the workspace — filename comes from the model.
    root = session.workspace.resolve()
    target = (root / filename).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="No such file.")

    return FileResponse(target, filename=target.name)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
