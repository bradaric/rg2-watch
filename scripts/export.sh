#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Build one sanitized, standalone public snapshot. This script never uploads it.
# usage: scripts/export.sh <election-id> [extra export args, e.g. --out public-results]
require_db
uv run rg2watch export-results --parent "$ID" --db "$DB" "${CFG[@]}" "$@"
