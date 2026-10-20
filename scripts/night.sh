#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Election night: register if needed, then watch (with Claude analysis unless --watch-only).
# usage: scripts/night.sh <election-id> [--watch-only] [extra watch args]
analyze=(--analyze)
args=()
for a in "$@"; do
    if [[ $a == --watch-only ]]; then analyze=(); else args+=("$a"); fi
done
echo "page: http://127.0.0.1:8770 (serve_port in the config changes it)"
first=0; [[ -f $DB ]] || first=1
if [[ ${#analyze[@]} -gt 0 ]]; then claude_env; else ENVF=(); fi
register_if_needed
if [[ $first -eq 1 ]]; then
    echo "hint: starting late? add --baseline to record what is already published without alerts"
fi
exec uv run "${ENVF[@]}" rg2watch watch --parent "$ID" --db "$DB" "${CFG[@]}" "${analyze[@]}" "${args[@]}"
