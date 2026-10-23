# Analysis and evaluation

[README](../README.md) · [Operator runbook](runbook.md)

Configure paid Claude analysis or local Ollama models, request second opinions, and evaluate findings.
All commands run from the repository root. For operational recovery, see the [runbook](runbook.md#3-when-something-goes-wrong).

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
watcher's blocked pause. During `watch --analyze`, an uncached RIK PDF that answers HTTP 404 is also
retried every 120 s while that document URL remains active. These retries do not use analysis
attempts or produce D3; analysis resumes automatically if the PDF becomes available. A 404 breaks
the consecutive refused-download count, while 3 consecutive 401/403 responses stop downloads for
`blocked_pause_s`. You get one "[rg2-watch] PDF downloads blocked/unavailable since HH:MM" alert at
that pause, or after 10 download deferrals in a row across documents, and one when downloads work
again. A malformed or non-PDF response remains a permanent D3. This automatic 404 retry applies to
the watch pipeline, not one-off or batch analysis, and does not imply that a browser PDF link is
available yet.
After fixing a problem, put failed forms back in the queue:

    uv run rg2watch requeue [--parent <parent>]            # forms that used all 3 attempts (X2)
    uv run rg2watch requeue [--parent <parent>] --failed   # every failed form, also D3/X1
    uv run rg2watch requeue [--parent <parent>] --skipped  # also forms seen before analysis was on

On Ctrl-C/SIGTERM, `watch` waits up to `stop_wait_s` (default 80 s) for in-flight analyses so
their cost is recorded; anything still running is retried on the next start. The default fits
under systemd's default `TimeoutStopSec` of 90 s: with the defaults a shutdown takes at most
`stop_wait_s` + 5 s (status and page threads, together) + 3 s (alert delivery) = 88 s; raise
`TimeoutStopSec` together with `stop_wait_s`.
`--baseline` only silences D-rule alerts: already-published RG-2 forms are analysed too.

Analysis needs Anthropic credentials (`ANTHROPIC_API_KEY` or an ant profile). Without them
`watch --analyze` logs one warning and only watches; `watch --once` never analyses. Forms published
while analysis is off (after it was first enabled) stay queued and are analysed by the next run
with analysis on. Spend is recorded per call at the price of the model that answered (built-in table, USD per
MTok in/out, Anthropic list prices as of 20/10/2026: Haiku 5.5 0.10/0.50, Sonnet 5.5 2/10, Opus 5.5
4/20, `ollama:*` 0/0; also used for estimates and with `--model`; a server-side fallback answer is
priced by the responding model; cache reads at 5% of the input price). `price_in_per_mtok`/
`price_out_per_mtok` override the table only when set; a model outside the table is refused
without them and capped per election: `max_spend_usd` (default 60) is the sum of all
analyses and case-drafting calls of that election (`parent`) in this DB, so each election has its own budget; `eval` runs
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

For a focused second read of A1-A3 result numbers, use `--arithmetic-only`. This uses a separate
prompt/cache from full-form analysis, asks for every list row, and prints the three reconciliation
checks without changing the primary report or unofficial totals:

    scripts/second-opinion.sh 288966 --rule A2,A3 --arithmetic-only

The watcher can run these reads automatically with `arithmetic_recheck_enabled = true`. A2/A3
reports enter a persistent recheck queue. One local worker uses `arithmetic_local_model`; when 10
uncached local jobs are already queued/running (`arithmetic_local_queue_limit`), new jobs spill
directly to `arithmetic_sonnet_model`. Local reads that are incomplete, still fail A1-A3, or differ
in more than one value also go to Sonnet. A result changes the primary report only when it is
complete, differs in exactly one result value, agrees everywhere else and makes A1-A3 reconcile.
Otherwise the original report remains flagged for manual review. Raw reads and promotion provenance
are retained. Sonnet calls count against the election's `max_spend_usd`; when the cap is reached the
form remains for manual review. Recheck workers are separate from primary analysis workers, so slow
Ollama reads do not hold up Haiku.

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
