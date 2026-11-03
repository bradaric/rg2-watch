# Runtime reference

[README](../README.md) · [Operator runbook](runbook.md)

Detailed runtime behavior for operators and developers. Configuration options and defaults are
also documented in [rg2watch.example.toml](../rg2watch.example.toml); use
`uv run rg2watch <command> --help` for command options.

## File layout and local server

Each database has its own page folder, `<db dir>/<db stem>/` (`data/election-662405.sqlite` -> `data/election-662405/`: `index.html`, JSON snapshots and vendored libraries); the PDF cache stays shared at `<db dir>/pdfs/`. Operator output is JSON-only; obsolete `data.js`/`status.js` twins are not generated. Two elections can run side by side (`serve` on one DB, `watch` on another): they never write the same files, and each server only accepts review verdicts for its own DB. `status.json` and `data.json` carry the DB name and the page shows a red banner if they disagree. Old files directly in `data/` are from the earlier shared layout and are safe to delete after verifying they are not current snapshots.

`watch` serves the page on `serve_port` (default 8770; `--serve-port`, `0` = off); if that port is busy it tries the next ones up to +10. The URL actually used is printed (`page: http://127.0.0.1:PORT`), recorded in `status.json` and shown by `rg2watch status`; open the URL it prints. `rg2watch serve [--port N]` serves the same files without
watching (same port fallback, prints the URL before a missing or stale tally is rebuilt). The private
operator page must be opened at that HTTP URL, never with `file://`. The server binds 127.0.0.1 only,
answers GET/HEAD, and serves a fixed allow-list
(`index.html`, `status.json`, `data.json`, `data-meta.json`, `reviews.json` (live verdicts), the vendored libraries, `pdfs/<sha256>.pdf`; the `.js` twins are not served, and cross-site requests are refused). Strict same-origin `/operator-feed` and `/operator-bootstrap` reads return bounded revision pages. The DB,
`.env` and everything else are 404. No free port: a warning, and the watcher carries on. Writes are
limited to `POST /review`, audited numeric-correction and document-classification routes,
and the explicit case preparation/publication routes described in [Review and sharing](review-and-sharing.md#public-case-handoff-google-drive-and-viber-copy). They
are accepted only from the page itself - Host
127.0.0.1/localhost on the server's port, `Sec-Fetch-Site: same-origin` (or no such header and an
`Origin` equal to the server's), `Content-Type: application/json`, header `X-RG2W: 1` (a cross-site
form or fetch cannot send it without a preflight, which is never answered), a bounded JSON body and
an exact stored document URL. Classification accepts only confirm-as-RG-2, quarantine-as-mislabeled-
RG-7, or revocation for a
RIK-listed RG-2 URL; review accepts verdict `ok`/`escalate`/`unsure` and a note of at most 500
characters. Anything else is 403/400 and nothing is written.

SQLite stores a stable database-instance ID and a transactionally updated operator-change journal.
The journal retains the newest 10,000 revisions per election. A browser behind the durable floor, a
restored older database, or a changed presentation/config generation discards incompatible form and
tally state and bootstraps again. All full snapshot writers share a per-database/page process lock.
The feed never invokes RIK or a model and never returns token inputs or secrets.

## Politeness

All lanes share one rate limiter (default 2 requests/s, `max_rps`) that halves on timeouts and 5xx
and recovers slowly. PDF downloads for analysis use a separate limiter (`download_max_rps`, default
0.5/s, halved on 429/5xx/timeouts but not on slow transfers), so analysis never slows detection
and the combined load on RIK stays at most `max_rps + download_max_rps` (2.5 requests/s by default). The watcher keeps running when RIK is overloaded. All lane work is resumable
and charged to one per-tick request budget (`tick_request_budget`, default 40, minimum 5) in priority
order: rare-type sweeps, removal re-checks, watchlist, RG-2 probe, missing stations, safety sweep.
A share of the budget (`reserved_share`, default 0.25) is held back from watchlist and probe for the
safety sweep and the missing-station lane while they have work, so a busy probe cannot starve them.
Rare-type count checks are not charged and run every `rare_interval_s`, so a new RG-8/RG-9 is never
held up by a long probe round or sweep (RG-8/RG-9 sweeps go first); every `rare_resweep_interval_s` all non-empty rare types are
re-swept to catch replacements that leave counts unchanged. Never point `replay`, tests or
experiments at the live site beyond what an election night needs.

## Privacy

Only polling-station and municipality names, document types, timestamps and URLs are stored or sent.
No personal names are stored or included in alerts. Content fetched from RIK is treated as untrusted data.
Form scans are sent to the Anthropic API for analysis; the report schema has no name fields and the
prompt forbids transcribing names. Text inside the scans is treated as data, never as instructions.
An explicit case-draft click sends document context and the operator's review note to Anthropic; the
operator must edit/approve the returned public text before a separate Publish click. Google OAuth
client secrets and tokens stay in ignored local files and are never put into page data, responses, or
logs.
