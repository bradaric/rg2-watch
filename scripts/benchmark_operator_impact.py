#!/usr/bin/env python3
"""Safe local watcher/analysis impact benchmark using only fake clients and disposable databases."""
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
from rg2watch.models import Doc, Event
from rg2watch.notify import Notifier
from rg2watch.operator_feed import OperatorFeed
from rg2watch.pipeline import AnalysisPipeline
from rg2watch.rik_client import AdaptiveLimiter, RikClient
from rg2watch.store import Store
from rg2watch.watcher import Watcher

# The benchmark reuses deterministic test fakes. No request can leave httpx.MockTransport.
from test_pipeline import FakeAnthropic, resp  # noqa: E402
from test_rules import clean_report  # noqa: E402
from test_watcher import ELECTION, MUNIS, STATIONS, FakeTime, make_records  # noqa: E402


def summary(values):
    ordered = sorted(values)
    return {"samples": len(values), "p50_ms": statistics.median(values) * 1000,
            "p95_ms": ordered[max(0, math.ceil(len(values) * .95) - 1)] * 1000,
            "total_s": sum(values)}


def load_loop(store, cfg, parent, root, stop, counters):
    feed = OperatorFeed(store, cfg, root / "pdfs")
    since = store.operator_feed_state(parent)[1]
    while not stop.is_set():
        try:
            payload = feed.feed(parent, since)
            since = payload["through"]
            counters["feeds"] += 1
            build_page(store, root / "page" / "index.html", root / "pdfs", parent, cfg)
            counters["snapshots"] += 1
        except Exception as exc:  # preserve exact failures in benchmark output
            counters["errors"].append(repr(exc))
            stop.set()


def start_load(store, cfg, parent, root):
    stop = threading.Event()
    counters = {"feeds": 0, "snapshots": 0, "errors": []}
    thread = threading.Thread(target=load_loop,
                              args=(store, cfg, parent, root, stop, counters))
    thread.start()
    while counters["snapshots"] < 1 and not counters["errors"]:
        time.sleep(.001)
    return stop, counters, thread


def watcher_case(loaded):
    root = Path(tempfile.mkdtemp(prefix="watch-impact-", dir="/tmp/opencode"))
    clock = FakeTime()
    fake = FakeRik(ELECTION, make_records(), MUNIS, STATIONS)
    client = RikClient(ELECTION, AdaptiveLimiter(1000, clock=clock.clock, sleep=clock.sleep),
                       httpx.Client(transport=httpx.MockTransport(fake.handle)))
    store = Store(root / "election.sqlite")
    cfg = Config(tick_request_budget=1000, browse_interval_s=120)
    watcher = Watcher(client, store, cfg, lambda events: None,
                      clock=clock.clock, sleep=clock.sleep)
    watcher.run_once()  # bootstrap and populate the local fixture before measurement
    counters = {"feeds": 0, "snapshots": 0, "errors": []}
    stop = thread = None
    if loaded:
        stop, counters, thread = start_load(store, cfg, ELECTION.parent, root)
    values = []
    for _ in range(30):
        clock.now += 60
        started = time.perf_counter()
        watcher.run_once()
        values.append(time.perf_counter() - started)
    if stop:
        stop.set()
        thread.join()
    store.close()
    client.http.close()
    return {**summary(values), "load": counters}


def analysis_case(loaded):
    root = Path(tempfile.mkdtemp(prefix="analysis-impact-", dir="/tmp/opencode"))
    pdf_dir = root / "pdfs"
    pdf_dir.mkdir()
    image = Image.new("RGB", (900, 1200), (255, 255, 255))
    docs = []
    for number in range(1, 13):
        image.save(pdf_dir / f"{number}.pdf", "PDF", resolution=100)
        docs.append(Doc("Alpha", str(number), "School", "RG-2", 6,
                        f"{ELECTION.base_url}/extfile/{number}.pdf", "2026-10-19 20:00:00"))
    fake = FakeRik(ELECTION, [], {}, {}, pdf_dir=pdf_dir)
    client = RikClient(ELECTION, AdaptiveLimiter(1000, sleep=lambda seconds: None),
                       httpx.Client(transport=httpx.MockTransport(fake.handle)))
    store = Store(root / "election.sqlite")
    store.upsert_election(ELECTION)
    store.enable_analysis(ELECTION.parent)
    cfg = Config(model="claude-opus-5-5", analysis_workers=1,
                 arithmetic_recheck_enabled=False, max_spend_usd=1000, browse_interval_s=120)
    ai = FakeAnthropic(resp(clean_report()))
    pipeline = AnalysisPipeline(client, store, Notifier(cfg, None), ai, cfg, lambda found: None,
                                root / "cache")
    events = [Event("added", doc) for doc in docs]
    store.apply_events(ELECTION.parent, store.start_sweep(ELECTION.parent, "benchmark"), events)
    pipeline.submit(events)
    counters = {"feeds": 0, "snapshots": 0, "errors": []}
    stop = thread = None
    if loaded:
        stop, counters, thread = start_load(store, cfg, ELECTION.parent, root)
    values = []
    while True:
        started = time.perf_counter()
        more = pipeline.process_next()
        elapsed = time.perf_counter() - started
        if not more:
            break
        values.append(elapsed)
    if stop:
        stop.set()
        thread.join()
    store.close()
    client.http.close()
    return {**summary(values), "throughput_per_s": len(values) / sum(values),
            "load": counters, "ai_calls": ai.calls}


if __name__ == "__main__":
    print(json.dumps({"watcher_baseline": watcher_case(False),
                      "watcher_with_snapshot_feed": watcher_case(True),
                      "analysis_baseline": analysis_case(False),
                      "analysis_with_snapshot_feed": analysis_case(True)}, indent=2))
