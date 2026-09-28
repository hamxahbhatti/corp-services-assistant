#!/usr/bin/env bash
# One-time setup on macOS (Apple silicon, 16 GB RAM recommended). Safe to re-run.
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
echo "Pulling models (≈6 GB the first time)…"
ollama pull qwen2.5:7b
ollama pull bge-m3
ollama create corp-qwen2.5 -f Modelfile

[ -f .env ] || cp .env.example .env
echo "Building the policy index…"
python -m app.knowledge.ingest
echo
echo "Setup complete. Start the demo with:  ./scripts/start.sh"
