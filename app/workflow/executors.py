"""Workflow executors for the critical path:

  Gateway -> Router -> (Status || Knowledge) -> Answer -> Draft -> Approval (human) -> Action

Deterministic control flow lives in the graph; LLM reasoning happens inside individual steps.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid

from agent_framework import Agent, Executor, MCPStreamableHTTPTool, WorkflowContext, handler, response_handler

from .. import safety
from ..config import settings
from ..identity import APP_AUDIENCE, obo_exchange, validate
from ..knowledge.arabic import has_arabic
from ..knowledge.search import get_index
from ..llm import chat_client
from ..telemetry import tracer
from ..tools.client import call_tool
from ..tools.enterprise_mcp import READ_TOOLS
from . import prompts
from .models import (ActionRequest, Answered, AnswerOut, ApprovalDecision, ApprovalRequest, EmailDraft,
                     GroundednessVerdict, KnowledgeResult, RouteDecision, Routed, RunContext, StatusResult, TurnInput)

log = logging.getLogger("workflow")
STATUS_INTENTS = {"invoice_status", "pr_status", "ticket_status"}
INV = re.compile(r"\b(5100[-\s]?\d{4})\b")
PR = re.compile(r"\b(1000[-\s]?\d{4})\b")
TKT = re.compile(r"\b((?:INC|RITM)\d{6,8})\b", re.I)


def gen_options(**extra) -> dict:
    """gpt-5-family reasoning models (Azure) reject a custom temperature; local models get temperature 0."""
    base = {} if settings.llm_provider == "azure" else {"temperature": 0}
    base.update(extra)
    return base


async def structured(instructions: str, message: str, schema, name: str):
    """Run a single-shot agent with JSON-schema structured output; one retry for small local models."""
    agent = Agent(chat_client(), instructions, name=name)
    last_err = None
    for attempt in range(2):
        resp = await agent.run(message, options=gen_options(response_format=schema))
        try:
            return resp.value
        except Exception as e:  # malformed JSON from a small model
            last_err = e
            message = message + "\n\nReturn ONLY valid JSON for the schema."
    raise ValueError(f"{name}: structured output failed: {last_err}")


def _headers(rc: RunContext, **extra) -> dict:
    h = {"Authorization": f"Bearer {obo_exchange(rc.user_token)}", "X-Correlation-Id": rc.correlation_id}
    h.update(extra)
    return h


# ------------------------------------------------------------------ Gateway
class GatewayExecutor(Executor):
    """Stands in for the APIM AI gateway: token validation + prompt shield on the user input."""

    def __init__(self, rc: RunContext):
        super().__init__(id="gateway")
        self.rc = rc

    @handler
    async def run(self, msg: TurnInput, ctx: WorkflowContext[TurnInput, dict]) -> None:
        with tracer().start_as_current_span("gateway.validate_and_shield") as sp:
            claims = validate(self.rc.user_token, APP_AUDIENCE)
            sp.set_attribute("user.oid", claims["oid"])
            sp.set_attribute("user.groups", ",".join(claims["groups"]))
            shield = safety.shield_user_prompt(msg.text)
            sp.set_attribute("prompt_shield.attack", shield.attack)
            sp.set_attribute("prompt_shield.engine", shield.engine)
        ctx.set_state("claims", {k: claims[k] for k in ("oid", "name", "email", "employee_id", "groups")})
        if shield.attack:
            lang = "ar" if has_arabic(msg.text) else "en"
            await ctx.yield_output({"kind": "blocked", "reason": "prompt_attack", "detail": shield.detail,
                                    "text": "لا يمكنني تنفيذ هذا الطلب لأنه يحاول تجاوز قواعد الاستخدام." if lang == "ar"
                                    else "I can't help with that request because it tries to override the assistant's rules."})
            return
        await ctx.send_message(msg)


# ------------------------------------------------------------------ Router
class RouterExecutor(Executor):
    def __init__(self, rc: RunContext):
        super().__init__(id="router")
        self.rc = rc

    @handler
    async def run(self, msg: TurnInput, ctx: WorkflowContext[Routed, dict]) -> None:
        with tracer().start_as_current_span("router.intent") as sp:
            try:
                route: RouteDecision = await structured(prompts.ROUTER, msg.text, RouteDecision, "router")
                r = route.model_dump()
            except Exception as e:  # deterministic fallback keeps the demo alive with weak local models
                log.warning("router fallback: %s", e)
                r = {"language": "ar" if has_arabic(msg.text) else "en", "intents": ["policy_question"], "domain": "General",
                     "entities": {"invoice_number": None, "pr_number": None, "ticket_number": None}, "search_queries": [msg.text],
                     "is_prompt_attack": False, "fallback": True}
            # entity safety net: regex extraction beats a hallucinated/missed number
            ents = r["entities"]
            for key, rx, intent in (("invoice_number", INV, "invoice_status"), ("pr_number", PR, "pr_status"), ("ticket_number", TKT, "ticket_status")):
                m = rx.search(msg.text)
                if m:
                    ents[key] = m.group(1).upper() if key == "ticket_number" else m.group(1)
                    if intent not in r["intents"]:
                        r["intents"].append(intent)
            r["language"] = "ar" if has_arabic(msg.text) else "en"
            sp.set_attribute("route.intents", ",".join(r["intents"]))
            sp.set_attribute("route.language", r["language"])
        if r.get("is_prompt_attack"):
            await ctx.yield_output({"kind": "blocked", "reason": "prompt_attack", "detail": "router classification",
                                    "text": "I can't help with that request because it tries to override the assistant's rules."})
            return
        ctx.set_state("route", r)
        ctx.set_state("turn", {"text": msg.text, "correlation_id": msg.correlation_id, "persona": msg.persona})
        await ctx.send_message(Routed(turn=msg, route=r))


# ------------------------------------------------------------------ Status (tool calling via MCP)
class StatusExecutor(Executor):
    def __init__(self, rc: RunContext):
        super().__init__(id="status_agent")
        self.rc = rc

    @handler
    async def run(self, msg: Routed, ctx: WorkflowContext[StatusResult]) -> None:
        if not (set(msg.route["intents"]) & STATUS_INTENTS):
            await ctx.send_message(StatusResult(skipped=True))
            return
        facts, calls = [], []
        with tracer().start_as_current_span("status_agent.tools") as sp:
            async with MCPStreamableHTTPTool(name="enterprise", url=settings.mcp_url, allowed_tools=READ_TOOLS,
                                             static_headers=_headers(self.rc), load_prompts=False) as mcp_tool:
                agent = Agent(chat_client(), prompts.STATUS, name="status_agent", tools=mcp_tool)
                resp = await agent.run(msg.turn.text, options=gen_options())
                for m in resp.messages:
                    for c in m.contents:
                        if c.type == "function_call":
                            calls.append({"tool": c.name, "arguments": c.arguments})
                        elif c.type == "function_result":
                            try:
                                facts.append(json.loads(c.result) if isinstance(c.result, str) else c.result)
                            except Exception:
                                facts.append({"raw": str(c.result)})
            fallback = False
            if not calls:  # small local models sometimes answer without calling the tool: call it deterministically
                e = msg.route["entities"]
                for key, tool in (("invoice_number", "get_invoice_status"), ("pr_number", "get_purchase_request_status"),
                                  ("ticket_number", "get_ticket_status")):
                    if e.get(key):
                        facts.append(await call_tool(tool, {key: e[key]}, _headers(self.rc)))
                        calls.append({"tool": tool, "arguments": {key: e[key]}, "fallback": True})
                        fallback = True
            sp.set_attribute("tools.called", ",".join(c["tool"] for c in calls))
            sp.set_attribute("tools.fallback", fallback)
        await ctx.send_message(StatusResult(facts=facts, tool_calls=calls, fallback_used=fallback))


# ------------------------------------------------------------------ Knowledge (permission-aware RAG)
class KnowledgeExecutor(Executor):
    def __init__(self, rc: RunContext):
        super().__init__(id="knowledge_agent")
        self.rc = rc

    @handler
    async def run(self, msg: Routed, ctx: WorkflowContext[KnowledgeResult]) -> None:
        claims = validate(self.rc.user_token, APP_AUDIENCE)  # groups come from the signed token, never from the client
        qs = [msg.turn.text] + [q for q in msg.route.get("search_queries", []) if q and q != msg.turn.text]
        qs = qs[:3]
        with tracer().start_as_current_span("knowledge.search") as sp:
            res = get_index().search(qs, claims["groups"])
            sp.set_attribute("search.queries", " | ".join(qs))
            sp.set_attribute("search.candidates_visible", res.candidates)
            sp.set_attribute("search.security_trimmed_docs", ",".join(res.trimmed_docs))
            sp.set_attribute("search.hits", ",".join(h.chunk_id for h in res.hits))
        with tracer().start_as_current_span("safety.document_shield") as sp:
            verdicts = safety.shield_documents([h.text for h in res.hits])
            dropped = [h.chunk_id for h, v in zip(res.hits, verdicts) if v.attack]
            sp.set_attribute("shield.dropped", ",".join(dropped))
        sources = [h.__dict__ for h, v in zip(res.hits, verdicts) if not v.attack]
        await ctx.send_message(KnowledgeResult(sources=sources, dropped_by_shield=dropped, trimmed_docs=res.trimmed_docs, queries=qs))


# ------------------------------------------------------------------ Answer + groundedness
class AnswerExecutor(Executor):
    def __init__(self, rc: RunContext):
        super().__init__(id="answer")
        self.rc = rc

    @handler
    async def run(self, results: list[StatusResult | KnowledgeResult], ctx: WorkflowContext[Answered, dict]) -> None:
        route, turn = ctx.get_state("route"), ctx.get_state("turn")
        status = next(r for r in results if isinstance(r, StatusResult))
        know = next(r for r in results if isinstance(r, KnowledgeResult))
        lang = route["language"]
        sources = know.sources[:5]
        facts = [f for f in status.facts if isinstance(f, dict)]
        found_facts = [f for f in facts if f.get("found")]

        class _H:  # adapter for spotlight()
            def __init__(self, d): self.__dict__.update(d)
        src_block = "\n".join(safety.spotlight(i + 1, _H(s)) for i, s in enumerate(sources))
        prompt = (f"Answer language: {lang}\nEmployee question: {turn['text']}\n\n<system_facts>\n"
                  f"{json.dumps(facts, ensure_ascii=False)}\n</system_facts>\n\n{src_block}")

        if not sources and not found_facts:
            ans = {"answer": "لم أجد مصدراً معتمداً يجيب عن سؤالك. يمكنك التواصل مع الفريق المختص." if lang == "ar"
                   else "I could not find an approved source that answers this. Please contact the owning team.",
                   "citations": [], "used_general_knowledge": False}
            verdict = {"score": 5, "unsupported_claims": []}
            label = "no_source"
        else:
            with tracer().start_as_current_span("answer.generate") as sp:
                try:
                    a: AnswerOut = await structured(prompts.ANSWER, prompt, AnswerOut, "answer")
                    ans = a.model_dump()
                except Exception as e:
                    log.warning("answer structured output failed: %s", e)
                    ans = {"answer": "تعذر إنشاء إجابة موثوقة." if lang == "ar" else "I could not produce a reliable answer.",
                           "citations": [], "used_general_knowledge": True}
                ans["citations"] = [c for c in ans["citations"] if 1 <= c <= len(sources)]
                for c in map(int, re.findall(r"\[(\d+)\]", ans["answer"])):
                    if 1 <= c <= len(sources) and c not in ans["citations"]:
                        ans["citations"].append(c)
                sp.set_attribute("answer.citations", ",".join(map(str, ans["citations"])))
            with tracer().start_as_current_span("safety.groundedness") as sp:
                if not settings.enable_judge:  # speed option for slow laptops: citation-validity check only
                    verdict = {"score": 5 if (ans["citations"] or found_facts) else 2, "unsupported_claims": [], "judge": "disabled"}
                else:
                  try:
                    v: GroundednessVerdict = await structured(prompts.JUDGE, f"SOURCES:\n{src_block}\n\nFACTS:\n{json.dumps(found_facts, ensure_ascii=False)}\n\nANSWER:\n{ans['answer']}",
                                                              GroundednessVerdict, "judge")
                    verdict = v.model_dump()
                  except Exception:
                    verdict = {"score": 1, "unsupported_claims": ["judge failed"]}
                sp.set_attribute("groundedness.score", verdict["score"])
            grounded = verdict["score"] >= settings.groundedness_threshold and (ans["citations"] or found_facts) and not ans["used_general_knowledge"]
            if not ans["citations"] and not found_facts and not ans["used_general_knowledge"]:
                label = "no_source"  # model abstained: nothing in the approved sources answers the question
            else:
                label = "grounded" if grounded else "general_guidance"
        findings = safety.output_findings(ans["answer"])
        cited = [sources[i - 1] for i in ans["citations"]]
        payload = {"kind": "answer", "language": lang, "text": ans["answer"], "grounding": label, "groundedness": verdict,
                   "citations": [{"n": i, "doc_id": s["doc_id"], "title": s["title"], "section": s["section"], "section_no": s["section_no"],
                                  "version": s["version"], "owner": s["owner"], "chunk_id": s["chunk_id"], "text": s["text"],
                                  "effective_date": s["effective_date"]} for i, s in zip(ans["citations"], cited)],
                   "facts": found_facts, "tool_calls": status.tool_calls, "tool_fallback": status.fallback_used,
                   "retrieval": {"queries": know.queries, "hits": [s["chunk_id"] for s in sources], "security_trimmed_docs": know.trimmed_docs,
                                 "dropped_by_shield": know.dropped_by_shield}, "output_findings": findings, "route": route}
        wants_draft = "draft_email" in route["intents"] and bool(found_facts)
        payload["draft_pending"] = wants_draft
        await ctx.yield_output(payload)
        if wants_draft:
            await ctx.send_message(Answered(turn=TurnInput(**turn), route=route, answer=payload))


# ------------------------------------------------------------------ Draft (structured output)
class DraftExecutor(Executor):
    def __init__(self, rc: RunContext):
        super().__init__(id="drafting_agent")
        self.rc = rc

    @handler
    async def run(self, msg: Answered, ctx: WorkflowContext[ApprovalRequest]) -> None:
        a = msg.answer
        lang = a["language"]
        context = "\n\n".join(f"[{c['doc_id']} §{c['section_no'] or c['section']}]\n{c['text']}" for c in a["citations"]) or "(no policy cited)"
        with tracer().start_as_current_span("drafting.generate") as sp:
            prompt = (f"Answer language: {lang}\nEmployee request: {msg.turn.text}\n"
                      f"Facts (SAP): {json.dumps(a['facts'], ensure_ascii=False)}\n\nPolicy text:\n{context}")
            try:
                d: EmailDraft = await structured(prompts.DRAFT, prompt, EmailDraft, "draft")
                draft = d.model_dump()
            except Exception as e:
                log.warning("draft failed: %s", e)
                draft = {"to": [f"accounts-payable@{settings.internal_mail_domain}"], "subject": "Follow-up", "body": ""}
            external = [t for t in draft["to"] if not t.lower().endswith("@" + settings.internal_mail_domain)]
            if external or not draft["to"]:
                draft["to"] = [f"accounts-payable@{settings.internal_mail_domain}"]
            sp.set_attribute("draft.removed_external_recipients", ",".join(external))
        f = a["facts"][0]
        payload = {
            "type": "follow_up_email", "language": lang, "to": draft["to"], "subject": draft["subject"], "body": draft["body"],
            "facts": [{"field": "payment_status", "value": f.get("status"), "source": f.get("source"), "retrieved_at": f.get("retrieved_at")}],
            "citations": [{"doc_id": c["doc_id"], "section": c["section_no"] or c["section"], "chunk_id": c["chunk_id"], "version": c["version"]} for c in a["citations"]],
            "grounding": a["grounding"], "requires_approval": True,
            "proposed_action": {"tool": "send_mail", "autonomy_level": "L3", "idempotency_key": str(uuid.uuid4())},
            "removed_external_recipients": external,
        }
        h = hashlib.sha256(json.dumps({k: payload[k] for k in ("to", "subject", "body")}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        await ctx.send_message(ApprovalRequest(draft=payload, payload_hash=h, answer={"text": a["text"]}))


# ------------------------------------------------------------------ Human approval (HITL port)
class ApprovalExecutor(Executor):
    def __init__(self, rc: RunContext):
        super().__init__(id="human_approval")
        self.rc = rc

    @handler
    async def run(self, req: ApprovalRequest, ctx: WorkflowContext) -> None:
        with tracer().start_as_current_span("approval.requested") as sp:
            sp.set_attribute("approval.payload_hash", req.payload_hash)
        await ctx.request_info(req, ApprovalDecision)  # workflow pauses here; state is checkpointed

    @response_handler
    async def on_decision(self, original: ApprovalRequest, decision: ApprovalDecision, ctx: WorkflowContext[ActionRequest, dict]) -> None:
        draft = dict(original.draft)
        if decision.edited_body:
            draft["body"] = decision.edited_body
        h = hashlib.sha256(json.dumps({k: draft[k] for k in ("to", "subject", "body")}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        approval_id = "apr-" + uuid.uuid4().hex[:10]
        rec = {"ts": time.time(), "approval_id": approval_id, "approved": decision.approved, "approver": decision.approver,
               "payload_hash": h, "edited": bool(decision.edited_body), "correlation_id": self.rc.correlation_id}
        with (settings.data_dir / "approvals.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with tracer().start_as_current_span("approval.decided") as sp:
            sp.set_attribute("approval.approved", decision.approved)
            sp.set_attribute("approval.id", approval_id)
            sp.set_attribute("approval.edited", bool(decision.edited_body))
        if not decision.approved:
            await ctx.yield_output({"kind": "rejected", "approval_id": approval_id, "text": "Draft discarded. Nothing was sent."})
            return
        await ctx.send_message(ActionRequest(draft=draft, approval_id=approval_id, answer=original.answer))


# ------------------------------------------------------------------ Action (write tool, approved only)
class ActionExecutor(Executor):
    def __init__(self, rc: RunContext):
        super().__init__(id="action")
        self.rc = rc

    @handler
    async def run(self, req: ActionRequest, ctx: WorkflowContext[None, dict]) -> None:
        d = req.draft
        with tracer().start_as_current_span("action.send_mail") as sp:
            res = await call_tool("send_mail", {"to": d["to"], "subject": d["subject"], "body": d["body"],
                                                "idempotency_key": d["proposed_action"]["idempotency_key"]},
                                  _headers(self.rc, **{"X-Approval-Id": req.approval_id}))
            sp.set_attribute("action.result", json.dumps(res)[:200])
        await ctx.yield_output({"kind": "sent" if res.get("sent") else "action_failed", "approval_id": req.approval_id, "result": res,
                                "text": ("تم إرسال الرسالة." if d["language"] == "ar" else "Email sent.") if res.get("sent") else res.get("error", "Action failed")})
