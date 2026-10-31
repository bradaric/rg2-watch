import json
import re
from pathlib import Path

from rg2watch.models import DOC_TYPES, LABEL_TO_TYPE, Election, ListingPage
from rg2watch.rik_parse import (
    parse_election_page,
    parse_listing,
    parse_municipalities,
    parse_station_options,
)

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.rik.parlament.gov.rs"


def _listing():
    return json.loads((FIX / "get_election_662405_rg2_p1.json").read_text(encoding="utf-8"))


def test_label_to_type_matches_fixture_select():
    html = (FIX / "zapisnici_662405.html").read_text(encoding="utf-8")
    sel = re.search(r'name="election_document_type".*?</select>', html, re.S).group(0)
    ids = {int(v) for v in re.findall(r'<option value="(\d+)"', sel)}
    assert set(LABEL_TO_TYPE.values()) == ids == set(DOC_TYPES)
    assert LABEL_TO_TYPE["Записник о раду бирачког одбора (Образац РГ-2)"] == 6


def test_parse_election_page_662405():
    html = (FIX / "zapisnici_662405.html").read_text(encoding="utf-8")
    assert parse_election_page(html, BASE) == Election(
        parent="662405", round="660578", election_type="3",
        title="Записници локални избори 2025", base_url=BASE,
    )


def test_parse_election_page_779676():
    html = (FIX / "zapisnici_779676.html").read_text(encoding="utf-8")
    e = parse_election_page(html, BASE)
    assert (e.parent, e.round, e.election_type) == ("779676", "680072", "2")


def test_parse_listing_rg2_page1():
    page = parse_listing(_listing(), BASE)
    assert page.records == 10
    assert page.max_page == 23
    first = page.docs[0]
    assert first.municipality == "Град Зајечар"
    assert first.station_no == "1"
    assert first.doc_type == 6
    assert first.url.startswith(BASE)
    assert first.url.endswith("@BM-01_d1fc6918e842cb22c3e386e650a452fa.pdf")
    assert first.uploaded_at == "2025-06-08T23:29"
    st1 = [d for d in page.docs if d.station_key == "Град Зајечар|1"]
    assert {d.doc_type for d in st1} >= {6, 12}


def test_parse_listing_multiple_docs_in_one_blob():
    page = parse_listing(_listing(), BASE)
    bm03 = [d for d in page.docs if ("BM-03" in d.url or "BM03" in d.url)]
    assert len(bm03) >= 3
    assert any(d.label.startswith("Решење о исправљању") for d in bm03)
    assert {d.doc_type for d in bm03} >= {6, 12, 19}


def test_parse_listing_unknown_label_gives_none_type():
    payload = {"records": [{
        "municipality": "M", "election_station_number": "5", "election_station_name": "S",
        "extfiles": [{"extfile": "<div class=''><span>Нешто  ново</span></div>"
                      "<div class='table-file'><a href='/x/a.pdf'>a</a><span>01-02-2025 03:04h</span></div>"}],
    }], "pagination": ""}
    page = parse_listing(payload, BASE)
    assert page.docs[0].doc_type is None
    assert page.docs[0].label == "Нешто ново"
    assert page.max_page == 1


def test_parse_listing_keeps_nsnm_council_in_station_identity():
    payload = {"records": [{
        "municipality": "Ада", "election_station_number": "1",
        "election_station_name": "I МЕСНА ЗАЈЕДНИЦА",
        "minority": "Национални савет МАЂАРСКЕ националне мањине",
        "extfiles": [{"extfile": "<div class=''><span>Записник о раду бирачког одбора (Образац РГ-2)</span></div>"
                      "<div class='table-file'><a href='/x/a.pdf'>a</a><span>01-02-2025 03:04h</span></div>"}],
    }], "pagination": ""}

    doc = parse_listing(payload, BASE).docs[0]

    assert doc.minority == "Национални савет МАЂАРСКЕ националне мањине"
    assert doc.station_key == "Ада|1|Национални савет МАЂАРСКЕ националне мањине"


def test_parse_listing_repairs_known_missing_nsnm_councils():
    expected = {
        "/extfile/sr/election_results/63715b6eb9bc2/@14БУГ_66494ba92e1f7f78d0c0f00ab1deaec7.pdf":
            "Национални савет БУГАРСКЕ националне мањине",
        "/extfile/sr/election_results/637152ec06a83/@25БО_37295f736f26f0f6388efdd4dfe6a48d.pdf":
            "Национални савет БОШЊАЧКЕ националне мањине",
    }
    payload = {"records": [{
        "municipality": "M", "election_station_number": str(i), "election_station_name": "S",
        "minority": None,
        "extfiles": [{"extfile": "<div class=''><span>Записник о раду бирачког одбора (Образац РГ-2)</span></div>"
                      f"<div class='table-file'><a href='{url}'>a</a><span>01-02-2025 03:04h</span></div>"}],
    } for i, url in enumerate(expected, 1)], "pagination": ""}

    docs = parse_listing(payload, BASE).docs

    assert {doc.url: doc.minority for doc in docs} == {
        BASE + url: council for url, council in expected.items()
    }


def test_parse_listing_empty():
    assert parse_listing({"records": [], "pagination": ""}, BASE) == ListingPage([], 0, 0)


def test_parse_municipalities():
    assert parse_municipalities([{"id": 1, "name": "A"}, {"id": "2", "name": "B"}]) == {"1": "A", "2": "B"}


def test_parse_station_options():
    s = '<option value="-1">x</option><option value="207620">1</option>'
    assert parse_station_options(s) == {"207620": "1"}


def _one(stamp_span: str) -> dict:
    return {"records": [{
        "municipality": "M", "election_station_number": "5", "election_station_name": "S",
        "extfiles": [{"extfile": "<div class=''><span>Записник о раду бирачког одбора (Образац РГ-2)</span></div>"
                      f"<div class='table-file'><a href='/x/a.pdf'>a</a>{stamp_span}</div>"
                      "<div class=''><span>Записник о посматрачима (Образац РГ-11/НП)</span></div>"
                      "<div class='table-file'><a href='/x/b.pdf'>b</a><span>01-02-2025 03:04h</span></div>"}],
    }], "pagination": ""}


def test_malformed_timestamps_keep_docs_and_warn_once(caplog):
    with caplog.at_level("WARNING", logger="rg2watch.rik_parse"):
        empty = parse_listing(_one("<span></span>"), BASE)
        odd = parse_listing(_one("<span> 8.6.2025 23:29 </span>"), BASE)
        parse_listing(_one("<span> 8.6.2025 23:29 </span>"), BASE)
    assert [d.uploaded_at for d in empty.docs] == ["", "2025-02-01T03:04"]
    assert [d.uploaded_at for d in odd.docs] == ["8.6.2025 23:29", "2025-02-01T03:04"]
    assert [d.doc_type for d in odd.docs] == [6, 12]
    msgs = [r.getMessage() for r in caplog.records]
    assert len([m for m in msgs if "8.6.2025 23:29" in m]) == 1


def test_link_without_timestamp_logs_count_mismatch(caplog):
    with caplog.at_level("WARNING", logger="rg2watch.rik_parse"):
        page = parse_listing(_one(""), BASE)
    assert [(d.url, d.doc_type) for d in page.docs] == [(BASE + "/x/b.pdf", 12)]
    assert any("2 links" in r.getMessage() and "1 document" in r.getMessage() for r in caplog.records)


def test_href_may_contain_opposite_quote():
    payload = {"records": [{
        "municipality": "M", "election_station_number": "5", "election_station_name": "S",
        "extfiles": [{"extfile": "<div class=''><span>Записник о раду бирачког одбора (Образац РГ-2)</span></div>"
                      "<div class='table-file'><a href='/x/a\"b.pdf'>a</a><span>01-02-2025 03:04h</span></div>"
                      "<div class=''><span>Записник о посматрачима (Образац РГ-11/НП)</span></div>"
                      "<div class='table-file'><a href=\"/x/c'd.pdf\">b</a><span>01-02-2025 03:05h</span></div>"}],
    }], "pagination": ""}
    page = parse_listing(payload, BASE)
    assert [(d.url, d.doc_type) for d in page.docs] == [
        (BASE + '/x/a"b.pdf', 6), (BASE + "/x/c'd.pdf", 12)]
