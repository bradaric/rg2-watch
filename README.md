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

   HIGH findings (RG-8/RG-9 published, RG-2 replaced, with the old URL as "was: ...") are pushed
   immediately, MEDIUM in digests, LOW only printed. Every finding is also printed (and flushed) to
   stdout. If ntfy is unreachable, findings stay un-alerted and pushes back off (5 s doubling to
   60 s) before being retried. Ctrl-C or SIGTERM stops cleanly; state is kept in the SQLite DB
   (`data/rg2watch.sqlite` by default, `--db` to change), so a restart resumes without re-alerting.

   If RIK answers 401/403 repeatedly, all lanes pause for `blocked_pause_s`. After `blind_alert_s`
   without a successful request you get one HIGH "[rg2-watch] RIK unreachable/blocking since HH:MM"
   push, and "[rg2-watch] RIK reachable again" on recovery.

   Starting mid-night on an empty DB? Add `--baseline`: each lane's first full pass records what is
   already published without alerting, prints one `[baseline] <type>: N document(s) recorded` line
   per document type, and later changes alert normally.
4. Alerts are leads for manual review, not conclusions: open the PDF link and check it yourself.

## Visual analysis of RG-2 forms

While `watch` runs, every newly published RG-2 (and repeat-vote RG-2: types 6/10/15/16) is
downloaded through the same rate limiter, rendered into page tiles and checked by Claude vision for
corrections, missing signatures, a different pen/hand in the results block, arithmetic errors and
procedural flags. This happens in background worker threads (`analysis_workers`, default 4), so the
watcher keeps sweeping. Findings (rules V*, P*, A*) are stored and alerted like the D-rule findings.
Documents that cannot be processed give a LOW `D3` (not a PDF / unreadable), a LOW `X1` (model
refused) or, after 3 failed attempts with 60 s / 120 s backoff (Anthropic 429/5xx/overloaded/
connection errors), a LOW `X2`. Each document URL is analysed once; a replaced file is a new URL
and is analysed again.

Errors that would fail every form (invalid key, no permission, unknown model, invalid request)
pause all analysis for `analysis_pause_s` (default 600 s) with one "[rg2-watch] analysis paused:
..." alert; no attempts are used. After the pause one form is tried; when it works, analysis
resumes ("[rg2-watch] analysis resumed"). When RIK is busy or blocking a download, the form is
retried after 120 s without using an attempt, and workers sit out the watcher's blocked pause.
After fixing a problem, put failed forms back in the queue:

    uv run rg2watch requeue [--parent <parent>]            # forms that used all 3 attempts (X2)
    uv run rg2watch requeue [--parent <parent>] --failed   # every failed form, also D3/X1

On Ctrl-C/SIGTERM, `watch` waits up to `stop_wait_s` (default 120 s) for in-flight analyses so
their cost is recorded; anything still running is retried on the next start.
`--baseline` only silences D-rule alerts: already-published RG-2 forms are analysed too.

Analysis needs Anthropic credentials (`ANTHROPIC_API_KEY` or an ant profile). Without them `watch`
logs one warning and only watches; `--no-analyze` turns analysis off explicitly and `watch --once`
never analyses. Skipped forms stay queued in the DB and are analysed by the next run that has
analysis on. Spend is recorded per call (`price_in_per_mtok`/`price_out_per_mtok`); when it reaches
`max_spend_usd` (default 100) analysis pauses with one "[rg2-watch] spend cap reached" alert while
the watcher keeps running. With several workers busy, the cap can be overshot by up to one form per
worker. PDFs are cached under `<db dir>/pdfs`.

Check a single form by hand (local file, or a URL on the host of an election registered with `add`):

    uv run rg2watch analyze path/to/form.pdf [--json]
    uv run rg2watch analyze https://www.rik.parlament.gov.rs/extfile/... [--json]

It prints the cost, scan quality and findings (or, with `--json`, the full report and findings).
`analyze` exits 2 when credentials are missing. Its spend is not counted towards `max_spend_usd`.

`watch --once` bootstraps, runs every lane once (including a full safety sweep, ignoring the page
budget) and exits. `watch` also
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
and recovers slowly. PDF downloads for analysis use a separate limiter (`download_max_rps`, default
0.5/s, halved on 429/5xx/timeouts but not on slow transfers), so analysis never slows detection
and the combined load on RIK stays at most `max_rps + download_max_rps` (2.5 requests/s by default). The watcher keeps running when RIK is overloaded. All lane work is resumable
and charged to one per-tick request budget (`tick_request_budget`, default 40, minimum 5) in priority
order: rare-type sweeps, removal re-checks, watchlist, RG-2 probe, missing stations, safety sweep.
A share of the budget (`reserved_share`, default 0.25) is held back from watchlist and probe for the
safety sweep and the missing-station lane while they have work, so a busy probe cannot starve them.
Rare-type count checks are not charged and run every `rare_interval_s`, so a new RG-8/RG-9 is never
held up by a long probe round or sweep (RG-8/RG-9 sweeps go first); every `rare_resweep_interval_s` all non-empty rare types are
re-swept to catch replacements that leave counts unchanged. Never point `replay`, tests or
experiments at the live site beyond what an election night needs.

## Privacy

Only polling-station and municipality names, document types, timestamps and URLs are stored or sent.
No personal names are stored or included in alerts. Content fetched from RIK is treated as untrusted data.
Form scans are sent to the Anthropic API for analysis; the report schema has no name fields and the
prompt forbids transcribing names. Text inside the scans is treated as data, never as instructions.

## Tests

    uv run pytest -q
