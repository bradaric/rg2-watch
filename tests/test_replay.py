import json
import threading
import urllib.request
from datetime import datetime
from pathlib import Path

import httpx

from rg2watch.fake_rik import FakeRik
from rg2watch.models import Election
from rg2watch.replay import (
    ReplayClock,
    fault_plan,
    fetch_fixtures,
    load_fake,
    make_replay_server,
)
from rg2watch.rik_client import AdaptiveLimiter, RikClient

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://www.rik.parlament.gov.rs"
ELECTION = Election("662405", "660578", "3", "Test election", BASE)


def raw_records():
    recs = json.loads((FIXTURES / "get_election_662405_rg2_p1.json").read_text(encoding="utf-8"))["records"]
    return recs[:4]


def make_fixture_dir(tmp_path: Path) -> Path:
    root = tmp_path / "fx" / "662405"
    root.mkdir(parents=True)
    recs = raw_records()
    for i, rec in enumerate(recs):
        rec["_municipality_id"] = "101" if i < 2 else "102"
    (root / "election.json").write_text(json.dumps({
        "parent": "662405", "round": "660578", "election_type": "3", "title": "Test",
        "base_url": BASE, "page_html": ""}), encoding="utf-8")
    (root / "listing.json").write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    (root / "municipalities.json").write_text(json.dumps({"101": "A", "102": "B"}))
    (root / "stations.json").write_text(json.dumps({"101": {"10101": "1"}, "102": {"10201": "1"}}))
    return root


def listing_count(fake: FakeRik) -> int:
    req = httpx.Request("POST", BASE + "/get-election", content=b"page=1")
    return len(fake.handle(req).json()["records"])


def test_load_fake_from_fixture_subset(tmp_path):
    fake = load_fake(make_fixture_dir(tmp_path), None)
    assert len(fake.records) == 4
    rec = fake.records[0]
    assert rec["_municipality_id"] == "101"
    types = sorted(d["type"] for d in rec["_docs"])
    assert types == [6, 12]
    rg2 = next(d for d in rec["_docs"] if d["type"] == 6)
    assert rg2["uploaded"] == "2025-06-08 23:29:00"
    assert rg2["href"].startswith("/extfile/") and rg2["href"].endswith(".pdf")
    assert fake.election == Election("662405", "660578", "3", "Test", BASE)


def test_replay_clock_reveals_docs_over_time(tmp_path):
    wall = [0.0]
    start = datetime(2025, 6, 8, 22, 0)  # before the earliest upload (23:01)
    clock = ReplayClock(start, 60.0, wall=lambda: wall[0])
    assert clock.now_str() == "2025-06-08 22:00:00"
    fake = load_fake(make_fixture_dir(tmp_path), clock)
    assert listing_count(fake) == 0
    wall[0] = 180.0  # 3 simulated hours at 60x
    assert clock.now_str() == "2025-06-09 01:00:00"
    assert listing_count(fake) >= 1


def test_faults_schedule_deterministic(tmp_path):
    root = make_fixture_dir(tmp_path)
    recs = load_fake(root, None).records
    # spread uploads so the 25/50/75% points are meaningful
    for i, rec in enumerate(recs):
        for d in rec["_docs"]:
            d["uploaded"] = f"2025-06-08 2{i}:00:00"
    plan = fault_plan(recs)
    assert plan == fault_plan(recs)
    assert [f["kind"] for f in plan] == ["replace", "remove", "add"]
    assert [f["at"] for f in plan] == sorted(f["at"] for f in plan)


def test_faults_applied_lazily_by_server(tmp_path):
    root = make_fixture_dir(tmp_path)
    wall = [0.0]
    clock = ReplayClock(datetime(2025, 6, 8, 22, 0), 1.0, wall=lambda: wall[0])
    server = make_replay_server(root, port=0, faults=True, clock=clock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]

        def listing() -> dict:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/get-election", data=b"page=1",
                                         method="POST")
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read().decode())

        assert listing()["records"] == []
        wall[0] = 30 * 86400.0  # past every fault point
        text = json.dumps(listing(), ensure_ascii=False)
        assert "synthetic-rg9.pdf" in text
    finally:
        server.shutdown()
        server.server_close()


def build_remote(tmp_path):
    pdfs = tmp_path / "remote_pdfs"
    pdfs.mkdir()
    recs = raw_records()
    for i, rec in enumerate(recs):
        rec["_docs"] = []
        rec["_municipality_id"] = "101" if i < 2 else "102"
    src = load_fake(make_fixture_dir(tmp_path), None)
    for rec in src.records:
        for d in rec["_docs"]:
            (pdfs / d["href"].rsplit("/", 1)[-1]).write_bytes(b"%PDF-" + d["href"].encode())
    fake = FakeRik(ELECTION, src.records, {"101": "A", "102": "B"},
                   {"101": {"10101": "1", "10102": "2"}, "102": {"10201": "3", "10202": "4"}},
                   pdf_dir=pdfs)
    # records' numbers must match station maps
    for rec, no in zip(fake.records, "1234"):
        rec["election_station_number"] = no
        rec["municipality"] = "M" + rec["_municipality_id"]
    return fake


def test_fetch_fixtures_roundtrip_and_resume(tmp_path):
    fake = build_remote(tmp_path)
    client = RikClient(ELECTION, AdaptiveLimiter(1000.0, sleep=lambda s: None),
                       httpx.Client(transport=httpx.MockTransport(fake.handle)))
    root = fetch_fixtures(client, tmp_path / "out", with_pdfs=True)
    assert root == tmp_path / "out" / "662405"
    listing = json.loads((root / "listing.json").read_text(encoding="utf-8"))
    assert len(listing) == 4
    assert {r["_municipality_id"] for r in listing} == {"101", "102"}
    assert json.loads((root / "municipalities.json").read_text()) == {"101": "A", "102": "B"}
    assert len(list((root / "pdfs").iterdir())) == 9
    assert "ТИМОК" in (root / "listing.json").read_text(encoding="utf-8")  # ensure_ascii=False
    replayed = load_fake(root, None)
    assert len(replayed.records) == 4 and replayed.pdf_dir == root / "pdfs"

    fake.request_log.clear()
    fetch_fixtures(client, tmp_path / "out", with_pdfs=True)
    assert fake.request_log == []


def test_fetch_fixtures_limit_pdfs(tmp_path):
    fake = build_remote(tmp_path)
    client = RikClient(ELECTION, AdaptiveLimiter(1000.0, sleep=lambda s: None),
                       httpx.Client(transport=httpx.MockTransport(fake.handle)))
    root = fetch_fixtures(client, tmp_path / "out", with_pdfs=True, limit_pdfs=3)
    assert len(list((root / "pdfs").iterdir())) == 3
