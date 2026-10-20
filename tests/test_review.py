import http.client
import json
import sqlite3

import pytest

from rg2watch.browse import build_page, record_review, reviews_payload
from rg2watch.cli import main
from rg2watch.models import Finding
from rg2watch.server import start_server
from rg2watch.store import Store
from test_browse import BASE, P, add, doc, rows_of, store

URL = BASE + "/extfile/a.pdf"


def reviewed_store():
    s = store()
    d = doc(URL)
    add(s, d)
    s.add_finding(P, Finding("V1", "HIGH", d, "crossed out"), d.url)
    return s, d


def test_set_review_keeps_history_latest_wins_and_rejects_bad_input():
    s, d = reviewed_store()
    assert s.set_review(BASE + "/nope.pdf", "ok") is None
    s.set_review(URL, "unsure", "  look again ")
    r = s.set_review(URL, "escalate", "")
    assert r["verdict"] == "escalate" and r["note"] is None
    assert [x["verdict"] for x in s.latest_reviews()] == ["escalate"]
    assert s._db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2
    with pytest.raises(ValueError):
        s.set_review(URL, "fine")
    with pytest.raises(ValueError):
        s.set_review(URL, "ok", "x" * 501)
    assert s.latest_reviews("other") == []


def test_todo_is_findings_without_verdict_and_a_new_version_starts_unreviewed():
    s, d = reviewed_store()
    assert [r["url"] for r in s.review_todo()] == [URL]
    s.set_review(URL, "ok")
    assert s.review_todo() == []
    new = doc(BASE + "/extfile/b.pdf", up="2026-10-19T22:00")
    add(s, new, "replaced", URL)
    s.add_finding(P, Finding("V1", "HIGH", new, "again"), new.url)
    assert [r["url"] for r in s.review_todo()] == [new.url]


def test_reviews_table_created_on_an_old_db(tmp_path):
    path = tmp_path / "old.sqlite"
    Store(path).close()
    db = sqlite3.connect(path)  # a DB from before reviews existed
    db.execute("DROP TABLE reviews")
    db.commit()
    db.close()
    Store(path).close()
    names = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert {"reviews", "reviews_url"} <= names


def test_browse_data_has_url_and_latest_verdict_per_version(tmp_path):
    s, d = reviewed_store()
    record_review(s, URL, "escalate", "call the commission")
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    r = rows_of(out)[0]
    assert r["u"] == URL and r["rv"]["v"] == "escalate" and r["rv"]["n"] == "call the commission"
    assert reviews_payload(s)[URL]["v"] == "escalate"


# ---- server ---------------------------------------------------------------------------------
@pytest.fixture
def srv(tmp_path):
    s, _ = reviewed_store()
    (tmp_path / "index.html").write_text("<html></html>")
    server = start_server(tmp_path, tmp_path, 0, lambda: reviews_payload(s),
                          lambda u, v, n: record_review(s, u, v, n))
    server.store = s
    yield server
    server.shutdown()
    server.server_close()


def post(srv, body=None, path="/review", host=None, headers=None, raw=None):
    port = srv.server_port
    host = host or f"127.0.0.1:{port}"
    h = {"Host": host, "Content-Type": "application/json", "X-RG2W": "1",
         "Sec-Fetch-Site": "same-origin", "Origin": f"http://{host}"}
    h.update(headers or {})
    data = raw if raw is not None else json.dumps(body or {"url": URL, "verdict": "ok"}).encode()
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        if v is not None:
            c.putheader(k, v)
    if "Content-Length" not in h:
        c.putheader("Content-Length", str(len(data)))
    c.endheaders()
    try:
        c.send(data)
    except OSError:
        pass
    r = c.getresponse()
    out = r.status, r.read()
    c.close()
    return out


def stored(srv):
    return srv.store._db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]


def test_review_accepted_same_origin(srv):
    s, body = post(srv, {"url": URL, "verdict": "escalate", "note": "x"})
    assert s == 200 and json.loads(body)["review"]["v"] == "escalate" and stored(srv) == 1
    port = srv.server_port
    assert post(srv, headers={"Sec-Fetch-Site": None})[0] == 200  # older browser: Origin only
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("GET", "/reviews.json", headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    assert r.status == 200 and json.loads(r.read())[URL]["v"] == "ok"
    c.close()


@pytest.mark.parametrize("headers, status", [
    ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Sec-Fetch-Site": "same-site"}, 403),
    ({"Origin": "http://evil.example"}, 403),
    ({"Sec-Fetch-Site": None, "Origin": None}, 403),
    ({"X-RG2W": None}, 403),
    ({"X-RG2W": "0"}, 403),
    ({"Content-Type": "text/plain"}, 400),
    ({"Content-Type": "application/x-www-form-urlencoded"}, 400),
    ({"Content-Length": "5000"}, 400),
])
def test_review_refused(srv, headers, status):
    assert post(srv, headers=headers)[0] == status and stored(srv) == 0


def test_review_refuses_foreign_host_oversize_bad_json_and_unknown_url(srv):
    assert post(srv, host="evil.example")[0] == 403
    assert post(srv, raw=b"x" * 4097)[0] == 400
    assert post(srv, raw=b"{not json")[0] == 400
    for bad in ({"url": URL, "verdict": "fine"}, {"url": URL}, {"url": 1, "verdict": "ok"},
                {"url": URL, "verdict": "ok", "note": "x" * 501}, {"url": URL, "verdict": "ok", "x": 1}, [URL]):
        assert post(srv, bad)[0] == 400
    s, body = post(srv, {"url": BASE + "/other.pdf", "verdict": "ok"})
    assert s == 400 and b"unknown document" in body
    assert post(srv, path="/review?x=1")[0] == 405 and post(srv, path="/index.html")[0] == 405
    assert stored(srv) == 0


def test_get_on_review_is_404_and_server_without_store_refuses(srv, tmp_path):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port)
    c.request("GET", "/review", headers={"Host": f"127.0.0.1:{srv.server_port}"})
    assert c.getresponse().status == 404
    c.close()
    bare = start_server(tmp_path, tmp_path, 0)
    try:
        h = f"127.0.0.1:{bare.server_port}"
        c = http.client.HTTPConnection("127.0.0.1", bare.server_port)
        c.request("POST", "/review", body=b"{}", headers={
            "Host": h, "Origin": "http://" + h, "Sec-Fetch-Site": "same-origin",
            "X-RG2W": "1", "Content-Type": "application/json"})
        assert c.getresponse().status == 403
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", bare.server_port)
        c.request("GET", "/reviews.json", headers={"Host": h})
        assert c.getresponse().status == 404
        c.close()
    finally:
        bare.shutdown()
        bare.server_close()


# ---- CLI ------------------------------------------------------------------------------------
def db_with_review(tmp_path):
    path = tmp_path / "w.sqlite"
    s = Store(path)
    s.upsert_election(store().get_election(P))
    d = doc(URL)
    add(s, d)
    s.add_finding(P, Finding("V1", "HIGH", d, "crossed out"), d.url)
    s.add_finding(P, Finding("V4", "MEDIUM", d, "unsigned"), d.url)
    d2 = doc(BASE + "/extfile/c.pdf", no="2")
    add(s, d2)
    s.add_finding(P, Finding("A5", "MEDIUM", d2, "outside"), d2.url)
    s.close()
    return path


def test_cli_review_and_list(tmp_path, capsys):
    db = db_with_review(tmp_path)
    assert main(["review", "--db", str(db), "--list", "todo"]) == 0
    assert "2 form(s)" in capsys.readouterr().out
    assert main(["review", "--db", str(db), URL, "escalate", "--note", "phone RIK"]) == 0
    assert main(["review", "--db", str(db), BASE + "/nope.pdf", "ok"]) == 1
    capsys.readouterr()
    assert main(["review", "--db", str(db), "--list", "escalated", "--parent", P]) == 0
    out = capsys.readouterr().out
    assert "escalate" in out and "phone RIK" in out and "1 form(s)" in out
    assert main(["review", "--db", str(db), "--list", "todo"]) == 0
    assert "1 form(s)" in capsys.readouterr().out
    assert main(["review", "--db", str(db)]) == 2
    with pytest.raises(SystemExit):
        main(["review", "--db", str(db), URL, "maybe"])


def test_cli_labels_from_reviews_writes_a_new_file_only(tmp_path, capsys):
    db = db_with_review(tmp_path)
    main(["review", "--db", str(db), URL, "ok", "--note", "legit correction"])
    main(["review", "--db", str(db), BASE + "/extfile/c.pdf", "unsure"])
    out = tmp_path / "sugg.jsonl"
    assert main(["labels", "--db", str(db), "--from-reviews", "--out", str(out)]) == 0
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1 and lines[0]["file"] == "a.pdf" and lines[0]["expect"] == {"V1": False, "V4": False}
    assert lines[0]["verified"] is False and "legit correction" in lines[0]["note"]
    before = out.read_bytes()
    assert main(["labels", "--db", str(db), "--from-reviews", "--out", str(out)]) == 2
    assert out.read_bytes() == before
