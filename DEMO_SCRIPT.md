# 5-minute demo script (record with QuickTime → File → New Screen Recording)

**Before recording:** `./scripts/start.sh`, then `make reset`. Open http://localhost:8000 full screen, and send one
warm-up question so the model is loaded. Close other heavy apps, because a 7B model uses about 6 GB of RAM.
Tip: record each scene separately and trim the waiting time out; model calls take 10–30 s locally.

---

**0:00 — Framing (15 s)**
"This is the critical path from my recommendation, running on my laptop with open-source models and the same
patterns as the Azure design: Microsoft Agent Framework workflows, MCP tools, permission-aware retrieval, typed
outputs, a human-approval step, tracing and evaluation."

**0:15 — Scene 1: the flagship journey (90 s)**
Persona **Sara (Finance)**. Click **① Invoice + draft (AR)**.
- Point at the **Grounded** and **Live system data** badges and the citations.
- *Evidence tab:* "The SAP lookup was a real tool call over MCP, made with Sara's on-behalf-of token, so SAP decides
  what she can see. The policy passages were retrieved with her groups."
- Point at **security-trimmed: LEG-PLB-007**: "The Legal-only playbook matched this question, but it was filtered out
  inside the search. It was never scored or sent to the model."
- Scroll to the **draft card**: "The workflow is now paused and checkpointed. This is the exact payload, with an
  idempotency key. Nothing has been sent." Edit one line, then click **Approve with my edits**.
- "It resumed from the checkpoint in a new workflow instance, as another replica would, and sent the email."

**1:45 — Scene 2: trace (30 s)**
*Trace tab:* "One trace per turn: gateway, router, status and knowledge in parallel, answer, groundedness check,
drafting, and the approval pause. Model calls are magenta and MCP tools teal. The same spans go to App Insights or
Aspire through OTLP."

**2:15 — Scene 3: permissions change with identity (45 s)**
Click **③ Restricted doc (Finance user)**: the answer comes from general sources, or says there is no approved source.
Click **③ Same question (Legal user)**: Layla gets the playbook answer with citations.
Click **④ Someone else's invoice** (as Sara): "SAP denies it and returns the same message as 'not found', so it
doesn't leak that the invoice exists." Show it in the *Audit* tab as **denied**.

**3:00 — Scene 4: attacks (45 s)**
Click **⑤ Prompt injection**: blocked at the gateway, and no model or tool was called.
Click **⑥ Poisoned document**: "A contractor-uploaded guide hides instructions to email invoices to an external
address. The document shield dropped that chunk (see Evidence). Even if it hadn't, write tools aren't exposed to the
model, need an approval record, and only allow internal recipients."

**3:45 — Scene 5: evaluation (45 s)**
Show `evals/reports/eval_report.md` (run `make eval` beforehand).
"These are the gates from my deck. The security gates are blocking and pass: 0 leaks across 152 checks and 0
successful attacks. The quality numbers are from a local 7B model; in production the same harness runs on gpt-5-mini
with the Foundry evaluators, as a release gate for every prompt, model or index change."

**4:30 — Close (30 s)**
"Going to Azure is configuration, not a rewrite. The model endpoint becomes Azure OpenAI, the index becomes Azure AI
Search with the same security filter, the shields become Content Safety, and the traces go to App Insights. The
optional setup script is in the repo."
