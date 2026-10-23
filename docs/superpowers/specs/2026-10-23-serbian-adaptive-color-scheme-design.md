# Serbian Adaptive Color Scheme Design

## Goal

Give both the operator dashboard and standalone public-results page a coherent Serbian election-monitor identity while preserving their dense, operationally useful layouts. The design must remain readable, accessible, offline-capable, and adaptive to the device's light or dark preference.

## Palette

Use cool navy-based neutrals in both modes rather than the existing generic gray palette.

- Light mode uses a cool blue-gray page background, white or lightly blue-tinted panels, dark navy text, and muted slate-blue secondary text.
- Dark mode uses a deep midnight-blue page background, lighter navy panels, near-white text, and muted pale-blue secondary text.
- Serbian red provides restrained branding and selected emphasis.
- Brighter blue remains available for links, focus indicators, and secondary interactive states.
- Semantic warning, success, and severity colors remain distinct and keep sufficient contrast against their surfaces.

## Serbian Flag Treatment

Add a very faint red-blue-white horizontal treatment to the page background. It should evoke the Serbian tricolor without placing a prominent literal flag or coat of arms behind operational data.

The treatment must:

- remain low-opacity so it does not compete with content;
- preserve legibility by keeping cards, tables, dialogs, and controls sufficiently opaque;
- use CSS only, with no image or network dependency;
- remain visible but restrained in both light and dark modes;
- avoid changing the existing Content Security Policy of the public page.

## Components and Behavior

Apply matching theme tokens to `src/rg2watch/static/index.html` and `src/rg2watch/static/public_results.html`. Existing page structure, responsive breakpoints, controls, tables, status labels, and chart behavior remain unchanged.

Red must not become the sole indicator of warnings or election-status meaning. Existing labels, text, shapes, and severity treatments continue to communicate state independently of color. Multi-series result colors remain distinguishable rather than being replaced wholesale by national colors.

## Scope

This is a CSS-focused visual change affecting the operator dashboard and exported public-results page. It does not change data, result calculations, review workflows, export behavior, hosting, or publication.

## Verification

- Add focused static assertions for the adaptive navy tokens and CSS tricolor treatment on both templates.
- Run the focused browse/render and public-results tests.
- Inspect the final diff for behavioral or unrelated changes.
- Confirm that the templates retain adaptive `prefers-color-scheme` handling and the public page remains self-contained.
