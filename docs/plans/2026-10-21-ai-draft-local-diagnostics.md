# AI Draft Local Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the immediate failure of **Prepare draft** diagnosable in the operator's local `serve`/`watch` terminal without leaking provider payloads or internal case content.

**Architecture:** Instrument the existing Anthropic request exception boundary in `CaseDrafter.draft`. Log a single sanitized failure record using the application's existing stderr logger, and retain the current generic warning/manual-template fallback in the page and job result. Do not add a log file, HTTP diagnostics endpoint, provider retry, or change drafting/publication behavior.

**Tech Stack:** Python logging, existing Anthropic SDK, pytest/caplog.

---

## Evidence and boundaries

- The operator supplied a completed `/case/jobs/<id>` preparation result in conversation with `warning: "AI draft failed; use the manual template"` (operator-provided observation, not independently reproducible from repository files). In `src/rg2watch/case_draft.py:43-53`, that exact warning is produced only when `client.messages.create(...)` raises. The cached PDF, escalation eligibility, credential-client creation, model-pricing check, and spend-cap check therefore got past their earlier gates in that run. The underlying provider exception is not yet known; do not claim a particular API status, model problem, or auth problem without a new observed log.
- `src/rg2watch/case_handoff.py:169-179` catches the draft failure and returns the manual template plus warning. `src/rg2watch/static/index.html:607-615` intentionally displays only a generic warning. `src/rg2watch/cli.py:1229-1237` configures INFO-or-higher logging to stderr for `watch` and `serve`; this is the local terminal to use. `src/rg2watch/publication_service.py` logs only publication-job failures; preparation falls back successfully and ends as a `done` job.
- **Modify:** `src/rg2watch/case_draft.py` (sanitized request-failure diagnostic only).
- **Test:** `tests/test_publish.py` (request exception, sanitized log, successful request, fallback semantics).
- **Document:** `docs/review-and-sharing.md` (where to find and safely share the diagnostic classification).
- Do not modify the server, the browser warning, configuration, database schema, Drive publication, or other analysis-worker logging. No live Anthropic call in automated tests.

## Task 1: Reproduce and log the failing request safely

- [ ] Add a focused test in `tests/test_publish.py` using the existing `handoff(tmp_path)` fixture/helper and a fake client's `messages.create` that raises an exception with a deliberately sensitive `__str__` (e.g. `secret-token internal-note prompt-body`). Set a safe integer `status_code` on the fake exception (for example 404). With `caplog.at_level(logging.WARNING, logger="rg2watch.case_draft")`, call `service.prepare(URL, draft=True)` after assigning a `CaseDrafter` with that fake client.
- [ ] Assert preparation still returns the manual template and the **unchanged** `AI draft failed; use the manual template` warning, with no exception sent to the caller. Assert exactly one warning log from `rg2watch.case_draft` for the request failure; it must contain a stable stage label such as `anthropic-request`, the exception **type**, and `status=404`. Assert the sensitive sentinel strings, URL, review note, prompt, exception message and traceback are absent from both the log record's rendered text and the job/browser-facing warning. Assert `record.exc_info is None` and no exception instance is supplied as a logging argument. (Avoid assertions on other logging records from unrelated components.)
- [ ] Add a second fake request exception with no valid integer `status_code` to assert the log uses `status=unknown`, without interpolating arbitrary attribute objects. Add a third fake exception with a `status_code` property that **raises** an exception containing a sensitive sentinel when read; exercise it through `service.prepare(URL, draft=True)` and assert one sanitized `status=unknown` log, no sentinel in log text, no exception escaping, and the unchanged generic `warning` plus manual template. Add a success-path assertion that a working `FakeMessages` call produces no failure record and still records draft cost.
- [ ] Run the focused tests before implementation (e.g. `uv run pytest tests/test_publish.py -k 'draft' -q`); confirm the new logging assertions fail on the current code.
- [ ] In `src/rg2watch/case_draft.py`, obtain `logging.getLogger("rg2watch.case_draft")`. Inside the existing `except Exception as exc:` around `client.messages.create`, emit **one WARNING** log before preserving the existing failed-cost accounting and generic `DraftUnavailable` chaining. Extract `status_code` in a narrow guarded block (`try: status = getattr(exc, "status_code", None)` / `except Exception: status = None`), then accept it only if `type(status) is int` and `100 <= status <= 599`; otherwise use the fixed string `unknown`. No exception from the status accessor or its value may escape or alter the existing manual-template fallback. Render only constant text plus `type(exc).__name__` and that sanitized status. Never log `str(exc)`, `%s` with `exc`, `exc.args`, `exc.body`, request/response objects, prompt, note, case URL, model response, `exc_info`, `stack_info`, `logger.exception`, or arbitrary raw provider headers. Do not attach `extra` values that contain the exception object. The record should read like `AI draft failure stage=anthropic-request exception=NotFoundError status=404`.
- [ ] Rerun the focused tests and confirm both the safety and fallback assertions pass. The status is diagnostic evidence, not a user-facing error and not proof of a specific root cause.

## Task 2: Document and verify the operator workflow

- [ ] Add a brief troubleshooting paragraph in `docs/review-and-sharing.md` after the existing preparation description: when **Prepare draft** falls back with the generic warning, inspect the terminal that launched `rg2watch serve`/`watch`; a single sanitized `rg2watch.case_draft` WARNING records the provider exception class and integer HTTP status (or `unknown`). Explain that this is local stderr, not browser output or a persistent debug file; do not ask the operator to paste full job JSON, provider payloads, URLs, prompts, notes, credentials, or raw traceback. The classification alone can be shared for further diagnosis. Restart the local server after a diagnostics release, then explicitly click Prepare draft to reproduce; no automatic AI call.
- [ ] Run `uv run pytest tests/test_publish.py -q` and then `uv run pytest -q`; report actual results. If an environment-dependent test fails, record the exact failure rather than asserting a pass. Confirm no networked Anthropic or Drive operation was triggered by tests. Review the diff for leakage paths and unrelated changes.
- [ ] In the developer handoff, ask the operator for the sanitized `AI draft failure stage=... exception=... status=...` line from their local server after one explicitly initiated click. Do not diagnose the original failure more precisely until that evidence exists.

## Acceptance criteria

1. One failed provider request creates one safe, local stderr WARNING identifying its exception class and valid HTTP status (or `unknown`), at the default `serve`/`watch` INFO level; no `--verbose` requirement.
2. No raw exception text/traceback, credentials, internal note, prompt, case URL or provider payload is logged by this diagnostic or returned in the job/browser warning.
3. The manual template, warning text, cost accounting, success path, publication eligibility and UI behavior are unchanged.
4. Automated tests exercise valid/missing/raising status accessors and successful drafting without a live provider call.
