#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
election_id "$@"; shift
# Unofficial totals read by the model from the stored forms.
# usage: scripts/tally.sh <election-id> [extra tally args, e.g. --clean]
require_db
exec uv run rg2watch tally --parent "$ID" --db "$DB" "${CFG[@]}" "$@"
