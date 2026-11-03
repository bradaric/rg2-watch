#!/usr/bin/env python3
"""Build and benchmark a disposable election-sized operator database; never accepts an input DB."""
import argparse
import cProfile
import json
import logging
import resource
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_rules import clean_report  # noqa: E402

from rg2watch.browse import build_page  # noqa: E402
from rg2watch.config import Config  # noqa: E402
from rg2watch.models import Election  # noqa: E402
from rg2watch.operator_feed import OperatorFeed  # noqa: E402
from rg2watch.pipeline import PROMPT_VERSION  # noqa: E402
from rg2watch.store import Store  # noqa: E402


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * fraction) - 1)]


def profiled_feed_stages(feed, store, parent: str, count: int, samples: int) -> dict:
    """Separate SQLite C-call time without adding timing fields to protocol responses."""
    values = []
    since = store.operator_feed_state(parent)[1]
    for sample in range(samples):
        for i in range(1, count + 1):
            store.set_review(f"https://example.invalid/{i}.pdf",
                             "ok" if sample % 2 else "unsure", f"profile {sample}")
        profile = cProfile.Profile()
        started = time.perf_counter()
        profile.enable()
        payload = feed.feed(parent, since)
        profile.disable()
        total = time.perf_counter() - started
        sqlite_time = sum(entry.inlinetime for entry in profile.getstats()
                          if ("sqlite3.Connection" in str(entry.code)
                              or "sqlite3.Cursor" in str(entry.code))
                          and any(name in str(entry.code) for name in
                                  ("execute", "fetchone", "fetchall")))
        started = time.perf_counter()
        json.dumps(payload)
        serialization = time.perf_counter() - started
        values.append((total, sqlite_time, max(0, total - sqlite_time), serialization))
        since = payload["through"]
    names = ("profiled_total", "sqlite_execute_fetch", "python_remainder", "json_serialization")
    return {name: {"p50_ms": statistics.median([row[index] for row in values]) * 1000,
                   "p95_ms": percentile([row[index] for row in values], .95) * 1000}
            for index, name in enumerate(names)}


def fixture(root: Path, stations: int) -> tuple[Store, str]:
    parent = "benchmark-election"
    store = Store(root / "election.sqlite")
    with store._lock, store._db:
        triggers = store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'operator_feed_%'"
        ).fetchall()
        for trigger in triggers:
            store._db.execute(f'DROP TRIGGER "{trigger["name"]}"')
        store._db.execute(
            "INSERT INTO elections VALUES(?,?,?,?,?)",
            (parent, "1", "national", "Disposable benchmark election", "https://example.invalid"))
        documents = []
        for i in range(1, stations + 1):
            municipality = f"Municipality {i % 174:03d}"
            documents.append((i, parent, municipality, str(i), "School", f"{municipality}|{i}",
                              None, 6, "RG-2", f"https://example.invalid/{i}.pdf",
                              "2026-11-01T20:00:00Z", "2026-11-01T20:00:01Z", 1, "done"))
        store._db.executemany(
            "INSERT INTO documents(id,parent,municipality,station_no,station_name,station_key,"
            "minority,doc_type,label,url,uploaded_at,first_seen_at,active,analysis_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", documents)
        report = json.dumps(clean_report(), ensure_ascii=False)
        analyses = [(i, i, "primary-model", PROMPT_VERSION, report, "2026-11-01T20:01:00Z",
                     0.0, "done", parent) for i in range(1, stations + 1)]
        secondary = [(stations + i, i, "fallback-model", PROMPT_VERSION, report,
                      "2026-11-01T20:02:00Z", 0.0, "done", parent)
                     for i in range(1, stations // 5 + 1)]
        store._db.executemany(
            "INSERT INTO analyses(id,document_id,model,prompt_version,result_json,created_at,cost_usd,"
            "status,parent) VALUES(?,?,?,?,?,?,?,?,?)", [*analyses, *secondary])
        store._db.executemany(
            "INSERT INTO findings(document_id,parent,municipality,station_no,rule,severity,summary,"
            "created_at) VALUES(?,?,?,?,?,?,?,?)",
            [(i, parent, documents[i - 1][2], str(i), "P1", "MEDIUM", "benchmark finding",
              "2026-11-01T20:03:00Z") for i in range(10, stations + 1, 10)])
        store._db.executemany(
            "INSERT INTO reviews(document_id,url,verdict,note,created_at) VALUES(?,?,?,?,?)",
            [(i, documents[i - 1][9], "ok", "benchmark review", "2026-11-01T20:04:00Z")
             for i in range(20, stations + 1, 20)])
        next_id = stations + 1
        rare = []
        for i in range(50, stations + 1, 50):
            rare.append((next_id, parent, documents[i - 1][2], str(i), "Decision",
                         documents[i - 1][5], None, 19, "RG-7", f"https://example.invalid/r7-{i}.pdf",
                         "2026-11-01T21:00:00Z", "2026-11-01T21:00:01Z", 1, "new"))
            next_id += 1
        for i in range(200, stations + 1, 200):
            rare.append((next_id, parent, documents[i - 1][2], str(i), "Correction",
                         documents[i - 1][5], None, 13, "ISPRAVKA",
                         f"https://example.invalid/t13-{i}.pdf", "2026-11-01T21:10:00Z",
                         "2026-11-01T21:10:01Z", 1, "new"))
            next_id += 1
        store._db.executemany(
            "INSERT INTO documents(id,parent,municipality,station_no,station_name,station_key,minority,"
            "doc_type,label,url,uploaded_at,first_seen_at,active,analysis_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rare)
    store.close()
    return Store(root / "election.sqlite"), parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", type=int, default=8253)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="rg2watch-election-benchmark-", dir="/tmp/opencode"))
    store, parent = fixture(root, args.stations)
    cfg = Config(model="primary-model", list_names={parent: ["List 1", "List 2"]},
                 tally_scope={parent: "national"})
    out = root / "page" / "index.html"
    logging.basicConfig(level=logging.INFO)

    fixture_counts = {
        "stations": args.stations,
        "documents": store._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "analyses": store._db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],
        "findings": store._db.execute("SELECT COUNT(*) FROM findings").fetchone()[0],
        "reviews": store._db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
    }
    tracemalloc.start()
    cpu_start = time.process_time()
    started = time.perf_counter()
    build_page(store, out, root / "pdfs", parent, cfg)
    instrumented = time.perf_counter() - started
    instrumented_cpu = time.process_time() - cpu_start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    full = []
    cpu_start = time.process_time()
    for _ in range(3):
        started = time.perf_counter()
        build_page(store, out, root / "pdfs", parent, cfg)
        full.append(time.perf_counter() - started)
    cpu = time.process_time() - cpu_start

    feed = OperatorFeed(store, cfg, root / "pdfs")
    feed_results = []
    feed_stages = {}
    payload_paths = {}
    for count in (0, 1, 10, 53):
        durations, sizes = [], []
        since = store.operator_feed_state(parent)[1]
        payload = None
        for sample in range(args.samples):
            for i in range(1, count + 1):
                store.set_review(f"https://example.invalid/{i}.pdf",
                                 "ok" if sample % 2 else "unsure", f"sample {sample}")
            started = time.perf_counter()
            payload = feed.feed(parent, since)
            durations.append(time.perf_counter() - started)
            encoded = json.dumps(payload).encode()
            sizes.append(len(encoded))
            since = payload["through"]
        payload_path = root / f"feed-{count}.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        payload_paths[count] = str(payload_path)
        feed_results.append({"groups": count, "p50_ms": statistics.median(durations) * 1000,
                             "p95_ms": percentile(durations, .95) * 1000,
                             "mean_bytes": statistics.mean(sizes), "max_bytes": max(sizes)})
        feed_stages[count] = profiled_feed_stages(feed, store, parent, count, args.samples)

    write_latencies, wal_sizes = [], []
    finished = threading.Event()
    def rebuild():
        try:
            build_page(store, out, root / "pdfs", parent, cfg)
        finally:
            finished.set()
    thread = threading.Thread(target=rebuild)
    thread.start()
    value = 0
    while not finished.is_set():
        started = time.perf_counter()
        store.set_count(parent, "benchmark", value)
        write_latencies.append(time.perf_counter() - started)
        value += 1
        wal = Path(str(store.path) + "-wal")
        wal_sizes.append(wal.stat().st_size if wal.exists() else 0)
        time.sleep(.01)
    thread.join()

    result = {
        "fixture": fixture_counts,
        "full_snapshot": {"runs_s": full, "p50_s": statistics.median(full),
                          "cpu_s_for_3": cpu, "traced_peak_mb": peak / 1_000_000,
                          "instrumented_wall_s": instrumented,
                          "instrumented_cpu_s": instrumented_cpu,
                          "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                          "json_bytes": (out.parent / "data.json").stat().st_size},
        "feed": feed_results,
        "feed_profiled_stages": feed_stages,
        "concurrent_ingestion": {
            "writes": len(write_latencies),
            "write_p95_ms": percentile(write_latencies, .95) * 1000 if write_latencies else None,
            "wal_max_bytes": max(wal_sizes, default=0)},
        "artifacts": {"root": str(root), "snapshot": str(out.parent / "data.json"),
                      "feed_payloads": payload_paths},
    }
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    store.close()


if __name__ == "__main__":
    main()
