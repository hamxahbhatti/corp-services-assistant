"""Scripted OpenAI-compatible model server — a deterministic TEST DOUBLE, not an AI model.

Used for (1) automated tests/CI and (2) an offline replay mode if no real model is available during
a live demo. It implements /v1/chat/completions (incl. tool calls and JSON-schema outputs) and
/v1/embeddings with simple rules, so the *real* application code path (Agent Framework, MCP tools,
workflow, approval, tracing) is exercised end to end. Quality metrics from this mode are meaningless;
run evaluations against Ollama or Azure OpenAI.

Run:  python -m scripted_llm.server   (listens on http://127.0.0.1:8099/v1)
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid

from fastapi import FastAPI, Request

from app.knowledge.arabic import has_arabic, tokens

app = FastAPI(title="scripted-llm (test double)")

SYN = {  # tiny cross-lingual map so the hashing embedder can match AR queries to EN docs
    "فاتوره": "invoice", "فواتير": "invoice", "دفع": "payment", "متاخره": "overdue", "موقوفه": "blocked", "فرق": "variance",
    "سعر": "price", "اجازه": "leave", "اجازات": "leave", "حداد": "compassionate", "وفاه": "compassionate", "شراء": "purchase",
    "طلب": "request", "رساله": "email", "متابعه": "follow", "كلمه": "password", "مرور": "password", "حسابات": "accounts",
    "دائنه": "payable", "مورد": "supplier", "امومه": "maternity", "ابوه": "paternity", "vpn": "vpn", "شبكه": "vpn",
}
INV = re.compile(r"\b(5100[-\s]?\d{4})\b")
PR = re.compile(r"\b(1000[-\s]?\d{4})\b")
TKT = re.compile(r"\b((?:INC|RITM)\d{6,8})\b", re.I)


def _canon(text: str) -> list[str]:
    return [SYN.get(t, t) for t in tokens(text)]


def _embed(text: str, dim: int = 384) -> list[float]:
    v = [0.0] * dim
    for t in _canon(text):
        h = int(hashlib.md5(t.encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 8) % 2 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _role(messages: list[dict]) -> str:
    sys = " ".join(m.get("content") or "" for m in messages if m["role"] in ("system", "developer") and isinstance(m.get("content"), str))
    m = re.search(r"\[agent:(\w+)\]", sys)
    return m.group(1) if m else "general"


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "user":
            c = m.get("content")
            return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def _router(q: str) -> dict:
    ql = q.lower()
    lang = "ar" if has_arabic(q) else "en"
    intents = []
    ents = {"invoice_number": None, "pr_number": None, "ticket_number": None}
    if (m := INV.search(q)) or "invoice" in ql or "فاتور" in q:
        intents.append("invoice_status"); ents["invoice_number"] = m.group(1) if m else None
    if (m := PR.search(q)) or "purchase request" in ql:
        if m:
            intents.append("pr_status"); ents["pr_number"] = m.group(1)
    if m := TKT.search(q):
        intents.append("ticket_status"); ents["ticket_number"] = m.group(1).upper()
    if any(k in ql for k in ("draft", "email", "follow-up", "follow up", "write")) or any(k in q for k in ("رسالة", "جهز", "جهّز", "اكتب")):
        intents.append("draft_email")
    if not intents or any(k in ql for k in ("policy", "how many", "entitle", "what is", "which", "can i", "how do")) or "كم" in q or "سياسة" in q:
        intents.append("policy_question")
    dom = "Finance" if any(k in ql for k in ("invoice", "payment", "expense", "per diem")) or "فاتور" in q else \
          "HR" if any(k in ql for k in ("leave", "maternity", "salary")) or "إجاز" in q or "اجاز" in q else \
          "Procurement" if any(k in ql for k in ("purchase", "quotation", "tender")) or "شراء" in q else \
          "IT" if any(k in ql for k in ("vpn", "password", "software", "laptop", "printer")) or "كلمة المرور" in q else "General"
    qs = [q]
    en = " ".join(_canon(q)) if lang == "ar" else None
    if en:
        qs.append(en)
    return {"language": lang, "intents": intents, "domain": dom, "entities": ents, "search_queries": qs,
            "is_prompt_attack": False}


def _sources(text: str) -> list[tuple[int, str, str]]:
    out = []
    for m in re.finditer(r"<source id=\"(\d+)\" doc=\"([^\"]+)\"[^>]*>\n?(.*?)\n?</source>", text, re.S):
        out.append((int(m.group(1)), m.group(2), m.group(3).strip()))
    return out


def _first_sentences(t: str, n: int = 2) -> str:
    body = t.split("\n", 1)[-1]
    s = re.split(r"(?<=[.!؟?])\s+", body.strip())
    return " ".join(s[:n])


def _answer(user: str) -> dict:
    srcs = _sources(user)
    lang = "ar" if re.search(r"Answer language:\s*ar", user) else "en"
    facts = re.search(r"<system_facts>\n?(.*?)\n?</system_facts>", user, re.S)
    fact_txt = ""
    if facts and facts.group(1).strip() not in ("", "[]", "none"):
        try:
            fs = json.loads(facts.group(1))
            f = next((x for x in fs if x.get("found")), None)
            if f and "invoice_number" in f:
                if lang == "ar":
                    fact_txt = f"الفاتورة {f['invoice_number']} حالتها: {f['status']} ({f.get('block_reason')}) ومتأخرة {f.get('days_overdue', 0)} يوماً. "
                else:
                    fact_txt = (f"Invoice {f['invoice_number']} from {f['vendor']} is {f['status'].replace('_', ' ')}"
                                f" ({(f.get('block_reason') or '').replace('_', ' ')}) and is {f.get('days_overdue', 0)} days overdue. ")
            elif f:
                fact_txt = ("الحالة: " if lang == "ar" else "Status: ") + json.dumps({k: v for k, v in f.items() if k not in ("found", "source")}, ensure_ascii=False) + " "
        except Exception:
            pass
    if not srcs and not fact_txt:
        return {"answer": "لم أجد مصدراً معتمداً يجيب عن سؤالك." if lang == "ar" else "I could not find an approved source that answers this.",
                "citations": [], "used_general_knowledge": False}
    parts = [f"{_first_sentences(t, 2)} [{i}]" for i, _, t in srcs[:2]]
    return {"answer": fact_txt + " ".join(parts), "citations": [i for i, _, _ in srcs[:2]], "used_general_knowledge": False}


def _draft(user: str) -> dict:
    inv = INV.search(user)
    po = re.search(r"\"po\":\s*\"([\d-]+)\"", user)
    var = re.search(r"\"variance_aed\":\s*([\d.]+)", user)
    ar = "Answer language: ar" in user
    n = inv.group(1) if inv else "the invoice"
    if ar:
        body = (f"السادة فريق الحسابات الدائنة،\n\nأرجو التكرم بمتابعة الفاتورة رقم {n} المرتبطة بأمر الشراء {po.group(1) if po else ''}، "
                f"والموقوفة للدفع بسبب فرق في السعر بقيمة {var.group(1) if var else ''} درهم. وفق سياسة شروط الدفع (FIN-POL-012 البند 4.2) "
                "يُرجى معالجة الفرق وإفادتي بموعد الإفراج عن الدفعة.\n\nمع الشكر،")
        subj = f"متابعة: الفاتورة {n} – فرق في السعر"
    else:
        body = (f"Dear Accounts Payable team,\n\nPlease follow up on invoice {n} (PO {po.group(1) if po else ''}), which is blocked for payment "
                f"due to a price variance of AED {var.group(1) if var else ''}. Under FIN-POL-012 section 4.2, please resolve the discrepancy "
                "and confirm the expected payment release date.\n\nKind regards,")
        subj = f"Follow-up: invoice {n} – price variance"
    return {"to": ["accounts-payable@authority.example"], "subject": subj, "body": body}


def _completion(model: str, content: str | None = None, tool_calls: list | None = None) -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:12], "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 80, "total_tokens": 480}}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    msgs = body.get("messages", [])
    role = _role(msgs)
    user = _last_user(msgs)
    model = body.get("model", "scripted")
    if role == "router":
        return _completion(model, json.dumps(_router(user), ensure_ascii=False))
    if role == "status":
        if any(m["role"] == "tool" for m in msgs):
            return _completion(model, "Status retrieved.")
        names = {t["function"]["name"] for t in body.get("tools", [])}
        calls = []
        for rx, tool, arg in ((INV, "get_invoice_status", "invoice_number"), (PR, "get_purchase_request_status", "pr_number"),
                              (TKT, "get_ticket_status", "ticket_number")):
            m = rx.search(user)
            if m and tool in names:
                calls.append({"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                              "function": {"name": tool, "arguments": json.dumps({arg: m.group(1)})}})
        if not calls and "list_my_open_items" in names:
            calls.append({"id": "call_" + uuid.uuid4().hex[:8], "type": "function", "function": {"name": "list_my_open_items", "arguments": "{}"}})
        return _completion(model, None, calls)
    if role == "answer":
        return _completion(model, json.dumps(_answer(user), ensure_ascii=False))
    if role == "judge":
        has = bool(re.search(r"\[\d+\]", user.split("ANSWER:", 1)[-1])) or "could not find" in user or "لم أجد" in user
        return _completion(model, json.dumps({"score": 5 if has else 2, "unsupported_claims": [] if has else ["answer has no citations"]}))
    if role == "draft":
        return _completion(model, json.dumps(_draft(user), ensure_ascii=False))
    return _completion(model, "OK")


@app.post("/v1/embeddings")
async def embeddings(req: Request):
    body = await req.json()
    inp = body["input"] if isinstance(body["input"], list) else [body["input"]]
    return {"object": "list", "model": body.get("model", "scripted"),
            "data": [{"object": "embedding", "index": i, "embedding": _embed(t)} for i, t in enumerate(inp)],
            "usage": {"prompt_tokens": 10, "total_tokens": 10}}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "scripted", "object": "model"}]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8099, log_level="warning")
