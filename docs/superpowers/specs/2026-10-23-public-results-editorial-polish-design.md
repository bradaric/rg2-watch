# Public Results Editorial Polish Design

## Goal

Give the public-results page a more finished editorial hierarchy without making it busier or changing the established Serbian navy, red, white, and flag treatment. Show every electoral list individually rather than combining lower-scoring lists.

## Visual Treatment

- Add a thin red rule beneath the header to give the page stronger structure.
- Refine the hierarchy among the unofficial-status badge, election title, timestamp, and municipality control without materially increasing header height.
- Add a small `LIVE TALLY`-style eyebrow above the results-chart heading.
- Give the first summary tile a restrained red top accent while leaving the remaining tiles navy-neutral.
- Make conservative adjustments to metric and chart-row typography and spacing.
- Avoid additional shadows, animation, gradients, or decorative artwork.

The existing adaptive light/dark palette, waving Serbian flag, responsive layout, cards, and chart structure remain in place.

## Results Lists

Render every electoral list as its own chart row, sorted from highest to lowest vote count. Remove the eight-row limit and the `Остале (…)` aggregate row entirely. Each row continues to show its list name, vote total, percentage, and proportional bar.

The page may grow naturally when an election has many lists. Do not add pagination, collapsing, or another aggregation threshold.

## Scope

Changes are limited to `src/rg2watch/static/public_results.html` and focused public-results regression tests. Generated data, calculations, CSP, export format, operator dashboard, and publication behavior remain unchanged.

## Verification

- Add static assertions for the red header rule, chart eyebrow, and first-tile accent.
- Update the rendering harness to verify that every supplied electoral list gets its own row and no `Остале` row is generated.
- Run the focused public-results test suite.
- Render-check the page in light and dark modes.
- Inspect the final diff for unrelated changes.
