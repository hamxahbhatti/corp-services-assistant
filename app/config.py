"""Central configuration. Everything is driven by environment variables (see .env.example).

LLM_PROVIDER
  ollama   - local models through Ollama's OpenAI-compatible API (default, free, runs on a laptop)
  azure    - Azure OpenAI / Foundry Models (v1 OpenAI-compatible endpoint)   [adapter included, untested]
  scripted - deterministic OpenAI-compatible test double (scripted_llm/server.py) for CI and offline replay

SEARCH_BACKEND
  local    - in-process hybrid index (BM25 + vectors + RRF) with security trimming (default)
  azure    - Azure AI Search hybrid + semantic ranker with a security filter   [adapter included, untested]
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "ollama"))
    # OpenAI-compatible endpoint (Ollama, scripted double, or Azure v1 endpoint)
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    chat_model: str = field(default_factory=lambda: _env("CHAT_MODEL", "qwen2.5:7b"))
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "bge-m3"))
    # Azure OpenAI specifics (only used when LLM_PROVIDER=azure)
    azure_openai_endpoint: str = field(default_factory=lambda: _env("AZURE_OPENAI_ENDPOINT"))
    azure_openai_api_key: str = field(default_factory=lambda: _env("AZURE_OPENAI_API_KEY"))

    search_backend: str = field(default_factory=lambda: _env("SEARCH_BACKEND", "local"))
    azure_search_endpoint: str = field(default_factory=lambda: _env("AZURE_SEARCH_ENDPOINT"))
    azure_search_key: str = field(default_factory=lambda: _env("AZURE_SEARCH_API_KEY"))
    azure_search_index: str = field(default_factory=lambda: _env("AZURE_SEARCH_INDEX", "corp-policies"))

    content_safety_endpoint: str = field(default_factory=lambda: _env("AZURE_CONTENT_SAFETY_ENDPOINT"))
    content_safety_key: str = field(default_factory=lambda: _env("AZURE_CONTENT_SAFETY_KEY"))

    mcp_url: str = field(default_factory=lambda: _env("ENTERPRISE_MCP_URL", "http://127.0.0.1:8765/mcp"))
    identity_secret: str = field(default_factory=lambda: _env("DEMO_IDENTITY_SECRET", "local-demo-only-change-me-32bytes!!"))
    top_k: int = field(default_factory=lambda: int(_env("RETRIEVAL_TOP_K", "6")))
    enable_judge: bool = field(default_factory=lambda: _env("ENABLE_JUDGE", "true").lower() != "false")
    groundedness_threshold: float = field(default_factory=lambda: float(_env("GROUNDEDNESS_THRESHOLD", "4")))
    internal_mail_domain: str = field(default_factory=lambda: _env("INTERNAL_MAIL_DOMAIN", "authority.example"))

    data_dir: Path = ROOT / "data"
    corpus_dir: Path = ROOT / "app" / "knowledge" / "corpus"

    @property
    def base_url(self) -> str:
        if self.llm_base_url:
            return self.llm_base_url
        if self.llm_provider == "ollama":
            return "http://localhost:11434/v1"
        if self.llm_provider == "scripted":
            return "http://127.0.0.1:8099/v1"
        if self.llm_provider == "azure":
            return self.azure_openai_endpoint.rstrip("/") + "/openai/v1/"
        raise ValueError(f"Unknown LLM_PROVIDER {self.llm_provider}")

    @property
    def api_key(self) -> str:
        if self.llm_provider == "azure":
            return self.azure_openai_api_key or self.llm_api_key
        return self.llm_api_key or "not-needed"


settings = Settings()
settings.data_dir.mkdir(exist_ok=True)
