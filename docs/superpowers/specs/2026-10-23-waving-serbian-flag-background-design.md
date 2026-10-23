# Waving Serbian Flag Background Design

## Goal

Replace the broad tricolor page wash with a large, unmistakably real Serbian state flag that appears to wave from the upper-left and fades diagonally before it interferes with election data.

## Artwork

Use the official Serbian state flag, including the coat of arms, as inline SVG artwork. Preserve the official colors and crest details. Record the public-domain Wikimedia Commons source in a nearby source comment.

Inline artwork is preferred over a raster image or separate asset because it remains sharp at every viewport size, works offline, preserves the public page's Content Security Policy, and keeps exported public results in one self-contained HTML file.

## Presentation

- Position one oversized decorative flag layer above and left of the viewport.
- Rotate it slightly downward toward the page center.
- Shape its outer contour into a soft fabric wave.
- Add restrained, static highlights and shadows to imply folds without animation.
- Use a diagonal alpha mask so the flag is strongest at the upper-left, substantially reduced behind the header, and transparent before the primary data area.
- Use lower opacity in light mode and slightly higher opacity in dark mode.
- Replace the existing full-page horizontal tricolor gradient rather than stacking both treatments.

## Integration and Accessibility

Apply the treatment to both `src/rg2watch/static/index.html` and `src/rg2watch/static/public_results.html`. The flag layer must be outside normal layout, ignore pointer input, and be marked `aria-hidden="true"`. Existing panels remain sufficiently opaque to preserve text and data contrast.

The effect is static. It must not add animation, network requests, scripts, layout changes, or new user controls. Existing responsive behavior, public export behavior, and CSP remain unchanged.

## Verification

- Add focused assertions that both pages contain the inline official flag layer.
- Verify the decorative layer is hidden from assistive technology and pointer input.
- Verify the diagonal mask and adaptive light/dark opacity treatment are present.
- Confirm the public export remains self-contained and its CSP is unchanged.
- Run the focused browse and public-results test suites and inspect the final diff.
