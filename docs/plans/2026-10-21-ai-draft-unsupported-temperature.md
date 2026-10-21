# Remove Unsupported AI Draft Temperature Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow **Prepare draft** to reach the installed Anthropic SDK by removing the unsupported `temperature` argument from its draft request.

**Architecture:** Change only the `CaseDrafter.draft` request kwargs and add a strict fake-client regression test that rejects unexpected arguments. Preserve the prompt, model, token limit, timeout, pricing/cost accounting, local exception logging and generic manual-template fallback. The provider's default generation temperature replaces explicit `temperature=0`; no alternate SDK path or dependency pin.

**Tech Stack:** Python 3.12, existing Anthropic SDK integration, pytest.

---

## Root cause and scope

- Operator-provided local traceback: `src/rg2watch/case_draft.py:48` called `client.messages.create` and the installed SDK raised `TypeError: Messages.create() got an unexpected keyword argument 'temperature'` before an API request. This is direct evidence of the immediate draft failure, not evidence of whether later provider calls will succeed.
- `src/rg2watch/case_draft.py:47-50` currently passes `model=self.cfg.model`, `max_tokens=600`, `temperature=0`, `messages=[...]`, and `timeout=30.0`. `tests/test_publish.py:301-313` currently uses a `FakeMessages.create(**kw)` that accepts any kwargs and therefore missed this incompatibility. Existing draft tests around lines 401-415 check content and spend accounting.
- User chose removal of `temperature=0` over changing/pinning the SDK, accepting provider-default sampling behavior. Changes to the **manual fallback template** were explicitly deferred to a separate future design/plan; do not edit the template or UI here.
- Modify only `src/rg2watch/case_draft.py` and `tests/test_publish.py`. Do not change analysis requests, config, dependencies, server, publication, error logging, or documentation for this fix.

## Task 1: Lock in the SDK-compatible request shape

- [ ] In `tests/test_publish.py`, add a focused regression test using `handoff(tmp_path)` and `CaseDrafter`, with a fake `messages.create` whose **explicit keyword-only signature** accepts exactly `model`, `max_tokens`, `messages`, `timeout` (not `temperature`, and no `**kwargs`). It should capture these values and return the existing `FakeMessages`-style Cyrillic response and usage data (reuse the fake response helper if possible without weakening the strict signature). Call `service.prepare(URL, draft=True)`.
- [ ] Assert the call produced a Cyrillic AI summary rather than the manual template; `warning is None`; the captured model, `max_tokens=600`, `timeout=30.0`, and single user prompt with the internal note match existing behavior; the draft cost was recorded. This test must fail before the fix with the observed unexpected-`temperature` `TypeError` caught into the generic warning, not pass via manual fallback. No live API or Drive operation.
- [ ] Run `uv run pytest tests/test_publish.py -k 'sdk_compatible_draft_request' -q` (name the new test accordingly) and record the expected pre-fix failure; the failure may appear as a generic `warning` assertion because `service.prepare` catches the SDK-shaped `TypeError`.

## Task 2: Apply the minimal request correction and verify

- [ ] Remove only `temperature=0` from the `client.messages.create(...)` call in `src/rg2watch/case_draft.py`; leave all other kwargs and the exception/cost/fallback paths untouched. Do not substitute another temperature configuration or silently change the prompt.
- [ ] Rerun the strict regression and `uv run pytest tests/test_publish.py -q`; report actual results. Confirm existing fallback, error traceback and cost tests remain green.
- [ ] Run the full suite with the live paid-API opt-in explicitly absent: `env -u RG2W_LIVE_API uv run pytest -q`. Report exact pass/fail/skip counts and confirm `RG2W_LIVE_API` was unset for that test process. Do not run a live Anthropic call as an automated test. Review the diff for the two-file scope.
- [ ] After integration, tell the operator to restart their local `serve`/`watch`, explicitly click **Prepare draft** once, and report the success or the new traceback. Do not assert the draft will necessarily succeed beyond the previously failing SDK call; credentials, model support and provider response remain unverified until then.

## Acceptance criteria

1. The draft request does not pass `temperature` and works with a client whose `create` method rejects unknown kwargs.
2. Existing prompt, model, token budget, timeout, spend tracking, fallback and local debugging behavior are preserved; provider-default temperature is accepted.
3. No manual-template changes or live provider/Drive calls occur during automated verification.
