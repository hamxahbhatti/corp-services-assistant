"""Web API + demo UI.  uvicorn app.server:app --port 8000"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .identity import PERSONAS
from .telemetry import setup as setup_telemetry, spans_for
from .workflow.build import Session, ask, decide, new_session

logging.basicConfig(level=logging.WARNING)
for n in ("agent_framework", "httpx", "mcp"):
    logging.getLogger(n).setLevel(logging.WARNING)

app = FastAPI(title="Corporate Services Assistant (demo)")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
SESSIONS: dict[str, Session] = {}


@app.on_event("startup")
def _startup():
    setup_telemetry()


class NewSession(BaseModel):
    persona: str


class Chat(BaseModel):
    session_id: str
    text: str


class Decision(BaseModel):
    session_id: str
    approved: bool
    edited_body: str | None = None


def _clean(o):
    return json.loads(json.dumps(o, default=lambda x: x.__dict__ if hasattr(x, "__dict__") else str(x), ensure_ascii=False))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    import socket
    from urllib.parse import urlparse

    import httpx

    def reachable_http(url):
        try:
            return httpx.get(url, timeout=3, headers={"Authorization": f"Bearer {settings.api_key}", "api-key": settings.api_key}).status_code < 500
        except Exception:
            return False

    u = urlparse(settings.mcp_url)
    with socket.socket() as s:
        s.settimeout(1)
        mcp_ok = s.connect_ex((u.hostname, u.port or 80)) == 0
    return {"llm_provider": settings.llm_provider, "chat_model": settings.chat_model, "embed_model": settings.embed_model,
            "search_backend": settings.search_backend, "mcp_url": settings.mcp_url,
            "model_endpoint_ok": reachable_http(settings.base_url.rstrip("/") + "/models"), "mcp_ok": mcp_ok}


@app.get("/api/personas")
def personas():
    return [{"key": k, "name": p.name, "title": p.title, "groups": list(p.groups), "language": p.language} for k, p in PERSONAS.items()]


@app.post("/api/session")
def create_session(body: NewSession):
    if body.persona not in PERSONAS:
        raise HTTPException(404, "unknown persona")
    sid = uuid.uuid4().hex
    SESSIONS[sid] = new_session(body.persona)
    return {"session_id": sid, "correlation_id": SESSIONS[sid].correlation_id}


@app.post("/api/chat")
async def chat(body: Chat):
    s = SESSIONS.get(body.session_id) or HTTPException(404, "session")
    if isinstance(s, HTTPException):
        raise s
    try:
        return JSONResponse(_clean(await ask(s, body.text)))
    except Exception as e:  # surface model/runtime errors to the demo UI
        logging.exception("chat failed")
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.post("/api/approve")
async def approve(body: Decision):
    s = SESSIONS.get(body.session_id)
    if not s:
        raise HTTPException(404, "session")
    try:
        return JSONResponse(_clean(await decide(s, body.approved, body.edited_body)))
    except Exception as e:
        logging.exception("approve failed")
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.get("/api/trace/{session_id}")
def trace(session_id: str):
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(404, "session")
    return spans_for(s.trace_ids)


@app.get("/api/audit")
def audit(limit: int = 12):
    def tail(p: Path):
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[-limit:]]
    return {"tool_audit": tail(settings.data_dir / "audit.jsonl"), "approvals": tail(settings.data_dir / "approvals.jsonl"),
            "outbox": tail(settings.data_dir / "outbox.jsonl")}
