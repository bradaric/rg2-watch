from rg2watch.diff import diff
from rg2watch.models import Doc, Event, doc_key


def doc(url="https://x/1.pdf", muni="Сечањ", no="1", dt=6, label="lbl"):
    return Doc(muni, no, "ОШ", label, dt, url, "2026-10-19T20:15")


def known_of(*docs):
    return {doc_key(d): d for d in docs}


def test_added():
    d = doc()
    assert diff({}, [d], complete=False) == [Event("added", d)]


def test_replaced():
    old, new = doc("https://x/old.pdf"), doc("https://x/new.pdf")
    assert diff(known_of(old), [new], complete=True) == [
        Event("replaced", new, previous_url=old.url)]


def test_unchanged_no_events():
    d = doc()
    assert diff(known_of(d), [d], complete=True) == []


def test_removed_only_when_complete():
    d = doc()
    assert diff(known_of(d), [], complete=False) == []
    assert diff(known_of(d), [], complete=True) == [Event("removed", d)]


def test_duplicates_collapsed():
    d = doc()
    assert diff({}, [d, d], complete=True) == [Event("added", d)]
    old, new = doc("https://x/old.pdf"), doc("https://x/new.pdf")
    assert len(diff(known_of(old), [new, new], complete=True)) == 1


def test_two_docs_same_station_different_types_both_added():
    a, b = doc("https://x/a", dt=6), doc("https://x/b", dt=12)
    assert diff({}, [a, b], complete=True) == [Event("added", a), Event("added", b)]


def test_unknown_labels_distinct_keys():
    a = doc("https://x/a", dt=None, label="Нешто 1")
    b = doc("https://x/b", dt=None, label="Нешто 2")
    events = diff({}, [a, b], complete=True)
    assert events == [Event("added", a), Event("added", b)]
    assert diff(known_of(a, b), [a, b], complete=True) == []


def test_same_key_two_urls_does_not_flap():
    from rg2watch.models import Election, Scope
    from rg2watch.store import Store
    s = Store(":memory:")
    s.upsert_election(Election("P", "1", "t", "T", "u"))
    a = doc("https://x/a")
    b = Doc(a.municipality, a.station_no, "ОШ", "lbl", 6, "https://x/b", "2026-10-19T21:00")
    counts = []
    for _ in range(3):
        ev = diff(s.known_docs("P", Scope()), [a, b], complete=True)
        counts.append(len(ev))
        s.apply_events("P", s.start_sweep("P", "l"), ev)
    assert counts == [1, 0, 0]
    assert list(s.known_docs("P", Scope()).values()) == [b]
