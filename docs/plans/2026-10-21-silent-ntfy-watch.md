# Silent ntfy Watch Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator run `rg2watch watch --no-ntfy` (including `scripts/rehearse.sh <id> --no-ntfy`) without any ntfy posts, while printing and permanently handling both existing pending and newly generated findings.

**Architecture:** Make this a watch-process-only override of `Config.ntfy_topic`, applied after `load_config` resolves TOML and `RG2W_NTFY_TOPIC` and before any `Notifier` is constructed. Reuse the existing `Notifier` no-topic path and `_handlers` delivery/marking path rather than bypassing delivery or changing persisted configuration. This also prevents watcher self-status pushes and reports ntfy disabled in the live status panel.

**Tech Stack:** Python 3, argparse, pytest, httpx MockTransport, Bash wrapper, SQLite store.

---

## File map and invariants

- Modify `src/rg2watch/cli.py`: declare watch-only CLI flag and apply override in `_cmd_watch`/`_config` before notifier construction. No changes to `notify.py`, `store.py`, or `scripts/rehearse.sh` are needed: the notifier already treats `ntfy_topic=None` as stdout-only and marks findings delivered; the script forwards extra watch arguments.
- Modify `tests/test_until_done.py`: exercise the actual watch CLI with configured ntfy topic, disabled status, old/new findings and a later run. Add a focused CLI override test if the full watch fixture alone cannot expose both config and environment precedence.
- Modify `docs/runbook.md`: document invocation, precedence, stdout/status behavior, and permanent consumption of outstanding pending alerts.
- Default (flag absent) must retain the existing ntfy behavior. The flag must not affect `add`, `analyze`, `serve`, or any non-watch command; it must not persist to TOML or globally remove environment variables.
- `--no-ntfy` is **not** `--baseline`, `--no-analyze`, or a dry run: watching, analysis, storing findings, and database marking still happen. An interrupted run can leave undelivered findings unmarked if its delivery/marking step has not run; do not claim a blanket purge of every finding on invocation. A subsequent normal run can send only such still-pending findings.

## Chunk 1: Watch flag and behavior

### Task 1: Specify the flag and precedence with failing tests

**Files:** Test: `tests/test_until_done.py`; modify later: `src/rg2watch/cli.py:78-106,214-222`.

- [ ] Add a test using `_parser()` (import from `rg2watch.cli`) to assert `watch --no-ntfy` parses successfully and other subcommands reject that flag. Test `watch` without the flag still has `no_ntfy == False`.
- [ ] Add parametrized tests for `rg2watch.toml`-provided topic and `RG2W_NTFY_TOPIC`-provided topic, using `tmp_path` and `monkeypatch`; exercise the same config-resolution/override boundary the CLI will use. Assert the flag clears the *effective* topic despite environment precedence, and flag absence preserves it. Do not inspect or log real topic values. If testing `_config` directly, construct args from `_parser()` and pass the config path; account for the autouse `tests/conftest.py` environment-topic remover by setting the test topic inside the test.
- [ ] Run `uv run pytest -q tests/test_until_done.py` and confirm the new tests fail for the absent flag/override, not for a fixture or network failure.

### Task 2: Implement the minimal CLI override

**Files:** Modify `src/rg2watch/cli.py:78-106,214-222`.

- [ ] Add `s.add_argument("--no-ntfy", action="store_true", help="print and mark alerts handled without sending ntfy (including pending alerts)")` to the `watch` subparser only.
- [ ] In `_config(args)`, immediately after `cfg = load_config(args.config)`, add `if getattr(args, "no_ntfy", False): cfg.ntfy_topic = None`. The `getattr` keeps all other command parsers unchanged. Leave notifier construction, `_handlers`, delivery threads, shutdown flush, and `--until-done` behavior untouched.
- [ ] Run `uv run pytest -q tests/test_until_done.py tests/test_notify.py tests/test_config.py`; expect all pass.

### Task 3: Verify no posts, persistent handling, and unchanged default

**Files:** Test: `tests/test_until_done.py`; inspect as needed: `src/rg2watch/cli.py:562-603,710-900`, `src/rg2watch/store.py` finding helpers, `tests/test_e2e_replay.py:294-316`.

- [ ] Extend the `rehearsal` fixture or add a dedicated equivalent using its replay-server and temporary DB setup. Configure a fake ntfy topic; monkeypatch `cli._notifier_http` to a client with `httpx.MockTransport` that records requests, so no real ntfy endpoint can be contacted. Set `--serve-port 0` and `--until-done` for a bounded run. Set topic from TOML in one case and, in a separate test, from `monkeypatch.setenv("RG2W_NTFY_TOPIC", "test-topic")` after fixtures set up.
- [ ] Seed an existing unalerted HIGH finding in the registered election using `Store` (`apply_events` to ensure a document record, then `add_finding`); also arrange a new HIGH or MEDIUM finding during the watch (e.g. mocked `Watcher.run_once` invokes its configured `on_events` callback with a suitable event once, or arrange a test-controlled `AnalysisPipeline` callback). Assert both IDs start unalerted, the muted watch makes **zero** requests, both are printed to captured stdout, and `store.unalerted(parent, {"HIGH", "MEDIUM"})` is empty after shutdown. Assert the final `status.json` ntfy snapshot has `enabled: false` and a watcher status alert generated by calling `notifier.status` or exercising `watcher.check_health` is printed but never posted. Do not use `--baseline` for the new-event portion because it suppresses first-pass findings.
- [ ] Start a second watch on that same DB **without** `--no-ntfy` using a recording MockTransport. Assert the formerly handled findings do not generate posts. Separately seed another pending HIGH finding, run without the flag, and assert normal ntfy delivery posts and marks it (or use an existing focused default notifier test for the post behavior plus CLI assertion of topic retention). If the replay fixture's second full pass is expensive, use a targeted `_handlers` test on the same persisted DB for the second-run delivery check, while still testing the first run through CLI.
- [ ] Run `uv run pytest -q tests/test_until_done.py tests/test_notify.py tests/test_e2e_replay.py`; expect all pass. Avoid sleep-based assertions; use the fixture's shortened done confirmation and final `deliver(flush=True)` path. Where a fake HTTP client is injected, do not use a real URL or an actual phone topic.

## Chunk 2: Operator guidance and regression checks

### Task 4: Document safe usage

**Files:** Modify `docs/runbook.md:11-24,193-220`.

- [ ] In Quick start, show `scripts/rehearse.sh <id> --no-ntfy` and direct `uv run --env-file .env rg2watch watch --parent <id> --no-ntfy` (with the usual config/db arguments as relevant). Clarify that a plain rehearsal still sends alerts when a topic is configured, and `--no-ntfy` wins over either TOML topic or `.env`/`RG2W_NTFY_TOPIC` for that watch process only.
- [ ] In watcher behavior, state that watcher status messages and findings still print locally, the status panel shows ntfy disabled, and successful delivery/marking consumes both old pending and new findings without pushing them later. Highlight that an interrupted run before marking can leave findings pending; use a separate DB if the operator wants to preserve an existing backlog. Do not claim the flag prevents unrelated API/network operations (RIK, model analysis, etc.).

### Task 5: Verify and hand off implementation

- [ ] Run `uv run pytest -q`; expect exit 0. If the full suite has unrelated failures, report exact failures and distinguish them from tests above.
- [ ] Run `uv run rg2watch watch --help` and confirm `--no-ntfy` is documented; check `scripts/rehearse.sh` passes extra args unchanged without editing the script.
- [ ] Review `git diff --check` and the changed-file list; keep changes limited to the CLI, targeted tests and runbook unless a test-justified adjustment is necessary. Do not log or commit actual ntfy topics or credentials.
- [ ] Implementation commits, if used by the developer, must follow the repository's applicable conventional-commit guidance. The architect's plan-file commit is a separate gate and is not part of these implementation tasks.
