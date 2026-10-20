# rg2-watch: targeted runs and live status

Four additions requested on 20/10/2026, for election night and follow-up checks.
Builds on the browse page (docs/plans/2026-10-20-rg2-watch-browse-page.md).

## Task A: municipality filter for `watch`

- `watch --only "<name>[,<name>...]"` and config `only_municipalities = []` (CLI wins).
- Names match the stored municipality list case-insensitively, as a substring, in Cyrillic or
  Serbian Latin (transliterate Latin to Cyrillic before matching, incl. lj, nj, dž digraphs and
  č ć š ž đ). Exact match wins over substring. No match or an ambiguous match: exit 2 and print the
  candidates. Bootstrap (municipality list) runs first if the list is empty.
- With a filter, every lane only spends requests on those municipalities: probe, watchlist,
  missing, safety and rare-type sweeps use `filters[municipality]` per selected municipality
  instead of global listings; global count probes are replaced by per-municipality counts.
  Documents outside the set are neither recorded nor alerted. Removal checks stay correct
  (a document is not "removed" because it is outside the filter).
- Log the resolved set at start. Analysis then naturally covers only those forms.
- Without the option, behaviour is unchanged (existing tests must still pass unmodified).

## Task B: batch re-analysis and second opinions

- `analyze` gains a batch mode selecting stored, active RG-2 documents of a registered election:
  `analyze --parent P [--municipality NAME] [--station NO] [--rule R[,R]] [--severity LOW|MEDIUM|HIGH]
  [--state replaced|removed|current] [--limit N] [--model M] [--yes] [--max-usd X]`.
  `--rule`/`--severity` select by the findings already stored (any model). NAME matches like Task A.
- Before spending: print the number of forms and a cost estimate (same estimator as `eval`);
  require `--yes` for a paid model (not for `ollama:`). Stop when the batch spend reaches
  `--max-usd` (default `max_spend_usd`). Batch and single `analyze` spend stay under `adhoc`.
- Skip forms that already have an analysis from the same model and prompt version
  (`latest_report`); print them as cached and re-apply the rules for free.
- Polite: downloads through the download limiter; one form at a time is fine.
- Output per form: municipality, station, model, cost, findings; summary at the end.
- Results are stored per model (existing `analyses` table). No alerts are pushed for second
  opinions.

## Task C: second opinions on the browse page

- For each form, show findings from every stored analysis model next to the watch findings
  (re-apply the current rules to each stored report; pure, no API). Mark which model produced
  each, and highlight disagreement (a HIGH from one model that another lacks).
- Filter: "model" and "models disagree".

## Task D: live status panel

- Top of `data/index.html`: what the watcher is doing and what it found, updated without a page
  reload so filters and scroll stay put.
- Mechanism (works on file://, where fetch() is blocked): the page loads `status.js` and
  `data.js` via `<script src>`; it re-injects `status.js?t=<now>` every 10 s and `data.js` every
  `browse_interval_s`. Each file sets a global (`window.RG2_STATUS`, `window.RG2_DATA`).
  `index.html` itself is static. Strings still go to the DOM via textContent only.
- `watch` writes `status.js` every ~10 s (atomic write, monotonic clock, failures logged, never
  blocks detection) from an in-memory snapshot plus cheap DB counts:
  - watcher: running since, last tick, last successful RIK request, RIK health (ok / slow /
    blocked / blind since), current rate, requests this tick / budget, lanes and their last run,
    municipality filter in use
  - documents: RG-2 seen / expected stations (from counts), new in last 10 / 60 min, replaced,
    removed, rare types (RG-7/8/9, Примедба) seen
  - analysis: on/off, model, queued, running, done, failed, paused (why, since), spend vs cap,
    average seconds per form, estimated time to clear the queue
  - alerts: last 20 (time, severity, rule, municipality, station, summary), ntfy delivery state
  - stale marker: if `status.js` is older than 60 s, the page shows "watcher not running / stalled
    since HH:MM" prominently.
- `rg2watch browse` (no watcher) writes `data.js` and a status file saying "watcher not running",
  with the DB-derived parts filled.

## Table library (decided 20/10/2026)

The forms table uses DataTables 2.x + jQuery 3.7.x, vendored: exact versions downloaded once from
the official npm tarballs, sha256 recorded in `src/rg2watch/static/VERSIONS.txt`, committed under
`src/rg2watch/static/`, copied next to `index.html` on build (only when changed). No CDN, no
external requests from the page. `deferRender`, paging (default 100 rows), child rows for older
versions and second opinions, custom filters via `DataTable.ext.search`, `stateSave` for filters.
All data cells rendered with `DataTable.render.text()` (escaped); no HTML from data.

## Local web server (decided 20/10/2026)

- `watch` serves the page at `http://127.0.0.1:<serve_port>` (config `serve_port = 8770`,
  `0` = off; `--serve-port`). `rg2watch serve` does the same without watching.
- Binds 127.0.0.1 only. Serves an allow-list: `index.html`, `status.js`/`status.json`,
  `data.js`/`data.json`, the vendored static files, and `pdfs/<64 hex>.pdf`. Everything else 404
  (no directory listings, no DB, no `.env`, no path traversal). GET/HEAD only. Stdlib
  `http.server` (ThreadingHTTPServer) in a daemon thread; port in use: log a warning, keep watching.
- Over http the page polls `status.json`/`data.json` with fetch(); opened as a file it falls back
  to the script re-injection. Startup log prints the URL.

## Browse page review fixes (fold into C + D)

From the review of 09d953d:

- Build `data.js` / `status.js` in a background daemon thread, never in the watch loop; log build
  time. No build in `watch --once`.
- Debounce search (~200 ms); render at most 500 rows with "show more".
- No full-page reload (replaced by the script re-injection above); polling period comes from the
  data, 0 = off.
- `readHash`: reset absent keys to defaults.
- NULL `station_key`: group by document id instead.
- `json.dumps(..., ensure_ascii=True)` so a lone surrogate cannot break the build.
- Search, rule, severity and "with findings" filters include older versions' findings.
- Analysis state: distinguish "retrying" (failed with `analysis_next_at` set) from "failed".
- NULL municipality shown as "(unknown)", and selectable.
- `uploaded_at` is Serbian local time (Europe/Belgrade); store/show both columns consistently.
- Remove stale `*.tmp` from failed writes.
- Links (operator request 20/10/2026): two columns, "Local PDF" and "RIK", each sortable/filterable
  ("has local copy"); both open in a new tab (`target="_blank" rel="noopener noreferrer"`); the local
  link is a plain file link so right-click "copy link" gives a usable file:// URL. Same for older
  versions in child rows.

## Order

A, then B, then C + D (C and D both touch browse.py; one agent). Each task: tests, full suite
green (`uv run pytest -q -W error`), one commit, then review.

## Task E: unofficial count from the forms (decided 20/10/2026)

No fetching of RIK's official results (operator decision). Tally only what the analyses read.

- Per form, use the latest successful analysis of the watch model for the active version
  (second-opinion models do not feed the totals, but disagreement on any counted number marks
  the form).
- Totals overall and per municipality: registered, voted, ballots in box, invalid, valid,
  outside voted, turnout; votes per list row (10.5) and share of valid votes.
- Coverage: forms counted / stations known (stations table), per municipality.
- Excluded and listed separately: forms with confidence low, page quality poor/unreadable, any
  illegible counted number, a model disagreement on a counted number, or arithmetic findings
  (A1-A5). Totals are shown both "clean forms only" and "all forms".
- List names: optional per-election mapping `list_names = {"<parent>" = ["...", ...]}` in the
  config, filled by hand; default "List 1..N". Never read names from the scans.
- Page: a "Results" tab (DataTables) next to the forms table, labelled "unofficial - numbers read
  by a model from the scanned forms"; CSV download of the per-municipality table.
- `rg2watch tally [--parent P] [--municipality ...]` prints the same totals in the terminal.

## Follow-ups after C + D (cli.py / pipeline.py were busy)

- `_cmd_analyze_batch`: show `res["unrecorded"]` in the summary; validate `--max-usd` (positive,
  finite); warn at the gate when the estimate exceeds `--max-usd`.
- pipeline.py: store analyses under `stored_model(cfg.model)` with `responding_model` in the
  report (as batch.py/evaluate.py do since 706ccdf).

## Task F: tally scope by election type (operator, 20/10/2026)

Local elections have different lists in every municipality, so cross-municipality list totals are
meaningless; they only make sense for parliamentary and presidential elections.

- Scope per election: `national` (one ballot everywhere: list totals overall and per
  municipality) or `municipal` (each municipality its own ballot: list rows, modal row set and
  list names per municipality; no overall list totals).
- Auto from RIK's `property_election_type`: 2 = parliamentary -> national, 3 = local -> municipal
  (both seen in listings 779676 / 662405). Any other or unknown type -> municipal (never
  aggregate lists wrongly) with a note "unknown election type - set tally_scope". Config override
  `tally_scope = {"<parent>" = "national" | "municipal"}` (presidential type id not yet seen).
- `list_names`: `{"<parent>" = [...]}` for national, `{"<parent>" = {"<municipality>" = [...]}}`
  for municipal (validated; mismatch note per municipality).
- Overall row in municipal scope: turnout, coverage, forms, valid - no list columns.
- Page: municipal scope -> results table shows each municipality with its own list columns
  (per-municipality child table or a municipality picker for the list table); hero defaults to
  the municipality with most counted forms, "All municipalities" shows only turnout/coverage/
  forms/valid without bars. National scope unchanged.
- CLI `tally`: municipal scope prints a list table per municipality.
