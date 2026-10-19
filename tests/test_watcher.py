import httpx

from rg2watch.config import Config
from rg2watch.fake_rik import FakeRik
from rg2watch.models import LABEL_TO_TYPE, RARE_TYPES, Election, Scope
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


def env(guard=False, records=None, watchlist=(), munis=None, listing_names=None, stations=None,
        watcher_kw=None, **cfg_kw):
    t = FakeTime()
    fake = FakeRik(ELECTION, records or make_records(), munis or MUNIS, stations or STATIONS,
                   listing_names=listing_names)
    limiter = AdaptiveLimiter(2.0, clock=t.clock, sleep=t.sleep)
    client = RikClient(ELECTION, limiter, httpx.Client(transport=httpx.MockTransport(fake.handle)))
    store = Store(":memory:")
    cfg = Config(probe_cost_guard=guard, watchlist=list(watchlist), **cfg_kw)
    batches: list[list] = []
    w = Watcher(client, store, cfg, batches.append, clock=t.clock, sleep=t.sleep, **(watcher_kw or {}))
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


def full_sweep_pages(fake):
    """Pages 2 and 3 of the unfiltered RG-2 listing are only requested by a full sweep (34 records)."""
    return [f for f in listing_reqs(fake, **{"filters[election_document_type]": "6"})
            if "filters[municipality]" not in f and f["page"] in ("2", "3")]


def test_incomplete_municipality_list_falls_back_after_two_ticks():
    w, fake, store, t, batches, _ = env(munis={"101": "Alpha", "102": "Beta"})
    w.run_once()
    t.now += 60
    w.run_once()  # second consecutive shortfall -> one full sweep
    assert len(events(batches)) == 33  # Gamma docs found
    batches.clear()
    fake.add_doc("Gamma|12", doc(6, "/extfile/103/12/rg2_v1.pdf"))
    for _ in range(2):
        t.now += 60
        w.run_once()
    assert [(e.kind, e.doc.station_key) for e in events(batches)] == [("added", "Gamma|12")]
    fake.request_log.clear()
    t.now += 700
    w.run_once()
    assert any(r["path"].endswith("getElectionStations.php") for r in fake.request_log)


def test_persistent_shortfall_sweeps_once_not_every_tick():
    w, fake, store, t, batches, _ = env(munis={"101": "Alpha", "102": "Beta"},
                                        safety_sweep_interval_s=900)
    for _ in range(6):
        w.run_once()
        t.now += 60
    assert len(full_sweep_pages(fake)) == 2  # one sweep (pages 2 and 3), not one per tick
    assert store.get_count(P, "rg2") == 33


def test_race_during_municipality_loop_is_not_a_shortfall():
    w, fake, store, t, batches, _ = env()
    w.run_once()
    batches.clear()
    fake.add_doc("Beta|12", doc(6, "/extfile/102/12/rg2_v1.pdf"))
    t.now += 60
    fired = []

    def hook(req):
        f = req["form"]
        if f.get("filters[municipality]") == "102" and not fired:
            fired.append(1)
            fake.add_doc("Alpha|12", doc(6, "/extfile/101/12/rg2_v1.pdf"))  # Alpha already counted

    fake.before_request = hook
    for _ in range(3):
        w.run_once()
        t.now += 60
    assert not full_sweep_pages(fake)
    assert sorted(e.doc.station_key for e in events(batches)) == ["Alpha|12", "Beta|12"]


def test_refresh_failure_is_throttled():
    w, fake, store, t, batches, _ = env()
    w.bootstrap()
    w._refresh_wanted = True
    t.now += 700
    fake.fail_status = 404
    fake.request_log.clear()
    for _ in range(3):
        w.run_once()
    assert len([r for r in fake.request_log if r["path"].endswith("getElectionStations.php")]) == 1


def test_missing_state_dropped_once_station_has_rg2():
    w, fake, store, t, batches, _ = env()
    w.run_once()
    assert "Beta|12" in w._missing_state
    fake.add_doc("Beta|12", doc(6, "/extfile/102/12/rg2_v1.pdf"))
    t.now += 60
    w.run_once()
    assert "Beta|12" not in w._missing_state


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


# --- resumable, page-budgeted passes -----------------------------------------------------------

BIG_MUNIS = {str(200 + i): f"M{i:02d}" for i in range(30)}
BIG_STATIONS = {mid: {f"{mid}{n:02d}": str(n) for n in range(1, 21)} for mid in BIG_MUNIS}


def big_env(**kw):
    """30 municipalities x 20 stations = 600 RG-2 records = 60 listing pages."""
    recs = [{"municipality": name, "election_station_number": str(n),
             "election_station_name": f"School {n}", "number": f"{n}.", "_municipality_id": mid,
             "_docs": [doc(6, f"/extfile/{mid}/{n}/rg2_v1.pdf")]}
            for mid, name in BIG_MUNIS.items() for n in range(1, 21)]
    return env(guard=True, records=recs, munis=BIG_MUNIS, stations=BIG_STATIONS,
               sweep_pages_per_tick=5, **kw)


def tick_budget_ok(fake, budget=5):
    """Listing requests in a tick: rare-lane requests + 2 probe count requests + the page budget
    (per-station queries of the missing lane are bounded separately by missing_max_per_tick)."""
    reqs = [f for f in listing_reqs(fake) if "filters[election_station_no]" not in f]
    rare = [f for f in reqs if f.get("filters[election_document_type]") in {str(x) for x in RARE_TYPES}]
    return len(reqs) <= len(rare) + 2 + budget


def test_probe_full_pass_is_budgeted_and_emits_per_batch():
    # missing lane off: it would add RG-2s of not-yet-swept stations out of order
    w, fake, store, t, batches, _ = big_env(safety_sweep_interval_s=100_000, missing_station_threshold=0)
    seen = []
    for _ in range(12):
        fake.request_log.clear()
        w.run_once()
        assert tick_budget_ok(fake)
        seen.append(len(events(batches)))
        t.now += 60
    assert seen == [50 * (i + 1) for i in range(12)]  # 5 pages x 10 records per tick
    assert store.get_count(P, "rg2") == 600


def test_long_safety_pass_does_not_block_rare_lane():
    w, fake, store, t, batches, _ = big_env(safety_sweep_interval_s=100_000)
    for _ in range(12):
        w.run_once()
        t.now += 60
    assert store.get_count(P, "rg2") == 600
    batches.clear()
    t.now += 100_000
    fake.request_log.clear()
    w.run_once()  # safety pass starts: pages 1-5
    assert tick_budget_ok(fake)
    assert events(batches) == []
    old = store.known_docs(P, Scope(doc_type=6))[("M29|20", 6)].url
    fake.add_doc("M00|3", doc(9, "/extfile/200/3/rg9.pdf"))
    fake.replace_doc("M29|20", 6, "/extfile/229/20/rg2_v2.pdf")  # last page, count-neutral
    t.now += 60
    fake.request_log.clear()
    w.run_once()
    assert tick_budget_ok(fake)
    assert [(e.kind, e.doc.doc_type, e.doc.station_key) for e in events(batches)] == [
        ("added", 9, "M00|3")]
    batches.clear()
    for i in range(10):  # pages 11-60
        t.now += 60
        fake.request_log.clear()
        w.run_once()
        assert tick_budget_ok(fake)
        if i < 9:
            assert events(batches) == []
    assert [(e.kind, e.doc.station_key, e.previous_url) for e in events(batches)] == [
        ("replaced", "M29|20", old)]


def test_safety_pass_ignores_keys_updated_by_other_lanes_meanwhile():
    w, fake, store, t, batches, _ = big_env(safety_sweep_interval_s=100_000)
    for _ in range(12):
        w.run_once()
        t.now += 60
    t.now += 100_000
    w.run_once()  # safety pass has fetched page 1 (M00|1 with v1)
    fake.replace_doc("M00|1", 6, "/extfile/200/1/rg2_v2.pdf")
    # another lane learns about the replacement before the safety pass completes
    from rg2watch.models import Doc, Event
    known = store.known_docs(P, Scope(doc_type=6))[("M00|1", 6)]
    newer = Doc(known.municipality, known.station_no, known.station_name, known.label, 6,
                BASE + "/extfile/200/1/rg2_v2.pdf", known.uploaded_at)
    w._emit(store.start_sweep(P, "t"), [Event("replaced", newer, previous_url=known.url)])
    batches.clear()
    for _ in range(12):
        t.now += 60
        w.run_once()
    # page 1 was observed before the change: no stale "replaced" back to v1 for M00|1
    assert [(e.kind, e.doc.station_key) for e in events(batches)] == []


# --- blocked / blind -----------------------------------------------------------------------


def test_repeated_403_pauses_all_lanes_and_alerts_once():
    statuses = []
    w, fake, store, t, batches, _ = env(watcher_kw={"on_status": statuses.append})
    w.run_once()
    fake.fail_status = 403
    for _ in range(3):
        t.now += 30  # lanes due again
        w.run_once()
    fake.request_log.clear()
    paused_at = t.now
    while t.now < paused_at + 299:
        t.now += 1
        w.run_once()
    assert fake.request_log == []  # paused for blocked_pause_s
    t.now += 2
    w.run_once()
    assert fake.request_log
    assert len(statuses) == 1 and statuses[0].startswith("[rg2-watch] RIK unreachable/blocking since ")
    fake.fail_status = None
    for _ in range(400):
        t.now += 1
        w.run_once()
    assert statuses[1:] == ["[rg2-watch] RIK reachable again"]


def test_503_for_blind_alert_s_alerts_once_then_recovers():
    statuses = []
    w, fake, store, t, batches, _ = env(watcher_kw={"on_status": statuses.append})
    w.run_once()
    fake.fail_status = 503
    last_ok = w.client.last_ok
    alerted_at = []
    while t.now < last_ok + 900:
        before = t.now
        w.run_once()
        if statuses and not alerted_at:
            alerted_at.append((before, t.now))
        t.now += 1
    assert len(statuses) == 1 and "unreachable/blocking since" in statuses[0]
    tick_start, tick_end = alerted_at[0]
    assert tick_end - last_ok >= 300 and tick_start - last_ok < 300  # first tick past blind_alert_s
    fake.fail_status = None
    t.now += 60
    w.run_once()
    assert statuses[1:] == ["[rg2-watch] RIK reachable again"]


# --- missing lane / baseline -------------------------------------------------------------------


def test_missing_lane_empty_station_response_emits_no_removed():
    w, fake, store, t, batches, _ = env()
    fake.add_doc("Beta|12", doc(12, "/extfile/102/12/rg11.pdf"))
    w.run_once()
    assert ("Beta|12", 12) in store.known_docs(P, Scope())
    fake.remove_doc("Beta|12", 12)  # station now returns no record at all
    batches.clear()
    fake.request_log.clear()
    t.now += 60
    w.run_once()
    assert ("102", "10212") in listing_reqs_station(fake)
    assert [e for e in events(batches) if e.kind == "removed"] == []


def test_baseline_records_silently_then_alerts(capsys):
    recs = make_records()
    recs[2]["_docs"].append(doc(9, "/extfile/101/3/rg9.pdf"))
    w, fake, store, t, batches, _ = env(records=recs, watcher_kw={"baseline": True})
    w.run_once()
    assert batches == []
    assert ("Alpha|3", 9) in store.known_docs(P, Scope())
    out = capsys.readouterr().out.splitlines()
    assert "[baseline] RG-2: 33 document(s) recorded" in out
    assert "[baseline] RG-9: 1 document(s) recorded" in out
    fake.add_doc("Beta|2", doc(8, "/extfile/102/2/rg8.pdf"))
    t.now += 60
    w.run_once()
    assert [(e.kind, e.doc.doc_type) for e in events(batches)] == [("added", 8)]
    assert "[baseline]" not in capsys.readouterr().out


def test_baseline_waits_for_budgeted_pass_to_complete(capsys):
    # Probe pass (3 pages/tick while the safety pass shares the budget) ends at tick 20,
    # the safety pass (2, then 5 pages/tick) at tick 24.
    w, fake, store, t, batches, _ = big_env(watcher_kw={"baseline": True})
    shown = []
    for i in range(1, 31):
        w.run_once()
        t.now += 60
        if "[baseline]" in capsys.readouterr().out:
            shown.append(i)
    assert shown == [24]
    assert batches == []
