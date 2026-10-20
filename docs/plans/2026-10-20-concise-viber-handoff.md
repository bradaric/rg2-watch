# Concise Viber Handoff Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate concise Serbian Cyrillic Viber copy from an explicitly approved short reason for new cases, and provide a guarded one-time correction of the saved copy for the already-published Књажевац БМ 61 case.

**Architecture:** Keep the public case summary and staged PDF frozen as before; add a separately frozen `viber_reason` to new publication claims. Format new messages from that reason and the already-frozen case metadata/link, without parsing the free-form public summary. A local CLI preview/confirmation updates only the saved Viber text of the selected already-published legacy case after checking the old text and immutable identifiers. Older pending publications with no short reason retain their frozen retry behavior and legacy Viber format. No Drive, OAuth, sharing, or sending operation is added.

**Tech Stack:** Python 3.12, existing SQLite Store/CaseHandoff/DrivePublisher/PublicationJobs, local HTML/JS, pytest and Node DOM tests.

---

## Approved copy and boundaries

New and corrected message format (the links are the existing stored case-folder and RIK URLs):

```text
Могућа неправилност за проверу.
Књажевац, БМ 61: Потребна је провера броја гласача ван бирачког места (18 од 58) и броја гласова за једну листу (53 од 58).
Записник и опис: <existing case-folder URL>
РИК: <existing RIK URL>
```

- For new cases substitute frozen municipality/station and the operator-approved **short Viber reason**; do not infer or shorten the free-form public summary. The chosen label `Записник и опис:` assumes these published PDFs are записници; if the product later supports other document types, revisit the label rather than silently mislabeling them.
- The operator explicitly approved the quoted reason sentence for the existing Књажевац 61 case; the numbers are operator-approved, not independently verified by the app/architect. Do not change the case folder, PDF, text file, approved public summary, existing final URL or already-sent Viber messages. The correction changes only the **locally saved** copy returned by `case/publication` after page refresh. No automatic Viber dispatch.
- Existing pending rows with a NULL `viber_reason` predate this feature; they must resume with their original summary/PDF and legacy message template. Do not require a new reason or alter their frozen package on retry. Completed historical rows other than the operator-selected correction remain untouched.
- Keep case controls/errors in English and the copied Viber content in Serbian Cyrillic. Keep logged information sanitized; never log PDF bytes, free-form summary, Viber reason, full provider responses or OAuth credentials.

## File map

- `src/rg2watch/store.py`: nullable `viber_reason` migration on publications, immutable claim binding for new rows and compare-and-swap published-text correction; keep URL, folder IDs, PDF hash, summary hash, final URL and published timestamp unchanged.
- `src/rg2watch/case_handoff.py`: validate/normalize one-line short reason, new concise formatter and preserved legacy formatter for older pending rows and CLI old-text guard. No AI draft changes.
- `src/rg2watch/drive_publish.py`: pass frozen reason into claim, choose new or legacy formatter based on stored row, never change sharing/retry ID logic.
- `src/rg2watch/publication_service.py` and `src/rg2watch/server.py`: carry reason on publication jobs and HTTP publish requests; preserve safe errors and explicit public-sharing confirmation.
- `src/rg2watch/static/index.html`: for **new** cases add an editable, required short-reason textarea/field beside full summary, label both distinctly, send both on Publish; for legacy pending show frozen summary and existing retry only, and for a new pending row show both frozen inputs read-only. Published view continues to show the saved copy.
- `src/rg2watch/cli.py`: `revise-viber` subcommand using the existing `--db` override, exact document URL, short reason, dry-run preview by default and explicit `--confirm` for the guarded local correction. No Drive API calls or generic published-message editor.
- `tests/test_publish.py`, `tests/test_drive_publish.py`, `README.md`: migration, formatting, immutability, UI/HTTP, CLI guard and recovery tests; operator instructions. No live Drive in tests.

## Chunk 1: Freeze a separate short reason for new cases

### Task 1: Add failing Store and formatting tests

**Files:** `tests/test_publish.py`, `tests/test_drive_publish.py`.

- [ ] Test migration of an existing DB: historical `publications` gain nullable `viber_reason`, with existing `published` and pending rows unchanged. Test new claim stores a nonempty normalized short reason with full summary/PDF; retry with identical inputs succeeds, but changed reason or changed summary fails before Drive mutation. Existing pending NULL-reason row must still retry without a new reason.
- [ ] Test exact Cyrillic layout above using a case fixture, with station displayed **once**, no `људску`, no `није утврђена чињеница`, no `Разлог: Избори: ...`, `Записник и опис:` before `РИК:`, and both links unchanged. Test legacy pending produces exactly the former saved-message format (keep a named legacy formatter, not a call to the new formatter). Test a published row still returns its persisted text regardless of the new formatter.
- [ ] Run `uv run pytest tests/test_publish.py tests/test_drive_publish.py -q` and observe new tests fail before implementation.

### Task 2: Implement reason validation, storage and format selection

**Files:** `src/rg2watch/store.py`, `src/rg2watch/case_handoff.py`, `src/rg2watch/drive_publish.py`, corresponding tests.

- [ ] Add nullable `publications.viber_reason` by idempotent `PRAGMA table_info`/`ALTER TABLE` following the current migration style. Modify `claim_publication` to accept/store normalized short reason on insertion; for an existing row compare to its stored reason (including NULL legacy) and reject a changed/new reason, while retaining existing summary/PDF checksum comparisons. New claims require a reason; NULL may only be accepted for rows that already existed before this change. Do not turn a legacy pending NULL into a populated reason as a side effect.
- [ ] Normalize reason by trimming and collapsing whitespace to one line; require nonempty after normalization and at most 500 Unicode characters (reject rather than silently truncate); reject control characters and embedded URLs. The operator is responsible for the accuracy and script of the Serbian sentence; do not run the AI drafter or rewrite its content. Validate **before staging a PDF or inserting a new claim**, and freeze the normalized value with the claim before any remote ID allocation. On retry use the stored value and reject any submitted difference for new rows.
- [ ] Implement new formatter returning four lines: `Могућа неправилност за проверу.`, `{municipality}, БМ {station}: {reason}`, `Записник и опис: {folder_url}`, `РИК: {rik_url}`. Retain the old formatter as `legacy_viber_text` solely for NULL-reason pending rows and the CLI's exact old-text precondition. On finalization use the stored reason if non-NULL, not an HTTP caller-supplied reason; otherwise use legacy formatter. Preserve the published short-circuit returning stored link/text.
- [ ] Run focused tests, including preallocation/crash retry and checks that no second folder or direct child sharing occurs.

## Chunk 2: New-case UI and transport

### Task 3: Carry reason through the existing explicit Publish flow

**Files:** `src/rg2watch/publication_service.py`, `src/rg2watch/server.py`, `src/rg2watch/static/index.html`, `tests/test_publish.py`.

- [ ] Add required `viber_reason` in the POST `case/publish` body and job/publisher call for new cases. Preserve `confirm: true` check and safe error responses; enforce validation in Python, not only browser JS. For old pending rows, omit the reason on retry and ensure the server distinguishes legacy NULL from a new empty reason by reading the stored publication. For new pending rows return the frozen reason from the publication status API so the UI shows it read-only; never expose internal note.
- [ ] In the panel, label the public summary and short Viber reason separately in English; do not automatically derive the short reason from the summary. New Publish remains disabled or shows clear guidance until both are filled and the explicit sharing checkbox is checked. On a pending new claim, both inputs are read-only and Retry sends the frozen values; on legacy pending keep only the existing frozen summary and Retry. On success continue to render saved Serbian copy.
- [ ] Extend executable Node DOM and HTTP/job tests for missing reason, normal new publication, changed-reason retry, legacy pending retry, new pending frozen retry and completed publication. Verify no regressions to published-case links/copy buttons and no AI-draft behavior change. Run focused tests.

## Chunk 3: One published-case correction, locally only

### Task 4: Guarded preview/confirm CLI and atomic update

**Files:** `src/rg2watch/cli.py`, `src/rg2watch/store.py`, `src/rg2watch/case_handoff.py`, `tests/test_publish.py`.

- [ ] Add `rg2watch revise-viber --db data/election-677084.sqlite --url '<exact document URL>' --reason '<operator-approved sentence>' [--confirm]`. Require an explicit on-disk `--db` (do not default to a possibly different election DB), exact URL, reason; do not print OAuth paths/contents. A no-`--confirm` invocation opens SQLite **read-only** (`mode=ro`, without initializing a migration-writing `Store`), shows target municipality/station, saved old and proposed new messages (the operator's own terminal), and states that it would replace **only the saved Viber text**. It never calls Drive, moves folders or sends Viber.
- [ ] Confirmation preconditions, checked again **inside one `BEGIN IMMEDIATE` SQLite write transaction**: reread the selected row after acquiring the write lock, then verify `phase='published'`, municipality `Књажевац`, station `61`, final URL equals the URL derived from its stored case folder ID, nonempty stored frozen summary/PDF metadata (do **not** require the staged PDF file to exist after successful publication), and original legacy `viber_text == legacy_viber_text(case, summary, final_url)`; input reason equals the exact operator-approved sentence above. Recompute proposed text from this in-transaction row, **not** the earlier preview snapshot. Fail closed for missing/ambiguous row, wrong folder, already-edited copy, changed case data, NULL/invalid URL, or unsupported DB. Under that same transaction issue a conditional UPDATE comparing URL, phase, old Viber text, final URL **and the checked `case_json`, summary, summary SHA-256, staged PDF path, PDF SHA-256, folder ID, PDF ID and text ID** to the just-validated row; assert exactly one row changed, otherwise roll back. SQLite's reserved write lock prevents another writer from changing checked metadata between reread and UPDATE. Update only `viber_text` and `updated_at` (recording the local correction), **not** `published_at`, summary, PDF path/hash, IDs, final URL, or Drive state. An exact repeat with already-correct text reports a no-op without changing `updated_at`; any different existing text requires investigation, not overwriting.
- [ ] Test dry-run does not change bytes/row; wrong DB/URL/station/folder, changed old text and pending row fail before mutation. Simulate changes to old text or frozen metadata **between an initial read and acquiring the write lock**: the in-transaction reread must detect invalidated preconditions or recompute from the current snapshot rather than use stale text; verify a second writer cannot modify the row between `BEGIN IMMEDIATE` reread and UPDATE. A confirmed run updates only the selected saved text and a second identical run is idempotent. Preserve the original Viber copy in the dry-run output for the operator to retain if desired; no automatic backup/migration of other cases.
- [ ] Run focused tests and full `uv run pytest -q` plus configured checks; inspect diff to ensure no implementation writes under unrelated paths and no live Drive calls. Keep the one-time correction **operator-initiated** after branch integration; do not run it on the user's DB during development or tests.

### Task 5: Operator runbook and verification

**Files:** `README.md`.

- [ ] Explain the new separate short reason and concise layout, legacy pending compatibility, UI confirmation, and that already-sent Viber messages cannot be edited retroactively. Document preview first, then operator verification/confirmation against the intended `data/election-677084.sqlite` row; use the approved sentence above and the exact document URL from the local case page, not a guessed station-wide selector. Keep the long summary and PDF frozen; restart `serve`/refresh to display corrected saved copy after the command.
- [ ] Include final manual checks: same public case URL, same two files accessible in logged-out view, `RG2 Watch` still restricted, corrected Viber text shown in app before copying/sending. No app-initiated Viber send, no second publication, no Drive permission changes.

## Acceptance checks

1. New cases require a separately approved frozen short reason and produce the exact four-line layout with one station mention and `Записник и опис:` preceding RIK; full summary/PDF and public text file remain unchanged.
2. Pre-existing pending cases with NULL reason retain retry safety and their legacy Viber format; previously published cases display stored copy unchanged except the explicitly selected Књажевац 61 correction.
3. The CLI previews by default and, on explicit confirmation, changes only this selected row's saved message under exact guards; no Drive, OAuth, publication-phase or URL mutation.
4. Tests cover UI/HTTP, migration, concurrency/conditional update, negative guards and idempotency with local fakes only. The operator decides when to apply the one-time correction and when to send the new copy.
