.PHONY: setup start stop ingest eval eval-quick test reset offline doctor
setup: ; ./scripts/setup_mac.sh
start: ; ./scripts/start.sh
stop: ; ./scripts/stop.sh
doctor: ; . .venv/bin/activate && python scripts/doctor.py
ingest: ; . .venv/bin/activate && python -m app.knowledge.ingest
eval: ; . .venv/bin/activate && python -m evals.run_evals
eval-quick: ; . .venv/bin/activate && python -m evals.run_evals --quick
test: ; . .venv/bin/activate && python -m pytest -q tests
reset: ; ./scripts/reset_demo.sh
# offline replay with the scripted test double (no AI model needed)
offline: ; LLM_PROVIDER=scripted CHAT_MODEL=scripted EMBED_MODEL=scripted ./scripts/start.sh
