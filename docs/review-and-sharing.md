# Review and sharing

[README](../README.md) · [Operator runbook](runbook.md)

Browse findings, record manual verdicts, inspect unofficial results, and optionally share an approved case.
All commands run from the repository root.

## Browse page and status panel

Open the URL printed by `watch` or `serve`. For an existing election database,
`scripts/view.sh <id>` rebuilds and serves the page without calling RIK or a model API.
See [File layout and local server](reference.md#file-layout-and-local-server) for paths and server restrictions.

- **Forms table** (DataTables + jQuery, vendored in `src/rg2watch/static/`, versions and sha256 in
  `VERSIONS.txt`; no CDN, no external requests): one row per form, sortable, paged (50-500 rows),
  with filters for election, municipality, model, severity, rule, result inclusion, replaced/removed,
  analysis failed, has local copy and "models disagree"; the filters survive a refresh (URL hash /
  saved state). The
  "+" opens older versions and the findings of every stored analysis model (second opinions from
  `analyze --parent`, current rules re-applied, no API); a HIGH that another model lacks is
  highlighted. The visible **Numbers** column checks A1-A5 on the currently counted values (model
  plus applicable RG-7/operator correction): green **Reconcile**, red failed rule codes, or muted
  **Unavailable** when required values/list rows cannot be checked. The separate "number check"
  filter selects those current states, nonzero turnout gaps, or A1-A5 individually. A nonzero
  `voted - valid - invalid` contribution is shown as **Gap ±N** in the Numbers column; sort that
  column to bring the largest gaps together. **Result inclusion** says whether list totals count with
  corrections and as filed, with the exact omission reason in its tooltip; its filter finds forms
  omitted from either view. The existing rule filter continues to select original stored findings for
  audit. Station, times and status are merged columns; a "columns" menu shows or hides any column.
  The Models column starts hidden. Columns "Local PDF" (only when cached under `pdfs/` next to the DB) and "RIK" open in
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
Viber or Instagram message. The operator clicks **Prepare draft**, edits the full Serbian Cyrillic
public summary, and separately enters a short, operator-approved Viber reason. The short reason is not
generated or shortened from the summary. After explicitly confirming that anyone with the link can
open the folder, the operator clicks **Publish**. Only that case folder is made public. The app-managed
**RG2 Watch** parent remains private.

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
downloads a replacement from RIK. It freezes the operator-approved UTF-8 summary, separately approved
short Viber reason, and exact PDF bytes locally before creating anything remotely. A retry must use
those frozen inputs and staging file; changed/missing inputs are refused. A pending publication created
before the short-reason field existed keeps its original frozen summary/PDF and legacy Viber layout;
its retry does not ask for or add a reason. There is no replace/abort workflow.

The folder contains only `документ.pdf` and a plain-text case explanation with election,
municipality/station, exact document URL, RIK link, the approved cautious concern, and SHA-256 of the
PDF bytes. Uploads are verified while the folder remains restricted; only then is that individual
folder shared as `anyone` / `reader`. If an upload/share is interrupted, explicitly click Publish
again with the same text: preallocated Drive IDs prevent a second case. Verify the result in Drive's
**Share / General access** panel: “Anyone with the link — Viewer”, and open both child files in a
private browser window. A live smoke test is always operator-initiated; automated tests use fakes and
never create public Drive files.

New cases produce this concise four-line copy, using the frozen municipality/station and existing
stored links:

```text
Могућа неправилност за проверу.
<општина>, БМ <број>: <одобрени кратак разлог>
Записник и опис: <existing case-folder URL>
РИК: <existing RIK URL>
```

### Troubleshooting

For the specific Књажевац БМ 61 correction and election 677084 recovery, see
[Historical case recovery](historical-case-recovery.md). Those procedures are not general setup steps.

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

If **Prepare draft** falls back with the generic warning, inspect the terminal that launched
`rg2watch serve` or `rg2watch watch`. One `rg2watch.case_draft` WARNING records the provider
exception class, an integer HTTP status (or `unknown`), the raw error message, and the full chained
traceback. This is local stderr, not browser output or a persistent debug file. After installing a
diagnostics release, restart the local server and explicitly click **Prepare draft** once to reproduce
the failure; there is no automatic AI call. Raw errors and tracebacks may contain sensitive request or
provider details: inspect and redact them before sharing. For a safe initial report, share the
`stage=... exception=... status=...` fields and only the minimal relevant traceback lines after
redaction. Do not indiscriminately paste full traces, job JSON, provider payloads, URLs, prompts,
notes, credentials, headers, or response bodies.

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
of stations known). Applied RG-7 and operator-entered corrections replace those readings in the
corrected view; the as-filed view preserves the original RG-2 readings. RIK's official results are
never fetched. Flagged forms are left out of the clean
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

### Public results snapshot

Create a standalone public presentation page from the newest registered election (or select one with
`--parent`):

```sh
scripts/export.sh <id>
```

Extra arguments pass through, for example `scripts/export.sh <id> --out another-directory`. The
equivalent direct command is `uv run rg2watch export-results --parent <id> --db
data/election-<id>.sqlite --out public-results`.

The command writes only `public-results/index.html`. Open it locally before publishing and verify the
election title, snapshot time, municipality choices, totals and disclaimer. The page contains corrected,
all-counted aggregate values and aggregate review counts, with only a municipality selector. It contains
no form/document identifiers or links, findings, review notes, PDFs, database name, model name, audit
history, write controls, external assets or API/data requests. For safety, the command refuses an output
directory containing anything except an older `index.html`; do not point it at the private browse-page
directory.

For a manual Cloudflare Pages Direct Upload, create a Pages project and upload the `public-results`
directory. Cloudflare provides a stable production URL such as `https://rg2-rezultati.pages.dev/` and a
unique URL for each deployment; its deployment history can roll back a bad upload. A custom domain is
optional. Every publication is manual: run `export-results` again, inspect the local page, then upload that
directory as a new production deployment. The browser reloads the published page once per minute so a
new deployment appears without user action; the timestamp identifies the static unofficial snapshot.
Never upload the database's private page
folder, `data.json`, `reviews.json`, PDFs, SQLite file, configuration, or credentials.

#### Optional scheduled Cloudflare Pages updates

Automatic publication removes the manual inspection gate: every successful deployment immediately
changes the public site. Set it up only after manually exporting, inspecting and deploying one snapshot.
Disable it during prompt-version migrations, bulk reanalysis or operator-correction review; otherwise a
valid but temporary intermediate tally may become public. Cloudflare deployment limits also apply. Since
the snapshot time changes on every export, even unchanged totals produce a new deployment.

Use one guarded job for export and deployment, rather than separate cron jobs. This prevents a failed
export from being followed by an upload and `flock` prevents overlapping runs. Install Cloudflare's
`wrangler` CLI, create the Pages project, and confirm a manual deployment first. Give its API token only
the account/project permissions needed to deploy Pages; never put the token in the repository or directly
in the crontab.

Store the noninteractive credentials outside the repository:

```sh
install -d -m 700 ~/.config/rg2-watch
install -m 600 /dev/null ~/.config/rg2-watch/cloudflare.env
$EDITOR ~/.config/rg2-watch/cloudflare.env
```

The file should contain only the values required by Wrangler:

```sh
CLOUDFLARE_ACCOUNT_ID=replace-me
CLOUDFLARE_API_TOKEN=replace-me
```

Create a private wrapper such as `~/.local/bin/rg2watch-publish-results` and replace the three
placeholders. Use the absolute Wrangler path printed by `command -v wrangler`; cron may not have the
interactive shell's `PATH`.

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT=/home/replace-me/rg2-watch
ELECTION=replace-me
PROJECT=replace-me
WRANGLER=/absolute/path/to/wrangler

exec 9>"$ROOT/data/public-results-publish.lock"
flock -n 9 || exit 0

set -a
source "$HOME/.config/rg2-watch/cloudflare.env"
set +a

cd "$ROOT"
scripts/export.sh "$ELECTION" --out "$ROOT/public-results"
"$WRANGLER" pages deploy "$ROOT/public-results" --project-name "$PROJECT"
```

Make it executable, run it once manually, inspect the production URL, and check Cloudflare's deployment
history before scheduling it:

```sh
chmod 700 ~/.local/bin/rg2watch-publish-results
~/.local/bin/rg2watch-publish-results
```

For example, run every 15 minutes and keep a private local log:

```cron
*/15 * * * * /home/replace-me/.local/bin/rg2watch-publish-results >>/home/replace-me/rg2-watch/data/public-results-publish.log 2>&1
```

After adding the entry with `crontab -e`, verify both the log and the public snapshot time. On failure,
disable the cron entry first, inspect the local export and log, and use Cloudflare Pages deployment history
to roll back if necessary. Token rotation, project changes and resuming after bulk reanalysis should always
be tested with one manual wrapper run before cron is re-enabled.

## Operator-entered numeric verification and corrections

An analysed, active first-round RG-2 can have audited operator-entered values when a model reading
needs confirmation or correction and no RG-7 is required. Expand the form and choose **Verify /
correct values**. An unchanged snapshot is shown in history as **values verified**; changed numbers
are shown as **numbers corrected**. The dedicated dialog is outside the results table, so automatic
data/review refreshes do not replace it or interrupt typing. An unsaved draft is also retained in browser
`sessionStorage` for that database and exact document URL across an accidental page reload.

The dialog starts with the current model-plus-RG-7 values and requires a complete snapshot:
registered, voted, ballots in box, invalid, valid, outside voters, outside confirmations, and every
expected list row. It also requires a reason. A save is rejected unless all values are non-negative
whole numbers and A1-A5 reconcile: box <= voted, invalid + valid = box, list sum = valid, voted <=
registered, and outside voters <= confirmations. Correction and review are separate actions: saving
numbers does not set `ok`, `escalate`, or `unsure`.

History is append-only. **Open / revise** stores another complete snapshot; **Revoke correction**
records a revocation and returns the tally to its model/RG-7 source without deleting history. Each
entry belongs to the exact document URL, so a replacement RG-2 starts without a correction. A source
or correction revision token prevents a stale dialog or concurrent operator from overwriting newer
work. If the analysis changes or a new RG-7 appears (even before that decision is analysed), the
operator snapshot is suspended and is not counted until every value is reviewed and saved as a new
revision.

An active snapshot is applied after RG-7, then the rules are rerun. Corrected V1/V12 marks and resolved
A1-A5 arithmetic no longer exclude the form from clean-only totals. Original model readings and
findings remain visible for audit, and unrelated HIGH findings or HIGH rules produced by the corrected
values can still keep the form flagged. The results selectors and CLI corrected view include both
RG-7 and active operator-entered corrections; the as-filed view includes neither.

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
