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
model network calls. Each case starts from a newly generated 8,253-station, 8,459-document database.
The fake RIK lists the same 8,253 stations and document URLs as SQLite. The loaded case overlaps the
workload with continuous full election-sized snapshots and a second loop that alternates reviews for
53 stations and consumes their real selective feed payloads.

| Simulated workload | Baseline | With snapshot/feed load |
|---|---:|---:|
| watcher tick p50 | 1,873.48 ms | 4,091.28 ms |
| watcher tick p95 | 2,226.10 ms | 6,975.09 ms |
| one-worker analysis p50 | 366.92 ms | 448.26 ms |
| one-worker analysis p95 | 570.51 ms | 737.37 ms |
| analysis throughput | 2.614 forms/s | 1.970 forms/s |

The watcher result covers 30 sustained-backlog ticks using the production default 40-request budget
and a fake clock advancing 60 seconds per sample. During the loaded run, 10 complete 8,253-station
snapshots took 8.12–15.15 seconds, while 72 feeds returned 3,816 changed groups. The analysis result
adds 12 locally rendered 900×1200 PDFs to another copy of the election-sized database and processes
12 immediate fake-model responses on one real `AnalysisPipeline._loop` worker thread. Its loaded run
overlapped one 7.99-second full snapshot and 32 feeds returning 1,115 changed groups.

This intentionally CPU/SQLite-heavy local simulation shows material contention: watcher p95 rose
4.75 seconds and fake-worker throughput fell 24.6%. It does not establish production impact because
`MockTransport` has no RIK network wait, fake model responses return immediately, only one analysis
worker is measured, and hardware scheduling differs. In particular, the watcher numbers emphasize
local parsing and database work that real request latency may interleave differently. Live RIK/model
latency, multiple production workers, election-night hardware, and a safe production load policy
remain unmeasured because the live watcher/database could not be touched. The result therefore does
not justify running full snapshots continuously; production retains its existing background cadence.

## 2026-11-02 review-fix rerun

The order-signature/projection and monotonic paging changes were rerun with the same generated
8,253-station fixture and 20 samples per feed size. The disposable artifacts were written under
`/tmp/opencode`; no election database, watcher, RIK endpoint, or model endpoint was accessed.

| Changed groups | p50 | p95 | Mean JSON | Maximum JSON |
|---:|---:|---:|---:|---:|
| 0 | 0.58 ms | 0.61 ms | 350 B | 350 B |
| 1 | 2.13 ms | 2.19 ms | 1,916 B | 1,920 B |
| 10 | 10.50 ms | 10.71 ms | 16,085 B | 16,116 B |
| 53 | 52.01 ms | 69.68 ms | 84,352 B | 84,541 B |

Three warm full snapshots took 6.236, 6.238, and 6.305 seconds (p50 6.238 seconds), and the
concurrent-ingestion case completed 458 writes at 15.28 ms p95 with a 4,169,472-byte maximum WAL.
The resulting snapshot was 14,319,074 bytes. A separate 200-insert model-counter measurement had
0.090 ms p50 and 0.269 ms p95 transaction cost. Its traced order lookup was exactly:
`SELECT model,coverage FROM operator_model_counts WHERE parent=?`; it did not read `analyses` or
`documents`.

The simulated impact rerun measured watcher p50/p95 at 1,811.76/2,107.74 ms baseline and
3,902.03/6,652.46 ms with continuous snapshot/feed load. One-worker analysis p50/p95 was
346.09/364.13 ms baseline and 436.01/967.50 ms loaded; throughput changed from 2.868 to
2.037 forms/s. As above, this deliberately CPU-heavy fake workload is contention evidence, not a
production claim or authorization to increase snapshot frequency. Bootstrap replay/restart and hard
oversize behavior are deterministic protocol tests rather than useful latency measurements: changed
post-start groups are skipped then replayed, signature/floor changes restart, and a >2 MB tagged
group terminates with typed HTTP 413 instead of retrying or dropping rows.

The installed Playwright/system-Chromium harness also loaded all 8,253 rows over disposable
localhost HTTP after these changes. For 1/10/53 changed groups, synchronous redraw p50/p95 was
271.7/286.5, 272.3/277.9, and 285.8/302.6 ms; two-frame p50/p95 was 283.3/302.9,
298.0/298.7, and 309.8/326.3 ms. This non-interactive session could run the real headless page but
could not perform a human visual inspection; that remains an explicit manual delivery check.

Reproduction commands for the added measurements:

```sh
UV_ENV_FILE= uv run python scripts/benchmark_operator_impact.py
PLAYWRIGHT_CORE=/home/sasa/.local/share/mise/installs/npm-playwright/1.64.1/node_modules/.mise/playwright-core@1.64.1/node_modules/playwright-core \
  node scripts/benchmark_operator_browser.js /tmp/opencode/rg2watch-election-benchmark-7zelg0ev/page
```

## 2026-11-02 conformance-closure rerun

After the tally-recovery and deterministic race coverage changes, the disposable 8,253-station /
8,459-document benchmark was run again with no live database, RIK, or model access.

| Changed groups | p50 | p95 | Mean JSON |
|---:|---:|---:|---:|
| 0 | 0.61 ms | 0.65 ms | 350 B |
| 1 | 2.19 ms | 2.33 ms | 1,916 B |
| 10 | 10.64 ms | 11.02 ms | 16,085 B |
| 53 | 53.39 ms | 68.96 ms | 84,352 B |

Warm full snapshots took 6.191, 6.163, and 6.245 seconds (p50 6.191 seconds). Concurrent
ingestion completed 461 writes at 15.37 ms p95 with a 4,169,472-byte maximum WAL. The generated
snapshot was 14,319,074 bytes. The same real local page was loaded in system Chromium with 8,253
rows: 1/10/53-group redraw p95 was 280.1/293.4/317.9 ms synchronously and
303.7/314.8/342.5 ms at the next paint.

The fake watcher/analysis contention rerun measured watcher p50/p95 at 1,851.23/2,176.76 ms
baseline and 3,960.87/6,781.66 ms under continuous snapshot/feed load. One-worker fake analysis
p50/p95 was 366.46/383.14 ms baseline and 462.11/927.54 ms loaded; throughput changed from 2.707
to 1.986 forms/s. This deliberately CPU-heavy local simulation is evidence of material contention,
not a production-impact estimate or authorization to increase full-snapshot frequency. Human visual
inspection and live-system behavior remain unverified.

Commands and retained output for this rerun:

```sh
RG2W_LIVE_API= uv run python scripts/benchmark_operator_feed.py \
  --output /tmp/opencode/operator-feed-closure.json
RG2W_LIVE_API= uv run python scripts/benchmark_operator_impact.py \
  > /tmp/opencode/operator-impact-closure.json
PLAYWRIGHT_CORE=/home/sasa/.local/share/mise/installs/npm-playwright/1.64.1/node_modules/.mise/playwright-core@1.64.1/node_modules/playwright-core \
  node scripts/benchmark_operator_browser.js /tmp/opencode/rg2watch-election-benchmark-v9ijj_yi/page \
  > /tmp/opencode/operator-browser-closure.json
```
