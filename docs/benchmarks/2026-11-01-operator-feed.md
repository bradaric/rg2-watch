# Operator feed election-sized benchmark — 2026-11-01

This benchmark used only generated disposable SQLite databases under `/tmp/opencode`. It did not
open, copy, migrate, or write the live election database and made no RIK or model requests.

## Procedure and fixture

Run from the feature worktree:

```sh
UV_ENV_FILE= uv run python scripts/benchmark_operator_feed.py \
  --stations 8253 --samples 20 --output /tmp/opencode/operator-feed-election-benchmark.json
```

The fixture represents an election-scale workload rather than the earlier 1,200-row smoke fixture:

- 8,253 station groups and 8,459 documents, including 165 RG-7 and 41 type-13 documents;
- 9,903 complete stored analyses (8,253 primary and 1,650 fallback-model reads);
- 825 findings and 412 pre-existing reviews;
- complete two-list reports usable by the normal rule evaluator and tally builder;
- real journal/review writes for each 1/10/53-group feed sample.

The fixture bulk-load deliberately disables feed triggers, then closes and reopens the disposable
database so current migrations/triggers are installed before measurement. This avoids measuring
fixture construction as watcher activity. The benchmark script accepts no source database path.

For a before/after full-snapshot comparison, the same fixture procedure was run from a temporary
detached worktree at base commit `57f97e2`. Three warm local runs were measured in each checkout.
The feature benchmark separately ran one `tracemalloc`-instrumented build for memory evidence.

Node 22 then parsed the generated 14.3 MB snapshot and ran the actual pure `groupTag` /
`mergeGroupState` functions extracted from `static/index.html` 100 times per saved feed payload.

## Results

### Full snapshot

| Measurement | Base `57f97e2` | Feature branch |
|---|---:|---:|
| warm runs (s) | 6.529, 6.418, 6.466 | 6.470, 6.471, 6.614 |
| warm p50 | 6.466 s | 6.471 s |
| CPU, three runs | 19.33 s | 19.47 s |
| `data.json` | 13,607,336 B | 14,318,928 B |
| traced peak allocation | not measured | 135.8 MB |
| process max RSS after fixture/build | not measured | 426,468 KiB |

The instrumented build took 29.75 s because `tracemalloc` materially slows row assembly; it is not
used as the normal build-time result. The ordinary full build remained effectively unchanged and
below the existing 120-second default cadence.

### Revision feed

Twenty samples per batch, local read-only handler, uncached selective evaluator:

| Changed groups | p50 | p95 | Mean JSON | Maximum JSON |
|---:|---:|---:|---:|---:|
| 0 | 0.49 ms | 0.69 ms | 257 B | 257 B |
| 1 | 24.56 ms | 25.07 ms | 1,799 B | 1,803 B |
| 10 | 33.04 ms | 33.60 ms | 15,743 B | 15,773 B |
| 53 | 74.79 ms | 77.77 ms | 82,897 B | 83,070 B |

With a ten-second browser poll, measured handler plus pure merge p95 remains about 89 ms for 53
groups, so the target remains dominated by the poll interval rather than server computation. The
roughly ten-second objective is still a target, not an SLA.

### Browser/Node processing

| Input | p50 | p95 |
|---|---:|---:|
| Parse 14.3 MB full snapshot | 37.45 ms | 48.78 ms |
| Merge 1 changed group | 2.13 ms | 2.67 ms |
| Merge 10 changed groups | 3.25 ms | 3.63 ms |
| Merge 53 changed groups | 9.42 ms | 10.94 ms |

These numbers cover JSON parsing and the production pure keyed merge. They do **not** measure a real
browser's DataTables DOM redraw, layout, paint, open-child restoration, or a low-powered operator
machine; that visual/browser metric remains unmet because no browser automation is installed.

### Concurrent ingestion proxy

During another complete snapshot, the script performed one small SQLite status write every 10 ms:

- 468 writes completed;
- write p95: 15.28 ms;
- maximum WAL size: 4,284,832 bytes.

This demonstrates bounded WAL growth and responsive disposable-DB writes under the benchmarked
snapshot. It is a write-latency proxy, not a measured real watcher tick or paid analysis workload;
watcher tick duration and model-worker throughput remain unmet metrics and must be observed during an
authorized rehearsal, never by adding load to the live watcher.
