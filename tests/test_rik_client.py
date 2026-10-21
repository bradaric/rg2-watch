import json
import threading
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from rg2watch.config import Config
from rg2watch.models import Election, Filters
from rg2watch.rik_client import (
    AdaptiveLimiter,
    RikBlocked,
    RikClient,
    RikError,
    RikNotFound,
    RikUnavailable,
)

FIX = Path(__file__).parent / "fixtures"
BASE = "https://www.rik.parlament.gov.rs"
ELECTION = Election("662405", "660578", "3", "Test", BASE)


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def make(handler, max_rps=2.0):
    t = FakeTime()
    limiter = AdaptiveLimiter(max_rps, clock=t.clock, sleep=t.sleep)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return RikClient(ELECTION, limiter, http), limiter, t


def page_payload(n_records, max_page):
    rec = {"municipality": "M", "election_station_number": "1",
           "election_station_name": "S", "extfiles": []}
    pag = "".join(f'<li><a data-page="{i}">{i}</a></li>' for i in range(1, max_page + 1))
    return {"records": [rec] * n_records, "pagination": pag}


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def test_listing_sends_expected_form_fields():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=page_payload(1, 1))

    client, _, _ = make(handler)
    client.listing(Filters(doc_type=6), 3)
    r = seen[0]
    f = form(r)
    assert r.method == "POST" and r.url.path == "/get-election"
    assert f["filters[election_document_type]"] == "6"
    assert "filters[municipality]" not in f
    assert "filters[election_station_no]" not in f
    assert f["page"] == "3"
    assert f["data[type]"] == "1"
    assert f["data[parent]"] == "662405"
    assert f["data[election_round]"] == "660578"
    assert f["data[property_election_type]"] == "3"
    assert f["election_type"] == "3"
    assert r.headers["X-Requested-With"] == "XMLHttpRequest"
    assert r.headers["Referer"] == f"{BASE}/zapisnici/662405"


def test_listing_sends_all_filters_when_set():
    seen = []

    def handler(request):
        seen.append(form(request))
        return httpx.Response(200, json=page_payload(0, 0))

    client, _, _ = make(handler)
    client.listing(Filters(6, "207", "99"), 1)
    assert seen[0]["filters[municipality]"] == "207"
    assert seen[0]["filters[election_station_no]"] == "99"


def test_listing_parses_fixture():
    payload = json.loads((FIX / "get_election_662405_rg2_p1.json").read_text(encoding="utf-8"))
    client, _, _ = make(lambda r: httpx.Response(200, json=payload))
    page = client.listing(Filters(), 1)
    assert page.records == 10 and page.docs


def test_count_two_requests():
    pages = []

    def handler(request):
        p = int(form(request)["page"])
        pages.append(p)
        return httpx.Response(200, json=page_payload(10 if p == 1 else 3, 7))

    client, _, _ = make(handler)
    assert client.count(Filters(doc_type=6)) == 63
    assert pages == [1, 7]


def test_count_single_page():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=page_payload(4, 1))

    client, _, _ = make(handler)
    assert client.count(Filters()) == 4
    assert len(calls) == 1


def test_count_empty():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"records": [], "pagination": ""})

    client, _, _ = make(handler)
    assert client.count(Filters()) == 0
    assert len(calls) == 1


def test_limiter_halves_on_overload_and_recovers():
    t = FakeTime()
    lim = AdaptiveLimiter(2.0, clock=t.clock, sleep=t.sleep)
    assert lim.current_rps == 2.0
    lim.on_overload()
    assert lim.current_rps == 1.0
    lim.on_overload()
    assert lim.current_rps == 0.5
    for _ in range(10):
        lim.on_success(0.1)
    assert lim.current_rps == pytest.approx(1.5)
    for _ in range(20):
        lim.on_success(0.1)
    assert lim.current_rps == 2.0


def test_limiter_floor_and_mid_latency():
    lim = AdaptiveLimiter(2.0, min_rps=0.1)
    for _ in range(10):
        lim.on_overload()
    assert lim.current_rps == 0.1
    lim.on_success(3.0)
    assert lim.current_rps == 0.1


def test_limiter_acquire_spaces_requests():
    t = FakeTime()
    lim = AdaptiveLimiter(2.0, clock=t.clock, sleep=t.sleep)
    lim.acquire()
    lim.acquire()
    lim.acquire()
    assert t.now == pytest.approx(1.0)


def test_slow_response_counts_as_overload():
    t = FakeTime()

    def handler(request):
        t.now += 6.0
        return httpx.Response(200, json=page_payload(0, 0))

    limiter = AdaptiveLimiter(2.0, clock=t.clock, sleep=t.sleep)
    client = RikClient(ELECTION, limiter, httpx.Client(transport=httpx.MockTransport(handler)))
    client.listing(Filters(), 1)
    assert limiter.current_rps == 1.0


def test_retries_then_raises_unavailable():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    client, limiter, t = make(handler)
    with pytest.raises(RikUnavailable):
        client.listing(Filters(), 1)
    assert len(calls) == 4
    assert limiter.current_rps < 2.0
    backoffs = [s for s in t.sleeps if s >= 2]
    assert len(backoffs) == 3
    for b, base in zip(backoffs, (2, 4, 8)):
        assert base <= b < base + 1


def test_timeout_is_retried_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=page_payload(2, 1))

    client, limiter, _ = make(handler)
    assert client.listing(Filters(), 1).records == 2
    assert len(calls) == 3
    assert limiter.current_rps < 2.0


def test_429_retried_and_4xx_raises_immediately():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429 if len(calls) == 1 else 404)

    client, _, _ = make(handler)
    with pytest.raises(RikError) as e:
        client.listing(Filters(), 1)
    assert not isinstance(e.value, RikUnavailable)
    assert len(calls) == 2


def test_404_raises_typed_error_immediately_for_download_and_listing():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    client, _, _ = make(handler)
    with pytest.raises(RikNotFound) as download_error:
        client.download(BASE + "/extfile/missing.pdf")
    assert isinstance(download_error.value, RikError)
    assert not isinstance(download_error.value, RikUnavailable)
    assert len(calls) == 1
    assert client.download_last_ok is None

    client.download_blocked_streak = 2
    client.download_last_ok = 123.0
    with pytest.raises(RikNotFound) as listing_error:
        client.listing(Filters(doc_type=6), 1)
    assert isinstance(listing_error.value, RikError)
    assert not isinstance(listing_error.value, RikUnavailable)
    assert len(calls) == 2
    assert client.last_ok is None
    assert client.download_blocked_streak == 2
    assert client.download_last_ok == 123.0


def test_pdf_404_breaks_consecutive_blocked_streak():
    statuses = iter((403, 404, 403, 404, 403))
    client, _, _ = make(lambda request: httpx.Response(next(statuses)))
    url = BASE + "/extfile/missing.pdf"

    for status, expected_streak in zip((403, 404, 403, 404, 403), (1, 0, 1, 0, 1)):
        error = RikBlocked if status == 403 else RikNotFound
        with pytest.raises(error):
            client.download(url)
        assert client.download_blocked_streak == expected_streak
        assert client.download_last_ok is None


def test_municipalities_union():
    seen = []

    def handler(request):
        q = request.url.params["q"]
        seen.append(q)
        assert request.url.path == "/templates/election/getElectionStations.php"
        assert request.url.params["electionId"] == "660578"
        data = {
            "a": [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}],
            "e": [{"id": "2", "name": "B"}, {"id": "3", "name": "C"}],
            "i": [], "o": [{"id": "1", "name": "A"}], "u": [{"id": "4", "name": "D"}],
        }[q]
        return httpx.Response(200, json=data)

    client, _, _ = make(handler)
    assert client.municipalities() == {"1": "A", "2": "B", "3": "C", "4": "D"}
    assert seen == list("aeiou")


def test_stations_decodes_json_string_body_despite_html_content_type():
    inner = {"number": '<option value="-1" selected>x</option><option value="207620">1</option>'
                       '<option value="207621">2</option>', "name": "<option></option>"}

    def handler(request):
        f = form(request)
        assert request.url.path == "/templates/election/ajaxSelectMunicipality.php"
        assert f["method"] == "getelectionstation" and f["municipalityId"] == "207"
        assert f["html"] == "1" and f["get_both"] == "1"
        assert f["site_lang"] == "sr" and f["election_round"] == "660578"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        return httpx.Response(200, text=json.dumps(inner),
                              headers={"Content-Type": "text/html; charset=UTF-8"})

    client, _, _ = make(handler)
    assert client.stations("207") == {"207620": "1", "207621": "2"}


def test_from_url_uses_origin_or_override(monkeypatch):
    html = (FIX / "zapisnici_662405.html").read_text(encoding="utf-8")
    hosts = []

    def handler(request):
        hosts.append(str(request.url))
        return httpx.Response(200, text=html)

    orig = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))
    c1 = RikClient.from_url(f"{BASE}/zapisnici/662405", Config())
    c2 = RikClient.from_url(f"{BASE}/zapisnici/662405", Config(base_url="http://127.0.0.1:9"))
    assert hosts == [f"{BASE}/zapisnici/662405", "http://127.0.0.1:9/zapisnici/662405"]
    assert c1.election.parent == "662405" and c1.election.base_url == BASE
    assert c2.election.base_url == "http://127.0.0.1:9"


def test_html_200_is_overload_then_recovers():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) <= 2:
            return httpx.Response(200, text="<html>Service busy</html>",
                                  headers={"Content-Type": "text/html"})
        return httpx.Response(200, json=page_payload(2, 1))

    client, limiter, _ = make(handler)
    assert client.listing(Filters(), 1).records == 2
    assert len(calls) == 3
    assert limiter.current_rps < 2.0


@pytest.mark.parametrize("body", ['[1, 2]', '{"x": 1}', '"str"'])
def test_wrong_shape_200_raises_unavailable(body):
    client, limiter, _ = make(lambda r: httpx.Response(200, text=body))
    with pytest.raises(RikUnavailable):
        client.listing(Filters(), 1)
    assert limiter.current_rps < 2.0


def test_retry_after_respected_and_capped():
    for header, expected in (("30", 30.0), ("500", 60.0)):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503, headers={"Retry-After": header})
            return httpx.Response(200, json=page_payload(0, 0))

        client, _, t = make(handler)
        client.listing(Filters(), 1)
        assert max(t.sleeps) == expected


def test_limiter_threads_never_exceed_cap():
    lock = threading.Lock()
    now = [0.0]
    slots = []

    def clock():
        return now[0]

    def sleep(s):
        with lock:
            now[0] += s

    lim = AdaptiveLimiter(2.0, clock=clock, sleep=sleep)

    def worker():
        for _ in range(25):
            lim.acquire()
            with lock:
                slots.append(now[0])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    # 50 acquires at 2 rps must span at least 24.5 s of (fake) time
    assert len(slots) == 50
    assert now[0] >= 24.5


def test_403_is_blocked_overload_without_retry_and_tracked():
    calls = []
    status = [403]

    def handler(request):
        calls.append(1)
        return httpx.Response(status[0], json=page_payload(0, 0))

    client, limiter, t = make(handler)
    assert client.last_ok is None
    for _ in range(2):
        with pytest.raises(RikBlocked) as error:
            client.listing(Filters(), 1)
        assert not isinstance(error.value, RikNotFound)
    assert len(calls) == 2 and client.blocked_streak == 2
    assert limiter.current_rps < 2.0
    assert issubclass(RikBlocked, RikUnavailable)
    status[0] = 200
    client.listing(Filters(), 1)
    assert client.blocked_streak == 0 and client.last_ok is not None
