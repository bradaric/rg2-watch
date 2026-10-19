import httpx

from rg2watch.config import Config
from rg2watch.fake_rik import FakeRik
from rg2watch.models import LABEL_TO_TYPE, Election, Scope
from rg2watch.rik_client import AdaptiveLimiter, RikClient
from rg2watch.store import Store
from rg2watch.watcher import Watcher

BASE = "https://www.rik.parlament.gov.rs"
P = "662405"
ELECTION = Election(P, "660578", "3", "Test", BASE)
LABEL = {v: k for k, v in LABEL_TO_TYPE.items()}
MUNIS = {"101": "Alpha", "102": "Beta", "103": "Gamma"}
STATIONS = {mid: {f"{mid}{n:02d}": str(n) for n in range(1, 13)} for mid in MUNIS}


def doc(doc_type, href, uploaded="2026-10-19 20:00:00"):
    return {"type": doc_type, "label": LABEL[doc_type], "href": href, "uploaded": uploaded}


def make_records():
    recs = []
    for mid, name in MUNIS.items():
        for n in range(1, 13):
            recs.append({
                "municipality": name, "election_station_number": str(n),
                "election_station_name": f"School {n}", "number": f"{n}.",
                "_municipality_id": mid,
                "_docs": [doc(6, f"/extfile/{mid}/{n}/rg2_v1.pdf")] if n <= 11 else [],
            })
    return recs


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s


def env(guard=False, records=None, watchlist=(), munis=None, listing_names=None, **cfg_kw):
    t = FakeTime()
    fake = FakeRik(ELECTION, records or make_records(), munis or MUNIS, STATIONS,
                   listing_names=listing_names)
    limiter = AdaptiveLimiter(2.0, clock=t.clock, sleep=t.sleep)
    client = RikClient(ELECTION, limiter, httpx.Client(transport=httpx.MockTransport(fake.handle)))
    store = Store(":memory:")
    cfg = Config(probe_cost_guard=guard, watchlist=list(watchlist), **cfg_kw)
    batches: list[list] = []
    w = Watcher(client, store, cfg, batches.append, clock=t.clock, sleep=t.sleep)
    return w, fake, store, t, batches, limiter


def events(batches):
    return [e for b in batches for e in b]


def listing_reqs(fake, **match):
    out = []
    for r in fake.request_log:
        f = r["form"]
        if r["path"] == "/get-election" and all(f.get(k) == v for k, v in match.items()):
            out.append(f)
    return out


def test_bootstrap_stores_municipalities_and_stations():
    w, fake, store, *_ = env()
    w.bootstrap()
    assert store.municipality_ids(P) == {"Alpha": "101", "Beta": "102", "Gamma": "103"}
    assert store.station_ids(P, "101") == {str(n): f"101{n:02d}" for n in range(1, 13)}
    n = len(fake.request_log)
    w.bootstrap()
    assert len(fake.request_log) == n


def test_probe_detects_new_rg2_in_only_changed_municipality():
    w, fake, store, t, batches, _ = env(guard=False)
    w.run_once()
    assert len(events(batches)) == 33
    batches.clear()
    fake.add_doc("Beta|12", doc(6, "/extfile/102/12/rg2_v1.pdf"))
    t.now += 60
    fake.request_log.clear()
    w.run_once()
    ev = events(batches)
    assert [(e.kind, e.doc.station_key) for e in ev] == [("added", "Beta|12")]
    by_muni = {m: len(listing_reqs(fake, **{"filters[election_document_type]": "6",
                                            "filters[municipality]": mid}))
               for m, mid in (("Alpha", "101"), ("Beta", "102"), ("Gamma", "103"))}
    assert by_muni == {"Alpha": 2, "Beta": 4, "Gamma": 2}  # count probe only vs probe + sweep


def test_probe_cost_guard_triggers_full_sweep():
    w, fake, store, t, batches, _ = env(guard=True)
    w.run_once()
    assert len(events(batches)) == 33
    rg2 = listing_reqs(fake, **{"filters[election_document_type]": "6"})
    assert not [f for f in rg2 if "filters[municipality]" in f]
    assert {f["page"] for f in rg2} == {"1", "2", "3", "4"}
    assert len(rg2) == 6  # count probe (pages 1 and 4) + sweep of 4 pages


def test_rare_lane_emits_added_rg9():
    w, fake, store, t, batches, _ = env()
    w.run_once()
    batches.clear()
    fake.add_doc("Alpha|3", doc(9, "/extfile/101/3/rg9.pdf"))
    t.now += 60
    w.run_once()
    ev = events(batches)
    assert [(e.kind, e.doc.doc_type, e.doc.station_key) for e in ev] == [("added", 9, "Alpha|3")]


def test_replacement_detected_by_safety_sweep():
    w, fake, store, t, batches, _ = env()
    w.run_once()
    batches.clear()
    old = store.known_docs(P, Scope(doc_type=6))[("Beta|2", 6)].url
    fake.replace_doc("Beta|2", 6, "/extfile/102/2/rg2_v2.pdf")
    t.now += 900
    w.run_once()
    ev = events(batches)
    assert [(e.kind, e.previous_url) for e in ev] == [("replaced", old)]
    assert ev[0].doc.url.endswith("rg2_v2.pdf")


def test_removed_confirmed_by_direct_station_query():
    # Count drops, so the probe lane's per-municipality sweep of Alpha finds the candidate.
    w, fake, store, t, batches, _ = env()
    w.run_once()
    batches.clear()
    fake.remove_doc("Alpha|4", 6)
    t.now += 60
    fake.request_log.clear()
    w.run_once()
    assert listing_reqs(fake, **{"filters[election_station_no]": "10104"})
    assert [(e.kind, e.doc.station_key) for e in events(batches)] == [("removed", "Alpha|4")]
    assert ("Alpha|4", 6) not in store.known_docs(P, Scope(doc_type=6))


def test_shifted_page_does_not_emit_removed():
    w, fake, store, t, batches, _ = env()
    w.run_once()
    batches.clear()
    fake.hide_from_sweeps("Alpha|4")
    t.now += 900
    fake.request_log.clear()
    w.run_once()
    assert events(batches) == []
    assert ("Alpha|4", 6) in store.known_docs(P, Scope(doc_type=6))
    assert listing_reqs(fake, **{"filters[election_station_no]": "10104"})  # direct re-check


def test_unavailable_does_not_crash():
    w, fake, store, t, batches, limiter = env()
    w.bootstrap()
    fake.fail_status = 503
    w.run_once()
    assert events(batches) == []
    assert limiter.current_rps < 2.0


def test_missing_station_lane_queries_individual_stations():
    w, fake, store, *_ = env()
    fake.add_doc("Gamma|12", doc(6, "/extfile/103/12/rg2_v1.pdf"))
    w.run_once()
    station_reqs = listing_reqs_station(fake)
    assert sorted(station_reqs) == [("101", "10112"), ("102", "10212")]


def listing_reqs_station(fake):
    return [(f["filters[municipality]"], f["filters[election_station_no]"])
            for f in listing_reqs(fake) if "filters[election_station_no]" in f]


def test_malformed_record_does_not_crash_watcher():
    recs = make_records()
    recs.append({"_municipality_id": "101", "election_station_number": "99",
                 "_docs": [doc(9, "/extfile/101/99/rg9.pdf")]})  # no municipality key
    w, fake, store, t, batches, _ = env(records=recs)
    fake.add_doc("Alpha|3", doc(28, "/extfile/101/3/rg9r.pdf"))
    w.run_once()
    ev = events(batches)
    assert len([e for e in ev if e.doc.doc_type == 6]) == 33
    assert [e.doc.doc_type for e in ev if e.doc.doc_type != 6] == [28]


def test_missing_threshold_exceeded_queries_nothing():
    w, fake, *_ = env(missing_station_threshold=2)  # 3 stations lack RG-2
    w.run_once()
    assert listing_reqs_station(fake) == []


def test_missing_per_tick_budget_prefers_never_queried():
    w, fake, store, t, batches, _ = env(missing_max_per_tick=2)
    w.run_once()
    assert len(listing_reqs_station(fake)) == 2
    fake.request_log.clear()
    t.now += 30
    w.run_once()
    got = listing_reqs_station(fake)
    assert len(got) == 2 and ("103", "10312") in got  # never-queried station goes first


def test_missing_backoff_doubles():
    w, fake, store, t, batches, _ = env()
    fake.add_doc("Gamma|12", doc(6, "/extfile/103/12/rg2_v1.pdf"))  # 2 stations missing
    counts = []
    for _ in range(4):
        fake.request_log.clear()
        w.run_once()
        counts.append(len(listing_reqs_station(fake)))
        t.now += 30
    assert counts == [2, 2, 0, 2]  # queried at +0, +30, next due +90


def test_incomplete_municipality_list_falls_back_to_full_sweep(caplog):
    w, fake, store, t, batches, _ = env(munis={"101": "Alpha", "102": "Beta"})
    w.run_once()
    assert len(events(batches)) == 33  # Gamma docs found via the full sweep
    batches.clear()
    fake.add_doc("Gamma|12", doc(6, "/extfile/103/12/rg2_v1.pdf"))
    t.now += 60
    w.run_once()
    assert [(e.kind, e.doc.station_key) for e in events(batches)] == [("added", "Gamma|12")]
    assert store.get_count(P, "rg2") is None
    fake.request_log.clear()
    t.now += 700
    w.run_once()
    assert any(r["path"].endswith("getElectionStations.php") for r in fake.request_log)


def test_unknown_listing_name_warns_once_and_is_not_re_added(caplog):
    w, fake, store, t, batches, _ = env(listing_names={"103": "Gamma-L"})
    with caplog.at_level("WARNING", logger="rg2watch.watcher"):
        for _ in range(3):
            w.run_once()
            t.now += 60
    gamma = [e for e in events(batches) if e.doc.municipality == "Gamma-L"]
    assert len(gamma) == 11 and all(e.kind == "added" for e in gamma)
    assert len(events(batches)) == 33
    assert len([r for r in caplog.records if "Gamma-L" in r.getMessage()]) == 1
