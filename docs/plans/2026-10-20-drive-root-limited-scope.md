# Drive Root Limited-Scope Setup Fix Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `rg2watch drive-root` work with the existing `drive.file` OAuth scope without requiring the app to inspect Google's built-in My Drive root folder.

**Architecture:** Create the app-managed `RG2 Watch` root folder with no explicit `parents` field, which places a new folder directly in the authorized account's My Drive. Record its actual parent ID from the created app-owned folder's metadata in the existing token-adjacent durable root state, then verify that exact parent on subsequent reads. Keep the existing account binding, preallocated root ID, restricted permissions, and pending-case recovery procedure. No broader OAuth scope, no automatic migration, and no live Google actions from tests.

**Tech Stack:** Python 3.12, existing Drive v3 adapter and `drive.file`, token-adjacent JSON root state, fake-Drive pytest tests.

---

## Observed failure and constraints

- Operator ran `uv run rg2watch drive-root --config rg2watch.toml` and received `Google Drive My Drive root could not be inspected`. In `drive_publish.py`, `get_my_drive_root_id()` calls `get_file("root")`, which returns `None` on a 404. The app cannot rely on being allowed to read the built-in `root` item with its limited `drive.file` grant. Do not log or inspect the operator's OAuth token contents to diagnose this.
- `drive_root.py:97-122` writes `phase=reserved` with a generated ID **before** attempting root creation. On this failure the local root state may be reserved and no Drive root was created; recovery must reuse that ID. Treat the exact remote/local state as unverified until an operator-initiated retry, not an assumed fact. Do not delete or overwrite the reserved root state, pending case row, or the empty stranded case folder.
- Google Drive documents that a `files.create` without `parents` places the file in the user's My Drive; use this for the **app's private top-level root only**, never for case folders. Case folders still use explicit `parents=[registered_root_id]`. Source: https://developers.google.com/workspace/drive/api/guides/folder#create-file .
- Bind the created root to the same verified Drive `about.get user(permissionId)` as the state. The root must remain private and app-owned; never share it. Existing case-only sharing and frozen-input handling are unchanged. Already-published cases remain read-only.

## File map

- Modify `src/rg2watch/drive_publish.py`: add a root-only folder creation operation that omits `parents`; return/read actual `id,parents,owners(permissionId),name,mimeType,trashed`, while leaving case `create_folder(..., parent=root_id)` unchanged. Remove use of `get_file("root")` from setup/verification; do not broaden `drive.file`.
- Modify `src/rg2watch/drive_root.py`: persist `my_drive_parent_id` after app-root creation and verify exact parent thereafter; version/migrate the existing state format conservatively so a `phase=reserved` state from the failed setup remains usable with the same root ID and account binding.
- Modify `tests/test_drive_publish.py`: fake Drive distinguishes built-in root (inaccessible) from app-created folder (accessible), tests default My Drive placement and reserved-state recovery/crash boundaries.
- Modify `README.md` only to clarify that root setup uses default My Drive placement with no broader OAuth scope; no config change is needed.

## Chunk 1: Reproduce and fix

### Task 1: Test the real failure boundary and state migration

**Files:** Modify `tests/test_drive_publish.py`.

- [ ] Add a fake-Drive test where `files.get("root")` returns 404 but `about.get` and app-owned `files.get(preallocated_id)` work. Seed a token-adjacent `phase=reserved` state in the current version 1 shape (root ID + account, no parent) to simulate the reported failed setup. Assert current `DriveRoot.setup()` fails with the observed error and **does not** create another ID.
- [ ] Add expected-behavior tests: a new root-only create request omits `parents` and returns an app-owned folder under a simulated My Drive root ID; setup stores that returned parent; a second setup reuses the same root ID and checks its exact parent, owner, name, type and private permission. Case folder creation must still send `parents=[root_id]`.
- [ ] Run `uv run pytest tests/test_drive_publish.py -q` and observe failing tests before the fix.

### Task 2: Implement root-only default placement and durable parent binding

**Files:** Modify `src/rg2watch/drive_publish.py`, `src/rg2watch/drive_root.py`; test `tests/test_drive_publish.py`.

- [ ] Add a root-only adapter method using `files.create` with preallocated `id`, `name="RG2 Watch"`, folder MIME, **no** `parents`, fields including `id,parents`. After create/reconcile, `files.get` by that ID validates one nonempty parent, exact owner equals `about.get` permission ID, name/type and `trashed=false`, and no `anyone` permission. Never use name-only discovery or choose a new ID after an uncertain create/409.
- [ ] Root state: accept a valid version 1 **reserved** state left by the failed setup, preserving ID/account. Introduce version 2 with `my_drive_parent_id` (null only in `reserved`, nonempty for `created`). Persist the actual sole parent observed from the app-owned root before setting `created`, under the existing token-adjacent flock and atomic/fsynced state writer. A version 1 `created` state from earlier code has no proven parent: fail closed with a manual-investigation error instead of guessing or silently rebinding. Do not touch unrelated token/client files.
- [ ] On subsequent `drive-root` and `require_state` verification, compare the app-owned root's **actual parent** to the stored `my_drive_parent_id`, plus existing owner/name/MIME/permission checks. A `created` root returning 404 still fails closed; if the account ID changed, reject before any root `files.get` or create.
- [ ] A `reserved` state and an uncertain successful create (crash after remote creation but before version-2 state write) must reuse the same preallocated ID. If the app-owned folder is found with one parent, verify owner/name/type/privacy, then bind that parent and finalize the state. Explicitly document that this records the app-created folder's observed parent rather than independently inspecting the built-in My Drive root. If missing, retry create with the same ID; if 409, re-read that same ID; if inaccessible/mismatched, fail closed.
- [ ] Test crash after `files.create` but before root-state write, crash/retry after state write, wrong account, renamed/moved root, public root, missing app folder and failure to read owner/parent; assert no case share or duplicate root. Run focused tests and `uv run pytest -q`.

## Chunk 2: Operator check

### Task 3: Documentation and safe live retry guidance

**Files:** Modify `README.md`; verify tests above.

- [ ] Document this limited-scope setup behavior and state reconciliation. Explain that this fix does not automatically move the already stranded pending case; after the operator explicitly retries `drive-root`, they inspect the new private `RG2 Watch` folder and verify its location in My Drive before manually moving the confirmed-empty case folder. If root creation or identity verification fails, stop rather than moving/sharing anything.
- [ ] Verify full pytest suite and configured checks, with no live Google calls in automated tests. Inspect diff for accidentally broadened OAuth scope, token exposure, root sharing, changed case parent semantics or unrelated AI/UI changes.
- [ ] After separate plan clearance and implementation, instruct the operator to restart/re-run only `uv run rg2watch drive-root --config rg2watch.toml`; check the printed folder link opens under My Drive and is restricted. **Do not** publish or send a Viber message in this setup test. Subsequent manual case-folder move/retry remains a separate, operator-approved step.

## Acceptance checks

1. `drive-root` no longer calls `files.get("root")` and works under `drive.file` when that built-in item is unreadable.
2. The existing reserved root ID/account survive the failed setup and any crash; root state records the actual app-folder parent and rejects a later move, account change, or public root.
3. Case folders still have the app root as their sole explicit parent; only a case folder may later become link-viewable. The pending 677084 case folder and frozen inputs are not modified by root setup.
4. Tests use fake Drive; the first live retry is performed only by the operator, who verifies root location/privacy before any manual case move.
