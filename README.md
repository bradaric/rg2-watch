# rg2-watch

CLI that watches the Serbian electoral commission (RIK) site on election night, detects new, replaced
and removed polling-station documents (RG-2, RG-9, RG-7, ...) and pushes alerts for manual review.

## Install

    uv sync

Requires Python 3.12+. Config is optional: copy `rg2watch.example.toml` to `rg2watch.toml` and pass
`--config rg2watch.toml`. `RG2W_NTFY_TOPIC` overrides `ntfy_topic`.

## Election-night runbook

1. Before the polls close, register the election and bootstrap (municipality and station lists):

       uv run rg2watch add https://www.rik.parlament.gov.rs/zapisnici/<parent>
       uv run rg2watch watch --parent <parent> --once

2. Subscribe to your ntfy topic in the ntfy phone app (the topic name is the only secret: make it
   unguessable), then set `RG2W_NTFY_TOPIC=<topic>` (or `ntfy_topic` in the config).
3. Start the live loop and leave it running:

       RG2W_NTFY_TOPIC=<topic> uv run rg2watch watch --parent <parent> --config rg2watch.toml

   HIGH findings (RG-8/RG-9 published, RG-2 replaced) are pushed immediately, MEDIUM in digests, LOW
   only printed. Every finding is also printed to stdout. If ntfy is unreachable, findings stay
   un-alerted and are retried every tick. Ctrl-C stops cleanly; state is kept in the SQLite DB
   (`data/rg2watch.sqlite` by default, `--db` to change), so a restart resumes without re-alerting.
4. Alerts are leads for manual review, not conclusions: open the PDF link and check it yourself.

`watch --once` bootstraps, runs every lane once (including the safety sweep) and exits. `watch` also
accepts a zapisnici URL instead of `--parent`. `--base-url` overrides the RIK host; `-v` enables debug logs.

## Replay (rehearsal without RIK)

Download a past election once (polite default 1 request/s; PDFs optional):

    uv run rg2watch fixtures https://www.rik.parlament.gov.rs/zapisnici/662405 --out fixtures [--pdfs --limit-pdfs 50]

Re-enact it locally with a fake RIK server (documents appear along their original upload times,
`--speed` is simulated seconds per real second, `--faults` injects a replace, a removal and a synthetic RG-9):

    uv run rg2watch replay --fixtures fixtures/662405 --speed 600 --faults
    uv run rg2watch add http://127.0.0.1:8765/zapisnici/662405 --db /tmp/replay.sqlite
    uv run rg2watch watch --parent 662405 --base-url http://127.0.0.1:8765 --db /tmp/replay.sqlite

## Politeness

All lanes share one rate limiter (default 2 requests/s, `max_rps`) that halves on timeouts and 5xx
and recovers slowly. The watcher keeps running when RIK is overloaded. Never point `replay`, tests or
experiments at the live site beyond what an election night needs.

## Privacy

Only polling-station and municipality names, document types, timestamps and URLs are stored or sent.
No personal names are stored or included in alerts. Content fetched from RIK is treated as untrusted data.

## Tests

    uv run pytest -q
