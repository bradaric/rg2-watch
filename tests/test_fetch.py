import httpx
import pytest

from rg2watch.fetch import fetch_pdf
from rg2watch.models import Election
from rg2watch.rik_client import AdaptiveLimiter, RikClient, RikError

PDF = b"%PDF-1.4 fake"
BASE = "https://www.rik.parlament.gov.rs"


def make(handler):
    limiter = AdaptiveLimiter(2.0, clock=lambda: 0.0, sleep=lambda s: None)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return RikClient(Election("662405", "660578", "3", "Test", BASE), limiter, http), limiter, None


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
    client, _, _ = make(lambda r: httpx.Response(200, content=b"<html>maintenance</html>"))
    with pytest.raises(RikError):
        client.download("/extfile/a.pdf")
