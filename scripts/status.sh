#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Compact text status of an election from its DB (no RIK, no API).
# usage: scripts/status.sh <election-id>
require_db
exec uv run rg2watch status --parent "$ID" --db "$DB" "${CFG[@]}" "$@"
