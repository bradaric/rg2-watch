# Operator Feed Conformance Closure Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the demonstrated stale-snapshot browser race and prove the production reset/merge/tally lifecycle through one integrated harness; retain broader evidence work as a non-blocking verification backlog.

**Architecture:** Every finding receives a deterministic failing reproduction through the real component boundary before implementation changes. Browser tests execute the actual asynchronous loader and DataTables state; SQLite migration tests use separate processes and observable transaction boundaries; bootstrap/feed tests use real pagination and HTTP serialization. Existing source election data remains immutable: projection migrations rebuild only derived operator-feed tables from disposable `documents` and `analyses` fixtures.

**Tech Stack:** Python 3.12, SQLite/WAL, pytest, multiprocessing, Node.js, available headless Chromium, vanilla JavaScript/DataTables, existing local HTTP server and benchmark harness.

---

**Baseline and safety:** Work on `dev` at/after `703a28319fbce68cfa556348710ace510478de3c`; do not create or use a worktree. Controlling prior plans are `docs/plans/2026-11-02-operator-feed-review-fixes.md` and `docs/plans/2026-11-02-operator-feed-conformance-followup.md`. Do not push, deploy, open/migrate the live election DB, restart the live watcher, or call live RIK/model services. Migration and benchmarks use disposable DBs only. Projection migration may drop/rebuild **only** `operator_model_winners`, `operator_model_counts`, their metadata and triggers; it must not delete/update source `documents`, `analyses`, reviews, findings, corrections or election data. Before any later live migration: stop here, require a verified backup and separate explicit authorization.

**Approved scope amendment (2026-11-02):** After repeated reviews expanded the release gate into exhaustive proof permutations, the operator approved a scope freeze. Plan conformance now requires **only Chunk 1**: (a) epoch-fence every snapshot-failure path, including 404, and (b) execute production `loadData`, `beginBootstrap`, `applyData`, `applyFeedGroups`, reset and DataTables-owned row state in one controlled asynchronous test. Chunks 2–4 are an explicitly deferred, non-blocking verification backlog; they are useful confidence work but absence of any listed permutation is not a conformance defect and does not imply a product bug. Previously completed disposable migration/paging/HTTP/feed/invalidation tests and benchmarks remain retained. Deployment remains separately blocked on operator approval, backup and an operational migration rehearsal.

**Required execution discipline:** Use @test-driven-development and @systematic-debugging. For each task: add the exact reproduction, run it and capture the expected failure, make the smallest implementation change, rerun focused tests, inspect the diff, then commit only that task. Source-string assertions and isolated helper tests do not satisfy a requirement for asynchronous, DataTables, process, HTTP or SQLite-snapshot behavior. If a requested deterministic harness is impossible with installed tooling, stop and report the blocker rather than substituting weaker evidence silently.

**File map:**
- `src/rg2watch/static/index.html`: tally-only failure recovery, real table-row updates, request/epoch lifecycle and reset handling.
- `src/rg2watch/analysis_selection.py`: shared canonical NULL/empty/named model ordering.
- `src/rg2watch/store.py`: NULL-safe projection triggers/migration and test hooks limited to injectable callbacks/fixtures where necessary.
- `src/rg2watch/operator_feed.py`: consistent bounded feed/bootstrap behavior exposed to deterministic interleavings.
- `src/rg2watch/server.py`: exact encoded response bounds and typed 413 path.
- `tests/operator_browser_harness.js` (create if extraction in `test_browse.py` becomes unwieldy): minimal fake fetch/DOM/DataTables environment that executes production loader functions.
- `tests/test_browse.py`, `tests/test_store.py`, `tests/test_operator_feed.py`, `tests/test_server.py`: real lifecycle, migration, paging, revision and protocol proofs.
- Existing domain tests (`tests/test_rg7.py`, `tests/test_type13.py`, `tests/test_operator_corrections.py`, classification/public export/security tests): invalidation and regression matrix.
- `scripts/benchmark_operator_impact.py`, `docs/benchmarks/2026-11-01-operator-feed.md`, `docs/runbook.md`: measured post-fix evidence and recovery documentation.

## Chunk 1: Fix and prove tally-only browser recovery

### Task 1: Reproduce all three tally invalidation defects through the real loader

**Files:** Modify/create the browser harness and `tests/test_browse.py`; do not change production code in this task.

- [ ] Build a Node harness that loads the relevant production script functions from `static/index.html` and supplies controlled `fetch`, promises, `$`, status element, `rows`, `D`, `T`, and a DataTables fake whose row objects are **independent owned objects**, not aliases of global `rows`. It must execute `loadData()`, `invalidateTally()`, `beginBootstrap()`, `applyData()` and `applyFeedGroups()`; calling only `snapshotDecision()` or checking source text is insufficient.
- [ ] Add a failing test: forms/feed are current with accepted snapshot ID `S`; either `data.json` or `data-meta.json` returns 404. Assert no form bootstrap request, no epoch/registry clear, actual DataTables-owned rows change tally-dependent fields to Updating, `T` becomes unavailable, status says tally stale, and feed polling remains possible. Current expected failure: 404 invokes `beginBootstrap(false)` or leaves stale table row data.
- [ ] Add a failing test for corrupt JSON, mismatched pair and HTTP 500 after forms are current; all use tally-only invalidation. Initial startup with no valid form table must bootstrap instead. A positively parsed generation/order incompatibility must reset forms. Current expected failure must be documented per case.
- [ ] Add a failing recovery test: after invalidating accepted snapshot `S`, the same valid pair/ID `S` becomes readable again. It must be accepted and restore tally without form bootstrap. Current expected failure: duplicate snapshot ID is rejected. Include one in-flight load plus one coalesced follow-up.
- [ ] Run only these tests with `uv run pytest -q tests/test_browse.py -k 'tally or snapshot or loader'`; preserve red output in the task report.

### Task 2: Correct tally invalidation and asynchronous recovery

**Files:** Modify `src/rg2watch/static/index.html`; test `tests/test_browse.py`.

- [ ] Make snapshot 404 and all untrusted/unparseable pair failures call one `invalidateTally(reason)` **only when valid forms/table exist**. Initial no-form startup calls bootstrap. Parsed generation/order mismatch remains a genuine form reset.
- [ ] `invalidateTally` must update DataTables-owned row data: either mutate each object returned by `this.data()` and call `this.data(updated)`/invalidate it, or replace row data through a tested DataTables API. Keep global `rows` and `D.rows` identical to what the table owns. Mark `rec`, `tally_state`, `correctable`, selection/result inclusion and other tally-dependent values Updating according to existing selective-row semantics; never retain stale inclusion as current.
- [ ] Clear accepted tally state sufficiently to allow the same snapshot ID to recover: retain form generation/order/revision registries, but reset `T`, `tallyRevision` and accepted snapshot ID/revision as tally state. Do not reduce `feedRevision`, clear rows, increment bootstrap epoch or invent tombstones.
- [ ] Fence failure callbacks with request epoch/sequence exactly as success callbacks: an old failed request cannot invalidate a newer accepted tally. Keep one request in flight and coalesce to at most one follow-up.
- [ ] Run the new harness tests, existing merge/reset tests, then `uv run pytest -q tests/test_browse.py tests/test_review.py tests/test_operator_corrections.py tests/test_type13.py tests/test_server.py`. Inspect that no source-string-only test substitutes for the real loader tests. Commit this fix separately.

## Deferred verification backlog (non-blocking; not part of plan conformance)

The following original Chunks 2–4 are retained as future verification ideas and historical context.
They are not implementation requirements under the approved scope amendment and must not be used as
an open-ended release gate. Promote an item into a new cleared plan only when a concrete defect,
operational risk, or explicit operator request justifies it.

### Original Chunk 2: Complete projection and process migration proof

### Task 3: Complete NULL/empty/named projection mutation parity

**Files:** Modify `tests/test_store.py`, `tests/test_operator_feed.py`, `tests/test_browse.py`; modify `analysis_selection.py`/`store.py`/`operator_feed.py` only for reproduced defects.

- [ ] Add a parameterized parity matrix comparing `operator_model_counts(parent)` to legacy `operator_model_coverage(parent, PROMPT_VERSION)` after each mutation: direct insert; status/result/prompt/model update; NULL→named, named→NULL and NULL→NULL update; document-ID move; analysis delete; selected-analysis replacement; document parent/type change; document delete; two parents; repeated Store open. Include multiple equal-coverage `None`, `""` and named models in different insertion/SQLite row orders.
- [ ] Test the trigger path where an analysis UPDATE executes both old and new winner picks, asserting one tagged winner and exact count. Test failed migration/backfill rollback: projection tables/marker/triggers remain at the old committed state and source `documents`/`analyses` hashes/counts are unchanged.
- [ ] In separate processes/connections, assert full builder and selective builder choose the same model and compute the same signature. Assert canonical order is configured preferred first, then descending coverage, then `""`, named strings lexicographically, then `None`; query rows are deterministically ordered by tagged key. Trace normal count/order reads and assert no election-wide analysis/document scan.
- [ ] Run the matrix before production edits and record any red case. Fix only observed parity/ordering defects, using the shared comparator and tagged projection keys. Run `uv run pytest -q tests/test_store.py tests/test_operator_feed.py tests/test_browse.py` and commit separately.

### Task 4: Prove migration with two processes and committed-state visibility

**Files:** Modify `tests/test_store.py`; minimally modify `store.py` only if the proof fails.

- [ ] Create a top-level multiprocessing worker/helper (spawn-safe, no monkeypatch dependence crossing processes) and disposable old-projection DB. Add a test hook or fixture-controlled callback immediately **after `BEGIN IMMEDIATE` and in-transaction marker read**, before rebuild; production default is no-op. Process A pauses there through an IPC Event/Pipe. Process B attempts Store initialization and blocks/retries according to SQLite policy. A read-only observer concurrently sees the complete old committed projection, never empty/partial new counts.
- [ ] Count migration rebuild entry/commit through IPC or a disposable audit side channel outside the DB under migration; after releasing A, assert exactly one committed rebuild, B rechecks marker and does not rebuild, both report identical marker/counts/version-dependent trigger SQL, and source data is unchanged. Use bounded process joins and `finally` terminate/cleanup; no sleep-based ordering.
- [ ] Add failed-A case: inject failure after projection rebuild but before marker/commit; observer continues to see old committed state, A rolls back, B acquires lock and performs one correct rebuild, final counts/triggers match legacy query, source tables unchanged.
- [ ] Run each process test repeatedly (e.g. `pytest ... --count` only if plugin exists; otherwise parameterize 3 iterations) and the full Store suite. If current SQLite timeout behavior fails, make the smallest explicit busy-timeout/retry change and test it. Replace neither proof with the existing pre-migration thread barrier. Commit separately.

### Original Chunk 3: Real bootstrap, HTTP and feed races

### Task 5: Complete 102-group deterministic paging matrix

**Files:** Modify `tests/test_operator_feed.py`; production feed only if tests fail.

- [ ] Build a disposable parent with at least 102 real tagged groups and no monkeypatch of `BOOTSTRAP_ROWS`. Read page 1 at `R`; use events/hooks at defined read boundaries for cases: review in a page-2 group, insertion sorting before cursor, deletion after cursor, station→document and document→station move, write while page read snapshot is pinned, and pruning floor past `R`.
- [ ] Drain every bootstrap cursor and then feed replay from `R`. Compare final tagged groups/rows/tombstones to a fresh full selective read. Assert no post-`R` contents carry revision `R`, no unchanged group is skipped, no duplicate survives, insert-before-cursor and deletion are recovered by replay, and floor overrun visibly resets. Assert the Store read transaction is closed between page requests (writer/checkpoint can proceed).
- [ ] Run tests red-first. Correct only actual cursor/skip/replay defects; preserve short read snapshots and signed generation/order cursor. Run `uv run pytest -q tests/test_operator_feed.py` and commit separately.

### Task 6: Exercise exact real HTTP response boundaries

**Files:** Modify `tests/test_server.py`, possibly shared fixtures; production `operator_feed.py`/`server.py` only if red.

- [ ] Use a real `Store` + `OperatorFeed` + local test server and real `/operator-bootstrap`, not a fake feed raising directly. Generate a single group with historical data/report content and binary-search/pad content so the entire `json.dumps(..., ensure_ascii=True).encode()` envelope is immediately below/above `BOOTSTRAP_BYTES` and `OPERATOR_MAX_BYTES`. Include quotes/backslashes and non-ASCII so escaped wire size is tested.
- [ ] Assert: soft-limit-crossing single group below hard limit arrives whole; a group not fitting after earlier groups is left for the next cursor without advancement/loss; immediately-under-hard response is 200 and no larger than cap; immediately-over-hard returns typed 413 without report contents; repeated request terminates rather than hidden retry. Exercise browser terminal 413 through Task 1 harness. Preserve Host/Sec-Fetch/CSP/gzip/HEAD protections.
- [ ] Run red-first boundary tests then `uv run pytest -q tests/test_server.py tests/test_operator_feed.py`. Fix only measured envelope/handler issues and commit separately.

### Task 7: Prove pinned and pre-pinned feed revision interleavings

**Files:** Modify `tests/test_operator_feed.py`, `tests/test_browse.py`; production code only for red defects.

- [ ] Case A: pause after a SQLite feed read view is pinned and selected through `T`; commit same-group `T+1` from another Store. Response must contain pre-write state/revision from its snapshot with `through=T`; next poll from `T` delivers `T+1`.
- [ ] Case B: commit `T+1` before the feed read view is pinned while bounded selected journal chunk ends at `T`; response may return current row/tombstone revision `T+1` but `through=T`; replay is idempotent. Cover row update, row→tombstone, tombstone→re-created row and analysis replacement. Use events/hooks, not sleeps.
- [ ] Through the real browser/DataTables harness, apply stale row after newer tombstone, stale tombstone after newer row, equal-revision duplicates, revision-11 recreation, an older full snapshot, and actual `table.rows().remove()`. Registry persists across normal redraw/snapshot and clears only on genuine epoch reset.
- [ ] Run red-first tests and focused feed/browser suites; fix only demonstrated defects and commit separately.

### Original Chunk 4: Complete invalidation and end-to-end conformance evidence

### Task 8: Build the full invalidation matrix without redundant fixtures

**Files:** Modify `tests/test_store.py`, `tests/test_operator_feed.py` and existing domain suites.

- [ ] Inventory existing tests in a table in the test/commit notes. For each original requirement, cite an existing assertion or add a parameterized case: document add/replacement/removal/reassignment; station/document tagged identity in both directions; two NULL-station documents; analysis status/report/model/prompt; finding insert/update/delete; review; correction/revocation/target; type-13 source/old/new target; classification; RG-7/8/9 linked RG-2; two parents; aborted transaction; pruning/restart; non-visible `mark_alerted`, spend and status clocks emit nothing.
- [ ] Each case asserts exact old/new tagged groups, revision movement/no movement, through semantics, tombstone revision and selective/full non-tally parity. Use direct SQL where supported writers may use it. Do not duplicate already adequate tests just to increase counts.
- [ ] Add deterministic simultaneous analysis completion + review, full-build read transaction + feed update, config/order change without journal write, and two independent browser tab contexts. Assert no repeated reset for ordinary primary completions, real order change resets, no stale forms/tally shown current.
- [ ] Run domain-focused suites: `uv run pytest -q tests/test_store.py tests/test_operator_feed.py tests/test_rg7.py tests/test_type13.py tests/test_operator_corrections.py tests/test_tally.py tests/test_server.py tests/test_browse.py`. Fix only red conformance defects and commit matrix/fixes separately.

### Task 9: Public export, migration startup and benchmark closure

**Files:** Existing public export/security/migration tests, benchmark script/doc, runbook.

- [ ] Run public-results offline/file export and privacy fixtures and static allowlist/security tests; assert operator HTTP-only changes do not alter standalone public export. Start watch/serve against a disposable pre-projection DB and prove migration, bootstrap and background tally startup without live services.
- [ ] Re-run the election-sized disposable benchmark with 0/1/10/53 changed groups, active fake analysis writes, full snapshots, browser redraw and watcher contention. Add NULL-model migration duration, projection update p50/p95, bootstrap 102-page/replay timing, tally invalidation/recovery timing, query traces and memory/WAL observations. Update `docs/benchmarks/2026-11-01-operator-feed.md` with measured commands/results and limitations; never infer production impact.
- [ ] Update `docs/runbook.md` only for operator-visible tally-stale recovery and terminal oversized-group handling. State that eventual live migration requires verified backup and separate authorization; no source rows are intentionally modified by projection rebuild.
- [ ] Run `RG2W_LIVE_API= uv run pytest -q`, compileall, JS syntax/harness, available lint, and `git diff --check`. Report exact output, skipped/unavailable tools, human visual checks not run, commits, `dev` HEAD/status, migration safety and remaining risks. Request independent plan-conformance review against this plan; do not claim conformance, push or deploy before a clean verdict and separate operator approval.
