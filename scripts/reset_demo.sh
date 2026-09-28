#!/usr/bin/env bash
# Clears audit log, approvals, outbox and checkpoints before a recording.
cd "$(dirname "$0")/.."
rm -rf data/audit.jsonl data/approvals.jsonl data/outbox.jsonl data/checkpoints
echo "Demo state reset."
