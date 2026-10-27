# Ballot-Denominator Percentages Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Calculate every electoral-list share and margin against ballots in the box (`valid + invalid`) instead of valid votes.

**Architecture:** Keep vote totals and turnout unchanged. Add an aggregate denominator covering the same reconciled forms that contribute list totals, expose ballots-in-box in sanitized station data, and update operator/public rendering plus exports to consume that denominator. If the denominator is unavailable for any contributing form, suppress the aggregate percentage rather than mixing cohorts.

**Tech Stack:** Python 3.12, SQLite, server-rendered HTML with vanilla JavaScript, pytest, Node syntax/runtime checks.

---

## Chunk 1: Denominator data and rendering

### Task 1: Add the aggregate list-share denominator

**Files:**
- Modify: `src/rg2watch/tally.py`
- Modify: `docs/review-and-sharing.md`
- Test: `tests/test_tally.py`
- Test: `tests/test_rg7.py`

- [x] Add failing tests proving list percentages use ballots in the box and become unavailable when a contributing form lacks that value.
- [x] Run the focused tests and confirm they fail against the valid-vote denominator.
- [x] Track ballots in the box for the same forms contributing reconciled list totals.
- [x] Emit a nullable `lists_ballots_in_box` denominator without changing vote totals or turnout.
- [x] Update CLI national/municipal and corrected/as-filed percentages and headings, plus user documentation.
- [x] Test paired corrected/as-filed cohorts when one denominator is missing and when readable denominators differ; list vote totals must retain the paired cohort while percentages become unavailable when needed.
- [x] Run focused tally tests.

### Task 2: Update operator percentages and exports

**Files:**
- Modify: `src/rg2watch/static/index.html`
- Test: `tests/test_tally.py`

- [x] Add failing JavaScript assertions for aggregate, station, Top 2, margin, detail-table, and CSV percentages.
- [x] Replace valid-vote denominators with ballots-in-box denominators.
- [x] Preserve difference-mode percentage-point calculations using each view's new denominator.
- [x] Test unavailable non-tied and tied station margins: retain vote counts but show no percentage.
- [x] Run the operator JavaScript contract test.

### Task 3: Update public percentages and sanitized data

**Files:**
- Modify: `src/rg2watch/public_results.py`
- Modify: `src/rg2watch/static/public_results.html`
- Test: `tests/test_public_results.py`

- [x] Add failing tests for sanitized `ballots_in_box` station data and public list/station percentages.
- [x] Export only the additional corrected numeric field and aggregate denominator.
- [x] Update public aggregate, Top 2, margin, and expanded-station percentages.
- [x] Test unavailable non-tied and tied station margins: retain vote counts but show no percentage.
- [x] Verify private IDs, links, reviews, and correction metadata remain absent.

### Task 4: Verify the complete change

**Files:**
- Test: `tests/test_tally.py`
- Test: `tests/test_public_results.py`

- [x] Run focused tally/public tests.
- [x] Run the full pytest suite.
- [x] Run Node syntax checks for both operator and public scripts.
- [x] Run `git diff --check` and inspect the final diff for unrelated changes.
