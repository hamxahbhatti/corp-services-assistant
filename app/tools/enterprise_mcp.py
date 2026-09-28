"""Mock enterprise tool server (SAP S/4HANA, ServiceNow, Microsoft Graph mail) exposed over MCP (streamable HTTP).

It behaves like the APIM-fronted MCP servers in the target architecture:
  * every call must carry a downstream (OBO) bearer token for audience `enterprise-apis`
  * authorisation is enforced here, on the user's claims (the system of record decides, not the agent)
  * write tools require an approval record id (X-Approval-Id) and an idempotency key
  * every call is written to an audit log

Run:  python -m app.tools.enterprise_mcp     (listens on http://127.0.0.1:8765/mcp)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from ..config import settings
from ..identity import API_AUDIENCE, validate

DATA = settings.data_dir
AUDIT = DATA / "audit.jsonl"
OUTBOX = DATA / "outbox.jsonl"
_lock = threading.Lock()
TODAY = dt.date.today()

INVOICES = {
    "5100-2291": {"vendor": "Gulf Supplies LLC", "vendor_ar": "شركة الخليج للتوريدات", "vendor_id": "100482",
                  "requester": "E1043", "po": "4500-7713", "amount_aed": 48300.00, "variance_aed": 2150.00,
                  "status": "blocked_for_payment", "block_reason": "price_variance",
                  "due_date": str(TODAY - dt.timedelta(days=15)), "cost_center": "CC-410"},
    "5100-2305": {"vendor": "Desert Office Supplies", "vendor_ar": "صحراء للمستلزمات المكتبية", "vendor_id": "100511",
                  "requester": "E1043", "po": "4500-7790", "amount_aed": 3120.00, "variance_aed": 0.0,
                  "status": "paid", "block_reason": None, "due_date": str(TODAY - dt.timedelta(days=3)), "cost_center": "CC-410"},
    "5100-2310": {"vendor": "Al Noor Catering", "vendor_ar": "النور للتموين", "vendor_id": "100377",
                  "requester": "E5588", "po": "4500-7801", "amount_aed": 12900.00, "variance_aed": 0.0,
                  "status": "in_approval", "block_reason": None, "due_date": str(TODAY + dt.timedelta(days=12)), "cost_center": "CC-220"},
}
PURCHASE_REQUESTS = {
    "1000-5521": {"requester": "E1043", "description": "Ergonomic chairs x 6", "value_aed": 9600.0,
                  "status": "awaiting_director_approval", "approver": "Director, Finance Operations", "created": str(TODAY - dt.timedelta(days=4))},
}
TICKETS = {
    "INC0012345": {"caller": "E2210", "short_description": "Laptop battery drains quickly", "state": "In Progress",
                   "assignment_group": "End User Computing", "sla_due": str(TODAY + dt.timedelta(days=1))},
    "RITM0045678": {"caller": "E1043", "short_description": "Software request: Power BI Desktop", "state": "Awaiting approval",
                    "assignment_group": "Software Management", "sla_due": str(TODAY + dt.timedelta(days=2))},
}

import os as _os
mcp = FastMCP("enterprise-tools", host="127.0.0.1", port=int(_os.getenv("ENTERPRISE_MCP_PORT", "8765")), stateless_http=True, json_response=True)


def _audit(event: dict) -> None:
    with _lock, AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), **event}, ensure_ascii=False) + "\n")


def _caller(ctx: Context) -> dict:
    req = ctx.request_context.request
    auth = (req.headers.get("authorization") if req is not None else "") or ""
    if not auth.lower().startswith("bearer "):
        raise PermissionError("401: missing bearer token")
    claims = validate(auth.split(" ", 1)[1], API_AUDIENCE)  # raises if signature/audience/expiry invalid
    claims["_approval_id"] = req.headers.get("x-approval-id") if req is not None else None
    claims["_trace"] = req.headers.get("x-correlation-id") if req is not None else None
    return claims


def _norm_invoice(n: str) -> str:
    d = re.sub(r"\D", "", n or "")
    return f"{d[:4]}-{d[4:]}" if len(d) == 8 else (n or "").strip()


@mcp.tool()
def get_invoice_status(invoice_number: str, ctx: Context) -> dict:
    """SAP S/4HANA: payment status of a supplier invoice. Only the requester of the linked PO (or Accounts Payable) may see it.
    Args: invoice_number - e.g. 5100-2291"""
    c = _caller(ctx)
    inv_no = _norm_invoice(invoice_number)
    inv = INVOICES.get(inv_no)
    allowed = inv is not None and (inv["requester"] == c["employee_id"] or "FIN-AP" in c["groups"])
    _audit({"tool": "get_invoice_status", "user": c["employee_id"], "args": {"invoice_number": inv_no}, "allowed": allowed, "trace": c["_trace"]})
    if not allowed:  # same answer for "missing" and "forbidden" so the tool does not leak existence
        return {"found": False, "message": "No invoice with this number is linked to your purchase orders."}
    days_overdue = (TODAY - dt.date.fromisoformat(inv["due_date"])).days
    return {"found": True, "source": f"sap:SupplierInvoice/{inv_no}", "invoice_number": inv_no, **{k: v for k, v in inv.items() if k != "requester"},
            "days_overdue": max(days_overdue, 0), "retrieved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}


@mcp.tool()
def get_purchase_request_status(pr_number: str, ctx: Context) -> dict:
    """SAP S/4HANA: status of a purchase request (PR) raised by the caller. Args: pr_number - e.g. 1000-5521"""
    c = _caller(ctx)
    pr_no = _norm_invoice(pr_number)
    pr = PURCHASE_REQUESTS.get(pr_no)
    allowed = pr is not None and pr["requester"] == c["employee_id"]
    _audit({"tool": "get_purchase_request_status", "user": c["employee_id"], "args": {"pr_number": pr_no}, "allowed": allowed, "trace": c["_trace"]})
    if not allowed:
        return {"found": False, "message": "No purchase request with this number was raised by you."}
    return {"found": True, "source": f"sap:PurchaseRequisition/{pr_no}", "pr_number": pr_no, **{k: v for k, v in pr.items() if k != "requester"}}


@mcp.tool()
def get_ticket_status(ticket_number: str, ctx: Context) -> dict:
    """ServiceNow: status of an incident (INC...) or requested item (RITM...) opened by the caller."""
    c = _caller(ctx)
    t_no = (ticket_number or "").strip().upper()
    t = TICKETS.get(t_no)
    allowed = t is not None and t["caller"] == c["employee_id"]
    _audit({"tool": "get_ticket_status", "user": c["employee_id"], "args": {"ticket_number": t_no}, "allowed": allowed, "trace": c["_trace"]})
    if not allowed:
        return {"found": False, "message": "No ticket with this number was opened by you."}
    return {"found": True, "source": f"servicenow:task/{t_no}", "ticket_number": t_no, **{k: v for k, v in t.items() if k != "caller"}}


@mcp.tool()
def list_my_open_items(ctx: Context) -> dict:
    """List the caller's open invoices, purchase requests and tickets (SAP + ServiceNow)."""
    c = _caller(ctx)
    e = c["employee_id"]
    _audit({"tool": "list_my_open_items", "user": e, "args": {}, "allowed": True, "trace": c["_trace"]})
    return {"invoices": [k for k, v in INVOICES.items() if v["requester"] == e and v["status"] != "paid"],
            "purchase_requests": [k for k, v in PURCHASE_REQUESTS.items() if v["requester"] == e],
            "tickets": [k for k, v in TICKETS.items() if v["caller"] == e]}


def _require_approval(c: dict, tool: str) -> None:
    if not c.get("_approval_id"):
        _audit({"tool": tool, "user": c["employee_id"], "allowed": False, "reason": "missing approval record", "trace": c["_trace"]})
        raise PermissionError("403: write tools require an approval record (X-Approval-Id)")


_idem: dict[str, dict] = {}


@mcp.tool()
def send_mail(to: list[str], subject: str, body: str, idempotency_key: str, ctx: Context) -> dict:
    """Microsoft Graph (delegated): send an email as the caller. WRITE - requires an approval record; internal recipients only."""
    c = _caller(ctx)
    _require_approval(c, "send_mail")
    if idempotency_key in _idem:
        return {**_idem[idempotency_key], "duplicate": True}
    bad = [r for r in to if not r.lower().endswith("@" + settings.internal_mail_domain)]
    if bad:
        _audit({"tool": "send_mail", "user": c["employee_id"], "allowed": False, "reason": f"external recipients {bad}", "trace": c["_trace"]})
        return {"sent": False, "error": f"Blocked by policy: external recipients are not allowed ({', '.join(bad)})."}
    msg = {"from": c["email"], "to": to, "subject": subject, "body": body, "approval_id": c["_approval_id"], "idempotency_key": idempotency_key}
    with _lock, OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), **msg}, ensure_ascii=False) + "\n")
    res = {"sent": True, "message_id": f"msg-{abs(hash(idempotency_key)) % 10**8:08d}", "to": to}
    _idem[idempotency_key] = res
    _audit({"tool": "send_mail", "user": c["employee_id"], "allowed": True, "args": {"to": to, "subject": subject}, "approval_id": c["_approval_id"], "trace": c["_trace"]})
    return res


@mcp.tool()
def create_catalog_request(item: str, justification: str, end_date: str, idempotency_key: str, ctx: Context) -> dict:
    """ServiceNow: create a service catalog request (RITM) for the caller. WRITE - requires an approval record."""
    c = _caller(ctx)
    _require_approval(c, "create_catalog_request")
    if idempotency_key in _idem:
        return {**_idem[idempotency_key], "duplicate": True}
    num = f"RITM00{len(TICKETS) + 45679}"
    TICKETS[num] = {"caller": c["employee_id"], "short_description": f"{item}: {justification}"[:120], "state": "Awaiting approval",
                    "assignment_group": "Service Desk", "sla_due": str(TODAY + dt.timedelta(days=2))}
    res = {"created": True, "number": num, "state": "Awaiting approval"}
    _idem[idempotency_key] = res
    _audit({"tool": "create_catalog_request", "user": c["employee_id"], "allowed": True, "args": {"item": item}, "approval_id": c["_approval_id"], "trace": c["_trace"]})
    return res


READ_TOOLS = ["get_invoice_status", "get_purchase_request_status", "get_ticket_status", "list_my_open_items"]
WRITE_TOOLS = ["send_mail", "create_catalog_request"]

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
