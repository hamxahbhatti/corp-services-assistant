"""Tests run the real stack (Agent Framework workflow, MCP tool server, retrieval) against the scripted model double."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.update({"LLM_PROVIDER": "scripted", "CHAT_MODEL": "scripted", "EMBED_MODEL": "scripted", "SEARCH_BACKEND": "local"})


def _up(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session", autouse=True)
def servers():
    procs = []
    for mod, port in (("scripted_llm.server", 8099), ("app.tools.enterprise_mcp", 8765)):
        if not _up(port):
            procs.append(subprocess.Popen([sys.executable, "-m", mod], cwd=ROOT, env=os.environ.copy(),
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for _ in range(50):
        if _up(8099) and _up(8765):
            break
        time.sleep(0.2)
    from app.knowledge.ingest import build_local_index
    build_local_index()
    yield
    for p in procs:
        p.terminate()
