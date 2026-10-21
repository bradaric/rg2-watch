import io
import json
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

import rg2watch.cli as cli
from rg2watch.cli import _UntilDone, _config, _parser, main
from rg2watch.config import Config
from rg2watch.models import Doc, Election, Event, Finding
from rg2watch.notify import Notifier
from rg2watch.replay import make_replay_server
from rg2watch.store import Store
from test_e2e_replay import make_fixture_dir
from test_watcher import doc, env

P = "662405"


def test_no_ntfy_is_watch_only():
    parser = _parser()
    assert parser.parse_args(["watch", "--no-ntfy"]).no_ntfy is True
    assert parser.parse_args(["watch"]).no_ntfy is False

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["add", "http://rik.test/zapisnici/1", "--no-ntfy"])
    assert exc.value.code == 2


@pytest.mark.parametrize("source", ["toml", "environment"])
@pytest.mark.parametrize("disabled", [False, True])
def test_no_ntfy_overrides_effective_topic(tmp_path, monkeypatch, source, disabled):
    config_path = tmp_path / "rg2watch.toml"
    if source == "toml":
        config_path.write_text('ntfy_topic = "configured-test-topic"\n')
    else:
        config_path.write_text("")
        monkeypatch.setenv("RG2W_NTFY_TOPIC", "environment-test-topic")

    argv = ["watch", "--config", str(config_path)]
    if disabled:
        argv.append("--no-ntfy")
    cfg = _config(_parser().parse_args(argv))

    assert (cfg.ntfy_topic is None) is disabled


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    """A replayed election plus a fast watch loop; returns (argv for watch, db path)."""
    root = make_fixture_dir(tmp_path)
    server = make_replay_server(root, port=0, speed=1e9)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    cfg, db = tmp_path / "rg2watch.toml", tmp_path / "w.sqlite"
    cfg.write_text("max_rps = 1000.0\ntick_request_budget = 5\nbrowse_interval_s = 0\nprobe_interval_s = 1\n")
    common = ["--db", str(db), "--config", str(cfg)]
    assert main(["add", f"{base}/zapisnici/{P}", *common]) == 0
    real_sleep = cli.time.sleep
    monkeypatch.setattr(cli.time, "sleep", lambda s: real_sleep(0.01))
    monkeypatch.setattr(cli, "DONE_CONFIRM_S", 0.05)
    yield ["watch", "--parent", P, "--base-url", base, "--serve-port", "0", *common], db
    server.shutdown()
    server.server_close()


def test_exits_after_full_passes_without_analysis(rehearsal, capsys):
    argv, db = rehearsal
    assert main([*argv, "--until-done", "--baseline"]) == 0
    out = capsys.readouterr().out
    assert "[until-done] finished" in out
    assert "documents seen: RG-2" in out and "RG-7-1 1" in out
    assert "spend for this election: $0.00" in out
    assert f"rg2watch serve --db {db}" in out
    assert "RG-2 analysed" not in out  # analysis off: no analysis line
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 0
    assert list(db.parent.rglob("status.json"))


def test_final_status_says_stopped_and_digest_flushed(rehearsal, monkeypatch):
    argv, db = rehearsal
    calls, status_calls = [], []
    real = cli._handlers
    real_write_status = cli.write_status

    def handlers(*a):
        on_events, deliver = real(*a)
        return on_events, lambda flush=False, **kw: (calls.append(flush), deliver(flush, **kw))[1]
    monkeypatch.setattr(cli, "_handlers", handlers)
    monkeypatch.setattr(cli, "write_status", lambda *a, **kw: (
        status_calls.append((a, kw)), real_write_status(*a, **kw))[1])
    assert main([*argv, "--until-done"]) == 0
    assert calls[-1] is True and not any(calls[:-1])
    assert len(status_calls) >= 2
    assert all(a[4] == db.parent / "pdfs" for a, _ in status_calls)
    status = next(db.parent.rglob("status.json")).read_text()
    assert '"watching": false' in status


def test_stuck_on_spend_cap_exits_1(rehearsal, monkeypatch, capsys):
    argv, db = rehearsal
    monkeypatch.setattr(cli, "_analysis_client", lambda cfg: (object(), None))
    monkeypatch.setattr(cli, "STUCK_AFTER_S", 2.5)
    cfg = db.parent / "rg2watch.toml"
    cfg.write_text(cfg.read_text() + "max_spend_usd = 0.0\nstop_wait_s = 2\n")
    assert main([*argv, "--until-done", "--analyze"]) == 1
    cap = capsys.readouterr()
    assert "[until-done] STOPPED" in cap.out and "RG-2 analysed 0" in cap.out
    assert "analysis paused: spend cap reached; " in cap.err
    assert "form(s) not analysed" in cap.err and "rg2watch requeue" in cap.err


def test_until_done_conflicts_with_once():
    with pytest.raises(SystemExit) as e:
        main(["watch", "--parent", P, "--once", "--until-done"])
    assert e.value.code == 2


def test_flag_absent_loop_unchanged(rehearsal, monkeypatch):
    argv, db = rehearsal
    ticks = []

    def stop_after_two(self):
        ticks.append(1)
        if len(ticks) >= 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli.Watcher, "run_once", stop_after_two)
    assert main(argv) == 0
    assert len(ticks) == 2


@pytest.mark.parametrize("topic_source", ["toml", "environment"])
def test_no_ntfy_cli_prints_and_consumes_pending_and_new_findings(
        rehearsal, monkeypatch, capsys, topic_source):
    argv, db = rehearsal
    cfg_path = db.parent / "rg2watch.toml"
    extra = 'ntfy_url = "http://ntfy.test"\n'
    if topic_source == "toml":
        extra += 'ntfy_topic = "configured-test-topic"\n'
    else:
        monkeypatch.setenv("RG2W_NTFY_TOPIC", "environment-test-topic")
    cfg_path.write_text(cfg_path.read_text() + extra)

    old_doc = Doc("Old municipality", "1", "Old station", "old", 6,
                  "http://rik.test/old.pdf", "2026-10-20T10:00")
    store = Store(db)
    store.apply_events(P, store.start_sweep(P, "test-old"), [Event("added", old_doc)])
    assert store.add_finding(P, Finding("A1", "HIGH", old_doc, "old pending finding"), old_doc.url)
    old_id = store.unalerted(P, {"HIGH"})[0][0]
    store.close()

    requests = []
    monkeypatch.setattr(cli, "_notifier_http", lambda: httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (requests.append(request), httpx.Response(200))[1])))

    real_background = cli._background
    monkeypatch.setattr(cli, "_background", lambda name, stop, period, fn, first=None: (
        SimpleNamespace(join=lambda timeout: None) if name == "deliver"
        else real_background(name, stop, period, fn, first)))

    generated = []

    def controlled_run_once(self):
        if not generated:
            new_doc = Doc("New municipality", "2", "New station", "new", 9,
                          "http://rik.test/new.pdf", "2026-10-20T10:01")
            self.store.apply_events(P, self.store.start_sweep(P, "test-new"),
                                    [Event("added", new_doc)])
            self.on_events([Event("added", new_doc)])
            self.on_status("watcher test status")
            pending = self.store.unalerted(P, {"HIGH", "MEDIUM"})
            generated.append((new_doc, [fid for fid, _ in pending]))
        self._first_pass.clear()
        self.client.last_ok = 1.0

    monkeypatch.setattr(cli.Watcher, "run_once", controlled_run_once)
    capsys.readouterr()
    assert main([*argv, "--until-done", "--no-ntfy"]) == 0
    out = capsys.readouterr().out

    new_doc, initially_pending = generated[0]
    assert old_id in initially_pending and len(initially_pending) >= 2
    assert "old pending finding" in out
    assert "RG-9 published" in out
    assert "watcher test status" in out
    assert requests == []
    store = Store(db)
    assert store.unalerted(P, {"HIGH", "MEDIUM"}) == []
    store.close()
    status = json.loads((db.parent / db.stem / "status.json").read_text())
    assert status["ntfy"]["enabled"] is False

    assert main([*argv, "--until-done"]) == 0
    assert requests == []

    later_doc = Doc("Later municipality", "3", "Later station", "later", 6,
                    "http://rik.test/later.pdf", "2026-10-20T10:02")
    store = Store(db)
    store.apply_events(P, store.start_sweep(P, "test-later"), [Event("added", later_doc)])
    assert store.add_finding(P, Finding("A2", "HIGH", later_doc, "later pending finding"),
                             later_doc.url)
    effective = _config(_parser().parse_args(argv))
    notifier = Notifier(effective, cli._notifier_http(), out=io.StringIO())
    _, deliver = cli._handlers(store, notifier, P)
    deliver(flush=True)
    assert len(requests) == 1
    assert store.unalerted(P, {"HIGH", "MEDIUM"}) == []
    store.close()


# --- _UntilDone with a controllable queue --------------------------------------------------

class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def queue_env(n=2):
    store = Store(":memory:")
    store.upsert_election(Election(P, "1", "3", "T", "http://x"))
    docs = [Doc("M", str(i), "S", "l", 6, f"http://x/{i}.pdf", "2026-10-19T20:00") for i in range(n)]
    store.apply_events(P, store.start_sweep(P, "t"), [Event("added", d) for d in docs])
    watcher = SimpleNamespace(first_pass_left=[], client=SimpleNamespace(last_ok=1.0))
    pipeline = SimpleNamespace(status_snapshot=lambda: {"paused": None})
    clock = Clock()
    return store, docs, watcher, pipeline, clock


def test_waits_for_queue_then_two_checks():
    store, docs, watcher, pipe, clock = queue_env()
    d = _UntilDone(watcher, pipe, store, P, None, clock=clock, stuck_after=600, confirm=3)
    assert d.step() is None and d.remaining == 2
    store.mark_analysis(docs[0].url, "done")
    clock.now += 5
    assert d.step() is None and d.remaining == 1
    store.mark_analysis(docs[1].url, "failed")  # permanent failure: no retry time
    clock.now += 1
    assert d.step() is None  # first check with everything finished
    clock.now += 3
    assert d.step() == 0


def test_race_resets_the_confirmation():
    store, docs, watcher, pipe, clock = queue_env(1)
    store.mark_analysis(docs[0].url, "done")
    d = _UntilDone(watcher, pipe, store, P, None, clock=clock, stuck_after=600, confirm=3)
    assert d.step() is None
    late = Doc("M", "9", "S", "l", 6, "http://x/9.pdf", "2026-10-19T20:00")
    store.apply_events(P, store.start_sweep(P, "t"), [Event("added", late)])  # new form arrives
    clock.now += 3
    assert d.step() is None and d.remaining == 1
    store.mark_analysis(late.url, "done")
    clock.now += 1
    assert d.step() is None
    clock.now += 3
    assert d.step() == 0


def test_retry_and_running_count_as_unfinished():
    store, docs, watcher, pipe, clock = queue_env()
    store.mark_analysis(docs[0].url, "failed", "boom", retry_at=1e12)
    store.next_for_analysis(1, parent=P)  # claims the other as running
    d = _UntilDone(watcher, pipe, store, P, None, clock=clock)
    assert d.step() is None and d.remaining == 2


def test_waits_for_watcher_first_pass():
    store, docs, watcher, pipe, clock = queue_env(0)
    watcher.first_pass_left = ["safety"]
    d = _UntilDone(watcher, pipe, store, P, None, clock=clock, confirm=0)
    assert d.step() is None
    watcher.first_pass_left = []
    assert d.step() == 0


def test_stuck_reports_pause_reason_and_progress_resets_timer():
    store, docs, watcher, pipe, clock = queue_env()
    pipe.status_snapshot = lambda: {"paused": ["spend cap reached", 1.0]}
    d = _UntilDone(watcher, pipe, store, P, None, clock=clock, stuck_after=600)
    assert d.step() is None
    clock.now += 599
    store.mark_analysis(docs[0].url, "done")  # progress at 599 s
    assert d.step() is None
    clock.now += 599
    assert d.step() is None
    clock.now += 2
    assert d.step() == 1 and d.remaining == 1 and "spend cap reached" in d.why


def test_idle_start_then_forms_arrive_is_not_stuck():
    store, docs, watcher, pipe, clock = queue_env(0)
    d = _UntilDone(watcher, pipe, store, P, None, clock=clock, stuck_after=600, confirm=10_000)
    watcher.first_pass_left = ["probe"]  # watcher still working, queue empty for > stuck_after
    for _ in range(3):
        assert d.step() is None
        clock.now += 300
        watcher.client.last_ok += 1
    late = Doc("M", "9", "S", "l", 6, "http://x/9.pdf", "2026-10-19T20:00")
    store.apply_events(P, store.start_sweep(P, "t"), [Event("added", late)])
    assert d.step() is None and d.remaining == 1
    clock.now += 100
    store.mark_analysis(late.url, "done")
    assert d.step() is None and d.remaining == 0


def test_stuck_queue_exits_1_even_after_idle_start():
    store, docs, watcher, pipe, clock = queue_env(0)
    d = _UntilDone(watcher, pipe, store, P, None, clock=clock, stuck_after=600, confirm=10_000)
    clock.now += 700
    assert d.step() is None
    late = Doc("M", "9", "S", "l", 6, "http://x/9.pdf", "2026-10-19T20:00")
    store.apply_events(P, store.start_sweep(P, "t"), [Event("added", late)])
    assert d.step() is None
    clock.now += 601
    assert d.step() == 1


def test_first_pass_never_completing_exits_1_and_names_lanes():
    store, docs, watcher, pipe, clock = queue_env(0)
    watcher.first_pass_left = ["probe", "safety"]
    d = _UntilDone(watcher, pipe, store, P, None, clock=clock, stuck_after=600)
    assert d.step() is None
    clock.now += 599
    watcher.first_pass_left = ["safety"]  # a lane finished: timer restarts
    assert d.step() is None
    clock.now += 599
    assert d.step() is None
    clock.now += 2
    assert d.step() == 1 and "first pass incomplete: lanes safety" in d.why


def test_until_done_analyze_without_client_exits_2(rehearsal, monkeypatch, capsys):
    argv, db = rehearsal
    monkeypatch.setattr(cli, "_analysis_client", lambda cfg: (None, "no key"))
    assert main([*argv, "--until-done", "--analyze"]) == 2
    assert "--until-done needs analysis: no key" in capsys.readouterr().err


def test_municipality_filter_ignores_other_municipalities():
    store, docs, watcher, pipe, clock = queue_env(1)
    other = Doc("Other", "1", "S", "l", 6, "http://x/o.pdf", "2026-10-19T20:00")
    store.apply_events(P, store.start_sweep(P, "t"), [Event("added", other)])
    store.mark_analysis(docs[0].url, "done")
    d = _UntilDone(watcher, pipe, store, P, {"M"}, clock=clock, confirm=0)
    assert d.step() == 0


# --- MEDIUM digest ---------------------------------------------------------------------------

def test_flush_pushes_medium_digest_immediately():
    from rg2watch.models import Finding
    posts = []
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: (posts.append(r.headers["Title"]), httpx.Response(200))[1]))
    n = Notifier(Config(ntfy_topic="t", ntfy_url="http://ntfy.test", medium_digest_s=600), http,
                 out=io.StringIO())
    f = Finding("A1", "MEDIUM", Doc("M", "1", "S", "l", 6, "http://x/1.pdf", "2026-10-19T20:00"), "s")
    assert n.send([(1, f)]) == []
    assert posts == []
    assert n.send([], flush=True) == [1]
    assert posts == ["[MEDIUM] 1 finding"]


def test_first_pass_done_without_baseline():
    w, fake, store, t, batches, _ = env()
    assert not w.first_pass_done
    for _ in range(40):
        w.run_once()
        t.now += 60
    assert w.first_pass_done
