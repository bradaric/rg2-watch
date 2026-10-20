#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Re-analyse the stored forms of an election with another model (default: local ollama:bonsai-2:27b).
# Ollama must be running for ollama:* models. A paid model (--model claude-...) needs --yes to confirm spend.
# usage: scripts/second-opinion.sh <election-id> [extra analyze args]
require_db
model=(--model ollama:bonsai-2:27b)
ENVF=()
for a in "$@"; do
    if [[ $a == --model || $a == --model=* ]]; then model=(); fi
done
if [[ ${#model[@]} -eq 0 && " $* " != *" --model ollama:"* && " $* " != *" --model=ollama:"* ]]; then
    claude_env
fi
exec uv run "${ENVF[@]}" rg2watch analyze --parent "$ID" --db "$DB" "${CFG[@]}" "${model[@]}" "$@"
