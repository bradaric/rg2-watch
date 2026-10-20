import hashlib
import json
import re

import rg2watch.cli as cli
from rg2watch.browse import build_page
from rg2watch.cli import main
from rg2watch.models import Doc, Election, Event, Finding
from rg2watch.store import Store

P = "662405"
BASE = "https://rik.example"


def doc(url, muni="Сечањ", no="1", dt=6, up="2026-10-19T20:15", label="lbl"):
    return Doc(muni, no, "ОШ", label, dt, url, up)


def store():
    s = Store(":memory:")
    s.upsert_election(Election(P, "1", "t", "Title", BASE))
    return s


def add(s, d, kind="added", prev=None):
    s.apply_events(P, s.start_sweep(P, "rg2"), [Event(kind, d, prev)])


def rows_of(path):
    html = path.read_text(encoding="utf-8")
    return json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',
                                html, re.S).group(1))["rows"]


def test_groups_versions_and_states(tmp_path):
    s = store()
    old, new = doc(BASE + "/extfile/old.pdf"), doc(BASE + "/extfile/new.pdf", up="2026-10-19T21:00")
    add(s, old)
    add(s, new, "replaced", old.url)
    gone = doc(BASE + "/extfile/g.pdf", no="2")
    add(s, gone)
    add(s, gone, "removed")
    add(s, doc(BASE + "/extfile/seven.pdf", dt=7, no="3"))
    out = tmp_path / "index.html"
    assert build_page(s, out, tmp_path / "pdfs") == 2
    by_no = {r["no"]: r for r in rows_of(out)}
    assert by_no["1"]["state"] == "replaced" and by_no["1"]["up"] == "2026-10-19T21:00"
    assert [v["rik"] for v in by_no["1"]["v"]] == [old.url]
    assert by_no["2"]["state"] == "removed"


def test_severity_order_and_rik_link_built_from_relative_url(tmp_path):
    s = store()
    d = doc("/extfile/a.pdf")
    add(s, d)
    for rule, sev in (("X1", "LOW"), ("X2", "HIGH"), ("X3", "MEDIUM")):
        s.add_finding(P, Finding(rule, sev, d, rule + " s"), d.url)
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    (r,) = rows_of(out)
    assert [f[1] for f in r["f"]] == ["HIGH", "MEDIUM", "LOW"] and r["sev"] == 3
    assert r["rik"] == BASE + "/extfile/a.pdf"


def test_non_http_url_gets_no_rik_link(tmp_path):
    s = store()
    add(s, doc("javascript:alert(1)"))
    add(s, doc("file:///tmp/x.pdf", no="2"))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    assert all(r["rik"] is None for r in rows_of(out))


def test_pdf_link_only_when_cached(tmp_path):
    s = store()
    d = doc(BASE + "/extfile/a.pdf")
    add(s, d)
    out = tmp_path / "web" / "index.html"
    cache = tmp_path / "pdfs"
    build_page(s, out, cache)
    assert rows_of(out)[0]["pdf"] is None
    cache.mkdir()
    (cache / (hashlib.sha256(d.url.encode()).hexdigest() + ".pdf")).write_bytes(b"%PDF")
    build_page(s, out, cache)
    assert rows_of(out)[0]["pdf"] == "../pdfs/" + hashlib.sha256(d.url.encode()).hexdigest() + ".pdf"


def test_script_close_tag_escaped(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf", muni="</script><b>x", label="<!-- y"))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    html = out.read_text(encoding="utf-8")
    assert html.count("</script>") == 2 and "<!-- y" not in html
    assert rows_of(out)[0]["muni"] == "</script><b>x"


def test_atomic_write_leaves_no_temp_and_keeps_old_on_failure(tmp_path, monkeypatch):
    s = store()
    add(s, doc(BASE + "/e/a.pdf"))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    before = out.read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir()] == ["index.html"]

    def boom(*a):
        raise OSError("nope")

    monkeypatch.setattr("rg2watch.browse.os.replace", boom)
    try:
        build_page(s, out, tmp_path / "pdfs")
    except OSError:
        pass
    assert out.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["index.html"]


def test_cli_default_path_is_next_to_db(tmp_path, capsys):
    db = tmp_path / "d" / "w.sqlite"
    db.parent.mkdir()
    Store(db).close()
    assert main(["browse", "--db", str(db)]) == 0
    assert (tmp_path / "d" / "index.html").exists()
    assert str(tmp_path / "d" / "index.html") in capsys.readouterr().out


def _watch(tmp_path, monkeypatch, interval, ticks):
    s = Store(tmp_path / "w.sqlite")
    s.upsert_election(Election(P, "660578", "3", "Test", "http://127.0.0.1:9"))
    s.close()
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"browse_interval_s = {interval}\n")
    built, sleeps = [], []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= ticks:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "build_page", lambda *a: built.append(a))
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli.Watcher, "run_once", lambda self: None)
    assert main(["watch", "--parent", P, "--db", str(tmp_path / "w.sqlite"),
                 "--config", str(cfg)]) == 0
    return built


def test_watch_builds_at_start_and_by_interval(tmp_path, monkeypatch):
    assert len(_watch(tmp_path, monkeypatch, 120, 3)) == 1
    clock = iter([0, 0, 50, 130, 131, 132])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock, 999))
    assert len(_watch(tmp_path, monkeypatch, 120, 3)) >= 2


def test_watch_browse_off_with_zero(tmp_path, monkeypatch):
    assert _watch(tmp_path, monkeypatch, 0, 3) == []


def test_watch_browse_failure_does_not_stop_loop(tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(cli, "build_page", boom)
    monkeypatch.setattr(cli.Watcher, "run_once", lambda self: None)
    s = Store(tmp_path / "w.sqlite")
    s.upsert_election(Election(P, "1", "3", "T", "http://127.0.0.1:9"))
    s.close()
    n = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: n.append(1) if len(n) < 2 else (_ for _ in ()).throw(KeyboardInterrupt))
    assert main(["watch", "--parent", P, "--db", str(tmp_path / "w.sqlite")]) == 0
    assert len(n) == 2
