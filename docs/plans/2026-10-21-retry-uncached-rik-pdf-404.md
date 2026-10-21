# Retry Uncached RIK PDF 404s During Watching Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** During election-night `watch --analyze`, retry a listed document's uncached RIK PDF after an HTTP 404 instead of permanently failing it with D3.

**Architecture:** Preserve the existing SQLite analysis queue, active-document filter, download limiter, and 120-second RIK deferral. Introduce a typed HTTP-404 exception under `RikError` (not `RikUnavailable`) and catch it only at the analysis pipeline's PDF fetch boundary; refund its claimed attempt and schedule a retry. A PDF 404 breaks the consecutive 401/403 blocked-download streak, so interleaved missing files cannot trigger a blocked pause. Leave other consumers and other HTTP errors unchanged.

**Tech Stack:** Python 3.12+, httpx, SQLite, pytest, uv.

---

## Scope and invariants

- The observed condition is an HTTP 404 for a RIK-hosted PDF URL that may work minutes later. A cached PDF is already served by `fetch_pdf` without an HTTP request; do not change that path.
- Only `watch --analyze`'s `AnalysisPipeline` changes its outcome for PDF 404. The one-off `analyze`, batch analysis, listing/watcher health, and unrelated HTTP statuses retain their present behavior. No new configuration, DB columns, retry subsystem, or direct browser-link handling.
- An active document may remain deferred indefinitely at the existing `RIK_RETRY_S = 120` interval. Deferrals must not use up `MAX_ANALYSIS_ATTEMPTS`, create a D3/X2 finding, invoke the model, or spend API money. Once a replacement/removal makes the old row inactive, `Store.next_for_analysis` already excludes it. A newly listed replacement gets its own queue entry.
- Reuse the existing `_download_deferred(False)` / `_download_ok()` counters and existing delayed "PDF downloads blocked/unavailable" status threshold (10 consecutive deferrals). A PDF 404 **resets** `RikClient.download_blocked_streak` (consecutive 401/403), rather than incrementing or preserving it; it cannot complete a sequence of three consecutive blocks or cause a blocked pause. A 404 is not a successful download: it must not set `download_last_ok` or call `_download_ok()`. Be aware the deferral alert threshold is global across download deferrals, not per URL; do not introduce a per-URL alert state.
- A 404 that occurs when the document is already cached cannot reach the new catch. A 404 on a listing request can have a more precise exception type but still follows the existing `RikError` behavior, not a pipeline deferral.

## File map

- `src/rg2watch/rik_client.py`: add `RikNotFound(RikError)` and raise it for HTTP 404 from `_send`; in `RikClient.download`, reset `download_blocked_streak` on this exception and re-raise it. Preserve immediate failure, other 4xx behavior, retry/backoff for 429/5xx, and 401/403 semantics. Because `_read_capped` returns an HTTP response for PDF 4xx, the same `_send` branch covers PDF downloads.
- `src/rg2watch/pipeline.py`: import `RikNotFound`; add a narrow `except RikNotFound` inside `_run`'s fetch/render try block, before the `RikUnavailable` and `(RenderError, RikError)` handlers. Log deferral, call `mark_analysis(..., "failed", ..., retry_at=clock()+RIK_RETRY_S, refund=True)`, call `_download_deferred(False)`, return without findings.
- `tests/test_rik_client.py`: prove typed 404 on PDF and listing, immediate failure, PDF 404 breaking an interleaved 401/403 streak, and no changed 401/403/429/5xx policy.
- `tests/test_pipeline.py`: prove repeated uncached 404, scheduled/no-attempt retry, eventual recovery, cache bypass, and removal/replacement exclusion; keep existing bad-PDF D3 test as a regression.
- `docs/analysis.md`, `docs/runbook.md`: describe transient PDF 404 during watching, the retry cadence and visibility, and that recovery is automatic; distinguish from 401/403 pause and permanent malformed-PDF errors.

## Chunk 1: Typed HTTP 404 and pipeline behavior

### Task 1: Identify 404 without changing its existing error family

- [ ] Add a focused failing test in `tests/test_rik_client.py` using `make(lambda request: httpx.Response(404))`: `client.download(BASE + "/extfile/missing.pdf")` raises `RikNotFound`, is also a `RikError`, is **not** `RikUnavailable`, and sends exactly one request. Add a listing assertion using `client.listing(Filters(doc_type=6), 1)` that it likewise raises `RikNotFound` immediately; the watcher still sees a `RikError`. Keep the existing `test_429_retried_and_4xx_raises_immediately` assertions. If the 401/403 regression is not already covered, assert it still raises `RikBlocked`, not `RikNotFound`. Add a single-client sequence test where mocked PDF responses are 403, 404, 403, 404, 403: each 403 yields `RikBlocked`, each 404 yields `RikNotFound`, and `download_blocked_streak` is respectively `1, 0, 1, 0, 1` (no successful PDF between them). A listing 404 must not be mistaken for a PDF download success or change download health.
- [ ] Run `uv run pytest tests/test_rik_client.py -q`; verify the new assertion fails before implementation (missing class / wrong exception).
- [ ] In `src/rg2watch/rik_client.py`, define `RikNotFound(RikError)` adjacent to the other RIK exceptions; in `_send`, after the 401/403 branch and before the generic `>= 400` branch, raise `RikNotFound(f"{method} {url} -> HTTP 404")` when `resp.status_code == 404`. Do not make it a `RikUnavailable`: batch/listing callers should not acquire transient semantics from inheritance. In `RikClient.download`, add `except RikNotFound: self.download_blocked_streak = 0; raise` alongside the existing `except RikBlocked` (before the success-only `download_last_ok`/streak-reset lines); preserve the existing `RikBlocked` increment. Do not change `_read_capped`, `_request`, or their retry limits. Listing 404 must retain its prior watcher failure semantics; do not reset its `blocked_streak` or report it as a successful listing.
- [ ] Run `uv run pytest tests/test_rik_client.py -q`; verify passes.

### Task 2: Defer uncached PDF 404 only in the watch pipeline

- [ ] In `tests/test_pipeline.py`, use `Env`, `doc("missing.pdf")` (absent from its `pdf_dir`), and `e.publish(d)` to write a failing test: first `e.drain()` performs exactly one PDF request, records `("failed", 0)` and `analysis_next_at == e.clock.now + RIK_RETRY_S`, records an error containing `HTTP 404`, creates no findings, calls no model, and leaves `download_blocked_streak == 0`. Repeated `e.drain()` before due time makes no request. At each due time, advance `e.clock.now` by `RIK_RETRY_S`, drain, and verify attempts remain zero and no D3/X2; use enough rounds (at least four) to prove the three-attempt ceiling does not end retries. Keep the test time deterministic, without sleeping or live RIK requests. Assert a 404 does not set `_download_paused_until` or `download_last_ok`; rely on the client sequence test for interleaved-streak behavior.
- [ ] Run the focused test with `uv run pytest tests/test_pipeline.py -k <new_404_test_name> -q`; verify it fails by the current permanent D3 path.
- [ ] In `src/rg2watch/pipeline.py`, import `RikNotFound` and catch it *before* `RikUnavailable` / `RikError` in `_run`. Use the same log/`mark_analysis(... retry_at=self.clock() + RIK_RETRY_S, refund=True)` shape as the existing `RikUnavailable` deferral and `_download_deferred(False)`; return `None`. Catch scope is only the PDF-fetch/render try block, so listings and other commands are unaffected. Do not call `_permanent`, `_transient`, or `_download_paused_until` for this case.
- [ ] Run `uv run pytest tests/test_pipeline.py -k <new_404_test_name> -q`; verify passes.

### Task 3: Recovery, cache, active URL, and regression tests

- [ ] Add a test that first receives 404 for `doc("missing.pdf")`, then creates a valid PDF under the fixture `pdf_dir` (use the existing `make_pdf`). Advance the injected clock to the due time; verify exactly one more download, `("done", 1)`, one model call, a local cached file at `<tmp_path>/cache/<sha256(url)>.pdf`, and no D3. Optionally use `e.pipe._download_alerted` only if a test first deliberately crosses the existing 10-deferral threshold and checks its existing recovery status; do not add an alert merely for the first 404.
- [ ] Add a cache-bypass test: prepopulate `<tmp_path>/cache/<sha256(url)>.pdf` with a valid PDF while the fake RIK returns 404, publish and drain; verify zero PDF requests, one analysis, and done. Compute the digest exactly as `fetch_pdf` does (`hashlib.sha256(d.url.encode()).hexdigest()`).
- [ ] Add a test for inactivity: after a deferred 404, apply a `removed` event for that URL, advance past its retry time, and verify no second download. Separately apply a `replaced` event with `previous_url=old.url` and a new valid `doc("b.pdf")`; submit it to the pipeline and verify the new URL is processed but the old URL is never retried, even after its due time. Use `Store.apply_events`/`AnalysisPipeline.submit` as in `test_replacement_is_analysed_as_new_url`. No new store methods are needed.
- [ ] Preserve and run `test_bad_pdf_gives_d3` to show a 200 non-PDF or malformed PDF remains permanent. Run `uv run pytest tests/test_rik_client.py tests/test_pipeline.py tests/test_batch.py tests/test_watcher.py -q`; expect all pass. Investigate any failures rather than weakening assertions.

### Task 4: Operator docs and final verification

- [ ] Update `docs/analysis.md` near its current download-deferral paragraph and `docs/runbook.md` troubleshooting table: in `watch --analyze`, an uncached RIK PDF HTTP 404 is retried every 120 seconds while its document URL is active; it does not exhaust analysis attempts or generate D3; if the URL becomes accessible, analysis resumes automatically. Clarify the existing cumulative unavailable alert threshold and distinguish a 404 retry from the 401/403 blocked pause. Avoid promising browser RIK links cannot still show 404 or that batch/one-off analysis retries it.
- [ ] Run `uv run pytest -q`; expect all tests pass. Review `git diff --check` and `git diff --stat`; verify only the planned implementation, tests, and docs changed, and no DB migration or new config was introduced. Implementation commits, if made by the developer, should follow repository conventions; this planning stage does not commit implementation.

## Acceptance criteria

1. Repeated uncached PDF 404s during watching defer every 120 s without consuming attempts or making D3/X2; other queued active documents can still be processed.
2. When the same URL starts returning a PDF, it caches and analyses normally; if it was already cached, the external 404 cannot affect it.
3. Removed/replaced URLs are not newly claimed for a retry. Existing warning/status behavior is reused; PDF 404 breaks (does not preserve) a consecutive 401/403 blocked streak, so interleaved 404s cannot cause a blocked pause.
4. Listing, batch, one-off, non-404 download errors, and malformed PDF behavior remain as before; the focused suites and full test suite pass.
