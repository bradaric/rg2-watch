# Hero Accepted-Among-Flagged Count Implementation Plan

> **For agentic workers:** REQUIRED: Use subagent-driven-development (if subagents available) or executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show, on the public results hero only, how many forms in the on-screen `за проверу` set have been marked ok on their current version.

**Architecture:** Do not change `build_tally`, the tally JSON, the CLI, the CSV, or the English results table. In `renderHero`, keep the existing forms-tile first line and append one second line for corrected and as-filed views. Compute that count in the page by joining the already-built flagged-form list to current browse rows and the same live verdict `rvOf` already uses. Difference view has no form list, so it keeps its current single caption. A review submission, its rollback, and a changed review poll each paint the hero once through `renderStatus`, including when status data is absent.

**Tech Stack:** Single-file HTML/CSS/vanilla JavaScript (`src/rg2watch/static/index.html`), Python pytest and the existing Node-powered hero test (`tests/test_tally.py`).

---

## File map and boundaries

- Modify `src/rg2watch/static/index.html`: `#hs-forms` CSS, a small hero helper, the two non-difference `hs-forms` writes inside `renderHero`, `paintHero` beside `renderStatus`, and the success path of `loadReviews`. Do not change `build_tally`, `resultCols`, `tallyCsv`, `NV_LABEL`, operator controls, or the review-column rules.
- Modify `tests/test_tally.py`: the static hero assertion and the existing Node harness in `test_results_javascript_uses_selected_view_and_exports_explicit_unavailable_lists`. The same Node script must also execute the review refresh functions. No new test file and no Python tally fixture.

## Locked behavior

- The flagged count stays. Marking a form ok does not remove it from `за проверу` and does not change `counted`, `clean`, or `excluded`.
- The new line counts only forms that are in the flagged list for the view and municipality currently on screen, and whose **current** version's latest verdict is `ok`.
- `escalate`, `unsure`, no verdict, and an ok stored only on an older version do not count. An older escalation does not cancel a current ok.
- A verdict still saving counts immediately, and drops back if the save fails, because `rvOf` already prefers `LOCAL`. Opened as a file, `REV` is null and `rvOf` uses the verdict baked into the row.
- That update does not wait for status data. `renderStatus` returns before its current hero call when `S` is null, and both `refreshRow` and a table-backed review poll reach the hero only through `renderStatus`. Paint once on that path even when `S` is null. When status is present, do not paint a second time.
- The second line is always present in corrected and as-filed views, including when the count is 0. It is absent in difference view.
- The same `renderHero` serves the embedded hero and presentation mode. Do not add a presentation-only branch.

Exact copy, joined with one `\n` and no trailing blank line:

```text
12 без уочених проблема · 8 за проверу
Од оних за проверу, прихваћено после прегледа: 3
```

```text
без уочених проблема · 8 за проверу изостављено
Од оних за проверу, прихваћено после прегледа: 3
```

The first line is the current expression, unchanged. The number uses the existing `num()` formatter, so 0 is `0` and 1000 is `1,000`. Do not write `исправно`, `неисправно`, or `проверено`.

## Chunk 1: Hero accepted line

### Task 1: Lock the caption and the join with failing tests

**Files:**
- Modify: `tests/test_tally.py` (`test_hero_markup_labelled_and_safe` around the `#hs-valid` assertion; `test_results_javascript_uses_selected_view_and_exports_explicit_unavailable_lists` preamble and the `hs-forms` assertions)
- Test: `tests/test_tally.py`

- [ ] In `test_hero_markup_labelled_and_safe`, assert both `#hs-valid{white-space:pre-line}` and `#hs-forms{white-space:pre-line}`. Assert the page source contains `Од оних за проверу, прихваћено после прегледа: `. Do not ban those words outside the hero.

- [ ] In the Node preamble, extend the existing `D` literal with `rows: []`, and define the same verdict precedence the page uses, before the extracted hero code runs:

```javascript
let REV = null;
const LOCAL = {};
function rvOf(v) {
  return v.u in LOCAL ? LOCAL[v.u] : REV ? REV[v.u] || null : v.rv || null;
}
```

The extracted hero slice calls this global. Do not copy a second verdict rule into the hero.

- [ ] Update every existing non-difference exact `hs-forms` assertion so the current first line is unchanged and the second line is `Од оних за проверу, прихваћено после прегледа: 0` when that case supplies no flagged-form list. The four current expectations are:
  - `10 без уочених проблема · 5 за проверу`
  - `без уочених проблема · 5 за проверу изостављено`
  - `8 без уочених проблема · 7 за проверу`
  - `без уочених проблема · 7 за проверу изостављено`
  Assert each with `textContent.split("\n")`, not a substring, so a missing or extra line fails. Leave the difference expectation `промене броја: без уочених проблема / за проверу` as one line, and assert that difference text does not contain `прихваћено`.

- [ ] Add one dedicated case after those caption checks and restore `T.excluded`, `T.excluded_filed`, `D.rows`, `REV`, `LOCAL`, `heroMuni`, and `heroPicked` before the later chart and CSV cases. Use two municipality rows, `Alpha` and `Beta`, both with `excluded: 5` so the tile number cannot be mistaken for the accepted count. Lists:

```javascript
T.excluded = [
  {id: 1, m: "Alpha"}, {id: 2, m: "Alpha"}, {id: 4, m: "Alpha"}, {id: 3, m: "Beta"}
];
T.excluded_filed = [
  {id: 1, m: "Alpha"}, {id: 5, m: "Alpha"}
];
D.rows = [
  {id: 1, u: "u1", rv: {v: "ok"}, v: [{u: "old1", rv: {v: "escalate"}}]},
  {id: 2, u: "u2", rv: {v: "escalate"}, v: []},
  {id: 3, u: "u3", rv: {v: "ok"}, v: []},
  {id: 4, u: "u4", rv: null, v: [{u: "old4", rv: {v: "ok"}}]},
  {id: 5, u: "u5", rv: {v: "ok"}, v: []},
  {id: 9, u: "u9", rv: {v: "ok"}, v: []}
];
```

Assert all of these, with `heroPicked = true` before selecting a municipality so the municipal default cannot override it:

- Corrected, all forms, all municipalities: second line ends `: 2` (ids 1 and 3). Id 9 is ok but not flagged. Id 4's ok is only on an older version.
- Corrected, clean-only, `heroMuni = "Alpha"`: first line still starts `без уочених проблема ·`, and the second line is `: 1` (id 1 only). The same Alpha second line appears in all-forms mode. Id 1 still counts despite the older escalation.
- Filed, all forms, Alpha: second line is `: 2` (ids 1 and 5). This fails if filed reuses `T.excluded`.
- `REV = {u1: {v: "unsure"}}` overrides the baked `ok` on u1, so corrected Alpha drops to `: 0`.
- `LOCAL.u4 = {v: "ok", pending: true}` makes corrected Alpha `: 1` again. Delete that `LOCAL` entry and it returns to `: 0`. This fails if the hero reads `row.rv` instead of `rvOf`.
- A separate 1000-id corrected Alpha list, every current verdict `ok`, renders `прихваћено после прегледа: 1,000`. Restore the small lists after it.
- A flagged id with no row, and a missing `T.excluded`, both render `: 0` and do not throw. Difference view still has no accepted line while those lists are present.

- [ ] Extend the same Node script so it executes the refresh path, not merely searches the source. After the hero slice, append two exact slices: `function paintHero` through the character before `// ---- table`, and `function rowOf` through the character before `function escalatedCsv`. Do not include the page's `const HTTP` line. In the preamble, before those slices, define `const HTTP = true`, `let revKey = ""`, `let table = null`, `let rows = []`, and no-op `setTitle`, `card`, `ago`, `dur`, `ruleEl`, `when`, `reviewCounts`, `alertForm`, `localHref`, and `rikHref`. Give `document` an `activeElement` of null. The existing `$` stub must serve `status` and `banner`; `banner` needs a `style` object, which the current stub already has. Extend the existing `mk` stub with `append(...nodes) { this.children = this.children || []; this.children.push(...nodes); }`. `renderStatus` calls `append` on the alerts card even when `S.alerts` is empty. Do not leave the current `mk`, which has no `append`.

- [ ] Install a one-row table whose `rows().every` calls the callback with `this.data()`, `this.index()`, and `this.child.isShown()` returning false. Both `cell(index, name).invalidate()` and `cells(null, name).invalidate()` must exist and be no-ops: `refreshRow` calls `cell`, and a poll with a table calls `cells`. `draw()` is a no-op. `refreshRow` returns without painting if this lookup misses, so the row id must match.

- [ ] Wrap the script's final `console.log` in an async function that runs the refresh cases, then prints `{ok: true}`. On rejection, print the error and `process.exit(1)`. Flush at least four promise turns before asserting a fetch result. Replace `fetch` only inside these cases.

- [ ] With `S = null`, one flagged current row, and no baked verdict, assert each of these paints `renderHero` exactly once and changes the accepted line:
  - `submitReview({id, u}, "ok", "", null)` leaves `LOCAL[u].pending === true` and the line at `: 1` before `fetch` settles.
  - A `fetch` that rejects drops that `LOCAL` entry, paints once more, and returns the line to `: 0`. The fake row's child stays hidden, so the failure path must not need `.rvmsg`.
  - Call `loadReviews()` with `table = null` and `fetch` resolving to that JSON body. The line becomes `: 1`. A second call with the same payload does not paint again. Do not invent a separate apply function for the test; the poll entry point is `loadReviews`.
  - A pending `LOCAL` entry for a second flagged url survives that poll and still counts. The poll must not delete `pending: true` entries.

- [ ] Repeat one submission and one changed poll with status present. Each call paints exactly once, not twice. A missing table does not skip the poll paint. Use this fixture, not a partial `S`; `renderStatus` reads every field below before it reaches `paintHero`:

```javascript
S = {
  ts: Date.now() / 1000, watching: true, db: "election.sqlite",
  watcher: null,
  docs: {rg2_seen: 1, stations: 1, rg2_rik: null, new10: 0, new60: 0, replaced: 0, removed: 0, rg7: null, rare: {}},
  analysis: {on: false, paused: null, done: 0, failed: 0, spend: 0},
  ntfy: null, alerts: []
};
D.db = S.db;
rows = [];
```

`analysis.spend` must be a number because the off branch calls `toFixed`. `docs.rare` must be an object because the documents card calls `Object.entries`. `alerts` must be an array because the alerts card reads `length` and then skips the loop. Do not set `analysis.on` or `watcher` truthy in this case: those branches need further fields and are not the refresh behavior under test.

- [ ] Run `uv run pytest tests/test_tally.py -k 'hero_markup_labelled_and_safe or results_javascript_uses_selected_view' -v`. Expected: FAIL because the second line, the `#hs-forms` pre-line rule, and the status-independent hero paint are not implemented yet. Do not weaken an operator-table or CSV assertion to make this pass.

- [ ] Commit the failing tests:

```bash
git add tests/test_tally.py
git commit -m "test: expect accepted count among flagged forms"
```

### Task 2: Render the second line from the current verdict

**Files:**
- Modify: `src/rg2watch/static/index.html` (CSS near line 188, hero helpers after `heroListNames`, `renderHero` forms block near lines 1154-1162, `renderStatus` at lines 385-449, `loadReviews` at lines 667-684)
- Test: `tests/test_tally.py`

- [ ] Add `#hs-forms{white-space:pre-line}` beside the existing `#hs-valid` rule. Do not put `white-space: pre-line` on every `.tile .s`.

- [ ] Add this helper inside the extracted hero slice, after `heroListNames` and before `initHero`. It must not read the bare name `rows`: `renderHero` and `heroMunis` both bind a local `rows` later.

```javascript
function acceptedAmongFlagged(flagged, forms, reviewOf) {
  const byId = new Map();
  for (const form of forms || []) if (form && !byId.has(form.id)) byId.set(form.id, form);
  const seen = new Set();
  let n = 0;
  for (const item of flagged || []) {
    if (!item || seen.has(item.id)) continue;
    seen.add(item.id);
    const review = byId.has(item.id) ? reviewOf(byId.get(item.id)) : null;
    if (review && review.v === "ok") n++;
  }
  return n;
}
function heroFlagged(raw) {
  const list = NV() === "filed" ? T.excluded_filed : T.excluded;
  if (raw === T.overall) return list || [];
  return (list || []).filter(item => item.m === raw.m);
}
```

`raw === T.overall` is the all-municipalities selection. A selected municipality uses `item.m === raw.m`, which is the same municipality string the tally already puts on both the row and the flagged entry. Do not filter by the forms-table municipality control.

- [ ] In `renderHero`, keep the three current `put("hs-forms", ...)` strings exactly. For the two non-difference branches only, append `"\nОд оних за проверу, прихваћено после прегледа: " + num(acceptedAmongFlagged(heroFlagged(raw), D.rows, rvOf))`. Difference keeps the single existing string. Pass `D.rows`, not the chart `rows` declared later in `renderHero`. Call the page's `rvOf`; do not reimplement verdict precedence.

- [ ] Add `paintHero` immediately before `renderStatus`, and make `renderStatus` call it once on the `!S` return and once at the end of the status-present path. Delete the old hero call rather than leaving it beside the new one:

```javascript
function paintHero() {
  if (typeof renderHero === "function" && slots.length) renderHero();
}
function renderStatus() {
  setTitle();
  const box = $("status"), ban = $("banner");
  if (!S) {
    box.replaceChildren();
    ban.style.display = "block";
    ban.textContent = "no status yet";
    paintHero();
    return;
  }
  // existing status cards, unchanged, then:
  box.replaceChildren(...cards);
  paintHero();
}
```

`refreshRow` stays a single `renderStatus()` call. Do not also call `paintHero()` or `renderHero()` there: with status present that would paint twice.

- [ ] Move the `loadReviews` success body so a changed payload always reaches that one `renderStatus()`, including when `table` is missing. An unchanged key still returns before any paint. Do not call `paintHero()` from `loadReviews`:

```javascript
      REV = d;
      for (const [u, x] of Object.entries(LOCAL)) if (!x.pending && x.t < started) delete LOCAL[u];
      const key = JSON.stringify([REV, LOCAL]);
      if (key === revKey) return;
      revKey = key;
      if (table) {
        table.cells(null, "review:name").invalidate();
        table.rows().every(function () {  // never replace a panel the operator is typing in
          if (this.child.isShown() && !this.child()[0].contains(document.activeElement)) this.child(child(this.data())).show();
        });
        table.draw(false);
      }
      renderStatus();
```

- [ ] Run `uv run pytest tests/test_tally.py -k 'hero_markup_labelled_and_safe or results_javascript_uses_selected_view' -v`. Expected: PASS.

- [ ] Commit the implementation:

```bash
git add src/rg2watch/static/index.html tests/test_tally.py
git commit -m "feat: show accepted reviews among flagged forms"
```

### Task 3: Regression

**Files:**
- Test: `tests/test_tally.py`

- [ ] Run `uv run pytest tests/test_tally.py -v` and then `uv run pytest -q`. Expected: PASS. If `uv` or Node is unavailable, say so; do not claim the browser-logic test passed.

- [ ] Confirm the diff does not touch `src/rg2watch/tally.py`, CSV headings, or the English Flagged column. Confirm corrected and filed, all and clean-only, both show the second line, and difference does not. No visual check is required to finish.

## Scope guardrails

- No Python tally logic, metadata, or data-format changes. No new dependencies.
- Do not translate or redesign the operator results table, CSV, review filter, or status-panel review card. Those counts are all forms with findings, not the flagged tally set.
- Do not infer an accepted count from `r.excluded`. That number remains the tally's flagged count and can be larger than the joined list in a partial payload.
- A missing flagged list or a flagged id with no current row is 0, not an error and not a reason to omit the line.
- Do not paint the hero twice for one submission, rollback, or changed poll. `paintHero` is the only new caller of `renderHero` on those paths, and each path reaches it once through `renderStatus`.
- `прихваћено после прегледа` is an operator verdict on that version. It is not an official statement that the minutes are correct, and it is not an RG-7 correction.
