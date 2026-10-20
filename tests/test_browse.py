import copy
import hashlib
import json
import os
import threading
import time

import rg2watch.cli as cli
from rg2watch.analyze import AnalysisResult
from rg2watch.browse import STATIC, STATIC_FILES, build_page
from rg2watch.cli import main
from rg2watch.models import Doc, Election, Event, Finding
from rg2watch.store import Store
from test_rules import clean_report, nf

real_sleep = time.sleep
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
    return json.loads((path.parent / "data.json").read_text(encoding="utf-8"))["rows"]


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
    assert rows_of(out)[0]["pdf"] == hashlib.sha256(d.url.encode()).hexdigest()


def test_json_is_ascii_even_with_lone_surrogate():
    from rg2watch.browse import _json
    blob = _json({"x": "a\ud800b\u2028</script>"})
    assert blob.isascii() and "</script>" not in blob and json.loads(blob)["x"].startswith("a")


def test_script_close_tag_escaped_and_ascii_only(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf", muni="</script><b>x", label="<!-- y"))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    for name in ("data.js", "data.json"):
        raw = (tmp_path / name).read_bytes()
        assert raw.isascii() and b"</script>" not in raw and b"<!--" not in raw
    assert rows_of(out)[0]["muni"] == "</script><b>x"


def test_atomic_write_leaves_no_temp_and_keeps_old_on_failure(tmp_path, monkeypatch):
    s = store()
    add(s, doc(BASE + "/e/a.pdf"))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    before = (tmp_path / "data.json").read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))

    def boom(*a):
        raise OSError("nope")

    monkeypatch.setattr("rg2watch.browse.os.replace", boom)
    add(s, doc(BASE + "/e/b.pdf", no="2"))
    try:
        build_page(s, out, tmp_path / "pdfs")
    except OSError:
        pass
    assert (tmp_path / "data.json").read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.tmp"))


def test_stale_tmp_files_are_removed_fresh_ones_kept(tmp_path):
    s = store()
    old, fresh = tmp_path / "x.tmp", tmp_path / "y.tmp"
    old.write_text("x")
    fresh.write_text("y")
    t = time.time() - 3600
    os.utime(old, (t, t))
    build_page(s, tmp_path / "index.html", tmp_path / "pdfs")
    assert not old.exists() and fresh.exists()


def test_static_files_ship_and_are_copied_only_when_changed(tmp_path):
    for name in STATIC_FILES:
        assert (STATIC / name).is_file()
    versions = (STATIC / "VERSIONS.txt").read_text()
    for name in STATIC_FILES[1:]:
        assert name in versions and hashlib.sha256((STATIC / name).read_bytes()).hexdigest() in versions
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "innerHTML" not in html.replace("never innerHTML", "") and "http://" not in html
    s = store()
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    jq = tmp_path / "jquery.min.js"
    assert jq.read_bytes() == (STATIC / "jquery.min.js").read_bytes()
    mtime = jq.stat().st_mtime_ns
    build_page(s, out, tmp_path / "pdfs")
    assert jq.stat().st_mtime_ns == mtime
    jq.write_text("tampered")
    build_page(s, out, tmp_path / "pdfs")
    assert jq.read_bytes() == (STATIC / "jquery.min.js").read_bytes()


def test_null_station_key_and_municipality(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf", muni=None, no="1"))
    add(s, doc(BASE + "/e/b.pdf", muni=None, no="2"))
    s._db.execute("UPDATE documents SET station_key=NULL")
    s._db.commit()
    out = tmp_path / "index.html"
    assert build_page(s, out, tmp_path / "pdfs") == 2  # not merged into one row
    assert {r["muni"] for r in rows_of(out)} == {"(unknown)"}
    assert len({r["id"] for r in rows_of(out)}) == 2


def test_seen_and_uploaded_are_both_serbian_local_time(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", up="2026-10-19T20:15")
    add(s, d)
    s._db.execute("UPDATE documents SET first_seen_at='2026-10-19T18:30:00Z'")  # UTC, CEST = +2
    s._db.commit()
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    (r,) = rows_of(out)
    assert r["up"] == "2026-10-19T20:15" and r["seen"] == "2026-10-19T20:30"


def test_retrying_is_distinct_from_failed(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf", no="1"))
    add(s, doc(BASE + "/e/b.pdf", no="2"))
    s.mark_analysis(BASE + "/e/a.pdf", "failed", "x", retry_at=time.time() + 60)
    s.mark_analysis(BASE + "/e/b.pdf", "failed", "x")
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    assert {r["no"]: r["ast"] for r in rows_of(out)} == {"1": "retrying", "2": "failed"}


def test_older_versions_keep_their_findings(tmp_path):
    s = store()
    old, new = doc(BASE + "/e/o.pdf"), doc(BASE + "/e/n.pdf", up="2026-10-19T21:00")
    add(s, old)
    add(s, new, "replaced", old.url)
    s.add_finding(P, Finding("X9", "HIGH", old, "old problem"), old.url)
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    (r,) = rows_of(out)
    assert r["f"] == [] and r["v"][0]["f"] == [["X9", "HIGH", "old problem"]]


def _report(**numbers):
    r = copy.deepcopy(clean_report())
    r["numbers"].update(numbers)
    return r


def _analysis(s, d, model, report):
    s.save_analysis(d.url, AnalysisResult(report, 0, 0, 0.0, model, "stop"), "v6", parent="adhoc")


def test_second_opinions_merged_per_model_with_disagreement(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", no="12")
    add(s, d)
    bad = _report(ballots_in_box=nf(900))  # more ballots than voters
    _analysis(s, d, "opus", bad)
    _analysis(s, d, "sonnet", clean_report())
    _analysis(s, d, "opus", bad)  # same model twice: newest only
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    (r,) = rows_of(out)
    by = {o["m"]: o for o in r["ops"]}
    assert set(by) == {"opus", "sonnet"} and by["sonnet"]["f"] == []
    assert any(f[1] == "HIGH" for f in by["opus"]["f"])
    assert r["dis"] and r["dis"] == sorted({f[0] for f in by["opus"]["f"] if f[1] == "HIGH"})


def test_no_disagreement_when_models_agree_or_single(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", no="12")
    add(s, d)
    _analysis(s, d, "opus", _report(ballots_in_box=nf(900)))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    assert rows_of(out)[0]["dis"] == []
    _analysis(s, d, "sonnet", _report(ballots_in_box=nf(900)))
    build_page(s, out, tmp_path / "pdfs")
    assert rows_of(out)[0]["dis"] == []


def test_unevaluable_stored_report_is_skipped(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", no="12")
    add(s, d)
    _analysis(s, d, "weird", {"nonsense": 1})
    _analysis(s, d, "ok", clean_report())
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    assert [o["m"] for o in rows_of(out)[0]["ops"]] == ["ok"]


def test_status_for_browse_says_watcher_not_running_with_db_counts(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf"))
    add(s, doc(BASE + "/e/r.pdf", dt=9, no="3"))
    s.add_finding(P, Finding("X9", "HIGH", doc(BASE + "/e/a.pdf"), "boom"), BASE + "/e/a.pdf")
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["watching"] is False and st["watcher"] is None and abs(st["ts"] - time.time()) < 60
    assert st["docs"]["rg2_seen"] == 1 and st["docs"]["new10"] == 1 and st["docs"]["rare"] == {"RG-9": 1}
    assert st["analysis"]["on"] is False and st["analysis"]["queued"] == 1
    assert st["alerts"][0][1:3] == ["HIGH", "X9"]
    assert (tmp_path / "status.js").read_text().startswith("window.RG2_STATUS = {")


def test_page_marks_stale_status():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "STALE_S = 60" in html and "watcher not running / stalled since" in html


def test_status_with_live_snapshots(tmp_path):
    from rg2watch.status import build_status
    s = store()
    add(s, doc(BASE + "/e/a.pdf"))
    live = {"watcher": {"health": "ok"}, "ntfy": {"enabled": False},
            "analysis": {"model": "m", "paused": ["cap", 1.0], "avg_s": 30.0, "workers": 2, "cap": 20.0}}
    st = build_status(s, P, live)
    assert st["watching"] and st["watcher"] == {"health": "ok"}
    a = st["analysis"]
    assert a["on"] and a["model"] == "m" and a["paused"] == ["cap", 1.0] and a["cap"] == 20.0
    assert a["queued"] == 1 and a["eta_s"] == 15


def test_cli_default_path_is_next_to_db(tmp_path, capsys):
    db = tmp_path / "d" / "w.sqlite"
    db.parent.mkdir()
    Store(db).close()
    assert main(["browse", "--db", str(db)]) == 0
    assert (tmp_path / "d" / "index.html").exists()
    assert str(tmp_path / "d" / "index.html") in capsys.readouterr().out


def _watch(tmp_path, monkeypatch, interval, ticks, extra=(), run_once=lambda self: None, build=None):
    s = Store(tmp_path / "w.sqlite")
    s.upsert_election(Election(P, "660578", "3", "Test", "http://127.0.0.1:9"))
    s.close()
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"browse_interval_s = {interval}\n")
    built, sleeps = [], []

    def fake_sleep(sec):
        real_sleep(0.05)  # lets the background threads start
        sleeps.append(sec)
        if len(sleeps) >= ticks:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "build_page", build or (lambda *a: built.append(a)))
    monkeypatch.setattr(cli, "write_status", lambda *a: built.append(("status",)))
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli.Watcher, "run_once", run_once)
    assert main(["watch", "--parent", P, "--db", str(tmp_path / "w.sqlite"),
                 "--config", str(cfg), "--serve-port", "0", *extra]) == 0
    real_sleep(0.2)  # the daemon threads' first runs
    return [b for b in built if b != ("status",)], built


def test_watch_builds_in_background_at_start_and_status_is_written(tmp_path, monkeypatch):
    pages, everything = _watch(tmp_path, monkeypatch, 120, 3)
    assert len(pages) == 1 and ("status",) in everything
    assert pages[0][3] is None and pages[0][-1] == P  # all elections' rows, status of this watch


def test_watch_browse_off_with_zero(tmp_path, monkeypatch):
    pages, everything = _watch(tmp_path, monkeypatch, 0, 3)
    assert pages == [] and ("status",) in everything


def test_watch_once_builds_and_serves_nothing(tmp_path, monkeypatch):
    s = Store(tmp_path / "w.sqlite")
    s.upsert_election(Election(P, "1", "3", "T", "http://127.0.0.1:9"))
    s.close()
    calls = []
    for name in ("build_page", "write_status", "copy_static", "start_server"):
        monkeypatch.setattr(cli, name, lambda *a, n=name, **k: calls.append(n))
    monkeypatch.setattr(cli.Watcher, "run_once", lambda self: None)
    assert main(["watch", "--once", "--parent", P, "--db", str(tmp_path / "w.sqlite")]) == 0
    time.sleep(0.1)
    assert calls == []


def test_slow_build_does_not_delay_run_once(tmp_path, monkeypatch):
    release, ticks = threading.Event(), []
    t0 = time.monotonic()
    try:
        _watch(tmp_path, monkeypatch, 120, 5, run_once=lambda self: ticks.append(time.monotonic()),
               build=lambda *a: release.wait(10))
        assert len(ticks) == 5 and time.monotonic() - t0 < 5  # build still blocked
    finally:
        release.set()


def test_watch_browse_failure_does_not_stop_loop(tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(cli, "build_page", boom)
    monkeypatch.setattr(cli, "write_status", boom)
    monkeypatch.setattr(cli.Watcher, "run_once", lambda self: None)
    s = Store(tmp_path / "w.sqlite")
    s.upsert_election(Election(P, "1", "3", "T", "http://127.0.0.1:9"))
    s.close()
    n = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: n.append(1) if len(n) < 2 else (_ for _ in ()).throw(KeyboardInterrupt))
    assert main(["watch", "--parent", P, "--db", str(tmp_path / "w.sqlite"), "--serve-port", "0"]) == 0
    assert len(n) == 2


def test_serve_port_config_and_cli(tmp_path, monkeypatch):
    import socket

    import pytest

    from rg2watch.config import Config, load_config
    assert Config().serve_port == 8770
    bad = tmp_path / "bad.toml"
    bad.write_text("serve_port = 70000\n")
    with pytest.raises(ValueError):
        load_config(bad)
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        db = tmp_path / "w.sqlite"
        Store(db).close()
        assert main(["serve", "--db", str(db), "--port", str(busy.getsockname()[1])]) == 1
        assert (tmp_path / "index.html").exists()
        # watch keeps going when its port is taken
        monkeypatch.setattr(cli.Watcher, "run_once", lambda self: None)
        s = Store(db)
        s.upsert_election(Election(P, "1", "3", "T", "http://127.0.0.1:9"))
        s.close()
        monkeypatch.setattr(cli.time, "sleep", lambda s_: (_ for _ in ()).throw(KeyboardInterrupt))
        assert main(["watch", "--parent", P, "--db", str(db),
                     "--serve-port", str(busy.getsockname()[1])]) == 0
    finally:
        busy.close()


def test_status_without_registered_election_uses_documents_parent(tmp_path):
    s = Store(":memory:")  # an evaluation-style DB: documents but no elections row
    s.apply_events("eval", s.start_sweep("eval", "t"), [Event("added", doc(BASE + "/e/a.pdf"))])
    build_page(s, tmp_path / "index.html", tmp_path / "pdfs")
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["parent"] == "eval" and st["docs"]["rg2_seen"] == 1
