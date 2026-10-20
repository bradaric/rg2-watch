#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Rehearsal on a past election: full run with analysis, no "new document" alerts, exits when done.
# usage: scripts/rehearse.sh <election-id> [extra watch args]
claude_env
register_if_needed
# --baseline only on a database without documents of this election (else watch refuses it)
BASELINE=(--baseline)
if uv run python -c 'import sqlite3, sys; sys.exit(0 if sqlite3.connect(sys.argv[1]).execute(
    "SELECT 1 FROM documents WHERE parent=? LIMIT 1", (sys.argv[2],)).fetchone() else 1)' "$DB" "$ID"; then
    BASELINE=()
    echo "note: $DB already has documents of $ID: not passing --baseline"
fi
exec uv run "${ENVF[@]}" rg2watch watch --parent "$ID" --db "$DB" "${CFG[@]}" \
    --analyze "${BASELINE[@]}" --until-done "$@"
