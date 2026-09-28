"""Direct MCP client used for deterministic calls (approved write actions, and a logged fallback when a small
local model fails to emit a tool call). The LLM-facing path uses Agent Framework's MCPStreamableHTTPTool."""
from __future__ import annotations

import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..config import settings


def parse_result(res) -> dict:
    if getattr(res, "structuredContent", None):
        sc = res.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    text = "".join(getattr(c, "text", "") for c in res.content)
    try:
        return json.loads(text)
    except Exception:
        return {"error": text} if res.isError else {"text": text}


async def call_tool(name: str, args: dict, headers: dict) -> dict:
    async with streamablehttp_client(settings.mcp_url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(name, args)
            out = parse_result(res)
            if res.isError and "error" not in out:
                out = {"error": str(out)}
            return out
