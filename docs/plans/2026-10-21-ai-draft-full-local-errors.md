# AI Draft Full Local Error Logging Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local **Prepare draft** failure diagnostically useful by showing the actual exception message and complete chained traceback in the operator's `serve`/`watch` terminal.

**Architecture:** At the existing Anthropic `messages.create` exception boundary, retain the WARNING severity and existing stage/type/status fields but include the raw exception message and `exc_info=True`, which renders the full traceback and chained causes through Python's existing stderr logging handler. Leave browser/job warnings, fallback behavior, request arguments and provider interactions unchanged; do not introduce a persistent log file or explicitly dump prompts, provider bodies, headers or credentials.

**Tech Stack:** Python logging, pytest/caplog, existing Anthropic client integration.

---

## Context and approved disclosure

- The operator observed `WARNING rg2watch.case_draft: AI draft failure stage=anthropic-request exception=TypeError status=unknown`, which is insufficient to identify the error. Current `src/rg2watch/case_draft.py:47-65` catches exceptions from `client.messages.create`, logs only their type and validated status, then raises a generic `DraftUnavailable` that `CaseHandoff.prepare` converts into an editable manual-template fallback.
- The operator explicitly chose full **raw exception messages and complete tracebacks, including chained causes**, in the local terminal by default, not gated by `--verbose`. This supersedes the narrower diagnostic/redaction policy in `docs/plans/2026-10-21-ai-draft-local-diagnostics.md` for this one logging site. Such exception text or traceback may themselves contain sensitive request or provider details; that disclosure is accepted for local testing. Do not deliberately add separate logging of the prompt, note, API key, response body, headers or request object. Do not copy full log output into public issues or chats without inspection.
- `src/rg2watch/cli.py:1229-1237` already configures local stderr logging at INFO for `serve` and `watch`; no new handler/configuration is required. The browser warning remains `AI draft failed; use the manual template` and no HTTP endpoint gains traceback content.

## Files and boundaries

- Modify `src/rg2watch/case_draft.py`: only the existing request-failure log call. Preserve guarded `status_code` extraction, cost accounting, exception chaining and generic `DraftUnavailable`.
- Modify `tests/test_publish.py`: update prior sanitized-log assertions to reflect the newly authorized full error output, while preserving UI/fallback and status-accessor coverage.
- Modify `docs/review-and-sharing.md`: replace the old claim that only a sanitized classification appears with a local-logging warning and a safe way to report the failure without indiscriminately sharing raw traces.
- No changes to `src/rg2watch/static/index.html`, server endpoints, other logging sites, model configuration, retries, Google Drive, or database.

## Task 1: Specify the exact exception/log behavior with failing tests

- [ ] Locate the current `test_ai_draft_request_*` coverage in `tests/test_publish.py`, especially its assertion that log messages exclude sensitive exception text and that `record.exc_info is None`. Keep tests for valid, absent, invalid and raising `status_code` accessors, one warning per request failure, and the unchanged `manual_template`/generic warning result. Replace only assertions contradicted by the newly approved raw-error logging.
- [ ] Add/adjust a fake provider exception with message `draft request type mismatch` raised **from** a cause with message `underlying client detail`. Exercise through `service.prepare(URL, draft=True)` with `caplog.at_level(logging.WARNING, logger="rg2watch.case_draft")`. Assert exactly one `rg2watch.case_draft` WARNING for the request failure, preserving `stage=anthropic-request exception=TypeError status=unknown` (or validated status for a separate test), containing the raw outer message in `record.getMessage()` and the traceback in `caplog.text`, including the chained-cause message and traceback markers. Assert `record.exc_info` is set. Assert the response `warning` remains exactly `AI draft failed; use the manual template`, `summary` remains `manual_template(service._case(URL))`, and neither raw exception message nor traceback appears in the response.
- [ ] Keep an assertion for success: a working fake client emits no request-failure warning and still records cost. For an exception whose `status_code` property raises, assert its diagnostic lookup cannot escape or replace the generic browser/job warning. Raw traceback may now include the status accessor's sensitive message if chained inside the provider exception; do not assert redaction of traceback content at this logging site.
- [ ] Run the focused draft tests before implementation (`uv run pytest tests/test_publish.py -k 'draft' -q`) and verify the newly changed raw-message/traceback expectation fails on the old log call.

## Task 2: Log the error and update local troubleshooting

- [ ] In `src/rg2watch/case_draft.py` inside the existing `except Exception as exc:` block, retain the narrow guarded `status_code` read and strict integer-or-`unknown` validation. Change the **single** existing `logger.warning` call to include `error=%s` with `exc` as an argument and `exc_info=True`, so the configured stderr handler renders the raw error plus Python's full traceback/exception chain. Keep WARNING level so it is visible without `--verbose`; do not call `logger.exception` as an additional second record. Leave cost accounting and `raise DraftUnavailable("AI draft failed; use the manual template") from exc` untouched.
- [ ] Rerun focused tests and then `uv run pytest tests/test_publish.py -q`; inspect actual results. No live Anthropic or Drive calls in automated tests.
- [ ] Update the existing AI-draft troubleshooting paragraph in `docs/review-and-sharing.md` to say the local terminal WARNING now includes the raw error and full chained traceback; warn that those may include sensitive data and should be inspected/redacted before sharing. Keep the instruction to restart `serve`/`watch` and explicitly click **Prepare draft** once after integrating. Do not conflate this with the separate Drive child-access diagnostics.
- [ ] Run the full suite with the live-API opt-in explicitly removed from the test process: `env -u RG2W_LIVE_API uv run pytest -q`. `tests/test_live_api.py` invokes a real paid API when `RG2W_LIVE_API=1`; do not run an unguarded full-suite command. Report the actual pass/fail/skip counts **and confirm that `RG2W_LIVE_API` was unset for this run**. Review the diff for unrelated changes and report how to reproduce the local log. Do **not** infer the real cause of the operator's `TypeError` until its message/traceback is observed on a fresh run.

## Acceptance criteria

1. A failed Anthropic draft request emits exactly one WARNING in local stderr by default, with stage/type/status, raw exception message, and complete chained traceback; there is no second duplicate log record.
2. The preparation job/page still returns only the generic warning and manual template, never raw exception details.
3. Valid, absent, invalid and raising status accessors remain covered; no status accessor error breaks the fallback.
4. A successful draft produces no request-failure warning, and existing cost accounting and publication behavior are unchanged.
