# Phase 1a replay smoke run - results

Date: 19/10/2026. Code at 45e6334 (before final-review fix wave).

## Setup
- Fixtures: real local-elections 2025 listing (zapisnici 662405): 225 stations, 461 documents, fetched at 1 req/s (`rg2watch fixtures ... --max-rps 1`), 5 municipalities.
- `rg2watch replay --speed 120 --faults` on 127.0.0.1:8765; `rg2watch watch --parent 662405` with `max_rps = 10` (local), default intervals; 10 min wall clock = ~20 h simulated from first upload (08/06/2025 20:51).

## Results
| metric | value |
|---|---|
| requests in 10 min | 313 (~0.5 req/s) |
| lane runs (rare / probe / watchlist / missing) | 10 / 20 / 20 / 20 |
| requests (rare / probe) | 211 / 92 |
| documents detected | 111 (110 active) |
| events | 110 added, 1 replaced (injected fault) |
| findings | D1 MEDIUM x12 (RG-7 / Примедба / repeat RG-2), D2 HIGH x1 (injected replacement) |
| errors / tracebacks | none |

Not reached in 10 min: the remove and RG-9 faults (rank 50% / 75% of uploads fall days later in simulated time) and the first safety sweep (900 s interval).

## Bugs found
- Alerts printed to stdout were lost when stdout was redirected to a file and the process got SIGTERM (block buffering, no flush). Findings were marked alerted in the DB. Fixed in the final-review fix wave (flush per alert + SIGTERM graceful shutdown).

## Observations
- Rare lane dominates request volume (one count probe per rare doc type per minute, ~20 types). Acceptable at ~0.35 req/s; could be merged into one unfiltered count if RIK struggles.

## Re-run on final Phase 1a code (5f6d413)
Same fixtures, `--speed 240`, 5 min wall clock, stdout redirected to a file, stopped by SIGTERM (`timeout`).
- 169 requests in 5 min; no errors or tracebacks.
- 111 documents, 110 added + 1 replaced (injected fault); findings D1 MEDIUM x12, D2 HIGH x1, all marked alerted.
- All 13 alert lines present in the redirected stdout file (flush fix confirmed); "interrupted, shutting down" logged on SIGTERM.
