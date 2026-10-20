# rg2-watch

CLI that watches the Serbian electoral commission (RIK) site on election night, detects new, replaced
and removed polling-station documents (RG-2, RG-9, RG-7, ...) and pushes alerts for manual review.

## Install

    uv sync

Requires Python 3.12+. Config is optional: copy `rg2watch.example.toml` to `rg2watch.toml` and pass
`--config rg2watch.toml`. `RG2W_NTFY_TOPIC` overrides `ntfy_topic`.

## Scripts

`scripts/*.sh` wrap the commands above for one election: the only required argument is the election ID
(digits); further arguments pass through to the main command. The DB is `data/election-<id>.sqlite`
(rename an older one: `mv data/rehearsal-677087.sqlite data/election-677087.sqlite`), the config is
`rg2watch.toml` if it exists, and `.env` is loaded only for commands that call Claude.

    scripts/night.sh <id> [--watch-only]   register if needed, watch --analyze (add --baseline if late)
    scripts/rehearse.sh <id>               watch --analyze --baseline --until-done
    scripts/view.sh <id> [serve args]      browse + serve, no RIK, no API
    scripts/second-opinion.sh <id> [args]  analyze --parent with ollama:bonsai-2:27b (or your --model)
    scripts/tally.sh <id> [args]           tally --parent
    scripts/status.sh <id>                 rg2watch status: counts, spend, review state, watcher running
    scripts/escalated.sh <id>              review --list escalated

## Election-night runbook

1. Before the polls close, register the election and bootstrap (municipality and station lists):

       uv run rg2watch add https://www.rik.parlament.gov.rs/zapisnici/<parent>
       uv run rg2watch watch --parent <parent> --once

2. Subscribe to your ntfy topic in the ntfy phone app (the topic name is the only secret: make it
   unguessable), then set `RG2W_NTFY_TOPIC=<topic>` (or `ntfy_topic` in the config). The `httpx`/`httpcore`
   loggers are held at WARNING (even with `-v`) so request URLs, which contain the topic, never reach the logs.
3. Start the live loop and leave it running:

       RG2W_NTFY_TOPIC=<topic> uv run rg2watch watch --parent <parent> --config rg2watch.toml --analyze

   `--analyze` turns on the paid Claude analysis of RG-2 forms (see below); leave it out to only watch.

   For rehearsals and audits of a past election add `--until-done` (not with `--once`): the watcher
   exits by itself when every lane has completed a full pass and, with analysis on, no RG-2 of the
   election (or of the `--only` municipalities) is queued, running or waiting for a retry. It then
   delivers the pending alerts (flushing the MEDIUM digest), writes the final page and status, stops
   the pipeline and prints a summary (documents by type, RG-2 analysed/failed/skipped, findings by
   severity, spend, elapsed time). If analysis makes no progress for 10 minutes (spend cap, key
   error, PDF downloads blocked, Anthropic overloaded, forms only waiting for a retry), or a lane
   cannot finish its first pass for 10 minutes (RIK blocked or down) it exits with code 1, saying why and how many
   forms remain. Without the flag nothing changes: the loop runs until Ctrl-C.

   HIGH findings (RG-8/RG-9 published, RG-2 replaced, with the old URL as "was: ...") are pushed
   immediately, MEDIUM in digests, LOW only printed. Individual HIGH pushes are capped at
   `high_push_max` (10) per `high_push_window_s` (300 s); the rest roll into one "[HIGH] N findings"
   digest (same compact format) sent when the window allows, so a busy night cannot trip ntfy's rate
   limits. Status alerts (RIK unreachable, analysis paused, spend cap) are separate, go first and are
   never held back by the finding backlog. If one sweep replaces more than `mass_replace_threshold`
   (20) documents, ONE "[rg2-watch] mass replacement: N documents replaced in one sweep (RIK
   re-upload?)" alert is pushed and those D2 findings go to the digest.
   Alerts are delivered, and RIK reachability checked, by their own thread every 5 s, so a slow
   RIK tick (10 minutes at a throttled rate) never holds them back.
   A second `watch` on the same database is refused (exit 2, `<db>.lock` holds the pid of the first). A digest is compact (under 3.5 KB): counts by rule, up
   to 10 detail lines without URLs, and "+K more - open the page, filter MEDIUM" with the page URL when
   serving (a single-finding digest keeps its RIK URL). Every finding is also printed (and flushed) to
   stdout. If ntfy is unreachable, findings stay un-alerted and pushes back off (5 s doubling to
   60 s) before being retried. Ctrl-C or SIGTERM stops cleanly; state is kept in the SQLite DB
   (`data/rg2watch.sqlite` by default, `--db` to change), so a restart resumes without re-alerting.

   If RIK answers 401/403 repeatedly, all lanes pause for `blocked_pause_s`. After `blind_alert_s`
   without a successful request you get one HIGH "[rg2-watch] RIK unreachable/blocking since HH:MM"
   push, and "[rg2-watch] RIK reachable again" on recovery.

   Starting mid-night on an empty DB? Add `--baseline`: each lane's first full pass records what is
   already published without alerting, prints one `[baseline] <type>: N document(s) recorded` line
   per document type, and later changes alert normally.
   On a DB that already holds documents of the election `--baseline` is refused (exit 2) unless
   `--force-baseline` is given; `scripts/rehearse.sh` only passes it on an empty DB.
   Only care about some places? `--only "Beograd,Нови Сад"` (or `only_municipalities` in the config;
   Cyrillic or Latin, case-insensitive, exact name beats substring) limits every lane to those
   municipalities: no global listings are requested, other documents are not recorded, and an
   unknown or ambiguous name exits 2 with the candidates. Per-municipality counts replace the global
   ones, so a larger set costs more requests per round.
4. Alerts are leads for manual review, not conclusions: open the PDF link and check it yourself.

## Browse page, status panel and local server

Each database has its own page folder, `<db dir>/<db stem>/` (`data/election-662405.sqlite` -> `data/election-662405/`: `index.html`, `data.*`, `status.*`, the vendored libraries); the PDF cache stays shared at `<db dir>/pdfs/`. Two elections can run side by side (`serve` on one DB, `watch` on another): they never write the same files, and each server only accepts review verdicts for its own DB. `status.json` and `data.json` carry the DB name and the page shows a red banner if they disagree. Old `index.html`, `data.*`, `status.*` and library files directly in `data/` are from the earlier shared layout and are safe to delete.

`watch` serves the page on `serve_port` (default 8770; `--serve-port`, `0` = off); if that port is busy it tries the next ones up to +10. The URL actually used is printed (`page: http://127.0.0.1:PORT`), recorded in `status.json` and shown by `rg2watch status`; open the URL it prints. `rg2watch serve [--port N]` serves the same files without
watching (same port fallback, prints the URL). The server binds 127.0.0.1 only, answers GET/HEAD, and serves a fixed allow-list
(`index.html`, `status.json`, `data.json`, `reviews.json` (live verdicts), the vendored libraries, `pdfs/<sha256>.pdf`; the `.js` twins are not served, and cross-site requests are refused); the DB,
`.env` and everything else are 404. No free port: a warning, and the watcher carries on. Writes are
limited to `POST /review` and the explicit case preparation/publication routes described below. They
are accepted only from the page itself - Host
127.0.0.1/localhost on the server's port, `Sec-Fetch-Site: same-origin` (or no such header and an
`Origin` equal to the server's), `Content-Type: application/json`, header `X-RG2W: 1` (a cross-site
form or fetch cannot send it without a preflight, which is never answered), body at most 4 KB, a
stored document URL, verdict `ok`/`escalate`/`unsure`, note at most 500 characters; anything else is
403/400 and nothing is written.

- **Forms table** (DataTables + jQuery, vendored in `src/rg2watch/static/`, versions and sha256 in
  `VERSIONS.txt`; no CDN, no external requests): one row per form, sortable, paged (50-500 rows),
  with filters for election, municipality, model, severity, rule, replaced/removed, analysis failed,
  has local copy and "models disagree"; the filters survive a refresh (URL hash / saved state). The
  "+" opens older versions and the findings of every stored analysis model (second opinions from
  `analyze --parent`, current rules re-applied, no API); a HIGH that another model lacks is
  highlighted. Station, times and status are merged columns; a "columns" menu shows or hides any column. Columns "Local PDF" (only when cached under `pdfs/` next to the DB) and "RIK" open in
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
  "watcher not running" (`--out` overrides the page location).

## Operator review verdicts

Each version (URL) of a form can be marked `ok` (false alarm, or a legitimate correction),
`escalate` or `unsure`, with an optional note (at most 500 characters). The history is kept in the
`reviews` table and the newest verdict counts; a replaced form's new version starts unreviewed. No
ntfy message is sent for a verdict.

- **Page** (served over http by `watch` or `serve`): open a row with the arrow, then OK / ESCALATE /
  UNSURE (the typed note goes with it). The verdict shows at once and is confirmed by the server
  (a refusal reverts it with the reason; the buttons wait while it is saving). The "Review" column
  shows the verdict, the "review" filter shows to review, escalated, unsure or ok, and the status
  panel counts them for the chosen (else the watched) election. "To review" is one rule everywhere
  (page, counter, `review --list todo`): the current version is active, has stored findings and no
  verdict; removed forms are not to review. An escalation never drops out: a form whose older
  version was escalated shows "older version escalated", stays under "escalated" and in the count,
  and its new version is still to review if it has findings. "Escalated CSV" downloads every
  escalated version (station, which version, RIK link, findings, note, time; formula cells
  escaped). Notes lose control characters. Opened as a file the page shows the verdicts of the last build,
  read-only.
- **CLI:** `rg2watch review <url> ok|escalate|unsure [--note TEXT]`, and
  `rg2watch review --list escalated|ok|unsure|todo [--parent P]` (per URL; a version that was
  since replaced or removed is marked "(superseded)" / "(removed)").
- **Eval labels:** `rg2watch labels --from-reviews --out <new.jsonl> [--parent P]` writes label
  suggestions in the `tests/golden/labels.jsonl` format: `ok` -> the rules that fired are `false`,
  `escalate` -> `true`, `"verified": false`, the verdict and note in `note`, plus the document `url`.
  The file must not exist yet (nothing is overwritten; with nothing to suggest no file is written).
  `escalate` marks every rule that fired `true` and `ok` every one `false`: edit the suggestions
  before merging (a legitimate correction marked `ok` may still be a wanted alert, and an escalated
  form may have had one real and one spurious rule).

## Public case handoff (Google Drive and Viber copy)

This is an optional, manual handoff for one escalated document version at a time. It never sends a
Viber or Instagram message. The operator clicks **Prepare draft**, edits the Serbian Cyrillic public
summary, explicitly confirms that anyone with the link can open the folder, then clicks **Publish**.
Only that case folder is made public. The app-managed **RG2 Watch** parent remains private.

### One-time Google setup

1. In a Google Cloud project enable the Drive API, configure its OAuth consent screen, and create an
   OAuth client of type **Desktop app**. Download the client JSON into an ignored local location such
   as `data/google-oauth-client.json` (never commit it).
2. Configure paths (both `data/` and `rg2watch.toml` are ignored):

       drive_client_secrets = "data/google-oauth-client.json"
       drive_token_path = "data/drive-token.json"

3. Authorize the operator's own Google account in advance:

       uv run rg2watch drive-auth --config rg2watch.toml

   This is the only command that opens the installed-app OAuth browser. It requests only
   `https://www.googleapis.com/auth/drive.file` (files/folders this app creates) and saves the local
   token with mode 0600. `watch`, `serve`, and HTTP requests never start an OAuth flow. Missing or
   expired/revoked authorization disables publication without stopping the watcher or review page.
4. Explicitly create or verify the private app root, and open the printed link to inspect it:

       uv run rg2watch drive-root --config rg2watch.toml

   This creates one private **RG2 Watch** root per OAuth token and verified Google account. Root
   creation omits an explicit parent so Drive places the app-owned folder directly in My Drive; it
   does not inspect Google's built-in root item and needs no scope or config change beyond the existing
   `drive.file` grant. The app then reads that folder by its preallocated ID and records its observed
   sole parent for later verification. Every election database configured with that resolved token
   path shares it; there are no per-election parent folders. The token-adjacent
   `drive-token.json.root.json` contains only the preallocated root ID, creation phase, observed parent
   ID and account permission ID, never OAuth credentials. An interrupted setup reuses that ID and
   reconciles the same app-created folder rather than searching by name or creating another root. Do
   not share the root. `watch` and `serve` do not create it implicitly. Reauthorizing the same token
   path as a different Google account is refused before Drive file operations; recover the original
   account or investigate the state manually rather than allowing an automatic replacement root.

A new publication requires the exact URL's latest verdict to be `escalate` and its PDF already cached
under `<db dir>/pdfs/`. Manual review alone does not cache a PDF; analysis does. Publication never
downloads a replacement from RIK. It freezes the operator-approved UTF-8 summary and exact PDF bytes
locally before creating anything remotely. A retry must use that frozen text and staging file;
changed/missing bytes are refused, and this first version has no replace/abort workflow.

The folder contains only `документ.pdf` and a plain-text case explanation with election,
municipality/station, exact document URL, RIK link, the approved cautious concern, and SHA-256 of the
PDF bytes. Uploads are verified while the folder remains restricted; only then is that individual
folder shared as `anyone` / `reader`. If an upload/share is interrupted, explicitly click Publish
again with the same text: preallocated Drive IDs prevent a second case. Verify the result in Drive's
**Share / General access** panel: “Anyone with the link — Viewer”, and open both child files in a
private browser window. A live smoke test is always operator-initiated; automated tests use fakes and
never create public Drive files.

### One-time recovery of the pending election 677084 case

The previously attempted folder must be recovered manually; rg2watch never moves, adopts, deletes or
replaces it. Treat the report that it is empty and currently in My Drive as unverified until checking:

1. Run only `uv run rg2watch drive-root --config rg2watch.toml`, open its printed URL, and confirm
   **RG2 Watch** is directly under My Drive and restricted. If creation, identity, location or privacy
   verification fails, stop without moving or sharing anything. This retry does not move the existing
   pending case folder or publish/send a Viber message.
2. In the Google Drive UI, open the existing `RG2 предмет — Књажевац БМ 61` folder and inspect it for
   **any** files. An app-visible listing is not proof of emptiness. Stop and investigate if it is not
   empty.
3. Immediately after confirming it is empty, use the Drive UI to move that same folder into **RG2
   Watch**. Do not create a replacement. Confirm both the parent and case folder are still restricted.
4. Restart the local `serve`, open the pending case, verify that the frozen Serbian text is approved,
   explicitly confirm the warning, and click **Retry frozen publication**.
5. In Drive, confirm `My Drive / RG2 Watch / RG2 предмет — Књажевац БМ 61` is the same folder and now
   contains exactly the PDF and text file. The app checks the exact parent and original preallocated
   IDs; if the folder is still at My Drive root or elsewhere it stops with a manual-move instruction.
6. Before sending or pasting the Viber message, open the case link and both children in a logged-out
   private browser. The case folder alone must have “Anyone with the link — Viewer”; **RG2 Watch** must
   remain restricted. If Google policy blocks inherited child access, stop at the displayed error and
   do not send the link.

### Diagnosing a pending child-access check

An existing case folder and its two files may already be publicly accessible by link while the app's
publication row remains pending (`uploaded`) because the final child-access check failed. Do not send
the Viber text, and do not create, move, replace, or share any other file or folder while diagnosing
that state.

After integrating a diagnostics release, restart `serve` and first use the Drive UI to confirm that
**RG2 Watch** remains restricted and that the existing case folder, `документ.pdf`, and
`опис-предмета.txt` still match the approved frozen package. Only after those checks may the operator
explicitly authorize **one** **Retry frozen publication** action in the UI. Do not run that retry as an
automated or unattended test. The existing exact-ID reconciliation reuses the same public case folder
and children; it does not add direct child permissions or create another case.

Read the resulting INFO diagnostics in the terminal running `serve`. Lines sharing one
`diagnostic_id` show the publication checkpoints and the bounded `pdf` and `text` permission
type/role classifications. It is safe to report those sanitized lines. Do **not** paste OAuth files,
bearer tokens, client secrets, full HTTP errors, raw Drive JSON, PDF bytes, case text, document or
public links, or provider payloads into an issue or chat. The classifications are diagnostic evidence,
not a repair: any behavior change requires a separate root-cause diagnosis and separately approved
plan after the retry results are known.

Preparation sends only the document context and the internal review note (not the PDF, stored analysis
report, or personal names) to the configured Anthropic model, asking for cautious Cyrillic prose. It
runs only after an explicit click, never publishes model output automatically, and falls back to an
editable manual template when credentials/model/pricing/cap/API output are unavailable. Actual draft
usage is recorded in the same per-election `max_spend_usd` total as document analysis. As with the
existing analysis workers, simultaneous calls can exceed the cap slightly while already in flight.

The returned Drive URL and Viber text are copy/paste only. Anyone receiving or forwarding the URL can
open the case. A later correction or `ok`/`unsure` verdict does **not** revoke, replace, or silently
alter an already-public case; subsequent notification or correction management is manual and out of
scope. A completed case remains visible in the page with its original stable link/message.

Manual QA before election night: test a current and an older escalated version; keyboard-edit and copy
Cyrillic text; confirm missing-cache and missing-auth messages; observe that reviews remain responsive
during a slow fake/test upload; restart `serve` after an interrupted attempt and retry the same frozen
package; and confirm a completed case still displays after recording `ok`.

## Unofficial tally

`rg2watch tally [--parent P] [--municipality NAME,...] [--model M] [--clean]` and the "Results
(unofficial)" tab of the browse page total what the watch model (`model`, or `--model`) read from the
first-round RG-2 forms: registered, voted, ballots in box, invalid, valid, outside voted, turnout, and
votes and share of valid votes per list, per municipality and overall, with coverage (forms counted
of stations known). RIK's official results are never fetched. Flagged forms are left out of the clean
totals (`--clean` / the page switch "clean forms only (leave out flagged)" show only those; every counted form is shown by default) when the
confidence is low, the page quality is poor or unreadable, a counted number is illegible, another
stored model read a counted number differently, an arithmetic rule (A1-A5) fires, or the form has any
HIGH-severity alert (any rule, e.g. "HIGH alert: V1, V9"; the rules are re-applied to the stored
report, so document-level D-rule findings do not count; MEDIUM and LOW alerts such as P3 remarks or
V4 signatures never flag); the reasons are listed, with a link to the form. List names are never read from the scans: set
`[list_names]` in the config, default "List N". The page offers a
CSV download of the per-municipality table (cells starting with = + - @ are escaped); in municipal
scope it is long format, one row per municipality and list with that municipality's totals.

Scope: list totals only make sense where one ballot is used everywhere. RIK's election type 2
(parliamentary) is `national`: list votes overall and per municipality, `"<parent>" = ["Name 1", ...]`.
Type 3 (local) is `municipal`: every municipality has its own ballot, so the modal list-row set, names
(`"<parent>" = {"Municipality" = ["Name 1", ...]}`, Latin or Cyrillic) and list votes are per
municipality, and the overall row has no list columns; `tally` prints one list table per
municipality. The page follows the same rule: list votes open per municipality in the results table,
the CSV and the presentation view never total lists across municipalities (the hero starts on the
municipality with most counted forms; "All municipalities" shows turnout, stations, forms and valid
votes only). Forms without a municipality ("(unknown)") get shared totals only, no list totals, as
they may come from different ballots. Two `list_names` keys naming the same municipality (Latin and
Cyrillic) are a config error. Any other or unknown type (also an election without a stored type) is treated as
municipal with the warning "unknown election type - set tally_scope"; override with
`tally_scope = {"<parent>" = "national"}` (top-level key, before any `[table]`).

## RG-7 correction decisions

RG-7-1..4 and the repeat-vote 23-26 ("Решење о исправљању записника") change numbers in a station's
RG-2. With analysis on they are analysed like RG-2 forms (same limiter, workers, spend cap and
retries) with their own prompt and schema (`RG7_PROMPT_VERSION` r1; no names: list votes by row
number only). Decisions already stored by an older run and never analysed are queued automatically
at the next `watch --analyze` / `--until-done` (also `scripts/rehearse.sh <id>` on an existing DB).
Rules: R1 HIGH (a result item 10.1-10.4 or list votes changed) or MEDIUM (only other items), R2
MEDIUM (the decision's old number differs from our RG-2 reading; checked when the RG-7 is analysed
and again when the station's RG-2 is analysed later), R3 LOW (no changes read). The tally applies
the corrections of each counted RG-2 in decision order (latest wins, a decision's table is final),
re-runs A1-A5 on the corrected numbers and keeps the RG-2's visual alerts; the CLI marks corrected
municipalities with `*` and lists them, the page lists them too. Station rows show "RG-7 dd/mm:
10.2 180->178, ..." (links in the child row) and have the filter "corrected by RG-7".
Decisions are ordered by their parsed decision date (14.10.2026 or "9. јуна 2025."), else by upload
time, then id; the page shows that date as dd/mm/yyyy. A correction is applied only when the
decision was read with confidence above low and its old numbers agree with the RG-2 reading (or
with the previous decision's new value); otherwise the form keeps its RG-2 numbers and is flagged
"unverified RG-7 correction". A correction never fills an RG-2 field that was unreadable unless the
decision's own table gives it, and a null new number changes nothing. R2 is stored once per decision
(the findings table keeps one row per document and rule): it is not retracted or updated when the
RG-2 is later re-read. Other models' tallies apply the watch model's corrections. The page filter
"corrected by RG-7" and the badges show decisions read by the watch model; others show "read by
<model>" or "not read yet". RG-7 decisions are kept out of the RG-2 backlog, skipped and analysis
counts; the status panel has its own line "RG-7 decisions: read N / pending M".
Single file: `rg2watch analyze <rg7.pdf> --doc-type rg7` prints the changes.

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
An explicit case-draft click sends document context and the operator's review note to Anthropic; the
operator must edit/approve the returned public text before a separate Publish click. Google OAuth
client secrets and tokens stay in ignored local files and are never put into page data, responses, or
logs.

## Tests

    uv run pytest -q
