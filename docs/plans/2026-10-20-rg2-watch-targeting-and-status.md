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

## Order

A, then B, then C + D (C and D both touch browse.py; one agent). Each task: tests, full suite
green (`uv run pytest -q -W error`), one commit, then review.
