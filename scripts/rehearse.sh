#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Rehearsal on a past election: full run with analysis, no "new document" alerts, exits when done.
# usage: scripts/rehearse.sh <election-id> [extra watch args]
claude_env
register_if_needed
exec uv run "${ENVF[@]}" rg2watch watch --parent "$ID" --db "$DB" "${CFG[@]}" \
    --analyze --baseline --until-done "$@"
