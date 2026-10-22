# Public Results Wide Layout Design

## Goal

Use more of the desktop viewport on the standalone public-results page while preserving readable margins and the existing responsive layout.

## Layout

- Increase the page container's maximum width from 1,180px to 1,600px.
- Retain the existing 24px desktop padding and 14px padding below 780px.
- Apply the wider container uniformly to the header, summary tiles, chart, notes, and footer.
- Keep the existing four-column desktop, two-column tablet, and one-column phone tile layouts.

## Numeric ratios

The polling-station and form ratios must remain on one line when displayed as `counted / total`. Give those two values a dedicated class that prevents wrapping and scales their type more conservatively than the other hero values. The mobile column breakpoints remain the fallback for narrow screens.

## Unofficial-status badge

Render `Незванични резултати` as a compact soft-orange pill above the election title. Use a pale orange background with dark orange text so the badge communicates caution without looking like an error or competing with the title. Provide equivalent colors in dark mode and preserve the existing uppercase lettering and spacing.

## Chart alignment

Give every desktop result row the same explicit columns: `minmax(16rem, 30%)` for the list name, the remaining space for the bar, and `9rem` for the right-aligned vote and percentage value. This prevents content-dependent value widths from shifting each bar's endpoints. Below 780px, retain the existing two-row mobile chart layout.

## Scope

These changes affect only the standalone public-results template. They do not change exported data, private browse pages, export commands, hosting, or publication behavior.

## Verification

Add static regression assertions for the 1,600px cap, non-wrapping ratio style, soft-orange badge, and shared chart columns. Run the focused public-results tests, inspect the generated page, and run the full test suite before completion. Publishing a refreshed export to Cloudflare remains a separate manual action.
