"""Model clients. One code path for Ollama, Azure OpenAI (v1 endpoint) and the scripted test double:
all three expose the OpenAI-compatible Chat Completions + Embeddings APIs."""
from __future__ import annotations

from functools import lru_cache

from openai import AsyncOpenAI, OpenAI

from agent_framework.openai import OpenAIChatCompletionClient

from .config import settings


_async_clients: dict[int, AsyncOpenAI] = {}


def _async_openai() -> AsyncOpenAI:
    """One async client per event loop (tests/CLI create several loops; the web server has one)."""
    import asyncio
    try:
        key = id(asyncio.get_running_loop())
    except RuntimeError:
        key = 0
    if key not in _async_clients:
        _async_clients[key] = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key, timeout=180, max_retries=1)
    return _async_clients[key]


@lru_cache(maxsize=1)
def _sync_openai() -> OpenAI:
    return OpenAI(base_url=settings.base_url, api_key=settings.api_key, timeout=180, max_retries=1)


def chat_client() -> OpenAIChatCompletionClient:
    """Agent Framework chat client. For Azure, CHAT_MODEL is the deployment name (e.g. gpt-5-mini)."""
    return OpenAIChatCompletionClient(model=settings.chat_model, async_client=_async_openai())


def embed(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    client = _sync_openai()
    for i in range(0, len(texts), 16):
        resp = client.embeddings.create(model=settings.embed_model, input=texts[i:i + 16])
        out.extend([d.embedding for d in resp.data])
    return out


async def aembed(texts: list[str]) -> list[list[float]]:
    resp = await _async_openai().embeddings.create(model=settings.embed_model, input=texts)
    return [d.embedding for d in resp.data]
