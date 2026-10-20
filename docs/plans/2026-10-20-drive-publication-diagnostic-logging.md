# Drive Publication Diagnostic Logging Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an operator-initiated retry of the pending Drive publication explain, in server-side logs, why the app's child-access check fails even though the operator can open both files while logged out.

**Architecture:** Add a per-publication-attempt diagnostic ID, propagated from the asynchronous job to the publisher and Drive adapter with a thread-local `ContextVar`. At INFO, log bounded, redacted checkpoints and the child permissions' *type/role classifications* at the final check; retain the current UI error, exact-ID reconciliation, root/privacy verification, child-access predicate, and fail-closed publication behavior. This is instrumentation only, not a repair or permission change.

**Tech Stack:** Python logging, `contextvars`, pytest fake Drive and `caplog`; existing `drive.file` scope and `uv run pytest`.

---

## Observed state and constraints

- On the operator's explicit retry, the UI reported `Drive account policy did not expose both folder children`. The operator then confirmed the same case folder is `Anyone with the link — Viewer`, contains `документ.pdf` and `опис-предмета.txt`, and **both child links open in a logged-out private browser**. The children's Share panels also show `Anyone with the link — Viewer`. `RG2 Watch` remains restricted. No Viber link has been sent. The case may remain publicly accessible by link while the operator investigates; do not alter it automatically.
- `src/rg2watch/drive_publish.py:148-156` calls `_permissions(child)` for each child and requires a returned `type=anyone,role=reader` entry. `DrivePublisher.publish` calls that predicate after sharing the folder and before `finalize_publication` (`:261-282`). `PublicationJobs._submit` (`src/rg2watch/publication_service.py:22-46`) currently stores a short error string but emits no server log for this job. The actual live API responses have **not** been inspected: inherited-access presentation, response filtering, and policy behavior are hypotheses, not a diagnosed root cause.
- Google Drive documents inherited child access and `permissions.list`; logging the API's limited type/role classifications will distinguish an absent entry, another role, and a request failure without logging the full provider payload. Do not change OAuth scope, supply new credentials, issue unattended live Drive requests, turn off the final check, or add direct child permissions in this plan.
- Existing CLI logging sends INFO for `serve`/`watch` to stderr and keeps `httpx`/`httpcore` at WARNING (`src/rg2watch/cli.py:1178-1186`). Do not enable raw HTTP debug traces. Operator controls the `serve` terminal; browser error stays safe and unchanged.

## File map

- Modify `src/rg2watch/drive_publish.py`: private `ContextVar` for diagnostic ID, bounded INFO checkpoints at publication boundaries, and permission-result summaries inside `children_have_public_reader`; log **no** file IDs, account IDs, links, source document URL, summary, PDF data, token paths, request headers, raw response objects or provider exception strings. Preserve API requests, predicate and existing error wording.
- Modify `src/rg2watch/publication_service.py`: set/reset the diagnostic context for each `publication` job using its existing job ID; log started/done/failed with safe stage/exception class (not `str(exc)` or traceback). Keep current job result/error schema and draft behavior unchanged. A direct call to `DrivePublisher.publish` outside the job gets its own random attempt ID for its logs.
- Modify `tests/test_drive_publish.py` and `tests/test_publish.py`: assert diagnostic content and redaction, error behavior, job correlation and context isolation using fake services; no live Google or local OAuth-token reads.
- Modify `README.md`: explain where to read the new server-side diagnostics and how to share *redacted classifications* rather than tokens/links; document an explicitly operator-approved retry, not a blanket retry command.

## Chunk 1: Safe, correlated diagnostics

### Task 1: Reproduce unobservable failure in tests

**Files:** `tests/test_drive_publish.py`, `tests/test_publish.py`.

- [ ] Write a failing correlated end-to-end test with an **API-backed fake service passed through `GoogleDriveAdapter`** (not `FakeDrive.children_have_public_reader`): the folder-share API succeeds, `files.get` returns both correctly parented uploaded children, and `permissions.list` returns owner-only entries for **each** child. Connect that adapter to `DrivePublisher` through `PublicationJobs`. Assert the publication remains pending/`uploaded`, original folder and child IDs are retained, no second share or direct child permission is created, and `Drive account policy did not expose both folder children` remains the safe job error. A simpler fake that returns false may be used separately to isolate publication-state behavior, but cannot serve as the diagnostic reproduction.
- [ ] Add `caplog` expectations on the **API-backed reproduction** that a retry's **one** diagnostic ID ties the job start, `uploaded`/share checkpoints, both `pdf` and `text` owner-only check outcomes, and job failure together. Assert the current code lacks these records before implementation. Name the children only as `pdf` and `text` (not provider IDs or filenames).
- [ ] Add adversarial fixture strings in IDs, document URL, PDF bytes, approved summary and provider exception text; assert none occurs in logged text or the unchanged UI/job error. Run `uv run pytest tests/test_drive_publish.py tests/test_publish.py -q` and observe only the new diagnostic assertions fail.

### Task 2: Propagate a diagnostic ID and log publication boundaries

**Files:** `src/rg2watch/drive_publish.py`, `src/rg2watch/publication_service.py`, corresponding tests.

- [ ] Define a module-private `ContextVar[str | None]` in `drive_publish.py` and an INFO logger `rg2watch.drive_publish`. In `PublicationJobs._submit`, for `kind=publication` only, set the variable to the existing `job_id` immediately around `fn()` and **always** reset it in `finally` (including failures). Log `publication start/done/fail` with that ID; on failure include only the Python exception **class name**, not exception text, traceback or chained cause. Keep the existing in-memory job result/error values exactly as they are; do not log draft result or exception contents.
- [ ] In `DrivePublisher.publish`, if no job context is present, allocate one random UUID diagnostic ID for this direct attempt, set it for the duration of the call and reset it on every exit. With a job context, reuse its ID, never replace it. Emit INFO checkpoints for account/root validation completed, frozen package and IDs verified, files verified/`uploaded`, case-folder public confirmation, and final result (or child check failed). Do not log pre-validation package details or object metadata. Avoid duplicating the job's error line.
- [ ] Run focused tests. Add a second concurrently executed fake publication (or thread-pool jobs with distinct fake URLs) and assert no context leakage across jobs or retry attempts; a non-publication draft must not inherit a publication ID.

### Task 3: Log the decisive permission check, without weakening it

**Files:** `src/rg2watch/drive_publish.py`, `tests/test_drive_publish.py`.

- [ ] In `children_have_public_reader`, for **each** child in the passed two-ID list, log `child=pdf|text` based on position; number of returned permission entries; bounded classifications of `(type, role)` using **only known Drive enum tokens** (`anyone`, `user`, `group`, `domain`; `reader`, `commenter`, `writer`, `owner`, `other`). Replace unknown/untrusted strings with `other`, cap the displayed list (e.g. first 10) and give total count. Do not include permission IDs, email addresses, `permissionDetails`, raw values, response JSON or resource links. Report a boolean `anyone_reader` for each child. Do not log or expose a provider exception if `permissions.list` fails; log `child-check failed` and the sanitized exception class and re-raise existing `DriveError`.
- [ ] Evaluate **both** children for diagnostics even when the first lacks a matching entry; return the same conjunction of the current `type=anyone,role=reader` predicate. Do not change `_permissions`' request fields, create direct child permissions, assume the browser UI equals the API response, or finalize if either check returns false. Use logs to distinguish missing entries from errors rather than claiming the root cause now.
- [ ] Test owner-only, anyone-reader, non-reader-anyone and API-error responses, unknown/malicious type/role values, and >10 permissions. Assert only bounded enum classifications are logged; the existing return/error semantics and UI-safe messages remain unchanged. Run `uv run pytest tests/test_drive_publish.py tests/test_publish.py -q` and full `uv run pytest -q`.

## Chunk 2: Operational documentation and verification

### Task 4: Document the gated live retry

**Files:** `README.md`; verify tests above.

- [ ] Document that the existing case folder may already be publicly accessible, but the app's row is pending because the final check failed. Do not send Viber or create/move/share anything else. After integration, the operator restarts `serve`, checks that `RG2 Watch` is restricted and the existing case folder/files still match the approved frozen package, and only then explicitly authorizes **one** retry in the UI; do not run a live retry as an automated test. Existing idempotent same-ID logic should reuse the public case folder rather than add permissions or create another case.
- [ ] Explain that the operator may report the sanitized log lines for the diagnostic ID and `pdf`/`text` classifications. They should not paste OAuth files, bearer tokens, full HTTP errors, raw Drive JSON, PDF bytes, case text, or public links into an issue/chat. A future fix requires a **separate** diagnosis and approved plan after those results are known.
- [ ] Run full suite and configured format checks, inspect diff for secret-bearing logs or browser exposure and for changed Drive write/verification behavior. Reviewer should verify the package cannot be finalized just because logging was added. No live Google calls from developer/reviewer.

## Acceptance checks

1. A fake reproduction of the operator's post-share error remains pending with the same preallocated folder/child IDs; it emits a single correlated, bounded server-side trace of both child checks.
2. INFO logs show only predefined stage names, job/attempt ID, sanitized exception class and bounded permission type/role classifications; not tokens, raw provider content, document details or Drive IDs. Concurrent jobs and later retries have separate IDs.
3. Neither OAuth scope nor any Drive create/share/permission-check result changes; user-visible messages, public summary and Viber text remain unchanged.
4. The first live diagnostic retry is a separate operator action after integration and inspection. No link is sent before the app reports success and the operator rechecks the public folder and both child links.
