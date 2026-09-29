#!/usr/bin/env bash
# One-time setup on macOS (Apple silicon, 16 GB RAM recommended). Safe to re-run.
# Reuses models you already have in Ollama and only downloads what is missing.
#   CHAT_BASE=qwen2.5:latest ./scripts/setup_mac.sh   # force a specific base chat model
set -euo pipefail
cd "$(dirname "$0")/.."

PY=""
for c in python3.12 python3.13 python3.11; do command -v $c >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -z "$PY" ]; then
  echo "Python 3.11+ not found. Install it with:  brew install python@3.12   (then re-run this script)"; exit 1
fi
echo "Using $PY"
[ -d .venv ] || $PY -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama not found. Install it with:  brew install ollama   (or download from https://ollama.com/download)"; exit 1
fi
if ! curl -s http://localhost:11434/api/tags >/dev/null; then
  echo "Starting Ollama in the background…"; (ollama serve >/tmp/ollama.log 2>&1 &); sleep 4
fi

have() { ollama list | awk 'NR>1{print $1}' | grep -qx "$1"; }

# Chat model: qwen2.5 7B. "qwen2.5:latest" and "qwen2.5:7b" are the same model, so reuse whichever exists.
BASE="${CHAT_BASE:-}"
if [ -z "$BASE" ]; then
  if have "qwen2.5:7b"; then BASE="qwen2.5:7b"
  elif have "qwen2.5:latest"; then BASE="qwen2.5:latest"
  else echo "Pulling qwen2.5:7b (~4.7 GB)…"; ollama pull qwen2.5:7b; BASE="qwen2.5:7b"; fi
fi
echo "Chat model base: $BASE"

# Embedding model: bge-m3 is multilingual (Arabic + English). nomic-embed-text is English-only and
# would break Arabic retrieval, so it is not used.
if have "bge-m3:latest" || have "bge-m3"; then echo "Embedding model: bge-m3 (already installed)"
else echo "Pulling bge-m3 (~1.2 GB, multilingual embeddings)…"; ollama pull bge-m3; fi

# Local model with an 8k context window (Ollama's OpenAI endpoint cannot set num_ctx per request).
sed "s|^FROM .*|FROM ${BASE}|" Modelfile > /tmp/corp-assistant.Modelfile
ollama create corp-qwen2.5 -f /tmp/corp-assistant.Modelfile >/dev/null
echo "Created corp-qwen2.5 from $BASE"

[ -f .env ] || cp .env.example .env
echo "Building the policy index…"
python -m app.knowledge.ingest
echo
echo "Checking the models…"
python scripts/doctor.py || true
echo
echo "Setup complete. Start the demo with:  ./scripts/start.sh"
