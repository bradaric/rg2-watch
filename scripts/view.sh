#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Rebuild the page from the DB and serve it on 127.0.0.1 (open the URL it prints). No RIK, no API.
# usage: scripts/view.sh <election-id> [extra serve args, e.g. --port 8771]
require_db
uv run rg2watch browse --parent "$ID" --db "$DB" "${CFG[@]}"
exec uv run rg2watch serve --db "$DB" "${CFG[@]}" "$@"
