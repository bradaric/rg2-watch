#!/usr/bin/env python3
"""Safe local watcher/analysis impact benchmark using only fake clients and disposable databases."""
import argparse
import json
import math
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import httpx
from PIL import Image

from rg2watch.browse import build_page
from rg2watch.config import Config
from rg2watch.fake_rik import FakeRik
from rg2watch.models import Doc, Election, Event
from rg2watch.notify import Notifier
from rg2watch.operator_feed import OperatorFeed
from rg2watch.pipeline import AnalysisPipeline
from rg2watch.rik_client import AdaptiveLimiter, RikClient
from rg2watch.watcher import Watcher

# The benchmark reuses deterministic test fakes. No request can leave httpx.MockTransport.
from test_pipeline import FakeAnthropic, resp  # noqa: E402
from test_rules import clean_report  # noqa: E402
from test_watcher import FakeTime, doc as watcher_doc  # noqa: E402
from benchmark_operator_feed import fixture  # noqa: E402

BENCHMARK_ELECTION = Election("benchmark-election", "1", "national",
                              "Disposable benchmark election", "https://example.invalid")


def summary(values):
    ordered = sorted(values)
    return {"samples": len(values), "p50_ms": statistics.median(values) * 1000,
            "p95_ms": ordered[max(0, math.ceil(len(values) * .95) - 1)] * 1000,
            "total_s": sum(values)}


def election_records(stations):
    """Fake RIK data matching the election-sized SQLite fixture exactly."""
    municipalities = {f"{number:03d}": f"Municipality {number:03d}" for number in range(174)}
    station_ids = {municipality_id: {} for municipality_id in municipalities}
    records = []
    for number in range(1, stations + 1):
        municipality_id = f"{number % 174:03d}"
        municipality = municipalities[municipality_id]
        station_ids[municipality_id][f"{municipality_id}-{number}"] = str(number)
        docs = [watcher_doc(6, f"/{number}.pdf", "2026-11-01 20:00:00")]
        if number % 50 == 0:
            docs.append(watcher_doc(19, f"/r7-{number}.pdf", "2026-11-01 21:00:00"))
        if number % 200 == 0:
            docs.append(watcher_doc(13, f"/t13-{number}.pdf", "2026-11-01 21:10:00"))
        records.append({"municipality": municipality, "election_station_number": str(number),
                        "election_station_name": "School", "number": f"{number}.",
                        "_municipality_id": municipality_id, "_docs": docs})
    return records, municipalities, station_ids


def snapshot_loop(store, cfg, parent, root, stop, counters):
    while not stop.is_set():
        try:
            counters["snapshots_started"] += 1
            started = time.perf_counter()
            build_page(store, root / "page" / "index.html", root / "pdfs", parent, cfg)
            counters["snapshot_durations_s"].append(time.perf_counter() - started)
            counters["snapshots_completed"] += 1
        except Exception as exc:  # preserve exact failures in benchmark output
            counters["errors"].append(repr(exc))
            stop.set()


def feed_loop(store, cfg, parent, root, stop, counters):
    feed = OperatorFeed(store, cfg, root / "pdfs")
    since = store.operator_feed_state(parent)[1]
    sample = 0
    while not stop.is_set():
        try:
            for number in range(1, 54):
                store.set_review(f"https://example.invalid/{number}.pdf",
                                 "ok" if sample % 2 else "unsure", f"impact {sample}")
            payload = feed.feed(parent, since)
            since = payload["through"]
            counters["feeds"] += 1
            counters["feed_groups"] += len(payload["groups"])
            sample += 1
        except Exception as exc:  # preserve exact failures in benchmark output
            counters["errors"].append(repr(exc))
            stop.set()


def start_load(store, cfg, parent, root):
    stop = threading.Event()
    counters = {"feeds": 0, "feed_groups": 0, "snapshots_started": 0,
                "snapshots_completed": 0, "snapshot_durations_s": [], "errors": []}
    threads = [threading.Thread(target=target,
                                args=(store, cfg, parent, root, stop, counters))
               for target in (snapshot_loop, feed_loop)]
    for thread in threads:
        thread.start()
    while ((counters["snapshots_started"] < 1 or counters["feeds"] < 1)
           and not counters["errors"]):
        time.sleep(.001)
    return stop, counters, threads


def stop_load(stop, threads):
    stop.set()
    for thread in threads:
        thread.join()


def watcher_case(loaded, stations, samples):
    root = Path(tempfile.mkdtemp(prefix="watch-impact-", dir="/tmp/opencode"))
    store, parent = fixture(root, stations)
    clock = FakeTime()
    records, municipalities, station_ids = election_records(stations)
    fake = FakeRik(BENCHMARK_ELECTION, records, municipalities, station_ids)
    client = RikClient(BENCHMARK_ELECTION,
                       AdaptiveLimiter(1000, clock=clock.clock, sleep=clock.sleep),
                       httpx.Client(transport=httpx.MockTransport(fake.handle)))
    cfg = Config(model="primary-model", browse_interval_s=120,
                 list_names={parent: ["List 1", "List 2"]}, tally_scope={parent: "national"})
    watcher = Watcher(client, store, cfg, lambda events: None,
                      clock=clock.clock, sleep=clock.sleep)
    watcher.run_once()  # bootstrap and populate the local fixture before measurement
    counters = {"feeds": 0, "feed_groups": 0, "snapshots_started": 0,
                "snapshots_completed": 0, "snapshot_durations_s": [], "errors": []}
    stop = threads = None
    if loaded:
        stop, counters, threads = start_load(store, cfg, parent, root)
    values = []
    for _ in range(samples):
        clock.now += 60
        started = time.perf_counter()
        watcher.run_once()
        values.append(time.perf_counter() - started)
    if stop:
        stop_load(stop, threads)
    document_count = store._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    store.close()
    client.http.close()
    return {**summary(values), "fixture_stations": stations, "fixture_documents": document_count,
            "load": counters}


def analysis_case(loaded, stations, analysis_docs):
    root = Path(tempfile.mkdtemp(prefix="analysis-impact-", dir="/tmp/opencode"))
    store, parent = fixture(root, stations)
    pdf_dir = root / "pdfs"
    pdf_dir.mkdir()
    image = Image.new("RGB", (900, 1200), (255, 255, 255))
    docs = []
    for number in range(1, analysis_docs + 1):
        image.save(pdf_dir / f"analysis-{number}.pdf", "PDF", resolution=100)
        docs.append(Doc("Alpha", str(number), "School", "RG-2", 6,
                        f"{BENCHMARK_ELECTION.base_url}/extfile/analysis-{number}.pdf",
                        "2026-10-19 20:00:00"))
    fake = FakeRik(BENCHMARK_ELECTION, [], {}, {}, pdf_dir=pdf_dir)
    client = RikClient(BENCHMARK_ELECTION, AdaptiveLimiter(1000, sleep=lambda seconds: None),
                       httpx.Client(transport=httpx.MockTransport(fake.handle)))
    store.enable_analysis(parent)
    cfg = Config(model="claude-opus-5-5", analysis_workers=1,
                 arithmetic_recheck_enabled=False, max_spend_usd=1000, browse_interval_s=120,
                 list_names={parent: ["List 1", "List 2"]}, tally_scope={parent: "national"})
    ai = FakeAnthropic(resp(clean_report()))
    pipeline = AnalysisPipeline(client, store, Notifier(cfg, None), ai, cfg, lambda found: None,
                                root / "cache")
    events = [Event("added", doc) for doc in docs]
    store.apply_events(parent, store.start_sweep(parent, "benchmark"), events)
    pipeline.submit(events)
    counters = {"feeds": 0, "feed_groups": 0, "snapshots_started": 0,
                "snapshots_completed": 0, "snapshot_durations_s": [], "errors": []}
    stop = threads = None
    if loaded:
        stop, counters, threads = start_load(store, cfg, parent, root)
    # Start the real worker loop directly so startup does not enqueue the fixture's intentionally
    # unanalysed RG-7/type-13 documents; this case measures only the 12 submitted RG-2 forms.
    worker = threading.Thread(target=pipeline._loop, name="analysis-impact")
    pipeline._threads.append(worker)
    started = time.perf_counter()
    worker.start()
    while ai.calls < analysis_docs:
        time.sleep(.005)
    pipeline.stop()
    elapsed = time.perf_counter() - started
    values = list(pipeline._durations)
    if stop:
        stop_load(stop, threads)
    document_count = store._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    store.close()
    client.http.close()
    return {**summary(values), "worker_elapsed_s": elapsed,
            "throughput_per_s": len(values) / elapsed,
            "fixture_stations": stations, "fixture_documents": document_count,
            "load": counters, "ai_calls": ai.calls}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", type=int, default=8253)
    parser.add_argument("--watcher-samples", type=int, default=30)
    parser.add_argument("--analysis-docs", type=int, default=12)
    args = parser.parse_args()
    print(json.dumps({"watcher_baseline": watcher_case(
                          False, args.stations, args.watcher_samples),
                      "watcher_with_snapshot_feed": watcher_case(
                          True, args.stations, args.watcher_samples),
                      "analysis_baseline": analysis_case(
                          False, args.stations, args.analysis_docs),
                      "analysis_with_snapshot_feed": analysis_case(
                          True, args.stations, args.analysis_docs)}, indent=2))
