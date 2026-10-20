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

       RG2W_NTFY_TOPIC=<topic> uv run rg2watch watch --parent <parent> --config rg2watch.toml --analyze

   `--analyze` turns on the paid Claude analysis of RG-2 forms (see below); leave it out to only watch.

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
   Only care about some places? `--only "Beograd,Нови Сад"` (or `only_municipalities` in the config;
   Cyrillic or Latin, case-insensitive, exact name beats substring) limits every lane to those
   municipalities: no global listings are requested, other documents are not recorded, and an
   unknown or ambiguous name exits 2 with the candidates. Per-municipality counts replace the global
   ones, so a larger set costs more requests per round.
4. Alerts are leads for manual review, not conclusions: open the PDF link and check it yourself.

## Browse page, status panel and local server

`watch` serves the page at `http://127.0.0.1:8770` (the URL is logged at start; `serve_port` in the
config or `--serve-port`, `0` = off). `rg2watch serve [--port N]` serves the same files without
watching. The server binds 127.0.0.1 only, answers GET/HEAD, and serves a fixed allow-list
(`index.html`, `status.js[on]`, `data.js[on]`, the vendored libraries, `pdfs/<sha256>.pdf`); the DB,
`.env` and everything else are 404. Port taken: a warning, and the watcher carries on.

- **Forms table** (DataTables + jQuery, vendored in `src/rg2watch/static/`, versions and sha256 in
  `VERSIONS.txt`; no CDN, no external requests): one row per form, sortable, paged (50-500 rows),
  with filters for election, municipality, model, severity, rule, replaced/removed, analysis failed,
  has local copy and "models disagree"; the filters survive a refresh (URL hash / saved state). The
  "+" opens older versions and the findings of every stored analysis model (second opinions from
  `analyze --parent`, current rules re-applied, no API); a HIGH that another model lacks is
  highlighted. Columns "Local PDF" (only when cached under `pdfs/` next to the page) and "RIK" open in
  a new tab. Times are Serbian local time, DD/MM/YYYY HH:MM.
- **Status panel** (top of the page, updates in place, nothing reloads): watcher (running since, last
  tick, last RIK success, RIK health ok/slow/blocked/blind, rate, requests per tick, lanes, municipality
  filter), documents (RG-2 seen of expected stations, new in 10/60 min, replaced, removed, rare types),
  analysis (model, queued/running/done/failed/retrying, pause reason, spend vs cap, seconds per form,
  time to clear the queue), the last 20 alerts and ntfy delivery. A red banner "watcher not running /
  stalled since HH:MM" appears when the status is older than 60 s.
- **How it updates:** `watch` writes `status.js/json` every 10 s and `data.js/json` every
  `browse_interval_s` (0 = off) from daemon threads, atomically; failures are logged and never touch
  the watch loop. `watch --once` builds nothing. Over http the page polls the `.json` files; opened as
  a file it re-injects the `.js` files. `rg2watch browse` writes everything once with the status
  "watcher not running".

## Visual analysis of RG-2 forms

Analysis is opt-in because it calls a paid API: `watch` analyses only with `--analyze` or
`analysis_enabled = true` in the config (`--no-analyze` overrides the config). At start it logs the
spend cap, what this election has spent so far, how many forms are queued and how many older forms
are not analysed.

Only forms first seen after analysis was first enabled for an election in this DB are analysed
(the moment is stored per election). Forms recorded earlier, for example by a `watch` run without
`--analyze` or a DB from before analysis existed, are marked skipped and cost nothing; add
`--analyze-backlog` (together with `--analyze`) to analyse them too, or, once analysis has been
enabled for that election, queue them with `rg2watch requeue --skipped`. With `--baseline` on a fresh DB and `--analyze`, the forms recorded by
the baseline pass are seen after enabling and are analysed.

While analysis is on, every newly published RG-2 (and repeat-vote RG-2: types 6/10/15/16) is
downloaded through the same rate limiter, rendered into page tiles and checked by Claude vision for
corrections, missing signatures, a different pen/hand in the results block, arithmetic errors and
procedural flags. This happens in background worker threads (`analysis_workers`, default 4), so the
watcher keeps sweeping. Findings (rules V*, P*, A*) are stored and alerted like the D-rule findings.
Documents that cannot be processed give a LOW `D3` (not a PDF / unreadable), a LOW `X1` (model
refused) or, after 3 failed attempts with 60 s / 120 s backoff (e.g. output that is not valid
JSON), a LOW `X2`. When Anthropic is busy after the SDK's own retries (429, 5xx, 529 overloaded,
connection errors or timeouts, also when they arrive mid-stream), the form is retried after 120 s
without using an attempt; after 5 such failures in a row you get one "[rg2-watch] analysis delayed:
Anthropic overloaded since HH:MM" alert and one "[rg2-watch] analysis no longer delayed" when a form
goes through again. Each document URL is analysed once; a replaced file is a new URL and is
analysed again.

Errors that would fail every form (invalid key, no permission, unknown model) pause all analysis
for `analysis_pause_s` (default 600 s) with one "[rg2-watch] analysis paused: ..." alert; the form
that hit it keeps its attempts. A rejected request (HTTP 400) counts against that form only,
unless 3 different forms in a row are rejected (e.g. no credit left), which pauses globally too.
After the pause one probe form is tried (it uses an attempt, so a form that keeps failing ends as
X2); when it works, analysis resumes ("[rg2-watch] analysis resumed"). While the pause keeps being
extended, "[rg2-watch] analysis still paused since HH:MM" is re-sent at most once an hour. When
RIK is busy or blocking a download (including a download that takes longer than 120 s in one
attempt), the form is retried after 120 s without using an attempt, and workers sit out the
watcher's blocked pause. When PDF downloads are refused (401/403) 3 times in a row, workers stop
downloading for `blocked_pause_s`. You get one "[rg2-watch] PDF downloads blocked/unavailable since
HH:MM" alert (at that pause, or after 10 deferrals in a row) and one when downloads work again.
After fixing a problem, put failed forms back in the queue:

    uv run rg2watch requeue [--parent <parent>]            # forms that used all 3 attempts (X2)
    uv run rg2watch requeue [--parent <parent>] --failed   # every failed form, also D3/X1
    uv run rg2watch requeue [--parent <parent>] --skipped  # also forms seen before analysis was on

On Ctrl-C/SIGTERM, `watch` waits up to `stop_wait_s` (default 80 s) for in-flight analyses so
their cost is recorded; anything still running is retried on the next start. The default fits
under systemd's default `TimeoutStopSec` of 90 s; raise both together if you change it.
`--baseline` only silences D-rule alerts: already-published RG-2 forms are analysed too.

Analysis needs Anthropic credentials (`ANTHROPIC_API_KEY` or an ant profile). Without them
`watch --analyze` logs one warning and only watches; `watch --once` never analyses. Forms published
while analysis is off (after it was first enabled) stay queued and are analysed by the next run
with analysis on. Spend is recorded per call (`price_in_per_mtok`/`price_out_per_mtok`, cache reads
at 5% of the input price) and capped per election: `max_spend_usd` (default 20) is the sum of all
analyses of that election (`parent`) in this DB, so each election has its own budget; `eval` runs
count under the election `eval` and `analyze` under `adhoc`. When the cap is reached analysis
pauses with one "[rg2-watch] spend cap reached" alert while the watcher keeps running. With several
workers busy, the cap can be overshot by up to one form per worker. PDFs are cached under
`<db dir>/pdfs`.

Check a single form by hand (local file, or a URL on the host of an election registered with `add`):

    uv run rg2watch analyze path/to/form.pdf [--json]
    uv run rg2watch analyze https://www.rik.parlament.gov.rs/extfile/... [--json]

It prints the cost, scan quality and findings (or, with `--json`, the full report and findings).
`analyze` exits 2 when credentials are missing. Its spend is recorded under `adhoc`, so it never
counts against an election's cap (and is not capped itself: each run is an explicit request).

`watch --once` bootstraps, runs every lane once (including a full safety sweep, ignoring the page
budget) and exits. `watch` also
accepts a zapisnici URL instead of `--parent`. `--base-url` overrides the RIK host; `-v` enables debug logs.

## Batch re-analysis (second opinions)

`analyze --parent P` (no source) re-analyses stored, active RG-2 forms of a registered election with
another model or prompt. Filters are ANDed: `--municipality NAME[,NAME]` (Cyrillic or Latin, like
`watch --only`), `--station NO[,NO]`, `--rule R[,R]` and `--severity LOW|MEDIUM|HIGH` (forms with a
stored finding, from any model, of that rule / at least that severity), `--state current|replaced|
removed` (as on the browse page; `removed` uses the newest version), `--limit N`.

    uv run rg2watch analyze --parent 662405 --municipality "Нови Сад" --severity MEDIUM --model claude-opus-5-5
    uv run rg2watch analyze --parent 662405 --rule V1,V2 --yes --max-usd 5
    uv run rg2watch analyze --parent 662405 --state removed --model ollama:qwen3.8:27b-96k

It prints the number of forms, how many are already analysed by this model and prompt version
(cached: rules are re-applied for free) and an estimate for the rest. A paid model needs `--yes`;
without it the estimate is printed and nothing is spent. `ollama:` models need no `--yes`. Forms
are downloaded one at a time through the download limiter. The batch stops at `--max-usd` (default
`max_spend_usd`); its spend is recorded under `adhoc`, linked to the document, and results are
stored per model. Busy API / Ollama: a form is retried twice with backoff, then skipped; an
authentication or permission error stops the batch. No alerts are sent.

## Local models

`model = "ollama:<name>"` in the config (or `--model ollama:<name>` on `eval`, `analyze` and `watch`)
sends the same tiles, prompt and JSON schema to a local Ollama (`ollama_url`, `ollama_num_ctx`,
`ollama_timeout_s`; defaults `http://localhost:11434`, 65536, 900). No API key is needed, nothing is
billed (cost 0), and `eval` needs no `--yes`. The model must support vision. If Ollama is down or
times out the form is deferred like an Anthropic overload, without using an attempt.

Stored analyses are kept per (document, model, prompt version): `eval --cached` only reuses reports
from the same model and prompt version, so Claude and local runs can share one DB and be compared
on the same golden set:

    uv run rg2watch eval --labels tests/golden/labels.jsonl --pdf-dir PDFS --model ollama:qwen3.8:27b-64k

## Evaluation

`rg2watch eval` analyses a set of hand-labelled local PDFs and reports, per rule, how often it fires
correctly (precision) and how many real problems it catches (recall).

Labels live in `tests/golden/labels.jsonl`, one JSON object per line (a scaffold for 70 forms,
every `expect` empty). For each form you have checked by eye:

1. Open the PDF named in `file` and look at it yourself.
2. Fill `expect` with the rule ids you can judge, each `true` (the problem is really there, so the
   rule should fire) or `false` (it is not there, so the rule should stay quiet), for example
   `"expect": {"V1": true, "V3": false, "A1": false}`. Rule ids are the V*, P* and A* rules of
   the findings. A rule left out of `expect` is not scored for that form. Forms with an empty
   `expect` are analysed but not scored.
3. Set `"verified": true` once you are sure of every value you entered. Unverified entries are
   still scored, but the run prints how many scored forms are not verified.

Run it (the PDFs are looked up by `file` name inside `--pdf-dir`):

    uv run rg2watch eval --labels tests/golden/labels.jsonl --pdf-dir data/raw662405 [--limit N]

This calls the Anthropic API, so it first prints an estimate (the real tile count of each PDF,
planned without rendering, x ~5k input tokens, plus ~10k output tokens per form since adaptive
thinking is billed as output; actual cost may differ) and the remaining cap, and refuses to run
without `--yes`. If the estimate exceeds the remaining cap it also refuses unless you add
`--force-over-cap`. `--limit` must be at least 1. Every `expect` value must be `true` or `false`;
otherwise the labels file is rejected with its file:line. It needs credentials like `analyze`
(exit 2 without). Each analysis is recorded in the DB under the `eval` election, which has its own
`max_spend_usd` budget; the run stops when the cap is reached.
`--cached` re-scores from analyses already stored in the DB and never calls the API, which is
how you re-check after changing a rule. The table and per-form findings are also written to
`data/eval/<timestamp>.json` (next to the DB). With no filled-in `expect` it prints
"no verified labels yet - fill tests/golden/labels.jsonl".

Calibration runbook (keep its spend apart from election-night data):

1. Use a dedicated DB, so its spend and analyses never mix with a live election:
   `--db data/calib.sqlite`.
2. Pilot on 5 forms first and check the printed estimate against the actual spend:

       uv run rg2watch eval --labels tests/golden/labels.jsonl --pdf-dir data/raw662405 \
           --db data/calib.sqlite --limit 5 --yes

3. Then the full run with a config that sets `max_spend_usd = 25`:

       uv run rg2watch eval --labels tests/golden/labels.jsonl --pdf-dir data/raw662405 \
           --db data/calib.sqlite --config calib.toml --yes

4. Re-score after rule changes with `--cached` (no API calls).

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
