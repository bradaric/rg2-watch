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

Node 22 parsed the generated 14.3 MB snapshot and ran the actual pure `groupTag` /
`mergeGroupState` functions extracted from `static/index.html` 100 times per saved feed payload.
On 2026-11-02, the follow-up used installed Playwright 1.64.1 and system Chromium 155 to load the
generated page over a disposable localhost server and measure the production `applyFeedGroups`
DataTables redraw. No browser or package was installed for the benchmark.

## Results

### Full snapshot

| Measurement | Base `57f97e2` | Feature branch |
|---|---:|---:|
| warm runs (s) | 6.529, 6.418, 6.466 | 6.298, 6.267, 6.346 |
| warm p50 | 6.466 s | 6.298 s |
| CPU, three runs | 19.33 s | 18.84 s |
| `data.json` | 13,607,336 B | 14,318,928 B |
| traced peak allocation | not measured | 135.8 MB |
| comparable fresh-process peak RSS | 256,548 KiB | 259,288 KiB |

The instrumented build took 28.47 s because `tracemalloc` materially slows row assembly; it is not
used as the normal build-time result. The ordinary full build remained effectively unchanged and
below the existing 120-second default cadence. Peak RSS was measured in separate fresh Python
processes against byte-for-byte copies of the same disposable 8,459-document database, loading the
base source with `PYTHONPATH` and using `resource.getrusage(RUSAGE_SELF).ru_maxrss`. The requested
`/usr/bin/time -v` executable was absent (`/usr/bin/time: No such file or directory`), so the standard
library metric was used instead. The base and feature one-build wall times in those processes were
6.362 s and 6.451 s respectively.

### Revision feed

Twenty samples per batch, local read-only handler, uncached selective evaluator:

| Changed groups | p50 | p95 | Mean JSON | Maximum JSON |
|---:|---:|---:|---:|---:|
| 0 | 0.48 ms | 0.59 ms | 257 B | 257 B |
| 1 | 23.74 ms | 23.99 ms | 1,799 B | 1,803 B |
| 10 | 31.97 ms | 32.76 ms | 15,745 B | 15,773 B |
| 53 | 74.64 ms | 92.48 ms | 82,909 B | 83,070 B |

With a ten-second browser poll, the target remains dominated by the poll interval rather than server
computation. The roughly ten-second objective is still a target, not an SLA.

The benchmark also ran each feed under `cProfile` and summed only SQLite `execute`, `fetchone`, and
`fetchall` C-call inline time. This keeps timings out of protocol responses. “Python remainder” is
profiled handler wall time minus those calls and therefore includes row assembly and profiler
overhead; JSON serialization is timed separately outside the handler.

| Groups | SQLite p50 / p95 | Python remainder p50 / p95 | JSON p50 / p95 |
|---:|---:|---:|---:|
| 0 | 0.34 / 0.35 ms | 0.25 / 0.26 ms | 0.007 / 0.007 ms |
| 1 | 22.90 / 23.18 ms | 1.17 / 1.29 ms | 0.023 / 0.025 ms |
| 10 | 28.80 / 29.31 ms | 6.06 / 6.33 ms | 0.087 / 0.092 ms |
| 53 | 74.92 / 77.80 ms | 29.71 / 30.20 ms | 0.481 / 0.515 ms |

### Browser processing

Pure Node processing, retained to distinguish merge cost from DOM work:

| Input | p50 | p95 |
|---|---:|---:|
| Parse 14.3 MB full snapshot | 37.45 ms | 48.78 ms |
| Merge 1 changed group | 2.13 ms | 2.67 ms |
| Merge 10 changed groups | 3.25 ms | 3.63 ms |
| Merge 53 changed groups | 9.42 ms | 10.94 ms |

Actual Chromium/DataTables redraw, 30 samples at 1440×900:

| Changed groups | synchronous `applyFeedGroups` p50 / p95 | through two animation frames p50 / p95 |
|---:|---:|---:|
| 1 | 273.2 / 283.8 ms | 296.3 / 305.5 ms |
| 10 | 273.2 / 282.8 ms | 298.1 / 298.6 ms |
| 53 | 289.3 / 299.5 ms | 309.5 / 323.4 ms |

The browser harness loads all 8,253 rows, clones the selected current groups with monotonically newer
revisions, calls the real `applyFeedGroups`, and waits two `requestAnimationFrame` callbacks. It thus
includes the real DataTables remove/add/draw, layout and headless paint scheduling. It does not claim
performance for a low-powered operator machine or exercise every open-child interaction.

### Concurrent ingestion and simulated watcher/worker impact

During another complete snapshot, the script performed one small SQLite status write every 10 ms:

- 453 writes completed;
- write p95: 15.28 ms;
- maximum WAL size: 4,169,472 bytes.

This demonstrates bounded WAL growth and responsive disposable-DB writes under the election-sized
snapshot.

`scripts/benchmark_operator_impact.py` additionally uses `FakeRik` behind `httpx.MockTransport` and
`FakeAnthropic`; all databases and PDFs are generated under `/tmp/opencode`, so there are no RIK or
model network calls. It compares identical local work with and without a deliberately continuous
snapshot-plus-empty-feed loop:

| Simulated workload | Baseline | With snapshot/feed load |
|---|---:|---:|
| watcher tick p50 | 5.23 ms | 8.07 ms |
| watcher tick p95 | 7.34 ms | 11.59 ms |
| one-worker analysis p50 | 351.38 ms | 354.65 ms |
| one-worker analysis p95 | 357.79 ms | 365.13 ms |
| analysis throughput | 2.839 forms/s | 2.813 forms/s |

The watcher result covers 30 local ticks after bootstrapping a 36-station fake RIK fixture; 15 full
snapshots and feed polls completed concurrently. The analysis result covers 12 locally rendered
900×1200 PDFs and 12 immediate fake-model responses; 803 small full snapshots and feed polls ran
concurrently. This is a safe contention simulation, not a claim about live RIK latency, paid-model
latency, multiple production workers, or election-night hardware. Those live-environment effects
remain intentionally unmeasured because the live watcher/database could not be touched.

Reproduction commands for the added measurements:

```sh
UV_ENV_FILE= uv run python scripts/benchmark_operator_impact.py
PLAYWRIGHT_CORE=/home/sasa/.local/share/mise/installs/npm-playwright/1.64.1/node_modules/.mise/playwright-core@1.64.1/node_modules/playwright-core \
  node scripts/benchmark_operator_browser.js /tmp/opencode/rg2watch-election-benchmark-7zelg0ev/page
```
