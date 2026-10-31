from rg2watch.models import Doc, Election, Event, Filters, ListingPage
from rg2watch.station_documents import StationDocuments
from rg2watch.store import Store


BASE = "https://www.rik.parlament.gov.rs"
ELECTION = Election("779676", "680072", "2", "Test", BASE)


def doc(name, doc_type, label):
    return Doc("Љиг", "14", "School", label, doc_type,
               f"{BASE}/extfile/{name}.pdf", "2026-10-30T12:00")


def test_live_station_documents_compare_rik_listing_with_every_stored_url():
    stored = doc("rg2", 6, "RG-2")
    missing = doc("rg7", 7, "RG-7")
    store = Store(":memory:")
    store.upsert_election(ELECTION)
    store.save_municipalities(ELECTION.parent, {"m17": "Љиг"})
    store.save_stations(ELECTION.parent, "m17", {"14": "s140"})
    store.apply_events(ELECTION.parent, store.start_sweep(ELECTION.parent, "t"),
                       [Event("added", stored)])

    class FakeRik:
        election = ELECTION

        def __init__(self):
            self.calls = []

        def listing(self, filters, page):
            self.calls.append((filters, page))
            return ListingPage([stored, missing], 1, 1)

    rik = FakeRik()
    result = StationDocuments(store, lambda parent: rik)(stored.url)

    assert rik.calls == [(Filters(municipality_id="m17", station_no_id="s140"), 1)]
    assert result["parent"] == ELECTION.parent
    assert result["municipality"] == "Љиг" and result["station_no"] == "14"
    assert [(item["url"], item["stored"]) for item in result["documents"]] == [
        (stored.url, True), (missing.url, False)]
    assert result["missing"] == 1
    store.close()


def test_station_documents_reject_unknown_url_and_filter_mismatched_rik_rows():
    source = doc("rg2", 6, "RG-2")
    other = Doc("Other", "2", "Elsewhere", "RG-7", 7,
                f"{BASE}/extfile/other.pdf", "2026-10-30T12:00")
    store = Store(":memory:")
    store.upsert_election(ELECTION)
    store.save_municipalities(ELECTION.parent, {"m17": "Љиг"})
    store.save_stations(ELECTION.parent, "m17", {"14": "s140"})
    store.apply_events(ELECTION.parent, store.start_sweep(ELECTION.parent, "t"),
                       [Event("added", source)])

    class FakeRik:
        def listing(self, filters, page):
            return ListingPage([source, other], 1, 1)

    service = StationDocuments(store, lambda parent: FakeRik())
    assert [item["url"] for item in service(source.url)["documents"]] == [source.url]

    import pytest
    with pytest.raises(ValueError, match="unknown document"):
        service(f"{BASE}/extfile/not-stored.pdf")
    store.close()
