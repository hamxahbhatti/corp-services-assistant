"""Safety controls: prompt shields (user + documents), spotlighting, output checks.

Local mode uses transparent heuristics so the demo runs offline. With AZURE_CONTENT_SAFETY_* set,
the same functions call Azure AI Content Safety Prompt Shields (text:shieldPrompt) instead.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx

from .config import settings

_ATTACK = [
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|rules)", r"disregard (your|the) (instructions|system prompt)",
    r"you are now (in )?(developer|dan|jailbreak)", r"reveal (your|the) (system prompt|instructions)", r"system instruction:",
    r"act as (an? )?(unrestricted|unfiltered)", r"do anything now", r"override (the )?(safety|policy)",
    r"تجاهل (جميع |كل )?(التعليمات|الأوامر) السابقة", r"انس(َ)? (كل )?التعليمات", r"اكشف (لي )?(تعليمات النظام|موجه النظام)",
]
_ATTACK_RX = re.compile("|".join(_ATTACK), re.I)
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[\w-]+")
_EMIRATES_ID = re.compile(r"\b784-?\d{4}-?\d{7}-?\d\b")
_IBAN = re.compile(r"\bAE\d{2}\s?(\d{4}\s?){4}\d{3}\b")


@dataclass
class ShieldResult:
    attack: bool
    source: str  # "user" | "document"
    detail: str = ""
    engine: str = "heuristic"


def _azure_shield(user_prompt: str, documents: list[str]) -> dict | None:  # pragma: no cover - requires Azure
    if not (settings.content_safety_endpoint and settings.content_safety_key):
        return None
    r = httpx.post(settings.content_safety_endpoint.rstrip("/") + "/contentsafety/text:shieldPrompt?api-version=2024-09-01",
                   headers={"Ocp-Apim-Subscription-Key": settings.content_safety_key},
                   json={"userPrompt": user_prompt, "documents": documents}, timeout=10)
    r.raise_for_status()
    return r.json()


def shield_user_prompt(text: str) -> ShieldResult:
    az = _azure_shield(text, [])
    if az is not None:
        hit = az.get("userPromptAnalysis", {}).get("attackDetected", False)
        return ShieldResult(hit, "user", "Prompt Shields: user prompt attack" if hit else "", "azure-content-safety")
    m = _ATTACK_RX.search(text or "")
    return ShieldResult(bool(m), "user", m.group(0) if m else "")


def shield_documents(docs: list[str]) -> list[ShieldResult]:
    az = _azure_shield("", docs) if docs else None
    if az is not None:
        return [ShieldResult(d.get("attackDetected", False), "document", "", "azure-content-safety") for d in az.get("documentsAnalysis", [])]
    out = []
    for d in docs:
        m = _ATTACK_RX.search(d or "")
        out.append(ShieldResult(bool(m), "document", m.group(0) if m else ""))
    return out


def spotlight(i: int, hit) -> str:
    """Wrap untrusted retrieved content in explicit delimiters with provenance (spotlighting / delimiting)."""
    return (f'<source id="{i}" doc="{hit.doc_id}" section="{html.escape(hit.section)}" version="{hit.version}" lang="{hit.language}">\n'
            f"{hit.text}\n</source>")


def output_findings(text: str) -> list[str]:
    f = []
    ext = [e.group(0) for e in _EMAIL.finditer(text or "") if not e.group(0).lower().endswith("@" + settings.internal_mail_domain)]
    if ext:
        f.append(f"external email address in output: {', '.join(ext)}")
    if _EMIRATES_ID.search(text or ""):
        f.append("Emirates ID pattern in output")
    if _IBAN.search(text or ""):
        f.append("IBAN pattern in output")
    return f
