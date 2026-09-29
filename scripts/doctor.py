"""Pre-demo check: is the model server up, are the models installed, and do they behave the way the app needs?

    make doctor          (or: python scripts/doctor.py)

Checks
  1. The model server answers and the configured chat and embedding models exist.
  2. The chat model returns valid JSON for a JSON-schema structured-output request (the router,
     answer, judge and draft all rely on this), and how long it takes.
  3. The embedding model is multilingual: an Arabic question must be closer to the English question
     with the same meaning than to an unrelated Arabic question.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.llm import _sync_openai, embed  # noqa: E402

OK, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def report(name: str, status: str, detail: str) -> None:
    results.append((name, status, detail))
    print(f"[{status}] {name}: {detail}")


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def check_server() -> bool:
    print(f"Provider: {settings.llm_provider} · endpoint: {settings.base_url}")
    print(f"Chat model: {settings.chat_model} · embedding model: {settings.embed_model}\n")
    if settings.llm_provider != "ollama":
        report("Model server", OK, "not Ollama; skipping the installed-model check")
        return True
    try:
        tags = httpx.get("http://localhost:11434/api/tags", timeout=5).json()
    except Exception as e:  # noqa: BLE001
        report("Model server", FAIL, f"Ollama is not answering on localhost:11434 ({e}). Run: ollama serve")
        return False
    names = {m["name"] for m in tags.get("models", [])}
    names |= {n.removesuffix(":latest") for n in names}
    ok = True
    for model, hint in [(settings.chat_model, "run ./scripts/setup_mac.sh (creates corp-qwen2.5)"),
                        (settings.embed_model, "run: ollama pull bge-m3")]:
        if model in names:
            report(f"Model installed: {model}", OK, "found")
        else:
            report(f"Model installed: {model}", FAIL, f"not found; {hint}")
            ok = False
    return ok


def check_structured_output() -> None:
    schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": ["policy_question", "invoice_status", "ticket_status", "other"]},
            "language": {"type": "string", "enum": ["ar", "en"]},
            "wants_draft": {"type": "boolean"},
        },
        "required": ["intent", "language", "wants_draft"],
        "additionalProperties": False,
    }
    q = "ما حالة الفاتورة 5100-2291؟ وإذا كانت متأخرة، جهّز لي رسالة متابعة."
    t0 = time.perf_counter()
    try:
        resp = _sync_openai().chat.completions.create(
            model=settings.chat_model,
            messages=[{"role": "system", "content": "Classify the employee request. Reply in JSON only."},
                      {"role": "user", "content": q}],
            response_format={"type": "json_schema", "json_schema": {"name": "intent", "schema": schema, "strict": True}},
            temperature=0,
        )
    except Exception as e:  # noqa: BLE001
        report("Structured output", FAIL, f"request failed: {e}")
        return
    dt = time.perf_counter() - t0
    text = resp.choices[0].message.content or ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        report("Structured output", FAIL, f"not valid JSON after {dt:.1f}s: {text[:120]!r}")
        return
    good = data.get("intent") == "invoice_status" and data.get("language") == "ar" and data.get("wants_draft") is True
    report("Structured output", OK if good else WARN,
           f"{data} in {dt:.1f}s (first call includes loading the model)" + ("" if good else " · valid JSON, but the classification is off"))


def check_embeddings() -> None:
    en = "How many days of annual leave am I entitled to?"
    ar_same = "كم عدد أيام الإجازة السنوية المستحقة لي؟"
    ar_other = "ما حالة تذكرة الحاسوب المحمول الخاصة بي؟"
    try:
        t0 = time.perf_counter()
        v_en, v_same, v_other = embed([en, ar_same, ar_other])
        dt = time.perf_counter() - t0
    except Exception as e:  # noqa: BLE001
        report("Multilingual embeddings", FAIL, f"embedding call failed: {e}")
        return
    same, other = cosine(v_en, v_same), cosine(v_en, v_other)
    status = OK if same - other > 0.05 else FAIL
    report("Multilingual embeddings", status,
           f"EN↔AR same meaning {same:.2f} vs unrelated {other:.2f} ({len(v_en)} dims, {dt:.1f}s)"
           + ("" if status == OK else " · this model does not match Arabic to English; use bge-m3"))


def main() -> int:
    if check_server():
        check_structured_output()
        check_embeddings()
    fails = [r for r in results if r[1] == FAIL]
    print("\nAll checks passed." if not fails else f"\n{len(fails)} check(s) failed; fix these before the demo.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
