"""Workflow graph + a small session service used by the API, the CLI and the evaluation runner."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from agent_framework import FileCheckpointStorage, WorkflowBuilder

from ..config import settings
from ..identity import issue_user_token
from ..telemetry import current_trace_id, setup as setup_telemetry, tracer
from .executors import (ActionExecutor, AnswerExecutor, ApprovalExecutor, DraftExecutor, GatewayExecutor, KnowledgeExecutor,
                        RouterExecutor, StatusExecutor)
from .models import ApprovalDecision, RunContext, TurnInput

CHECKPOINTS = settings.data_dir / "checkpoints"
CHECKPOINTS.mkdir(parents=True, exist_ok=True)


def build_workflow(rc: RunContext):
    gw, router = GatewayExecutor(rc), RouterExecutor(rc)
    status, know = StatusExecutor(rc), KnowledgeExecutor(rc)
    answer, draft, approval, action = AnswerExecutor(rc), DraftExecutor(rc), ApprovalExecutor(rc), ActionExecutor(rc)
    return (WorkflowBuilder(name="corp-services-critical-path", start_executor=gw, checkpoint_storage=FileCheckpointStorage(CHECKPOINTS))
            .add_edge(gw, router)
            .add_fan_out_edges(router, [status, know])
            .add_fan_in_edges([status, know], answer)
            .add_edge(answer, draft)
            .add_edge(draft, approval)
            .add_edge(approval, action)
            .build())


@dataclass
class Session:
    persona: str
    user_token: str
    correlation_id: str = field(default_factory=lambda: "corr-" + uuid.uuid4().hex[:12])
    trace_ids: list[str] = field(default_factory=list)
    pending: dict | None = None  # {"request_id", "checkpoint_id", "request"}
    outputs: list[dict] = field(default_factory=list)

    @property
    def rc(self) -> RunContext:
        return RunContext(self.user_token, self.correlation_id, self.persona)


async def _drive(session: Session, wf, run_kwargs: dict, span_name: str) -> dict:
    t0 = time.perf_counter()
    outputs, request = [], None
    with tracer().start_as_current_span(span_name) as sp:
        sp.set_attribute("correlation_id", session.correlation_id)
        sp.set_attribute("persona", session.persona)
        session.trace_ids.append(current_trace_id())
        async for ev in wf.run(stream=True, **run_kwargs):
            if ev.type == "output":
                outputs.append(ev.data)
            elif ev.type == "request_info":
                request = {"request_id": ev.request_id, "request": ev.data}
    if request:
        cp = await wf.resolve_pause_checkpoint_id([request["request_id"]])
        session.pending = {**request, "checkpoint_id": cp}
    else:
        session.pending = None
    session.outputs.extend(outputs)
    return {"outputs": outputs, "pending_approval": _pending_view(session), "latency_ms": round((time.perf_counter() - t0) * 1000),
            "trace_ids": list(session.trace_ids), "correlation_id": session.correlation_id}


def _pending_view(s: Session) -> dict | None:
    if not s.pending:
        return None
    r = s.pending["request"]
    return {"request_id": s.pending["request_id"], "checkpoint_id": s.pending["checkpoint_id"], "draft": r.draft, "payload_hash": r.payload_hash}


def new_session(persona: str) -> Session:
    setup_telemetry()
    return Session(persona=persona, user_token=issue_user_token(persona))


async def ask(session: Session, text: str) -> dict:
    session.trace_ids = []
    wf = build_workflow(session.rc)
    return await _drive(session, wf, {"message": TurnInput(text=text, correlation_id=session.correlation_id, persona=session.persona)}, "turn")


async def decide(session: Session, approved: bool, edited_body: str | None = None) -> dict:
    """Resume from the persisted checkpoint in a *fresh* workflow instance (as a different server replica would)."""
    if not session.pending:
        raise ValueError("No pending approval")
    wf = build_workflow(session.rc)
    p = session.pending
    decision = ApprovalDecision(approved=approved, edited_body=edited_body, approver=session.persona)
    return await _drive(session, wf, {"checkpoint_id": p["checkpoint_id"], "responses": {p["request_id"]: decision}}, "approval_resume")
