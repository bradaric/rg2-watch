# Future work

This file records requested improvements that are intentionally deferred rather than changed during a
production freeze.

## Decouple PDF capture from analysis

**Priority:** Complete and exercise before the next election, expected to be the presidential election
in one or two months.

### Problem

The watcher currently queues a discovered RG-2 URL for one combined job: download and cache its PDF,
render it, call the analysis model, and store the result. Downloading therefore waits for analysis-worker
capacity and stops when analysis is paused, including at the spending cap. The analysis queue claims
active documents newest-first, so an RG-2 replaced before its turn may become inactive without its
original PDF ever being downloaded.

The replacement detector also identifies a replacement only when the full URL changes. If RIK replaces
the bytes at the same URL, the change is not currently detected. Once overwritten remotely, bytes that
were never downloaded cannot be recovered.

### Requested behavior

- Persist every discovered RG-2 in a dedicated download queue immediately, independently of analysis.
- Download oldest-first through the existing configured PDF rate limiter; do not raise the download rate.
- Do not cancel a pending download merely because that RG-2 has been replaced or removed from the
  current listing.
- Keep download retries and health independent of model availability, analysis workers, and the analysis
  spending cap.
- Let analysis consume the local cache and continue to analyze only versions eligible under the current
  active-document rules.
- Detect same-URL listing changes using available version metadata such as upload time, and retain
  distinct downloaded contents by byte digest while preserving their source URL and observed version.
- Expose download backlog, failures, and oldest-waiting age so the operator can see whether capture is
  falling behind.

### Safety and verification

Treat this as a post-election data-flow change, not an election-night patch. Design the persistent queue
and any SQLite migration with backup and restart recovery in mind. Test replacement-before-download,
same-URL changed content, bursts larger than the configured rate, retries, process restart, analysis and
spending-cap pauses, cache deduplication, and unchanged tally/review behavior. Run a rehearsal against a
fixture timeline before adopting it for production.

## Official RIK results drill-down

The first operator integration intentionally shows only RIK's national summary and party-list results.
A later version may add the official region, municipality and polling-station hierarchy using RIK's
undocumented `/get-regions/`, `/get-municipalities/`, `/get-election-stations/` and `/get_results/`
filters. Keep this separate from the unofficial tally, cache requests, handle dependent-selector races,
and retain the last successful response when RIK is unavailable or changes its response format.
