# Operator Results Editorial Polish Design

## Goal

Carry the public page's restrained editorial treatment into both operator result presentations while preserving the operator interface's density and controls. Show every electoral list individually in the hero chart.

## Visual Treatment

- Restyle the unofficial kicker with the established red status treatment.
- Replace the heavy hero divider with a thin red rule.
- Give the first hero metric a restrained red top accent.
- Tighten metric typography and spacing without reducing information.
- Add a small `Пресек резултата` eyebrow above the hero chart.
- Refine chart-row separators and value typography while preserving existing series colors and scale.
- Give the lower operator-only result sections consistent heading spacing and hierarchy.

Apply the treatment in the embedded Results tab and full-screen hero. Existing controls, warnings, tables, adaptive theme, flag background, and responsive behavior remain intact.

## Results Lists

Remove the `TOP_LISTS` limit and the `Остале (…)` aggregate row from the hero chart. Create chart-row slots dynamically so every electoral list is rendered, sorted using the existing vote or absolute-difference ordering.

Preserve ballot numbers, list names, vote totals, percentages, difference-mode values, and the existing color sequence. Embedded and full-screen views may grow and scroll naturally when many lists exist.

## Scope

Changes are limited to `src/rg2watch/static/index.html` and focused operator-results tests. Tally data, calculations, review workflows, controls, exports, public results, and publication behavior remain unchanged.

## Verification

- Add static assertions for the kicker, red divider, first-metric accent, chart eyebrow, and operator section hierarchy.
- Update the JavaScript rendering harness to verify dynamic slots and every list row with no `Остале` aggregate.
- Run focused tally and browse tests.
- Render-check embedded and full-screen result styles where practical.
- Inspect the final diff for unrelated changes.
