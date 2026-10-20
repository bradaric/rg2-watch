#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Forms you marked ESCALATE in the review.
# usage: scripts/escalated.sh <election-id>
require_db
exec uv run rg2watch review --list escalated --parent "$ID" --db "$DB" "${CFG[@]}" "$@"
