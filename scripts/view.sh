#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Serve promptly on 127.0.0.1; forms bootstrap immediately and a tally snapshot builds in background.
# usage: scripts/view.sh <election-id> [extra serve args, e.g. --port 8771]
require_db
exec uv run rg2watch serve --db "$DB" "${CFG[@]}" "$@"
