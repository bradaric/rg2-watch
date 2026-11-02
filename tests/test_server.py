import gzip
import hashlib
import http.client
import json
import socket

import pytest

from rg2watch.server import start_server
from rg2watch.operator_feed import OperatorGroupTooLarge

H = hashlib.sha256(b"x").hexdigest()


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("srv")
    out, cache = tmp_path / "web", tmp_path / "cache"
    out.mkdir()
    cache.mkdir()
    (out / "index.html").write_text("<html>page</html>")
    (out / "data.json").write_text('{"rows": []}')
    (out / "data-meta.json").write_text('{"built":"now","corrections":{}}')
    (out / "status.js").write_text("window.RG2_STATUS = {};")
    (out / "status.json").write_text("{}")
    (out / "official-results.json").write_text('{"lists": []}')
    (out / "data.js").write_text("window.RG2_DATA = {};")
    (out / "w.sqlite").write_text("secret")
    (tmp_path / ".env").write_text("KEY=1")
    (cache / (H + ".pdf")).write_bytes(b"%PDF-1.4 body")
    (cache / "notes.txt").write_text("x")
    server = start_server(out, cache, 0)
    yield server
    server.shutdown()
    server.server_close()


def req(srv, path, method="GET", host=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{srv.server_port}")
    for name, value in (headers or {}).items():
        c.putheader(name, value)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, r.getheader("Content-Type"), body, r


def classification_post(srv, body, origin=None):
    host = f"127.0.0.1:{srv.server_port}"
    raw = json.dumps(body).encode()
    headers = {"Host": host, "Origin": origin or f"http://{host}",
               "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json",
               "X-RG2W": "1"}
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port)
    c.request("POST", "/classification", body=raw, headers=headers)
    r = c.getresponse()
    result = r.status, json.loads(r.read())
    c.close()
    return result


def test_classification_endpoint_is_guarded_and_exact(tmp_path):
    calls = []
    server = start_server(tmp_path, tmp_path, 0,
                          classification_api=lambda url, action: calls.append((url, action)) or {"ok": True})
    try:
        assert classification_post(server, {"url": "https://x/doc.pdf",
                                            "action": "confirm_rg2"}) == (200, {"ok": True})
        assert calls == [("https://x/doc.pdf", "confirm_rg2")]
        assert classification_post(server, {"url": "https://x/doc.pdf",
                                            "action": "quarantine_rg7"}) == (200, {"ok": True})
        assert classification_post(server, {"url": "https://x/doc.pdf", "action": "revoke"},
                                   "http://evil.example")[0] == 403
        assert classification_post(server, {"url": "https://x/doc.pdf", "action": "other"})[0] == 400
    finally:
        server.shutdown()
        server.server_close()


def test_live_rik_station_endpoint_is_same_origin_and_exact(tmp_path):
    calls = []
    server = start_server(tmp_path, tmp_path, 0,
                          station_api=lambda url: calls.append(url) or {"documents": [], "missing": 0})
    try:
        headers = {"Sec-Fetch-Site": "same-origin"}
        status, content_type, body, _ = req(
            server, "/rik-station?url=https%3A%2F%2Frik.example%2Fdoc.pdf", headers=headers)
        assert status == 200 and content_type == "application/json"
        assert json.loads(body) == {"documents": [], "missing": 0}
        assert calls == ["https://rik.example/doc.pdf"]
        assert req(server, "/rik-station", headers=headers)[0] == 400
        assert req(server, "/rik-station?url=x&url=y", headers=headers)[0] == 400
        assert req(server, "/rik-station?url=x", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    finally:
        server.shutdown()
        server.server_close()


def test_operator_feed_endpoints_are_same_origin_read_only_and_exact(tmp_path):
    class Feed:
        def feed(self, parent, since):
            if parent == "missing":
                raise KeyError(parent)
            return {"parent": parent, "through": since, "generation": "g", "groups": []}

        def bootstrap(self, parent, cursor=None):
            if parent == "too-large":
                raise OperatorGroupTooLarge("hidden report contents")
            return {"parent": parent, "cursor": cursor, "generation": "g", "groups": []}

    server = start_server(tmp_path, tmp_path, 0, operator_feed=Feed())
    same = {"Sec-Fetch-Site": "same-origin"}
    try:
        status, content_type, body, _ = req(
            server, "/operator-feed?parent=662405&since=7", headers=same)
        assert status == 200 and content_type == "application/json"
        assert json.loads(body)["through"] == 7
        assert req(server, "/operator-feed?parent=662405&since=-1", headers=same)[0] == 400
        assert req(server, "/operator-feed?parent=missing&since=0", headers=same)[0] == 404
        assert req(server, "/operator-feed?parent=x&since=0&extra=1", headers=same)[0] == 400
        assert req(server, "/operator-feed?parent=x&since=0",
                   headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert req(server, "/operator-feed?parent=x&since=0")[0] == 403

        status, _, body, _ = req(server, "/operator-bootstrap?parent=662405", headers=same)
        assert status == 200 and json.loads(body)["cursor"] is None
        status, _, body, _ = req(
            server, "/operator-bootstrap?parent=662405&cursor=opaque", headers=same)
        assert status == 200 and json.loads(body)["cursor"] == "opaque"
        assert req(server, "/operator-bootstrap?parent=x&cursor=a&cursor=b", headers=same)[0] == 400
        status, _, body, _ = req(server, "/operator-bootstrap?parent=too-large", headers=same)
        assert status == 413
        assert json.loads(body) == {"error": "operator_group_too_large",
                                    "recovery": "reduce the tagged group before retrying bootstrap"}
        assert b"hidden report contents" not in body
    finally:
        server.shutdown()
        server.server_close()


def test_serves_allow_listed_files(srv):
    s, ct, body, r = req(srv, "/")
    assert s == 200 and body == b"<html>page</html>" and ct.startswith("text/html")
    assert "default-src 'self'" in r.getheader("Content-Security-Policy")
    assert req(srv, "/index.html?t=1")[0] == 200
    assert req(srv, "/data.json")[:2] == (200, "application/json")
    assert req(srv, "/data-meta.json")[:2] == (200, "application/json")
    assert req(srv, "/official-results.json")[:2] == (200, "application/json")
    s, ct, body, r = req(srv, f"/pdfs/{H}.pdf")
    assert (s, ct, body) == (200, "application/pdf", b"%PDF-1.4 body")
    assert r.getheader("Content-Security-Policy") is None  # sandbox breaks Chrome's PDF viewer
    csp = req(srv, "/")[3].getheader("Content-Security-Policy")
    assert all(x in csp for x in ("frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'"))


def test_script_variants_are_not_served_over_http(srv):
    # a cross-origin <script src> could read their globals; the page fetch()es the .json files
    assert req(srv, "/status.js")[0] == 404 and req(srv, "/data.js")[0] == 404


def test_cross_site_requests_refused(srv):
    for path in ("/", "/data.json", "/status.json", "/jquery.min.js"):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_port)
        c.putrequest("GET", path, skip_host=True)
        c.putheader("Host", f"127.0.0.1:{srv.server_port}")
        c.putheader("Sec-Fetch-Site", "cross-site")
        c.endheaders()
        r = c.getresponse()
        r.read()
        assert r.status == 404
        c.close()


def test_head_has_length_and_no_body(srv):
    s, _, body, r = req(srv, "/data.json", "HEAD")
    assert s == 200 and body == b"" and r.getheader("Content-Length") == "12"


def test_large_json_supports_gzip_get_and_head(tmp_path):
    out, cache = tmp_path / "web", tmp_path / "cache"
    out.mkdir()
    cache.mkdir()
    raw = ('{"rows":["' + "Ћ" * 5000 + '"]}').encode()
    (out / "data.json").write_bytes(raw)
    server = start_server(out, cache, 0)
    try:
        s, _, body, r = req(server, "/data.json", headers={"Accept-Encoding": "br, gzip"})
        assert s == 200 and r.getheader("Content-Encoding") == "gzip"
        assert r.getheader("Vary") == "Accept-Encoding"
        assert int(r.getheader("Content-Length")) == len(body)
        assert gzip.decompress(body) == raw

        s, _, body, r = req(server, "/data.json", "HEAD",
                            headers={"Accept-Encoding": "gzip"})
        assert s == 200 and body == b"" and r.getheader("Content-Encoding") == "gzip"
        assert int(r.getheader("Content-Length")) == len(gzip.compress(raw, mtime=0))

        assert req(server, "/data.json", headers={"Accept-Encoding": "gzip;q=0.0"})[2] == raw
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("path", [
    "/w.sqlite", "/../.env", "/%2e%2e/.env", "/..%2f.env", "/pdfs/../w.sqlite", "/pdfs/", "/pdfs",
    "/pdfs/notes.txt", f"/pdfs/{H}.pdf/x", f"/pdfs/{H[:-1]}.pdf", f"/pdfs/{H.upper()}.pdf",
    f"/pdfs/..%2f{H}.pdf", f"/pdfs/%2e%2e/{H}.pdf", "/pdfs/" + "0" * 64 + ".pdf",
    "/index.html/", "/static/", "/.env", "/%00", "/data.js%00.png", "\\..\\.env"])
def test_everything_else_is_404(srv, path):
    s, _, body, _ = req(srv, path)
    assert s == 404 and b"secret" not in body and b"KEY" not in body


def test_only_get_and_head(srv):
    for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
        s, _, _, r = req(srv, "/index.html", m)
        assert s == 405 and r.getheader("Allow") == "GET, HEAD"


def test_foreign_host_header_refused(srv):
    assert req(srv, "/index.html", host="evil.example")[0] == 404
    assert req(srv, "/index.html", host=f"localhost:{srv.server_port}")[0] == 200


def test_binds_loopback_only(srv):
    assert srv.server_address[0] == "127.0.0.1"


def test_port_in_use_returns_none_with_warning(tmp_path, srv, caplog):
    assert start_server(tmp_path, tmp_path, srv.server_port) is None
    assert "unavailable" in caplog.text


def test_serves_while_a_connection_is_idle(srv):
    idle = socket.create_connection(("127.0.0.1", srv.server_port))  # threaded: not blocked
    try:
        assert req(srv, "/")[0] == 200
    finally:
        idle.close()
