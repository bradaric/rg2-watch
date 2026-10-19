import threading

from rg2watch.models import Doc, Election, Event, Finding, Scope, doc_key
from rg2watch.store import Store

P = "662405"


def doc(url="https://x/1.pdf", muni="Сечањ", no="1", dt=6, name="ОШ"):
    return Doc(muni, no, name, "lbl", dt, url, "2026-10-19T20:15")


def store():
    s = Store(":memory:")
    s.upsert_election(Election(P, "1", "t", "Title", "https://x"))
    return s


def test_added_then_known():
    s = store()
    d = doc()
    sid = s.start_sweep(P, "rg2")
    s.apply_events(P, sid, [Event("added", d)])
    assert s.known_docs(P, Scope()) == {(d.station_key, 6): d}
    s.finish_sweep(sid, 1, 1, 1, 0, 0)
    ev = s._db.execute("SELECT kind, url, detected_at FROM events").fetchall()
    assert ev[0][0] == "added" and ev[0][1] == d.url and ev[0][2].endswith("Z")


def test_replaced_marks_old_inactive():
    s = store()
    old, new = doc("https://x/old.pdf"), doc("https://x/new.pdf")
    sid = s.start_sweep(P, "rg2")
    s.apply_events(P, sid, [Event("added", old)])
    s.apply_events(P, sid, [Event("replaced", new, previous_url=old.url)])
    assert s.known_docs(P, Scope()) == {(new.station_key, 6): new}
    active = s._db.execute("SELECT active FROM documents WHERE url=?", (old.url,)).fetchone()
    assert active[0] == 0


def test_removed_marks_inactive():
    s = store()
    d = doc()
    sid = s.start_sweep(P, "rg2")
    s.apply_events(P, sid, [Event("added", d)])
    s.apply_events(P, sid, [Event("removed", d)])
    assert s.known_docs(P, Scope()) == {}


def test_known_docs_scoped_by_municipality_and_type():
    s = store()
    a, b, c = doc("https://x/a"), doc("https://x/b", muni="Бор", no="2"), doc("https://x/c", dt=9)
    s.apply_events(P, s.start_sweep(P, "l"), [Event("added", x) for x in (a, b, c)])
    assert set(s.known_docs(P, Scope(municipality="Сечањ"))) == {(a.station_key, 6), (c.station_key, 9)}
    assert set(s.known_docs(P, Scope(doc_type=6))) == {(a.station_key, 6), (b.station_key, 6)}
    assert set(s.known_docs(P, Scope(doc_type=6, municipality="Бор"))) == {(b.station_key, 6)}
    assert set(s.known_docs(P, Scope(station_key=b.station_key))) == {(b.station_key, 6)}


def test_finding_dedupe():
    s = store()
    d = doc(dt=9)
    s.apply_events(P, s.start_sweep(P, "l"), [Event("added", d)])
    f = Finding("D1", "MEDIUM", d, "RG-9 published")
    assert s.add_finding(P, f, d.url) is True
    assert s.add_finding(P, f, d.url) is False
    out = s.unalerted(P, {"MEDIUM", "HIGH"})
    assert len(out) == 1 and out[0][1] == f
    assert s.unalerted(P, {"HIGH"}) == []
    s.mark_alerted([out[0][0]])
    assert s.unalerted(P, {"MEDIUM"}) == []


def test_counts_roundtrip():
    s = store()
    assert s.get_count(P, "all") is None
    s.set_count(P, "all", 5)
    s.set_count(P, "all", 7)
    assert s.get_count(P, "all") == 7


def test_municipalities_and_stations_roundtrip():
    s = store()
    s.save_municipalities(P, {"1": "Сечањ", "2": "Бор"})
    s.save_stations(P, "1", {"1": "11", "2": "12"})
    assert s.municipality_ids(P) == {"Сечањ": "1", "Бор": "2"}
    assert s.station_ids(P, "1") == {"1": "11", "2": "12"}


def test_usable_from_other_thread(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_election(Election(P, "1", "t", "T", "u"))
    errs = []

    def run():
        try:
            s.apply_events(P, s.start_sweep(P, "l"), [Event("added", doc())])
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert not errs and len(s.known_docs(P, Scope())) == 1


def test_known_docs_keys_use_doc_key():
    s = store()
    known = Doc("Сечањ", "1", "ОШ", "  Непознато   лабел ", None, "https://x/u", "2026-10-19T20:15")
    typed = doc("https://x/t")
    s.apply_events(P, s.start_sweep(P, "l"), [Event("added", known), Event("added", typed)])
    got = s.known_docs(P, Scope())
    assert set(got) == {doc_key(known), doc_key(typed)}
    assert doc_key(known) == ("Сечањ|1", "label:Непознато лабел")
