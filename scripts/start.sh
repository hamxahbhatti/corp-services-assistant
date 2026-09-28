#!/usr/bin/env bash
# Starts the enterprise MCP tool server and the web app (and the scripted model double if LLM_PROVIDER=scripted).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] && source .venv/bin/activate
mkdir -p data
# load .env without overriding variables already set in the shell (e.g. `make offline`)
if [ -f .env ]; then
  while IFS='=' read -r k v; do
    [[ -z "$k" || "$k" =~ ^# ]] && continue; k="${k// /}"; v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"
    [ -z "${!k:-}" ] && export "$k=$v"
  done < .env
fi
PROVIDER="${LLM_PROVIDER:-ollama}"

if [ "$PROVIDER" = "ollama" ] && ! curl -s http://localhost:11434/api/tags >/dev/null; then
  echo "Starting Ollama…"; (ollama serve >/tmp/ollama.log 2>&1 &); sleep 4
fi
if [ "$PROVIDER" = "scripted" ]; then
  python -m scripted_llm.server > data/scripted_llm.log 2>&1 & echo $! > data/scripted.pid; sleep 2
fi
# (re)build the index if missing or built with a different embedding model
python - <<'PY'
import json, pathlib, subprocess, sys
from app.config import settings
p = settings.data_dir / "index.json"
if not p.exists() or json.loads(p.read_text()).get("embed_model") != settings.embed_model:
    subprocess.check_call([sys.executable, "-m", "app.knowledge.ingest"])
PY

python -m app.tools.enterprise_mcp > data/mcp.log 2>&1 & echo $! > data/mcp.pid
sleep 2
echo "Warming up the model (first call loads it into memory)…"
python - <<'PY' || true
from app.llm import embed; embed(["warm up"])
PY
echo "Open http://localhost:8000"
exec uvicorn app.server:app --host 127.0.0.1 --port 8000
