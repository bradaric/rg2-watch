# Sourced by the scripts: validates the election ID and sets ID, DB, CFG and (via claude_env) ENVF.
# Call from the repo root as: election_id "$@"; shift
election_id() {
    NAME=$(basename "$0")
    if [[ $# -lt 1 || ! $1 =~ ^[0-9]+$ ]]; then
        echo "usage: scripts/$NAME <election-id> [extra args]   (election-id: digits, e.g. 677087)" >&2
        exit 2
    fi
    ID=$1
    DB="data/election-$ID.sqlite"
    if [[ -f rg2watch.toml ]]; then
        CFG=(--config rg2watch.toml)
    else
        CFG=()
        echo "note: no rg2watch.toml, using built-in defaults"
    fi
}

# For commands that call Claude: load the API key from .env if present.
claude_env() {
    if [[ -f .env ]]; then
        ENVF=(--env-file .env)
    else
        ENVF=()
        echo "warning: no .env, so no Anthropic key: analysis will be off" >&2
    fi
}

# Register the election unless the DB already has it (never contacts RIK otherwise).
register_if_needed() {
    if [[ -f $DB ]] && uv run python -c \
        'import sys; from rg2watch.store import Store; sys.exit(0 if Store(sys.argv[1]).get_election(sys.argv[2]) else 1)' \
        "$DB" "$ID"; then
        return 0
    fi
    uv run rg2watch add "https://www.rik.parlament.gov.rs/zapisnici/$ID" --db "$DB" "${CFG[@]}"
}

require_db() {
    if [[ ! -f $DB ]]; then
        echo "no database $DB (run scripts/night.sh or scripts/rehearse.sh $ID first)" >&2
        exit 1
    fi
}
