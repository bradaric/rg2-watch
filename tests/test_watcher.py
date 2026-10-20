import math

import httpx
import pytest

from rg2watch.config import Config
from rg2watch.fake_rik import FakeRik
from rg2watch.models import LABEL_TO_TYPE, RARE_TYPES, Election, Filters, Scope
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
    kw.setdefault("tick_request_budget", 5)
    return env(guard=True, records=recs, munis=BIG_MUNIS, stations=BIG_STATIONS, **kw)


def tick_budget_ok(fake, budget=5):
    """Listing requests in a tick: at most one count request per rare type + the request budget."""
    return len(listing_reqs(fake)) <= len(RARE_TYPES) + budget


def populate(w, store, t, max_ticks=30):
    for _ in range(max_ticks):
        w.run_once()
        t.now += 60
        if store.get_count(P, "rg2") == 600:
            return
    raise AssertionError("initial population did not finish")


def test_probe_full_pass_is_budgeted_and_emits_per_batch():
    # missing lane off: it would add RG-2s of not-yet-swept stations out of order
    w, fake, store, t, batches, _ = big_env(safety_sweep_interval_s=100_000, missing_station_threshold=0)
    seen = []
    while store.get_count(P, "rg2") != 600:
        assert len(seen) < 30
        fake.request_log.clear()
        w.run_once()
        assert tick_budget_ok(fake)
        seen.append(len(events(batches)))
        t.now += 60
    # tick 1: probe count (2 requests) + 3 pages; then 5 pages x 10 records per tick
    assert seen[0] == 30 and all(b - a == 50 for a, b in zip(seen, seen[1:-1]))
    assert seen[-1] == 600 and len(seen) == 13


def test_long_safety_pass_does_not_block_rare_lane():
    w, fake, store, t, batches, _ = big_env(safety_sweep_interval_s=100_000)
    populate(w, store, t)
    batches.clear()
    t.now += 100_000
    fake.request_log.clear()
    w.run_once()  # safety pass starts
    assert tick_budget_ok(fake)
    assert events(batches) == [] and w._safety_job is not None
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
    for _ in range(40):
        t.now += 60
        fake.request_log.clear()
        w.run_once()
        assert tick_budget_ok(fake)
        if w._safety_job is None:
            break
        assert events(batches) == []  # nothing before the pass completes
    assert w._safety_job is None
    assert [(e.kind, e.doc.station_key, e.previous_url) for e in events(batches)] == [
        ("replaced", "M29|20", old)]


def test_safety_pass_ignores_keys_updated_by_other_lanes_meanwhile():
    w, fake, store, t, batches, _ = big_env(safety_sweep_interval_s=100_000)
    populate(w, store, t)
    t.now += 100_000
    w.run_once()  # safety pass has fetched page 1 (M00|1 with v1)
    assert w._safety_job is not None and w._safety_job.next_page > 1
    fake.replace_doc("M00|1", 6, "/extfile/200/1/rg2_v2.pdf")
    # another lane learns about the replacement before the safety pass completes
    from rg2watch.models import Doc, Event
    known = store.known_docs(P, Scope(doc_type=6))[("M00|1", 6)]
    newer = Doc(known.municipality, known.station_no, known.station_name, known.label, 6,
                BASE + "/extfile/200/1/rg2_v2.pdf", known.uploaded_at)
    w._emit(store.start_sweep(P, "t"), [Event("replaced", newer, previous_url=known.url)])
    batches.clear()
    for _ in range(40):
        t.now += 60
        w.run_once()
        if w._safety_job is None:
            break
    assert w._safety_job is None
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
    assert w.client.paused_until == paused_at + 300  # shared with the analysis workers
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
    # Strict priority: the probe's full pass takes the budget first, then the safety pass.
    w, fake, store, t, batches, _ = big_env(watcher_kw={"baseline": True})
    shown, safety_done = [], None
    for i in range(1, 41):
        w.run_once()
        t.now += 60
        if safety_done is None and w._safety_job is None:
            safety_done = i
        if "[baseline]" in capsys.readouterr().out:
            shown.append(i)
    assert safety_done is not None and safety_done > 13  # after the probe pass (13 ticks)
    assert shown == [safety_done]
    assert batches == []


# --- unified request budget at parliamentary scale ------------------------------------------

PARL_MUNIS = {str(1000 + i): f"P{i:03d}" for i in range(170)}
PARL_STATIONS = {mid: {f"{mid}{n:02d}": str(n) for n in range(1, 51)} for mid in PARL_MUNIS}


def parl_env(without=("P169|50",), **kw):
    """170 municipalities x 50 stations (850 RG-2 pages), all with RG-2 except `without`;
    store already in sync."""
    from rg2watch.models import Doc, Event
    recs, docs = [], []
    for mid, name in PARL_MUNIS.items():
        for n in range(1, 51):
            href = f"/extfile/{mid}/{n}/rg2_v1.pdf"
            has = f"{name}|{n}" not in without
            recs.append({"municipality": name, "election_station_number": str(n),
                         "election_station_name": f"School {n}", "number": f"{n}.",
                         "_municipality_id": mid, "_docs": [doc(6, href)] if has else []})
            if has:
                docs.append(Doc(name, str(n), f"School {n}", LABEL[6], 6, BASE + href,
                                "2026-10-19T20:00"))
    w, fake, store, t, batches, limiter = env(records=recs, munis=PARL_MUNIS, stations=PARL_STATIONS,
                                              **kw)
    store.upsert_election(ELECTION)
    store.save_municipalities(P, PARL_MUNIS)
    for mid, st in PARL_STATIONS.items():
        store.save_stations(P, mid, {no: sid for sid, no in st.items()})
    store.apply_events(P, store.start_sweep(P, "seed"), [Event("added", d) for d in docs])
    for mid, name in PARL_MUNIS.items():
        store.set_count(P, f"rg2:m:{mid}", sum(1 for d in docs if d.municipality == name))
    store.set_count(P, "rg2", len(docs))
    for rt in RARE_TYPES:
        store.set_count(P, f"type:{rt}", 0)
    return w, fake, store, t, batches, limiter


@pytest.mark.slow
def test_parliament_scale_rg9_emitted_while_probe_round_is_midway():
    w, fake, store, t, batches, _ = parl_env(missing_station_threshold=0)
    fake.add_doc("P169|50", doc(6, "/extfile/1169/50/rg2_v1.pdf"))  # starts a long probe round
    found = None
    for i in range(12):
        if i == 3:
            assert w._round is not None  # round still in progress
            fake.add_doc("P010|5", doc(9, "/extfile/1010/5/rg9.pdf"))
        fake.request_log.clear()
        w.run_once()
        assert len(listing_reqs(fake)) <= len(RARE_TYPES) + 40
        if found is None and any(e.doc.doc_type == 9 for e in events(batches)):
            found = i
        t.now += 60
    assert found is not None and found - 3 <= 1  # within 2 ticks


@pytest.mark.slow
def test_parliament_scale_probe_round_over_several_ticks_finds_last_municipality():
    w, fake, store, t, batches, _ = parl_env(missing_station_threshold=0)
    fake.add_doc("P169|50", doc(6, "/extfile/1169/50/rg2_v1.pdf"))
    found = None
    for i in range(20):
        w.run_once()
        t.now += 60
        if w._round is not None:
            assert store.get_count(P, "rg2") == 8499  # not stored before the round completes
        if any(e.doc.station_key == "P169|50" for e in events(batches)):
            found = i
            break
    assert found is not None and found >= 5  # 170 municipality counts need several ticks
    assert [(e.kind, e.doc.station_key) for e in events(batches)] == [("added", "P169|50")]
    for _ in range(3):
        w.run_once()
        t.now += 60
    assert store.get_count(P, "rg2") == 8500  # stored at round completion


def test_rare_resweep_detects_count_neutral_rg7_replacement():
    recs = make_records()
    recs[2]["_docs"].append(doc(7, "/extfile/101/3/rg7_v1.pdf"))
    w, fake, store, t, batches, _ = env(records=recs)
    start = t.now
    w.run_once()
    assert ("Alpha|3", 7) in store.known_docs(P, Scope())
    batches.clear()
    fake.replace_doc("Alpha|3", 7, "/extfile/101/3/rg7_v2.pdf")
    detected = None
    while t.now < start + 300 + 4 * 60:
        t.now += 60
        w.run_once()
        if events(batches):
            detected = t.now
            break
    assert detected is not None
    assert start + 300 <= detected <= start + 300 + 2 * 60 + 30
    assert [(e.kind, e.doc.doc_type) for e in events(batches)] == [("replaced", 7)]


def test_budget_clamped_to_at_least_5():
    w, fake, store, t, batches, _ = big_env(tick_request_budget=1, missing_station_threshold=0,
                                            safety_sweep_interval_s=100_000)
    w.run_once()
    t.now += 60
    fake.request_log.clear()
    w.run_once()
    assert len(listing_reqs(fake)) == len(RARE_TYPES) + 5


@pytest.mark.slow
def test_parliament_scale_reserve_lets_safety_finish_under_continuous_uploads():
    without = [f"P{i:03d}|50" for i in range(0, 170, 2)]  # 85 stations still to upload
    w, fake, store, t, batches, _ = parl_env(without=set(without), missing_station_threshold=0,
                                             rare_interval_s=100_000, watcher_kw={"initial_safety": True})
    old = store.known_docs(P, Scope(doc_type=6))[("P169|49", 6)].url
    fake.replace_doc("P169|49", 6, "/extfile/1169/49/rg2_v2.pdf")  # last page, count-neutral
    bound = math.ceil(850 / (0.25 * 40)) + 2
    found = None
    for i in range(1, bound + 1):
        key = without[i % len(without)]  # one new upload per tick keeps the probe busy
        if not any(d["type"] == 6 for d in fake._record(key)["_docs"]):
            fake.add_doc(key, doc(6, f"/extfile/{key.replace('|', '/')}/rg2_v1.pdf"))
        w.run_once()
        t.now += 60
        if any(e.kind == "replaced" for e in events(batches)):
            found = i
            break
    assert found is not None, f"not detected within {bound} ticks"
    assert [(e.kind, e.previous_url) for e in events(batches) if e.kind == "replaced"] == [
        ("replaced", old)]


def test_rare_job_restarts_when_count_changes_after_it_started():
    # Reviewer scenario: the new RG-7 lands on a page the running rare job has already passed.
    w, fake, store, t, batches, _ = big_env(safety_sweep_interval_s=100_000, rare_resweep_interval_s=100_000,
                                            missing_station_threshold=0)
    for rec in fake.records:
        if not (rec["municipality"] == "M00" and rec["election_station_number"] == "1"):
            mid, n = rec["_municipality_id"], rec["election_station_number"]
            rec["_docs"].append(doc(7, f"/extfile/{mid}/{n}/rg7.pdf"))
    w.run_once()
    t.now += 60
    assert w._jobs["rare"] and w._jobs["rare"][0].next_page > 1
    fake.add_doc("M00|1", doc(7, "/extfile/200/1/rg7_new.pdf"))
    for _ in range(40):
        w.run_once()
        t.now += 60
    assert ("M00|1", 7) in store.known_docs(P, Scope())
    assert store.get_count(P, "type:7") == 600


def test_rare_jobs_ordered_count_change_first_rg8_rg9_family_first():
    recs = make_records()
    for i, t_ in enumerate((7, 8, 9, 10)):
        recs[i]["_docs"].append(doc(t_, f"/extfile/r/{t_}.pdf"))
    w, fake, store, t, batches, _ = env(records=recs)
    w.bootstrap()
    for t_, n in ((7, 0), (8, 0), (9, 1), (10, 1)):  # 7, 8 changed; 9, 10 only due for re-sweep
        store.set_count(P, f"type:{t_}", n)
    w._rare_resweep_at = float("-inf")
    w._run_lane("rare", lambda sid: w._rare_counts(w.clock()))
    assert [j.key for j in w._jobs["rare"]] == [8, 7, 9, 10]


def test_cost_guard_estimates_round_cost():
    munis = {str(300 + i): f"G{i:02d}" for i in range(15)}
    stations = {mid: {f"{mid}{n:02d}": str(n) for n in range(1, 41)} for mid in munis}
    recs = [{"municipality": name, "election_station_number": str(n), "election_station_name": "S",
             "number": "1.", "_municipality_id": mid,
             "_docs": [doc(6, f"/extfile/{mid}/{n}/rg2.pdf")] if n < 40 else []}
            for mid, name in munis.items() for n in range(1, 41)]  # 585 records, 59 pages
    w, fake, store, t, batches, _ = env(guard=True, records=recs, munis=munis, stations=stations,
                                        missing_station_threshold=0, tick_request_budget=1000)

    def muni_reqs():
        return [f for f in listing_reqs(fake, **{"filters[election_document_type]": "6"})
                if "filters[municipality]" in f]

    w.run_once()  # counts unknown -> 585/15 = 39 each (4 pages): 2*15 + 15*4 = 90 >= 59 -> full pass
    assert len(events(batches)) == 585 and muni_reqs() == []
    fake.add_doc("G03|40", doc(6, "/extfile/303/40/rg2.pdf"))
    t.now += 60
    fake.request_log.clear()
    w.run_once()  # one change, 39 stored -> 4 pages: 2*15 + 1*4 = 34 < 59 -> round
    assert muni_reqs()
    assert [(e.kind, e.doc.station_key) for e in events(batches)][-1] == ("added", "G03|40")
    for mid, name in munis.items():
        if name != "G03":
            fake.add_doc(f"{name}|40", doc(6, f"/extfile/{mid}/40/rg2.pdf"))
    t.now += 60
    fake.request_log.clear()
    w.run_once()  # 14 changes: 2*15 + 14*4 = 86 >= 60 -> full pass
    assert muni_reqs() == []
    assert len(events(batches)) == 600


def test_unused_reserve_flows_back_to_probe():
    # Missing lane has work (1 station missing, under threshold) but queries nothing: its reserve
    # must go back to the probe round in the same tick.
    w, fake, store, t, batches, _ = parl_env(missing_station_threshold=50, missing_max_per_tick=0,
                                             safety_sweep_interval_s=10**7, rare_interval_s=10**7)
    w.run_once()
    t.now += 60
    fake.add_doc("P169|50", doc(6, "/extfile/1169/50/rg2_v1.pdf"))
    per_tick = []
    for _ in range(4):
        fake.request_log.clear()
        w.run_once()
        t.now += 60
        per_tick.append(len(listing_reqs(fake)))
    assert w._round is not None  # still mid-round, so every tick had probe work
    assert per_tick == [40, 40, 40, 40]


def test_next_probe_round_waits_probe_interval_after_round_end():
    w, fake, store, t, batches, _ = env(missing_station_threshold=0)
    started = t.now
    w.run_once()
    ended = t.now  # the round's requests took a few (fake) seconds
    assert ended - started > 2
    fake.add_doc("Beta|12", doc(6, "/extfile/102/12/rg2_v1.pdf"))

    def global_counts():
        return [f for f in listing_reqs(fake, **{"filters[election_document_type]": "6"})
                if "filters[municipality]" not in f and f["page"] == "1"]

    t.now = started + 31  # 30 s after the round started, not after it ended
    fake.request_log.clear()
    w.run_once()
    assert global_counts() == []
    t.now = ended + 29
    w.run_once()
    assert global_counts() == []
    t.now = ended + 30
    w.run_once()
    assert global_counts()


def test_failed_batch_processing_is_retried_not_skipped():
    w, fake, store, t, batches, _ = env(guard=True, missing_station_threshold=0)
    real, calls = w._process, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database is locked")
        return real(*a, **k)

    w._process = flaky
    w.run_once()  # page 2 of the full pass fails to process; the probe's second slot retries it
    pages = [f["page"] for f in listing_reqs(fake, **{"filters[election_document_type]": "6"})
             if "filters[municipality]" not in f]
    assert pages.count("2") == 2  # re-fetched, not skipped
    assert len(events(batches)) == 33 and store.get_count(P, "rg2") == 33


def test_failed_finish_keeps_job_and_does_not_store_rg2():
    w, fake, store, t, batches, _ = env(missing_station_threshold=0)
    w.run_once()
    fake.add_doc("Beta|12", doc(6, "/extfile/102/12/rg2_v1.pdf"))
    real, calls = w._finish, []

    def flaky(sweep_id, job):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real(sweep_id, job)

    w._finish = flaky
    t.now += 60
    w.run_once()  # finish fails, the job stays queued and the probe's second slot finishes it
    assert [(e.kind, e.doc.station_key) for e in events(batches)][-1] == ("added", "Beta|12")
    assert store.get_count(P, "rg2") == 33  # that round was not ok: total not updated
    for _ in range(2):
        t.now += 60
        w.run_once()
    assert store.get_count(P, "rg2") == 34


@pytest.mark.slow
def test_parliament_scale_first_population_uses_full_pass():
    recs = [{"municipality": name, "election_station_number": str(n), "election_station_name": "S",
             "number": "1.", "_municipality_id": mid, "_docs": [doc(6, f"/extfile/{mid}/{n}/rg2.pdf")]}
            for mid, name in PARL_MUNIS.items() for n in range(1, 51)]  # 850 pages
    w, fake, store, t, batches, _ = env(guard=True, records=recs, munis=PARL_MUNIS,
                                        stations=PARL_STATIONS, missing_station_threshold=0)
    w.run_once()  # empty store: 2*170 + 170 * ceil(50 / 10) = 1190 >= 850 -> full pass
    assert w._round is None
    assert [j.filters for j in w._jobs["probe"]] == [Filters(doc_type=6)]
    assert not [f for f in listing_reqs(fake, **{"filters[election_document_type]": "6"})
                if "filters[municipality]" in f]


# --- municipality filter ---------------------------------------------------
BETA = {"Beta": "102"}


def only_beta_listings(fake):
    """Listing requests are for Beta, or global count probes of a rare type (never RG-2)."""
    reqs = [r["form"] for r in fake.request_log if r["path"] == "/get-election"]
    assert reqs
    for f in reqs:
        if "filters[municipality]" in f:
            assert f["filters[municipality]"] == "102"
        else:
            assert f.get("filters[election_document_type]") not in (None, "6")
    return reqs


def assert_only_beta(fake):
    only_beta_listings(fake)
    stations = [r["form"]["municipalityId"] for r in fake.request_log
                if r["path"].endswith("ajaxSelectMunicipality.php")]
    assert set(stations) == {"102"}


def test_filtered_run_requests_and_records_only_selected_municipality():
    w, fake, store, t, batches, _ = env(guard=True, watcher_kw={"only": BETA})
    fake.add_doc("Alpha|3", doc(9, "/extfile/101/3/rg9.pdf"))
    fake.add_doc("Beta|3", doc(9, "/extfile/102/3/rg9.pdf"))
    w.run_once()
    assert_only_beta(fake)
    assert {(e.doc.municipality, e.doc.doc_type) for e in events(batches)} == {("Beta", 6), ("Beta", 9)}
    assert len(events(batches)) == 12
    assert {d.municipality for d in store.known_docs(P, Scope()).values()} == {"Beta"}
    assert store.get_count(P, "rg2") is None  # no global count


def test_filtered_probe_and_missing_find_new_rg2_in_selected_only():
    w, fake, store, t, batches, _ = env(watcher_kw={"only": BETA})
    w.run_once()
    batches.clear()
    fake.add_doc("Alpha|12", doc(6, "/extfile/101/12/rg2_v1.pdf"))
    fake.add_doc("Beta|12", doc(6, "/extfile/102/12/rg2_v1.pdf"))
    t.now += 60
    fake.request_log.clear()
    w.run_once()
    assert [(e.kind, e.doc.station_key) for e in events(batches)] == [("added", "Beta|12")]
    only_beta_listings(fake)


def test_filtered_watchlist_skips_entries_outside_set():
    w, fake, store, t, batches, _ = env(watchlist=["Alpha", "102"], watcher_kw={"only": BETA})
    w.run_once()
    assert_only_beta(fake)
    assert len(events(batches)) == 11


def test_filtered_safety_sweep_removal_is_scoped_to_selected_municipality():
    w, fake, store, t, batches, _ = env(watcher_kw={"only": BETA, "initial_safety": True})
    w.run_once()
    batches.clear()
    fake.remove_doc("Beta|4", 6)
    fake.hide_from_sweeps("Beta|5")
    for _ in range(3):
        t.now += 400
        w.run_once()
    ev = [(e.kind, e.doc.station_key) for e in events(batches)]
    assert ev == [("removed", "Beta|4")]
    assert_only_beta(fake)


def test_filtered_sweep_does_not_remove_docs_of_other_municipalities():
    w, fake, store, t, batches, _ = env(watcher_kw={"initial_safety": True})
    w.run_once()  # unfiltered: everything stored
    assert len(store.known_docs(P, Scope(doc_type=6))) == 33
    w2 = Watcher(w.client, store, w.cfg, batches.append, clock=t.clock, sleep=t.sleep,
                 initial_safety=True, only=BETA)
    batches.clear()
    fake.request_log.clear()
    for _ in range(3):
        t.now += 400
        w2.run_once()
    assert events(batches) == []
    assert len(store.known_docs(P, Scope(doc_type=6))) == 33
    only_beta_listings(fake)


def test_filtered_replacement_in_selected_municipality_is_detected():
    w, fake, store, t, batches, _ = env(watcher_kw={"only": BETA})
    w.run_once()
    batches.clear()
    fake.replace_doc("Beta|2", 6, "/extfile/102/2/rg2_v2.pdf")
    fake.replace_doc("Alpha|2", 6, "/extfile/101/2/rg2_v2.pdf")
    for _ in range(3):
        t.now += 400
        w.run_once()
    assert [(e.kind, e.doc.station_key) for e in events(batches)] == [("replaced", "Beta|2")]


def _many(m):
    munis = {str(100 + i): f"M{i}" for i in range(m)}
    stations = {mid: {f"{mid}{n:02d}": str(n) for n in (1, 2)} for mid in munis}
    recs = [{"municipality": name, "election_station_number": str(n), "election_station_name": "S",
             "number": f"{n}.", "_municipality_id": mid,
             "_docs": [doc(6, f"/extfile/{mid}/{n}/rg2_v1.pdf")]}
            for mid, name in munis.items() for n in (1, 2)]
    return munis, stations, recs


def _rare_requests(only, m=10, rounds=6):
    munis, stations, recs = _many(m)
    kw = {"only": dict(zip(munis.values(), munis))} if only else {}
    w, fake, store, t, batches, _ = env(records=recs, munis=munis, stations=stations, watcher_kw=kw)
    w.run_once()
    fake.request_log.clear()
    for _ in range(rounds):
        t.now += 60
        w.run_once()
    return [r for r in fake.request_log if r["path"] == "/get-election"
            and r["form"].get("filters[election_document_type]") not in (None, "6")], fake


def test_filtered_rare_lane_costs_the_same_as_unfiltered_when_nothing_changes():
    filtered, _ = _rare_requests(True)
    unfiltered, _ = _rare_requests(False)
    # 20 global counts per interval, no per-municipality follow-up while the counts stay put
    assert len(filtered) == len(unfiltered)
    assert all("filters[municipality]" not in r["form"] for r in filtered)


def test_filtered_rare_followup_runs_per_municipality_only_for_the_changed_type():
    munis, stations, recs = _many(10)
    selected = {"M1": "101", "M2": "102"}
    w, fake, store, t, batches, _ = env(records=recs, munis=munis, stations=stations,
                                        watcher_kw={"only": selected})
    w.run_once()
    batches.clear()
    fake.add_doc("M1|1", doc(9, "/extfile/101/1/rg9.pdf"))
    fake.add_doc("M5|1", doc(9, "/extfile/105/1/rg9.pdf"))
    fake.request_log.clear()
    t.now += 60
    w.run_once()
    rg9 = [r["form"] for r in fake.request_log if r["path"] == "/get-election"
           and r["form"].get("filters[election_document_type]") == "9"
           and "filters[municipality]" in r["form"]]
    assert {f["filters[municipality]"] for f in rg9} == {"101", "102"}
    assert [(e.kind, e.doc.station_key, e.doc.doc_type) for e in events(batches)] == [("added", "M1|1", 9)]
    assert store.get_count(P, "type:9") == 2


def test_filtered_followup_is_charged_to_the_tick_budget():
    munis, stations, recs = _many(10)
    w, fake, store, t, batches, _ = env(records=recs, munis=munis, stations=stations,
                                        tick_request_budget=5,
                                        watcher_kw={"only": dict(zip(munis.values(), munis))})
    for _ in range(40):
        w.run_once()
        t.now += 1
    fake.add_doc("M3|1", doc(9, "/extfile/103/1/rg9.pdf"))
    batches.clear()
    t.now += 60
    fake.request_log.clear()
    w.run_once()
    per_tick = len([r for r in fake.request_log if r["form"].get("filters[municipality]")])
    assert per_tick <= 5 + 2  # follow-up pages are budgeted; the rest waits for the next tick
    for _ in range(30):
        t.now += 1
        w.run_once()
    assert [e.doc.station_key for e in events(batches)] == ["M3|1"]


def test_status_snapshot_reports_tick_lanes_and_health():
    w, fake, store, t, batches, limiter = env()
    snap = w.status_snapshot()
    assert snap["last_tick"] is None and snap["health"] == "ok" and snap["only"] is None
    w.run_once()
    snap = w.status_snapshot()
    assert snap["last_tick"] is not None and snap["tick_budget"] == w.cfg.tick_request_budget
    assert snap["tick_used"] > 0 and "probe" in snap["lanes"] and snap["max_rps"] == 2.0
    limiter.on_overload()
    assert w.status_snapshot()["health"] == "slow"
    w._paused_until = t.now + 100
    assert w.status_snapshot()["health"] == "blocked" and w.status_snapshot()["paused_until"]


def _replaced(i):
    from rg2watch.models import Doc, Event
    return Event("replaced", Doc("Alpha", str(i), "S", "x", 6, f"{BASE}/extfile/{i}.pdf", "2026-10-19 20:00:00"),
                 previous_url=f"{BASE}/extfile/old{i}.pdf")


def test_mass_replacement_alerts_once_and_marks_documents():
    statuses, marked = [], []
    w, fake, store, t, batches, _ = env(mass_replace_threshold=5, watcher_kw={
        "on_status": statuses.append, "on_mass": marked.append})
    store.upsert_election(ELECTION)

    def run(sweep_id):
        for i in range(8):
            w._emit(sweep_id, [_replaced(i)])

    w._run_lane("safety", run)
    assert statuses == ["[rg2-watch] mass replacement: 6 documents replaced in one sweep (RIK re-upload?)"]
    assert len(marked[-1]) == 8 and len(marked) == 3  # called from the 6th event on
    w._run_lane("safety", lambda sid: w._emit(sid, [_replaced(1)]))
    assert len(statuses) == 1  # a small sweep afterwards does not alert
