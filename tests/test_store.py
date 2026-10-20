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


def test_added_for_existing_key_leaves_one_active_row():
    s = store()
    a, b = doc("https://x/a"), doc("https://x/b")
    u1 = Doc("Сечањ", "1", "ОШ", "Х", None, "https://x/u1", "2026-10-19T20:15")
    u2 = Doc("Сечањ", "1", "ОШ", " Х ", None, "https://x/u2", "2026-10-19T20:15")
    s.apply_events(P, s.start_sweep(P, "l"),
                   [Event("added", a), Event("added", b), Event("added", u1), Event("added", u2)])
    assert s.known_docs(P, Scope()) == {doc_key(b): b, doc_key(u2): u2}
    n = s._db.execute("SELECT COUNT(*) FROM documents WHERE active=1").fetchone()[0]
    assert n == 2


def test_events_store_previous_url_and_finding_keeps_it():
    s = store()
    old, new = doc("https://x/old.pdf"), doc("https://x/new.pdf")
    sid = s.start_sweep(P, "rg2")
    s.apply_events(P, sid, [Event("added", old)])
    s.apply_events(P, sid, [Event("replaced", new, previous_url=old.url)])
    rows = s._db.execute("SELECT kind, previous_url FROM events ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("added", None), ("replaced", old.url)]
    s.add_finding(P, Finding("D2", "HIGH", new, "RG-2 file replaced", previous_url=old.url), new.url)
    assert s.unalerted(P, {"HIGH"})[0][1].previous_url == old.url


def test_old_events_table_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, sweep_id INTEGER, parent TEXT, kind TEXT, "
                "document_id INTEGER, station_key TEXT, doc_type INTEGER, url TEXT, uploaded_at TEXT, "
                "detected_at TEXT)")
    con.commit()
    con.close()
    s = Store(path)
    s.apply_events(P, s.start_sweep(P, "l"), [Event("replaced", doc(), previous_url="https://x/0.pdf")])
    assert s._db.execute("SELECT previous_url FROM events").fetchone()[0] == "https://x/0.pdf"
    s.close()
    Store(path).close()  # re-opening an already migrated DB is fine


def test_phase1a_db_migrates_analysis_queue(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE documents(id INTEGER PRIMARY KEY, parent TEXT NOT NULL, municipality TEXT,
            station_no TEXT, station_name TEXT, station_key TEXT, doc_type INTEGER, label TEXT,
            url TEXT NOT NULL UNIQUE, uploaded_at TEXT, first_seen_at TEXT, sha256 TEXT,
            status TEXT DEFAULT 'new', active INTEGER DEFAULT 1);
        CREATE TABLE analyses(id INTEGER PRIMARY KEY, document_id INTEGER, model TEXT,
            prompt_version TEXT, result_json TEXT, created_at TEXT, cost_usd REAL);
        INSERT INTO documents(parent, municipality, station_no, doc_type, label, url)
            VALUES('p', 'm', '1', 6, 'l', 'u6'), ('p', 'm', '1', 9, 'l', 'u9');
        INSERT INTO analyses(document_id, cost_usd) VALUES(1, 0.25);
    """)
    con.commit()
    con.close()
    s = Store(db)
    rows = dict(s._db.execute("SELECT url, analysis_status FROM documents").fetchall())
    assert rows == {"u6": "skipped", "u9": "skipped"}  # old rows are never analysed implicitly
    assert s.next_for_analysis(5, now=0) == []
    assert s.total_spend("p") == 0.25  # old analyses are attributed to their election
    assert s.requeue_skipped("p") == 1  # explicit opt-in still possible
    assert [d.url for d in s.next_for_analysis(5, now=0)] == ["u6"]
    s.save_failed_analysis("u6", "m", "v1", 0.5, "refused")
    assert s.total_spend("p") == 0.75 and s.total_spend("other") == 0
    s.close()
    Store(db).close()  # second open: migration is idempotent


def _seen(s, url, when):
    s._db.execute("UPDATE documents SET first_seen_at=? WHERE url=?", (when, url))


def _status(s, url):
    return s._db.execute("SELECT analysis_status FROM documents WHERE url=?", (url,)).fetchone()[0]


def test_enable_analysis_skips_documents_seen_before():
    s = store()
    old, failed, done, d9 = (doc("https://x/old.pdf"), doc("https://x/f.pdf", no="2"),
                             doc("https://x/d.pdf", no="3"), doc("https://x/9.pdf", dt=9))
    s.apply_events(P, s.start_sweep(P, "l"), [Event("added", x) for x in (old, failed, done, d9)])
    for x in (old, failed, done, d9):
        _seen(s, x.url, "2026-10-19T18:00:00Z")
    s.mark_analysis(failed.url, "failed", "x", retry_at=5.0)
    s.mark_analysis(done.url, "done")
    enabled_at = s.enable_analysis(P)
    assert {x.url: _status(s, x.url) for x in (old, failed, done, d9)} == {
        old.url: "skipped", failed.url: "skipped", done.url: "done", d9.url: "skipped"}
    assert s.analysis_backlog(P) == 2
    new = doc("https://x/new.pdf", no="4")
    s.apply_events(P, s.start_sweep(P, "l"), [Event("added", new)])  # seen after enabling
    assert [d.url for d in s.next_for_analysis(5, parent=P, now=10)] == [new.url]
    s.requeue_skipped(P)
    _seen(s, new.url, "2026-10-19T18:00:00Z")
    assert s.enable_analysis(P) == enabled_at  # kept: a later start does not re-skip
    assert _status(s, old.url) == "new"
    assert s.enable_analysis("other") != "" and _status(s, old.url) == "new"


def test_spend_is_per_parent():
    s = store()
    a, b = doc("https://x/a.pdf"), doc("https://x/b.pdf", no="2")
    s.apply_events(P, s.start_sweep(P, "l"), [Event("added", a)])
    s.apply_events("eval", s.start_sweep("eval", "l"), [Event("added", b)])
    s.save_failed_analysis(a.url, "m", "v2", 1.5, "x")
    s.save_failed_analysis(b.url, "m", "v2", 2.0, "x")
    s.save_failed_analysis("file:///tmp/x.pdf", "m", "v2", 0.5, "x", parent="adhoc")
    assert (s.total_spend(P), s.total_spend("eval"), s.total_spend("adhoc")) == (1.5, 2.0, 0.5)
    assert s.queued_for_analysis(P) == 1


def test_analysis_queries_restricted_to_selected_municipalities():
    s = store()
    a, b = doc("https://x/a.pdf", muni="А"), doc("https://x/b.pdf", muni="Б", no="2")
    s.apply_events(P, s.start_sweep(P, "l"), [Event("added", a), Event("added", b)])
    assert s.queued_for_analysis(P, {"А"}) == 1 and s.queued_for_analysis(P) == 2
    assert [d.url for d in s.next_for_analysis(5, parent=P, now=0, municipalities={"Б"})] == [b.url]
    assert s.requeue_running(P, {"А"}) == [] and _status(s, b.url) == "running"
    s.mark_analysis(a.url, "new")
    s.mark_analysis(b.url, "failed", "x", retry_at=None)
    s.requeue_failed(P, all_failed=True)
    s.mark_analysis(a.url, "skipped")
    s.mark_analysis(b.url, "skipped")
    assert s.analysis_backlog(P, {"Б"}) == 1
    assert s.requeue_skipped(P, {"Б"}) == 1
    assert (_status(s, a.url), _status(s, b.url)) == ("skipped", "new")
