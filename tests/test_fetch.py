import threading

import httpx
import pytest

from rg2watch.fetch import fetch_pdf
from rg2watch.models import Election
from rg2watch.rik_client import AdaptiveLimiter, RikClient, RikError, RikUnavailable

PDF = b"%PDF-1.4 fake"
BASE = "https://www.rik.parlament.gov.rs"


def make(handler, clock=lambda: 0.0, **kw):
    limiter = AdaptiveLimiter(2.0, clock=clock, sleep=lambda s: None)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return RikClient(Election("662405", "660578", "3", "Test", BASE), limiter, http,
                     download_limiter=limiter, **kw), limiter, None


def count_overloads(limiter):
    calls = []
    orig = limiter.on_overload
    limiter.on_overload = lambda: (calls.append(1), orig())
    return calls


def test_fetch_caches(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=PDF)

    client, _, _ = make(handler)
    url = "https://www.rik.parlament.gov.rs/extfile/sr/x/a.pdf"
    p1 = fetch_pdf(client, url, tmp_path / "cache")
    p2 = fetch_pdf(client, url, tmp_path / "cache")
    assert p1 == p2 and p1.read_bytes() == PDF
    assert len(calls) == 1


def test_download_percent_encodes_path():
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, content=PDF)

    client, _, _ = make(handler)
    client.download("/extfile/sr/election_results/668/@БИРАЧКО МЕСТО 7_8b.pdf")
    client.download("/extfile/sr/a%20b.pdf")
    assert seen[0] == ("/extfile/sr/election_results/668/@"
                       "%D0%91%D0%98%D0%A0%D0%90%D0%A7%D0%9A%D0%9E%20%D0%9C%D0%95%D0%A1%D0%A2%D0%9E%207_8b.pdf")
    assert seen[1] == "/extfile/sr/a%20b.pdf"


def test_download_rejects_non_pdf():
    client, _, _ = make(lambda r: httpx.Response(200, content=b"this is not a pdf"))
    with pytest.raises(RikError):
        client.download("/extfile/a.pdf")


def test_download_over_cap_rejected():
    client, _, _ = make(lambda r: httpx.Response(200, content=PDF + b"x" * 1000), max_pdf_bytes=100)
    with pytest.raises(RikError):
        client.download("/extfile/a.pdf")


def test_content_length_over_cap_rejected_without_reading_body():
    def body():
        raise AssertionError("body must not be read")
        yield b""

    client, _, _ = make(lambda r: httpx.Response(
        200, headers={"Content-Length": "999999"}, content=body()), max_pdf_bytes=100)
    with pytest.raises(RikError):
        client.download("/extfile/a.pdf")


def test_non_pdf_body_is_permanent_and_does_not_throttle():
    calls = []
    client, limiter, _ = make(lambda r: (calls.append(1), httpx.Response(200, content=b"<html>Not found</html>"))[1])
    overloads = count_overloads(limiter)
    with pytest.raises(RikError):
        client.download("/extfile/a.pdf")
    assert len(calls) == 1 and len(overloads) == 0


def test_maintenance_html_is_retried_as_overload():
    calls = []
    client, limiter, _ = make(lambda r: (calls.append(1), httpx.Response(
        200, content=b"<html><body>Site under maintenance</body></html>"))[1])
    overloads = count_overloads(limiter)
    with pytest.raises(RikError):
        client.download("/extfile/a.pdf")
    assert len(calls) == 4 and len(overloads) == 4


def test_download_rejects_foreign_host():
    client, _, _ = make(lambda r: httpx.Response(200, content=PDF))
    with pytest.raises(RikError):
        client.download("https://evil.example.com/a.pdf")


@pytest.mark.parametrize("location", ["https://evil.example.com/a.pdf", "/other.pdf"])
def test_redirect_is_not_followed(tmp_path, location):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host != "www.rik.parlament.gov.rs":
            return httpx.Response(200, content=PDF)
        return httpx.Response(302, headers={"Location": location})

    client, limiter, _ = make(handler)
    client.http.follow_redirects = True  # as RikClient.from_url configures it
    overloads = count_overloads(limiter)
    with pytest.raises(RikError):
        fetch_pdf(client, "/extfile/a.pdf", tmp_path / "cache")
    assert len(calls) == 1 and not overloads
    assert not list(tmp_path.rglob("*.pdf"))


def test_empty_body_never_cached(tmp_path):
    client, _, _ = make(lambda r: httpx.Response(200, content=b""))
    with pytest.raises(RikError):
        fetch_pdf(client, "/extfile/a.pdf", tmp_path / "cache")
    assert not list(tmp_path.rglob("*.pdf"))


def test_deadline_stops_trickling_download():
    now = [0.0]

    def body():
        yield PDF
        while True:
            now[0] += 50
            yield b"x"

    client, _, _ = make(lambda r: httpx.Response(200, content=body()), clock=lambda: now[0])
    with pytest.raises(RikUnavailable, match="time limit"):  # transient: retried, refunded
        client.download("/extfile/a.pdf")
    assert now[0] <= 150


def test_deadline_is_per_attempt_not_across_backoff():
    now = [0.0]
    calls = []

    def sleep(s):
        now[0] += s

    def handler(request):
        calls.append(now[0])
        if len(calls) < 3:
            now[0] += 20  # slow failing attempts
            return httpx.Response(503)

        def body():
            yield PDF
            for _ in range(3):
                now[0] += 35  # 105 s of transfer on the third attempt
                yield b"x"
        return httpx.Response(200, content=body())

    limiter = AdaptiveLimiter(2.0, clock=lambda: now[0], sleep=sleep)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = RikClient(Election("662405", "660578", "3", "Test", BASE), limiter, http,
                       download_limiter=limiter)
    assert client.download("/extfile/a.pdf", max_total_s=120).startswith(PDF)
    assert now[0] > 120  # the whole download, backoff included, took longer than one deadline


def test_large_non_pdf_stream_aborts_early():
    sent = []

    def body():
        for i in range(10_000):
            sent.append(i)
            yield b"A" * 1024

    client, limiter, _ = make(lambda r: httpx.Response(200, content=body()))
    with pytest.raises(RikError):
        client.download("/extfile/a.pdf")
    assert len(sent) <= 8


@pytest.mark.parametrize("html", [
    "<html><body>Сајт је у току одржавања</body></html>".encode(),
    "<html><body>Sajt je nedostupan zbog Održavanja</body></html>".encode(),
    "<!DOCTYPE html><html><body>Temporarily BUSY</body></html>".encode(),
])
def test_maintenance_terms_retry(html):
    calls = []
    client, _, _ = make(lambda r: (calls.append(1), httpx.Response(200, content=html))[1])
    with pytest.raises(RikError):
        client.download("/extfile/a.pdf")
    assert len(calls) == 4


def test_html_with_keyword_only_in_script_is_permanent():
    calls = []
    html = b"<html><head><script>var busy = 1; // maintenance</script></head><body>Not found</body></html>"
    client, _, _ = make(lambda r: (calls.append(1), httpx.Response(200, content=html))[1])
    with pytest.raises(RikError):
        client.download("/extfile/a.pdf")
    assert len(calls) == 1


def test_concurrent_fetch_uses_unique_tmp_files(tmp_path):
    barrier = threading.Barrier(2, timeout=10)

    def handler(request):
        barrier.wait()
        return httpx.Response(200, content=PDF)

    client, _, _ = make(handler)
    results, errors = [], []

    def work():
        try:
            results.append(fetch_pdf(client, "/extfile/a.pdf", tmp_path / "cache"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(results) == 2 and results[0] == results[1]
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [results[0].name]
    assert results[0].read_bytes() == PDF
