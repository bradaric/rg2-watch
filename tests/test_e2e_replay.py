import json
import sqlite3
import threading
from pathlib import Path

from rg2watch.cli import main
from rg2watch.replay import make_replay_server

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://www.rik.parlament.gov.rs"


def make_fixture_dir(tmp_path: Path) -> Path:
    root = tmp_path / "fx" / "662405"
    root.mkdir(parents=True)
    recs = json.loads((FIXTURES / "get_election_662405_rg2_p1.json").read_text(encoding="utf-8"))["records"][:4]
    for i, rec in enumerate(recs):
        rec["_municipality_id"] = "101" if i < 2 else "102"
    (root / "election.json").write_text(json.dumps({
        "parent": "662405", "round": "660578", "election_type": "3", "title": "Test",
        "base_url": BASE, "page_html": ""}), encoding="utf-8")
    (root / "listing.json").write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    (root / "municipalities.json").write_text(json.dumps({"101": "A", "102": "B"}))
    (root / "stations.json").write_text(json.dumps({"101": {"10101": "1"}, "102": {"10201": "1"}}))
    return root


def test_e2e_replay_watch_once_alerts_on_faulted_rg9(tmp_path, capsys):
    root = make_fixture_dir(tmp_path)
    server = make_replay_server(root, port=0, speed=1e9, faults=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        db = tmp_path / "w.sqlite"
        cfg = tmp_path / "rg2watch.toml"
        cfg.write_text("max_rps = 1000.0\n")
        common = ["--db", str(db), "--config", str(cfg)]
        assert main(["add", f"{base}/zapisnici/662405", *common]) == 0
        assert main(["watch", "--parent", "662405", "--once", "--base-url", base, *common]) == 0
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert "[HIGH] D1" in out
    assert "RG-9 published" in out
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 0
    assert con.execute(
        "SELECT COUNT(*) FROM findings WHERE rule='D1' AND severity='HIGH' AND alerted_at IS NOT NULL"
    ).fetchone()[0] >= 1


def test_watch_unknown_parent_exits_2(tmp_path):
    assert main(["watch", "--parent", "nope", "--once", "--db", str(tmp_path / "x.sqlite")]) == 2


def _doc(n):
    from rg2watch.models import Doc
    return Doc("M", str(n), "School", "label", 9, f"http://x/{n}.pdf", "2025-06-08T23:00")


def test_on_events_survives_one_bad_finding(caplog):
    import io
    import logging

    from rg2watch.cli import _handlers
    from rg2watch.config import Config
    from rg2watch.models import Election, Event
    from rg2watch.notify import Notifier
    from rg2watch.store import Store

    store = Store(":memory:")
    docs = [_doc(1), _doc(2), _doc(3)]
    evs = [Event("added", d) for d in docs]
    store.apply_events("p", store.start_sweep("p", "t"), [evs[0], evs[2]])  # doc 2 unknown to store
    out = io.StringIO()
    on_events, deliver = _handlers(store, Notifier(Config(), None, out=out), "p")
    with caplog.at_level(logging.ERROR, logger="rg2watch.cli"):
        on_events(evs)
    assert out.getvalue() == ""  # on_events only stores; the watch loop delivers once per tick
    deliver()
    assert out.getvalue().count("[HIGH] D1") == 2
    assert "http://x/2.pdf" in caplog.text and "D1" in caplog.text


def test_watch_loop_survives_tick_failure(tmp_path, monkeypatch, capsys):
    import rg2watch.cli as cli
    from rg2watch.store import Store

    root = make_fixture_dir(tmp_path)
    server = make_replay_server(root, port=0, speed=1e9, faults=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real, calls, sleeps = Store.unalerted, [], []

    def flaky(self, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real(self, *a, **k)

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(Store, "unalerted", flaky)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        cfg = tmp_path / "c.toml"
        cfg.write_text("max_rps = 1000.0\n")
        common = ["--db", str(tmp_path / "w.sqlite"), "--config", str(cfg)]
        assert main(["add", f"{base}/zapisnici/662405", *common]) == 0
        assert main(["watch", "--parent", "662405", "--base-url", base, *common]) == 0
    finally:
        server.shutdown()
        server.server_close()
    assert "[HIGH] D1" in capsys.readouterr().out
    assert len(sleeps) == 2


def test_add_unreachable_exits_1(tmp_path, capsys, monkeypatch):
    from rg2watch.rik_client import AdaptiveLimiter
    init = AdaptiveLimiter.__init__
    monkeypatch.setattr(AdaptiveLimiter, "__init__",
                        lambda self, rps: init(self, rps, sleep=lambda s: None))
    rc = main(["add", "http://127.0.0.1:9/zapisnici/1", "--db", str(tmp_path / "a.sqlite")])
    assert rc == 1
    assert "cannot reach" in capsys.readouterr().err


def test_notifier_http_timeouts():
    from rg2watch.cli import _notifier_http
    t = _notifier_http().timeout
    assert (t.connect, t.read, t.write, t.pool) == (3.0, 10.0, 10.0, 10.0)


def test_watch_sigterm_flushes_stdout_and_exits_0(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time

    root = make_fixture_dir(tmp_path)
    server = make_replay_server(root, port=0, speed=1e9, faults=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out_path = tmp_path / "out.txt"
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        cfg = tmp_path / "c.toml"
        cfg.write_text("max_rps = 1000.0\n")
        common = ["--db", str(tmp_path / "w.sqlite"), "--config", str(cfg)]
        assert main(["add", f"{base}/zapisnici/662405", *common]) == 0
        env = {k: v for k, v in os.environ.items() if k != "RG2W_NTFY_TOPIC"}
        with open(out_path, "w") as out:
            proc = subprocess.Popen(
                [sys.executable, "-m", "rg2watch.cli", "watch", "--parent", "662405", "--base-url", base,
                 *common], stdout=out, stderr=subprocess.DEVNULL, env=env)
        deadline = time.monotonic() + 30
        while "[HIGH] D1" not in out_path.read_text() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=30)
    finally:
        server.shutdown()
        server.server_close()
    assert rc == 0
    assert "RG-9 published" in out_path.read_text()


def test_replay_sigterm_exits_0(tmp_path):
    import signal
    import subprocess
    import sys

    root = make_fixture_dir(tmp_path)
    proc = subprocess.Popen([sys.executable, "-m", "rg2watch.cli", "replay", "--fixtures", str(root),
                             "--port", "0"], stderr=subprocess.PIPE, text=True)
    assert "replay server on" in proc.stderr.readline()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 0
    proc.stderr.close()


def test_watch_once_baseline_records_silently(tmp_path, capsys):
    root = make_fixture_dir(tmp_path)
    server = make_replay_server(root, port=0, speed=1e9, faults=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        db = tmp_path / "w.sqlite"
        cfg = tmp_path / "rg2watch.toml"
        cfg.write_text("max_rps = 1000.0\ntick_request_budget = 5\n")
        common = ["--db", str(db), "--config", str(cfg)]
        assert main(["add", f"{base}/zapisnici/662405", *common]) == 0
        assert main(["watch", "--parent", "662405", "--once", "--baseline", "--base-url", base,
                     *common]) == 0
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert "[HIGH]" not in out
    assert "[baseline] RG-9: 1 document(s) recorded" in out
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM documents WHERE doc_type=9").fetchone()[0] == 1


def test_deliver_runs_when_run_once_raises(tmp_path, monkeypatch):
    import rg2watch.cli as cli
    from rg2watch.models import Election
    from rg2watch.store import Store
    from rg2watch.watcher import Watcher

    db = tmp_path / "w.sqlite"
    s = Store(db)
    s.upsert_election(Election("662405", "660578", "3", "Test", "http://127.0.0.1:9"))
    s.close()
    delivered, sleeps = [], []
    real = cli._handlers

    def handlers(*a):
        on_events, _ = real(*a)
        return on_events, lambda: delivered.append(1)

    def boom(self):
        raise RuntimeError("tick blew up")

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_handlers", handlers)
    monkeypatch.setattr(Watcher, "run_once", boom)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    assert main(["watch", "--parent", "662405", "--db", str(db)]) == 0
    assert len(delivered) == 2
