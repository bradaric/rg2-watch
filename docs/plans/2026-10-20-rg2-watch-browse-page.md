# rg2-watch: local browse page

Goal: one self-contained HTML page to navigate, filter and search the stored RG-2 forms,
their findings and their local PDF copies. Rebuilt live by `watch`, and on demand by a
new `rg2watch browse` command.

## Scope

- RG-2 only: documents with `doc_type in ANALYSED_TYPES` (store.py).
- Read-only over the existing SQLite DB. No schema changes.
- No external resources (works offline, opened via file://). Vanilla JS + inline CSS.
- No personal names: show only columns the DB already holds (municipality, station no,
  station name (a place), label, times, statuses, findings).

## Page content

One row per station form (group key: parent, station_key, doc_type):

- election title, municipality, station no + station name, document label
- uploaded_at, first_seen_at (shown DD/MM/YYYY HH:MM, local time)
- state: current / replaced (has older versions) / removed (no active version)
- analysis status (pending, done, failed, off)
- findings: severity badge + rule + summary; row sorted/filtered by highest severity
- links: local PDF (relative path, only if the cached file exists) and RIK original
- older versions (inactive docs, same group) listed under the row, newest first, each with
  its own upload time, local PDF and RIK links, and findings

Local PDF path: `fetch_pdf` names files `sha256(url).pdf` in `_cache_dir(cfg)`; link
relative to the HTML file's directory.

## Controls

- free-text search (municipality, station, label, rule, summary)
- municipality select, election select (when the DB holds more than one parent)
- min severity: any / LOW / MEDIUM / HIGH; rule multi-select (from the data)
- toggles: only with findings, only replaced, only removed, only analysis failed
- sortable columns; counts "showing X of Y"
- filter + sort state kept in `location.hash`, so reloads keep it
- auto-reload every 120 s, with an on/off toggle (state in hash), and "built at" time

## Build

- `rg2watch/browse.py`: `build_page(store, out_path, cache_dir, parent=None) -> int`
  (rows written). Data embedded as JSON in a `<script type="application/json">` block,
  with `</` escaped. Atomic write (temp file + os.replace).
- CLI `rg2watch browse [--parent P] [--out PATH]`; default out `<db dir>/index.html`.
  Prints the path.
- `watch`: rebuild every `browse_interval_s` (config, default 120, 0 = off) from the
  main loop, after `deliver()`. Failures are logged, never stop the watcher.
- Light/dark via prefers-color-scheme; usable at phone width.

## Tests

- grouping (current + older versions, removed), severity ordering, PDF link only when
  the file exists, `</script>` escaping, atomic write, CLI default path, watch rebuild
  interval (0 disables).
