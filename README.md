# rg2-watch

CLI that watches the Serbian electoral commission (RIK) site for new, replaced and removed
polling-station documents, with alerts for manual review.

- Track RG-2 forms, RG-7 correction decisions, RG-8/RG-9 and other election documents.
- Optionally analyse scans with Claude or a local vision model through Ollama.
- Browse findings, record review verdicts and inspect unofficial vote totals.
- Optionally prepare an approved case for sharing through Google Drive and copied Viber text.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).
Run commands from the repository root:

```sh
uv sync --locked
```

Optional configuration:

```sh
cp rg2watch.example.toml rg2watch.toml
```

The wrapper scripts below use `rg2watch.toml` when present. With the CLI directly, pass
`--config rg2watch.toml`. See the [example config](rg2watch.example.toml) for available settings.

## Quick start

Use the numeric election ID from RIK's `https://www.rik.parlament.gov.rs/zapisnici/<id>` URL.
Replace `<id>` in the commands below with that number.

Start watching without model analysis or API charges:

```sh
scripts/night.sh <id> --watch-only
```

The script registers the election if needed, stores state in `data/election-<id>.sqlite`,
and starts the watcher and local review page. Open the page URL printed in the terminal.
If starting late on an empty database, add `--baseline` to record existing documents silently.
Stop with Ctrl-C; run the same command again to resume.

In another terminal, check progress:

```sh
scripts/status.sh <id>
```

To browse stored results after stopping the watcher (no RIK or model API calls):

```sh
scripts/view.sh <id>
```

### Enable analysis and phone alerts

For Claude analysis, set `ANTHROPIC_API_KEY` in your shell or an ignored `.env` file.
The analysis scripts load `.env` when present. Configure `max_spend_usd` in `rg2watch.toml`, then run:

```sh
scripts/night.sh <id>
```

This enables **paid analysis**. Watching alone is free; local Ollama models are also supported.
For ntfy phone alerts, subscribe to an unguessable topic and set `RG2W_NTFY_TOPIC` in the
process environment (or `.env` for scripts that load it), or set `ntfy_topic` in the config.
See the [operator runbook](docs/runbook.md) for preparation and recovery, and
[analysis guide](docs/analysis.md) for models, budgets and backlog handling.

## Important behavior

- **Findings are leads for manual review, not conclusions.** Open the document and check it.
- State and review history persist in SQLite. Wrapper scripts use one database per election;
  direct CLI commands default to `data/rg2watch.sqlite` unless configured otherwise.
- `--baseline` silences alerts for documents already present on the first pass; it does not
  disable analysis charges. It is refused on an election database that already contains documents
  unless explicitly forced.
- The review server binds to localhost. `view.sh` needs an existing election database.
- Scan analysis sends rendered document images to the selected model provider. Treat API keys, ntfy topics and
  Google OAuth files as secrets; see [privacy details](docs/reference.md#privacy).

## Documentation

| Guide | Covers |
|---|---|
| [Operator runbook](docs/runbook.md) | Scripts, election night, alerts, troubleshooting and rehearsals |
| [Analysis and evaluation](docs/analysis.md) | Claude, Ollama, second opinions, budgets and labelled evaluation |
| [Review and sharing](docs/review-and-sharing.md) | Browse UI, verdicts, unofficial tally, RG-7 and Drive/Viber handoff |
| [Runtime reference](docs/reference.md) | File layout, server restrictions, throttling and privacy |
| [Historical case recovery](docs/historical-case-recovery.md) | Specific past-case corrections and recovery procedures |

For command options:

```sh
uv run rg2watch --help
uv run rg2watch watch --help
```

## Development

Run the test suite:

```sh
uv run pytest -q
```

The live API test is opt-in (`RG2W_LIVE_API=1`) and requires Anthropic credentials.
For a local watcher rehearsal, see [Replay](docs/runbook.md#replay-rehearsal-without-rik).
