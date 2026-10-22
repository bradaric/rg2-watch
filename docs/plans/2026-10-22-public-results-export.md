# Public Results Export Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export the current corrected unofficial tally as a safe, self-contained public presentation page that can be manually uploaded to Cloudflare Pages.

**Architecture:** Add a dedicated exporter that builds the tally, reduces it to aggregate public fields, joins current review verdicts into counts, and embeds the sanitized payload into one static HTML file. The export has no forms, document IDs or URLs, findings, notes, PDFs, database metadata, write controls, external assets, or network requests. A new CLI command writes the file atomically; the existing private browse page remains unchanged.

**Tech Stack:** Python 3.12, argparse, existing SQLite `Store`/tally code, self-contained HTML/CSS/vanilla JavaScript, pytest, Node for focused browser-logic checks where useful.

---

## File map

- Create `src/rg2watch/public_results.py`: sanitize tally/review data and render one HTML export.
- Create `src/rg2watch/static/public_results.html`: responsive public presentation template with only a municipality selector.
- Modify `src/rg2watch/cli.py`: add `export-results --parent --out` and dispatch it.
- Create `scripts/export.sh`: election-ID wrapper using the standard database/config conventions.
- Create `tests/test_public_results.py`: payload privacy, review counts, rendering safety, CLI, and standalone-file tests.
- Modify `docs/review-and-sharing.md`: document export and Cloudflare Pages manual upload/rollback workflow.
- Modify `README.md`: mention the public export without implying automatic publication.

## Chunk 1: Sanitized export data

### Task 1: Build the public payload

- [ ] Write failing tests that create analysed forms and reviews, then assert the public payload contains only election metadata, snapshot time, corrected aggregate rows, list labels/totals, warnings, and aggregate review counts.
- [ ] Assert review categories sum to the flagged count and use only the current active document version.
- [ ] Assert serialized output contains no source URL, station/document ID, station number, finding text/rules, review note, PDF path, database name, model name, or operator-correction history.
- [ ] Implement `build_public_results(store, cfg, parent)` using `build_tally`, `stored_model`, and `Store.latest_reviews`.
- [ ] Keep only corrected/all-counted totals; do not export filed, difference, or clean-only views.
- [ ] Run `uv run pytest -q tests/test_public_results.py` and confirm the payload tests pass.

## Chunk 2: Self-contained presentation page

### Task 2: Render one safe HTML file

- [ ] Write failing tests that render the payload and assert there is exactly one output file, embedded script-safe JSON, no `fetch`, remote URL, external script/stylesheet, form/document detail, or write control.
- [ ] Add `public_results.html` with the current hero visual language: unofficial disclaimer, election/snapshot title, turnout, station coverage, form/review summary, valid/invalid/unreconciled values, and top-list chart.
- [ ] Provide only the municipality selector. Always display corrected, all-counted results.
- [ ] Use DOM `textContent`, responsive light/dark colors, semantic labels, keyboard-accessible controls, and a restrictive no-network CSP suitable for one inline style/script document.
- [ ] Render by replacing one unique data placeholder with ASCII JSON that escapes `<`, and write atomically with mode 0644.
- [ ] Add focused Node/static assertions for national and municipal data, warnings, list labels, snapshot text, review categories, and safe handling of hostile strings.
- [ ] Run `uv run pytest -q tests/test_public_results.py`.

## Chunk 3: CLI and operator workflow

### Task 3: Add `export-results`

- [ ] Write failing CLI tests for explicit/default parent, explicit/default output directory, missing registered election, and successful replacement of an older export.
- [ ] Add `rg2watch export-results --parent ID --out public-results`; default `--out` is `public-results`, interpreted as a directory containing `index.html`.
- [ ] Print the resulting path, election title, and snapshot nature; do not upload or make network requests.
- [ ] Run the focused CLI tests.

### Task 4: Document manual Cloudflare Pages publication

- [ ] Document local preview by opening `public-results/index.html` and checking election name, municipality choices, timestamp, disclaimer, and totals.
- [ ] Document Cloudflare Pages Direct Upload: upload the `public-results` directory, use the stable `<project>.pages.dev` production URL, and use deployment history for rollback.
- [ ] State clearly that every update requires a fresh export and manual production upload, previous exports may be stale, and no private browse files should be uploaded.
- [ ] Mention optional custom domains without prescribing DNS changes.

## Chunk 4: Verification

### Task 5: Final checks

- [ ] Run `git diff --check`.
- [ ] Run `uv run pytest -q tests/test_public_results.py tests/test_tally.py tests/test_browse.py`.
- [ ] Run `env -u RG2W_LIVE_API uv run pytest -q` so no paid live API test can run.
- [ ] Inspect the generated sample HTML and final diff for private fields, external requests, unrelated edits, and accidental deployment credentials.
- [ ] Leave all implementation changes unstaged and uncommitted for user review unless the user separately asks to commit.
