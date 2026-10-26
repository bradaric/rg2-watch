import json

import httpx
import pytest

from rg2watch.models import Election
from rg2watch.official_results import OfficialResultsClient, OfficialResultsError, write_official_results


ELECTION = Election("779676", "x", "3", "Записници Парламентарни избори 2026",
                    "https://rik.example")


def result_payload():
    return {
        "processed_config": {"data": {"datasets": [{"data": [680_318, 1_394_833]}],
                                        "labels": ["Није изашло", "Изашло"]}},
        "sum_config": {"data": {"datasets": [{"data": [1_374_739, 19_735]}],
                                  "labels": ["Важећи", "Неважећи"]}},
        "minute_from_election_station": "<b>must not be copied</b>",
        "stat_sum_numbers": {
            "available": "1.394.833", "number_of_stations": "8.392",
            "datetime": "26.10.2026 00:20:00", "election_title": "Парламентарни 2026",
            "allowed_download": True, "total_voters": "6.451.292",
            "processed_stations": "3.791",
        },
        "table_data": [
            {"count_number": 1, "list_image": "", "list_name": "1. Листа <један>",
             "won_number": "608.711", "won_percent": 43.65},
            {"count_number": 2, "list_image": "", "list_name": "2. Листа два",
             "won_number": "33.378", "won_percent": 2.49},
        ],
    }


def test_client_discovers_current_parliamentary_round_and_normalizes_national_results():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content.decode()))
        if request.url.path == "/542645/rezultati/":
            return httpx.Response(200, text="page", headers={"Set-Cookie": "sid=test; Path=/"})
        assert request.headers["Cookie"] == "sid=test"
        if request.url.path == "/get-elections/":
            return httpx.Response(200, json={"rounds": {
                "341140": "Парламентарни 2023", "680072": "Парламентарни 2026"}})
        if request.url.path == "/get_results/":
            return httpx.Response(200, json=result_payload())
        raise AssertionError(request.url)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OfficialResultsClient(ELECTION, http, now=lambda: "2026-10-26T00:21:00Z")

    data = client.fetch()

    assert [(x[0], x[1]) for x in seen] == [
        ("GET", "/542645/rezultati/"), ("POST", "/get-elections/"),
        ("POST", "/get_results/")]
    assert "election_round=680072" in seen[-1][2]
    assert data == {
        "built": "2026-10-26T00:21:00Z",
        "rik_at": "26.10.2026 00:20:00",
        "title": "Парламентарни 2026",
        "source": "https://rik.example/542645/rezultati/",
        "stations": {"processed": 3791, "total": 8392},
        "voters": {"registered": 6451292, "processed_registered": 2075151,
                   "voted": 1394833, "turnout": pytest.approx(1394833 / 2075151)},
        "ballots": {"valid": 1374739, "invalid": 19735},
        "lists": [
            {"no": 1, "name": "Листа <један>", "votes": 608711, "share": 0.4365},
            {"no": 2, "name": "Листа два", "votes": 33378, "share": 0.0249},
        ],
    }

    client.fetch()
    assert [x[1] for x in seen].count("/542645/rezultati/") == 1
    assert [x[1] for x in seen].count("/get-elections/") == 1
    assert [x[1] for x in seen].count("/get_results/") == 2


def test_client_rejects_missing_or_ambiguous_matching_round():
    def client_with(rounds):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, text="page")
            return httpx.Response(200, json={"rounds": rounds})
        return OfficialResultsClient(
            ELECTION, httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(OfficialResultsError, match="no RIK results round"):
        client_with({"1": "Парламентарни 2023"}).fetch()
    with pytest.raises(OfficialResultsError, match="multiple RIK results rounds"):
        client_with({"1": "Парламентарни 2026", "2": "Избори 2026"}).fetch()


def test_client_reestablishes_session_after_results_access_is_refused():
    results_calls = 0
    pages = 0

    def handler(request):
        nonlocal results_calls, pages
        if request.method == "GET":
            pages += 1
            return httpx.Response(200, text="page", headers={"Set-Cookie": f"sid={pages}; Path=/"})
        if request.url.path == "/get-elections/":
            return httpx.Response(200, json={"rounds": {"680072": "Парламентарни 2026"}})
        results_calls += 1
        if results_calls == 2:
            return httpx.Response(403, text="expired")
        return httpx.Response(200, json=result_payload())

    client = OfficialResultsClient(
        ELECTION, httpx.Client(transport=httpx.MockTransport(handler)))
    client.fetch()

    assert client.fetch()["stations"]["processed"] == 3791
    assert pages == 2 and results_calls == 3


def test_atomic_writer_keeps_a_self_contained_json_snapshot(tmp_path):
    path = tmp_path / "official-results.json"
    data = {"built": "now", "lists": [{"name": "Листа <један>"}]}

    write_official_results(path, data)

    assert json.loads(path.read_text()) == data
    assert list(tmp_path.iterdir()) == [path]
