# Hero Fullscreen Label And Footer Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the public hero's presentation button to `Цео екран` and spell out `вештачке интелигенције` in the footer disclaimer.

**Architecture:** Change only the two user-facing strings and the tests that lock them. Leave the hero toggle, Escape exit, kicker, empty-state footer, tally, and accepted-count line untouched.

**Tech Stack:** Single-file HTML/vanilla JavaScript (`src/rg2watch/static/index.html`) and the existing pytest/Node hero tests (`tests/test_tally.py`).

---

## File map and boundaries

- Modify `src/rg2watch/static/index.html`: the initial `#herobtn` text, the off-state assignment in `setHero`, and the data-present footer string in `renderHero`.
- Modify `tests/test_tally.py`: `test_hero_markup_labelled_and_safe` and the two Node assertions in `test_results_javascript_uses_selected_view_and_exports_explicit_unavailable_lists`.
- Do not edit `docs/plans/2026-10-20-results-hero-serbian-wording.md` or `docs/plans/2026-10-20-hero-accepted-among-flagged.md`. Those are historical.

## Locked copy

- Button, off: `Цео екран`.
- Button, on: `Излаз (Esc)`, unchanged.
- Footer when a tally exists: `НЕЗВАНИЧНО — Скенирани записници обрађени помоћу вештачке интелигенције; ово нису резултати РИК-а. Стање у ` plus the existing `epoch(...).slice(-5)` and a final `.`
- Footer when no tally exists: `НЕЗВАНИЧНО — резултати још нису доступни.`, unchanged.
- Kicker remains `НЕЗВАНИЧНО`. Do not use `АИ`, `ВИ`, Latin `AI`, or `NEZVANICNI`.

## Chunk 1: Public copy

### Task 1: Lock the new strings with failing tests

**Files:**
- Modify: `tests/test_tally.py:282`, `tests/test_tally.py:743`, `tests/test_tally.py:1055`
- Test: `tests/test_tally.py`

- [ ] In `test_hero_markup_labelled_and_safe`, replace the required text `Режим презентације` with `Цео екран`. Keep `Излаз (Esc)` in that same tuple. Add `assert "Режим презентације" not in html` and `assert "помоћу АИ" not in html`.

- [ ] In the Node test, replace the footer inclusion at the current line 743 with `НЕЗВАНИЧНО — Скенирани записници обрађени помоћу вештачке интелигенције; ово нису резултати РИК-а. Стање у `. Keep the separate `20:00` assertion and the existing absent-string loop. Add `АИ` to that absent loop so a leftover abbreviation fails.

- [ ] Replace the off-state button assertion `els.herobtn.textContent === "Режим презентације"` with `=== "Цео екран"`. Leave the on-state assertion `Излаз (Esc)` unchanged.

- [ ] Run `uv run pytest tests/test_tally.py -k 'hero_markup_labelled_and_safe or results_javascript_uses_selected_view' -v`. Expected: FAIL on the old button and footer strings. Do not change tally, CSV, or accepted-count assertions to force a pass.

- [ ] Commit the failing tests:

```bash
git add tests/test_tally.py
git commit -m "test: expect fullscreen label and spelled-out footer"
```

### Task 2: Replace the two strings

**Files:**
- Modify: `src/rg2watch/static/index.html:280`, `src/rg2watch/static/index.html:1166`, `src/rg2watch/static/index.html:1246`
- Test: `tests/test_tally.py`

- [ ] Change the initial button text from `Режим презентације` to `Цео екран`. Change only that text node.

- [ ] In `setHero`, change the off-state string from `Режим презентације` to `Цео екран`. Leave `Излаз (Esc)` and `setTab("results")` unchanged.

- [ ] In `renderHero`, replace `помоћу АИ` with `помоћу вештачке интелигенције` inside the data-present footer only. Keep the `НЕЗВАНИЧНО — ` prefix, the semicolon, `ово нису резултати РИК-а. Стање у `, the time slice, and the no-tally fallback exactly.

- [ ] Run `uv run pytest tests/test_tally.py -k 'hero_markup_labelled_and_safe or results_javascript_uses_selected_view' -v`. Expected: PASS.

- [ ] Commit the implementation:

```bash
git add src/rg2watch/static/index.html tests/test_tally.py
git commit -m "feat: rename hero fullscreen control and spell out AI"
```

### Task 3: Regression

**Files:**
- Test: `tests/test_tally.py`

- [ ] Run `uv run pytest tests/test_tally.py -v` and then `uv run pytest -q`. Expected: PASS. If `uv` or Node is unavailable, say so; do not claim the browser-logic test passed.

- [ ] Confirm the diff does not touch `src/rg2watch/tally.py`, the accepted-count line, the kicker, or the empty-state footer. No visual check is required to finish.

## Scope guardrails

- No behavior change to entering or leaving the enlarged hero. Escape, `?hero=1`, and the results-tab switch stay as they are.
- Do not shorten or translate the RIK disclaimer, and do not move it into the kicker.
- Do not rewrite historical plan files to match the new copy.
