import json
import sqlite3
import threading
import time
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

    real_sleep = time.sleep

    def fake_sleep(s):
        sleeps.append(s)
        real_sleep(0.3)  # the delivery thread runs on its own clock
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "DELIVER_EVERY_S", 0.05)
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


def _delivery_env(tmp_path, monkeypatch, delivered):
    import rg2watch.cli as cli
    from rg2watch.models import Election
    from rg2watch.store import Store

    db = tmp_path / "w.sqlite"
    s = Store(db)
    s.upsert_election(Election("662405", "660578", "3", "Test", "http://127.0.0.1:9"))
    s.close()
    real = cli._handlers

    def handlers(*a):
        on_events, _ = real(*a)
        return on_events, lambda flush=False, **kw: delivered.append(flush)

    monkeypatch.setattr(cli, "_handlers", handlers)
    monkeypatch.setattr(cli, "DELIVER_EVERY_S", 0.01)
    return ["watch", "--parent", "662405", "--db", str(db), "--serve-port", "0"]


def test_deliver_runs_when_run_once_raises(tmp_path, monkeypatch):
    import rg2watch.cli as cli
    from rg2watch.watcher import Watcher

    delivered, ticks = [], []
    argv = _delivery_env(tmp_path, monkeypatch, delivered)

    def boom(self):
        ticks.append(1)
        if len(ticks) >= 3:
            raise KeyboardInterrupt
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(Watcher, "run_once", boom)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert main(argv) == 0
    assert delivered


def test_blocked_tick_does_not_delay_delivery(tmp_path, monkeypatch):

    from rg2watch.watcher import Watcher

    delivered, seen = [], []
    argv = _delivery_env(tmp_path, monkeypatch, delivered)

    def blocked(self):  # a slow RIK tick: delivery must happen while it is still running
        deadline = time.monotonic() + 5
        while not delivered and time.monotonic() < deadline:
            time.sleep(0.01)
        seen.append(bool(delivered))
        raise KeyboardInterrupt

    monkeypatch.setattr(Watcher, "run_once", blocked)
    assert main(argv) == 0
    assert seen == [True]


def test_baseline_refused_on_database_with_documents(tmp_path, capsys):
    root = make_fixture_dir(tmp_path)
    server = make_replay_server(root, port=0, speed=1e9, faults=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        cfg = tmp_path / "rg2watch.toml"
        cfg.write_text("max_rps = 1000.0\ntick_request_budget = 5\n")
        common = ["--db", str(tmp_path / "w.sqlite"), "--config", str(cfg)]
        watch = ["watch", "--parent", "662405", "--once", "--base-url", base, *common]
        assert main(["add", f"{base}/zapisnici/662405", *common]) == 0
        assert main([*watch, "--baseline"]) == 0  # empty database: fine
        capsys.readouterr()
        assert main([*watch, "--baseline"]) == 2
        assert "refusing --baseline" in capsys.readouterr().err
        assert main([*watch, "--baseline", "--force-baseline"]) == 0
    finally:
        server.shutdown()
        server.server_close()


def _deliver_env(post):
    import io

    import httpx

    from rg2watch.cli import _handlers
    from rg2watch.config import Config
    from rg2watch.models import Finding
    from rg2watch.notify import Notifier
    from rg2watch.store import Store

    store = Store(":memory:")
    store.upsert_election(__import__("rg2watch.models", fromlist=["Election"]).Election(
        "p", "c", "3", "T", "http://x"))
    d = _doc(1)
    store.apply_events("p", store.start_sweep("p", "t"),
                       [__import__("rg2watch.models", fromlist=["Event"]).Event("added", d)])
    store.add_finding("p", Finding("D1", "HIGH", d, "new"), d.url)
    cfg = Config(ntfy_topic="t", ntfy_url="http://ntfy.test", ntfy_findings=True)
    http = httpx.Client(transport=httpx.MockTransport(post))
    notifier = Notifier(cfg, http, io.StringIO())
    _, deliver = _handlers(store, notifier, "p")
    return store, notifier, deliver


def test_final_deliver_waits_for_in_flight_delivery_no_double_push():
    import threading
    import time

    import httpx

    posts, inside, release = [], threading.Event(), threading.Event()

    def post(req):
        posts.append(req)
        inside.set()
        release.wait(5)
        return httpx.Response(200)

    store, _, deliver = _deliver_env(post)
    t = threading.Thread(target=deliver)
    t.start()
    assert inside.wait(5)
    threading.Timer(0.3, release.set).start()
    t0 = time.monotonic()
    deliver(flush=True, wait=5)  # blocks on the lock until the first run has marked
    assert time.monotonic() - t0 >= 0.2
    t.join()
    assert len(posts) == 1
    assert store.unalerted("p", {"HIGH"}) == []


def test_final_deliver_gives_up_after_bounded_wait(caplog):
    import threading

    import httpx

    inside, release = threading.Event(), threading.Event()

    def post(req):
        inside.set()
        release.wait(5)
        return httpx.Response(200)

    store, _, deliver = _deliver_env(post)
    t = threading.Thread(target=deliver)
    t.start()
    assert inside.wait(5)
    deliver(flush=True, wait=0.05)
    assert "still in flight" in caplog.text
    release.set()
    t.join()


def test_mark_alerted_failure_does_not_resend_then_marks_later():
    import httpx

    posts = []
    store, notifier, deliver = _deliver_env(lambda r: posts.append(r) or httpx.Response(200))
    real, fail = store.mark_alerted, [True]

    def flaky(ids):
        if fail[0]:
            raise RuntimeError("db busy")
        real(ids)

    store.mark_alerted = flaky
    deliver()
    assert len(posts) == 1 and len(store.unalerted("p", {"HIGH"})) == 1
    deliver()
    assert len(posts) == 1  # no re-send
    fail[0] = False
    deliver()
    assert len(posts) == 1 and store.unalerted("p", {"HIGH"}) == [] and not notifier.unmarked


def test_ctrl_c_runs_final_delivery(tmp_path, monkeypatch):
    import rg2watch.cli as cli
    from rg2watch.watcher import Watcher

    delivered = []
    argv = _delivery_env(tmp_path, monkeypatch, delivered)
    monkeypatch.setattr(cli, "DELIVER_EVERY_S", 1000)  # the loop thread only runs once
    seen = []

    def interrupt(self):
        seen.append(list(delivered))
        raise KeyboardInterrupt

    monkeypatch.setattr(Watcher, "run_once", interrupt)
    assert main(argv) == 0
    assert len(delivered) > len(seen[0])  # a delivery after the stop, with flush=False
    assert delivered[-1] is False
