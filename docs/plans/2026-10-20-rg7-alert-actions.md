# RG-7 Alert Actions Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give RG-7 findings in “Last 20 alerts” PDF, RIK, and (when an associated RG-2 row exists) show actions, without changing how other alerts behave.

**Architecture:** The status snapshot retains its existing eight alert fields and appends optional, validated link metadata **only for RG-7 correction-decision types 19–26**; other alert types receive null fallback links. The browser first resolves an alert URL against RG-2 versions and nested `r7` decisions; a nested match supplies PDF/RIK and the parent row's ID for show. For a decision with no parent row, the RG-7-only status metadata supplies PDF/RIK, and show is omitted. Do not add standalone RG-7 rows, fetch PDFs, or change findings/tally behavior.

**Tech Stack:** Python/SQLite, static vanilla JavaScript, pytest.

---

## Chunk 1: Alerts and navigation

### Task 1: Cover the alert lookup and orphan link contract with tests

**Files:**
- Modify: `tests/test_browse.py:142-145,568-575`
- Modify: `tests/test_rg7.py:259-274` (reuse its station/decision fixtures where helpful)
- Modify: `tests/test_browse.py:318-345` (watcher status stub and propagation assertions)

- [ ] Add a test that seeds an RG-2 and an RG-7 decision with an R1 finding. Assert `build_data(...)["rows"][0]["r7"]` contains the decision URL, its full HTTPS RIK URL, and its local PDF hash only when the cached PDF exists. Confirm `build_status(...)["alerts"]` still places the document URL at index 7 and exposes the decision's safe RIK URL and cached PDF hash in appended link fields when supplied the cache directory.
- [ ] Add an orphan RG-7 finding with no RG-2 station row. Assert forms data has no matching row/decision, while the status alert supplies a safe RIK link and cached-PDF hash when cached; when uncached, PDF is null. Also assert a non-HTTP URL does not become a clickable RIK link. Add an unmatched RG-8 (type 8) finding with a cached PDF and assert its fallback PDF/RIK fields are null and it remains actionless; likewise check existing RG-2 alert fallback fields stay null and their original first eight fields remain unchanged.
- [ ] Strengthen the existing HTML contract test to check that alert lookup visits `r.r7`, that nested hits use the containing `r.id` for show, and that absent hits use the status link fields without rendering show. Test that a non-RG-7 unmatched alert has null status fallback links. This is only a static contract test; manual click verification is required in Task 3.
- [ ] Test the actual status-writing paths: `build_page(..., cache_dir=cache, with_status=True)` must write `status.json` with a cached orphan RG-7 hash. Adapt `_watch`'s `write_status` stub to record its args/kwargs; assert the ordinary background status write receives `_cache_dir(cfg)`. Cover the `--until-done` final write using the existing watcher test setup or a focused test that drives the finalization branch; assert that second call receives `_cache_dir(cfg)` too. Preserve other `_watch` tests' expectations when changing the stub.
- [ ] Run `pytest -q tests/test_browse.py tests/test_rg7.py` before changes and observe new test failures.

### Task 2: Expose safe fallback links and resolve nested decisions

**Files:**
- Modify: `src/rg2watch/status.py:22-49`
- Modify: `src/rg2watch/store.py:687-692`
- Modify: `src/rg2watch/browse.py:223-248`
- Modify: `src/rg2watch/cli.py:802-804,859`
- Modify: `src/rg2watch/static/index.html:377-380,427-440`

- [ ] Add `d.doc_type` to `Store.recent_findings`'s SELECT (without changing its filtering/order). Extend `build_status(store, parent, live=None, cache_dir=None)` by appending `pdf` and `rik` after alert index 7: **only when `r["doc_type"] in RG7_TYPES`** compute links; append `[None, None]` for every other type. Resolve RIK URLs with the existing `_rik_link` helper from `browse.py` **without a circular import**: relocate the small URL validation helper to `status.py`, then import it from `browse.py` (or use an equally small shared helper). Use the election base URL already loaded in `build_status`; accept only HTTP(S). Compute SHA-256 of the stored document URL and expose its hash only if `(cache_dir / (hash + ".pdf")).is_file()`; otherwise null. `cache_dir=None` means no local PDF. Preserve indices 0–7 and all non-alert status behavior.
- [ ] Pass the real configured cache directory to `write_status` from **both** watcher call sites in `cli.py` (background and `--until-done` final write); pass `build_page`'s existing `cache_dir` to its `write_status` call. Keep `write_status`'s existing callers compatible by adding an optional cache-directory argument. `format_status` need not read the cache.
- [ ] Change `alertForm(url)` to search `[r, ...r.v]` first, then `r.r7` for each row, returning `[r, matchedVersion]`. Keep old-version RG-2 matching intact. In `renderStatus`, use the matched version's PDF/RIK if found, otherwise the appended status PDF/RIK (null for every non-RG-7 type). Render show only when there is a matching row; keep existing `showForm(r.id, ...)` behavior and existing `localHref`/`rikHref` validation. Do not synthesize a show target by municipality/station text.
- [ ] Run `pytest -q tests/test_browse.py tests/test_rg7.py tests/test_status_cli.py` and fix only regressions related to this task.

### Task 3: Verify end-to-end behavior

**Files:** No additional changes expected.

- [ ] Run full `pytest -q`; report any unrelated failures separately.
- [ ] In a local page seeded with an RG-7 R1 and cached decision PDF, verify PDF opens the decision PDF, RIK opens the decision RIK URL, and show selects its RG-2 row. Check an RG-2 alert still works. For an orphan decision, verify PDF/RIK appear (PDF only when cached) and show does not. Verify unmatched RG-8 still has no actions. Check uncached and invalid-URL states produce no broken or unsafe links.
- [ ] Keep this scoped to alert actions; no new review workflow, independent RG-7 row, database migration, or refactoring of the forms table.
