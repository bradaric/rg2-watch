import contextlib
import io
import json
import threading
import time
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from PIL import Image

from rg2watch import render
from rg2watch.config import Config
from rg2watch.fake_rik import FakeRik
from rg2watch.models import Doc, Election, Event
from rg2watch.notify import Notifier
from rg2watch.pipeline import PROMPT_VERSION, AnalysisPipeline
from rg2watch.rik_client import AdaptiveLimiter, RikClient
from rg2watch.store import Store
from test_rules import clean_report, nf

BASE = "https://www.rik.parlament.gov.rs"
P = "662405"
ELECTION = Election(P, "660578", "3", "Test", BASE)
CAP_TITLE = "[rg2-watch] spend cap reached"


@pytest.fixture(autouse=True)
def no_osd(monkeypatch):
    monkeypatch.setattr(render, "_osd_rotation", lambda img, timeout=10: 0)


def make_pdf(path):
    Image.new("RGB", (1653, 2338), (255, 255, 255)).save(path, "PDF", resolution=200)
    return path


def doc(name="a.pdf", dt=6, no="1"):
    return Doc("Alpha", no, "School", "lbl", dt, f"{BASE}/extfile/{name}", "2026-10-19 20:00:00")


def resp(report, stop="end_turn", tin=1000, tout=500):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(report))],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
        stop_reason=stop, model="claude-opus-5-5")


class FakeAnthropic:
    def __init__(self, reply):
        self.reply = reply  # a response, an exception, or a callable
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    @contextlib.contextmanager
    def _stream(self, **kw):
        self.calls += 1
        r = self.reply
        if callable(r):
            r = r()
        if isinstance(r, BaseException):
            raise r
        yield SimpleNamespace(get_final_message=lambda: r)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class Env:
    def __init__(self, tmp_path, reply, pdf_body=None, **cfg_kw):
        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        make_pdf(pdf_dir / "a.pdf")
        make_pdf(pdf_dir / "b.pdf")
        (pdf_dir / "html.pdf").write_bytes(b"<!doctype html><html><body>404 not found</body></html>")
        (pdf_dir / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")
        self.fake = FakeRik(ELECTION, [], {}, {}, pdf_dir=pdf_dir)
        limiter = AdaptiveLimiter(100.0, sleep=lambda s: None)
        self.rik = RikClient(ELECTION, limiter, httpx.Client(transport=httpx.MockTransport(self.fake.handle)))
        self.store = Store(":memory:")
        self.store.upsert_election(ELECTION)
        self.store.enable_analysis(P)  # everything published in a test is seen after enabling
        self.out = io.StringIO()
        self.cfg = Config(**cfg_kw)
        self.notifier = Notifier(self.cfg, None, out=self.out)
        self.ai = FakeAnthropic(reply)
        self.clock = Clock()
        self.findings = []
        self.pipe = AnalysisPipeline(self.rik, self.store, self.notifier, self.ai, self.cfg,
                                     self.on_findings, tmp_path / "cache", clock=self.clock)

    def on_findings(self, found):
        self.findings.extend(found)
        for f in found:
            self.store.add_finding(P, f, f.doc.url)

    def publish(self, *docs, kind="added"):
        events = [Event(kind, d) for d in docs]
        self.store.apply_events(P, self.store.start_sweep(P, "t"), events)
        self.pipe.submit(events)
        return events

    def drain(self, limit=20):
        n = 0
        while self.pipe.process_next() and n < limit:
            n += 1
        return n

    def status(self, url):
        r = self.store._db.execute(
            "SELECT analysis_status, analysis_attempts FROM documents WHERE url=?", (url,)).fetchone()
        return r[0], r[1]

    def rules(self):
        return sorted(f.rule for f in self.findings)

    def pdf_requests(self):
        return [r for r in self.fake.request_log if r["path"].startswith("/extfile/")]


def overwritten_report():
    r = clean_report()
    r["numbers"]["voted"] = nf(800, mark="overwritten")
    r["header"]["station_no"] = "1"
    return r


def test_added_rg2_analysed_and_findings_stored(tmp_path):
    e = Env(tmp_path, resp(overwritten_report()))
    d = doc()
    e.publish(d)
    assert e.drain() == 1
    assert e.rules() == ["V1"]
    stored = e.store.unalerted(P, {"HIGH", "MEDIUM", "LOW"})
    assert [(f.rule, f.severity, f.doc.url) for _, f in stored] == [("V1", "HIGH", d.url)]
    assert e.status(d.url) == ("done", 1)
    row = e.store._db.execute("SELECT prompt_version, status, result_json, model FROM analyses").fetchone()
    assert row[0] == PROMPT_VERSION == "v2" and row[1] == "done" and row[3] == "claude-opus-5-5"
    assert json.loads(row[2])["numbers"]["voted"]["mark"] == "overwritten"
    assert e.store.total_spend() == pytest.approx((1000 * 4 + 500 * 20) / 1e6)
    assert e.out.getvalue() == ""  # workers never alert directly


def test_duplicate_submit_analysed_once(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    d = doc()
    evs = e.publish(d)
    e.pipe.submit(evs)  # e.g. safety sweep re-reports it
    e.store.apply_events(P, e.store.start_sweep(P, "t"), evs)  # re-seen: ON CONFLICT keeps status
    e.pipe.submit(evs)
    assert e.drain() == 1
    e.pipe.submit(evs)
    assert e.drain() == 0
    assert e.ai.calls == 1 and len(e.pdf_requests()) == 1


def test_replacement_is_analysed_as_new_url(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    old, new = doc("a.pdf"), doc("b.pdf")
    e.publish(old)
    e.drain()
    e.store.apply_events(P, e.store.start_sweep(P, "t"), [Event("replaced", new, previous_url=old.url)])
    e.pipe.submit([Event("replaced", new, previous_url=old.url)])
    assert e.drain() == 1
    assert e.ai.calls == 2


def test_spend_cap_pauses_and_alerts_once(tmp_path):
    e = Env(tmp_path, resp(clean_report(), tin=1_000_000, tout=0), max_spend_usd=3.0)
    e.publish(doc("a.pdf", no="1"))
    assert e.drain() == 1  # spends $4 >= cap
    e.publish(doc("b.pdf", no="2"))
    for _ in range(3):
        assert e.pipe.process_next() is False
    assert e.ai.calls == 1
    assert e.status(doc("b.pdf").url) == ("new", 0)  # left queued, not claimed
    assert e.out.getvalue().count(CAP_TITLE) == 1


def test_bad_output_retried_then_failed_after_3(tmp_path):
    bad = resp(clean_report())
    bad.content[0].text = "<html>"  # not JSON: an attempt is used each time
    e = Env(tmp_path, bad)
    d = doc()
    e.publish(d)
    assert e.drain() == 1
    assert e.status(d.url) == ("failed", 1)
    assert e.drain() == 0  # not due yet (60 s backoff)
    e.clock.now += 60
    assert e.drain() == 1
    assert e.status(d.url) == ("failed", 2)
    e.clock.now += 119
    assert e.drain() == 0  # second backoff is 120 s
    e.clock.now += 1
    assert e.drain() == 1
    assert e.status(d.url) == ("failed", 3)
    assert e.rules() == ["X2"]
    assert "after 3 attempts" in e.findings[0].summary and e.findings[0].severity == "LOW"
    e.clock.now += 10_000
    assert e.drain() == 0
    assert e.ai.calls == 3
    assert len(e.pdf_requests()) == 1  # PDF cached across retries


def test_bad_pdf_gives_d3(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    html, broken = doc("html.pdf", no="1"), doc("broken.pdf", no="2")
    e.publish(html, broken)
    assert e.drain() == 2
    assert e.rules() == ["D3", "D3"]
    assert all(f.severity == "LOW" for f in e.findings)
    assert e.status(html.url) == ("failed", 1) and e.status(broken.url) == ("failed", 1)
    e.clock.now += 10_000
    assert e.drain() == 0  # permanent: never retried
    assert e.ai.calls == 0


def test_non_rg2_not_analysed(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    e.publish(doc("a.pdf", dt=9), doc("b.pdf", dt=7, no="2"))
    assert e.drain() == 0
    assert e.ai.calls == 0 and e.pdf_requests() == []
    assert e.status(doc("a.pdf").url) == ("skipped", 0)


def test_repeat_vote_rg2_types_analysed(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    e.publish(doc("a.pdf", dt=10), doc("b.pdf", dt=15, no="2"))
    assert e.drain() == 2


def test_removed_event_not_analysed(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    d = doc()
    e.store.apply_events(P, e.store.start_sweep(P, "t"), [Event("added", d), Event("removed", d)])
    e.pipe.submit([Event("removed", d)])
    assert e.drain() == 0


def test_refusal_gives_x1_and_records_cost(tmp_path):
    e = Env(tmp_path, resp(clean_report(), stop="refusal"))
    d = doc()
    e.publish(d)
    assert e.drain() == 1
    assert e.rules() == ["X1"] and e.findings[0].severity == "LOW"
    assert e.status(d.url) == ("failed", 1)
    row = e.store._db.execute("SELECT status, cost_usd, result_json FROM analyses").fetchone()
    assert row[0] == "failed" and row[1] > 0 and row[2] is None
    assert e.store.total_spend() == pytest.approx(row[1])
    e.clock.now += 10_000
    assert e.drain() == 0


def test_illegible_report_only_v8_v10(tmp_path):
    r = clean_report()
    for k in ("registered", "first_voter_number", "voted", "ballots_in_box", "invalid", "valid",
              "assisted", "outside_voted", "outside_confirmations"):
        r["numbers"][k] = nf(None, legible=False, mark="empty")
    r["numbers"]["list_votes"] = [{"row": i, "field": nf(None, legible=False, mark="empty")}
                                  for i in (1, 2)]
    r["yesno"] = {k: {"answer": "unclear"} for k in r["yesno"]}
    r["times"] = {"open_time": None, "close_time": None, "completed_at": None}
    r["header"].update(station_no=None, sections_present=[], pages_match_same_form=False)
    r["signatures"].update(president_signed=False, signatures_look_same_hand=True)
    r["visual"].update(page_quality="unreadable", pasted_or_patched_area=True)
    r["confidence"] = "low"
    e = Env(tmp_path, resp(r))
    e.publish(doc())
    e.drain()
    assert e.rules() == ["V10", "V8"]


def test_unexpected_exception_retried_worker_survives(tmp_path, monkeypatch):
    e = Env(tmp_path, resp(clean_report()))
    from rg2watch import pipeline
    real, calls = pipeline.render_tiles, []

    def flaky(path):
        calls.append(1)
        if len(calls) == 1:
            raise MemoryError("boom")
        return real(path)

    monkeypatch.setattr(pipeline, "render_tiles", flaky)
    d = doc()
    e.publish(d)
    assert e.drain() == 1
    assert e.status(d.url) == ("failed", 1)
    e.clock.now += 60
    assert e.drain() == 1
    assert e.status(d.url) == ("done", 2)


def test_restart_requeues_running(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    d = doc()
    e.publish(d)
    assert [x.url for x in e.store.next_for_analysis(5)] == [d.url]  # claimed, then "crash"
    assert e.status(d.url) == ("running", 1)
    assert e.drain() == 0
    pipe2 = AnalysisPipeline(e.rik, e.store, e.notifier, e.ai, e.cfg, e.on_findings,
                             tmp_path / "cache", clock=e.clock)
    pipe2.start()
    pipe2.stop()
    assert e.status(d.url)[0] in ("new", "done")
    assert pipe2.process_next() is False  # stopped
    e.drain()
    assert e.status(d.url) == ("done", 2)


def test_other_election_docs_ignored(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    other = doc("a.pdf")
    e.store.apply_events("999", e.store.start_sweep("999", "t"), [Event("added", other)])
    assert e.drain() == 0


def test_threads_process_and_stop(tmp_path):
    e = Env(tmp_path, resp(clean_report()), analysis_workers=2)
    e.pipe.start()
    try:
        e.publish(doc("a.pdf", no="1"), doc("b.pdf", no="2"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and e.ai.calls < 2:
            time.sleep(0.01)
        while time.monotonic() < deadline and e.status(doc("b.pdf").url)[0] != "done":
            time.sleep(0.01)
    finally:
        e.pipe.stop()
    assert e.ai.calls == 2
    assert not any(t.is_alive() for t in e.pipe._threads)
    assert e.status(doc("a.pdf").url)[0] == "done" and e.status(doc("b.pdf").url)[0] == "done"


def test_notifier_status_is_thread_safe():
    out = io.StringIO()
    n = Notifier(Config(), None, out=out)
    threads = [threading.Thread(target=lambda: [n.status("x") for _ in range(200)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out.getvalue() == "x\n\n" * 800


# CLI ---------------------------------------------------------------------------

def test_anthropic_client_detects_missing_credentials(monkeypatch):
    from rg2watch import cli

    monkeypatch.undo()  # drop the conftest stub to test the real factory

    class NoCreds:
        api_key = auth_token = credentials = None

    class WithKey(NoCreds):
        api_key = "k"

    def boom():
        raise RuntimeError("bad profile")

    monkeypatch.setattr(anthropic, "Anthropic", NoCreds)
    client, why = cli._anthropic_client()
    assert client is None and "ANTHROPIC_API_KEY" in why
    monkeypatch.setattr(anthropic, "Anthropic", boom)
    client, why = cli._anthropic_client()
    assert client is None and "bad profile" in why
    monkeypatch.setattr(anthropic, "Anthropic", WithKey)
    client, why = cli._anthropic_client()
    assert isinstance(client, WithKey) and why is None


def test_handlers_submit_to_pipeline_guarded(caplog):
    from rg2watch.cli import _handlers

    class Boom:
        def submit(self, events):
            raise RuntimeError("queue broke")

    store = Store(":memory:")
    d = doc(dt=9)
    evs = [Event("added", d)]
    store.apply_events(P, store.start_sweep(P, "t"), evs)
    on_events, _ = _handlers(store, Notifier(Config(), None, out=io.StringIO()), P, Boom())
    on_events(evs)  # must not raise
    assert [f.rule for _, f in store.unalerted(P, {"HIGH", "MEDIUM", "LOW"})] == ["D1"]
    assert "queue broke" in caplog.text


def _watch_env(tmp_path, monkeypatch, ai):
    from rg2watch import cli
    from rg2watch.watcher import Watcher

    db = tmp_path / "w.sqlite"
    s = Store(db)
    s.upsert_election(Election(P, "660578", "3", "Test", "http://127.0.0.1:9"))
    s.close()
    made, sleeps = [], []

    class FakePipeline:
        def __init__(self, *a, **kw):
            self.args, self.kw, self.started, self.stopped = a, kw, False, False
            made.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def submit(self, events):
            pass

    def fake_sleep(s):
        sleeps.append(s)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_anthropic_client", lambda: (ai, None if ai else "no credentials"))
    monkeypatch.setattr(cli, "AnalysisPipeline", FakePipeline)
    monkeypatch.setattr(Watcher, "run_once", lambda self: None)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    return db, made


def test_watch_starts_and_stops_pipeline(tmp_path, monkeypatch):
    from rg2watch.cli import main

    ai = FakeAnthropic(None)
    db, made = _watch_env(tmp_path, monkeypatch, ai)
    assert main(["watch", "--parent", P, "--db", str(db), "--analyze"]) == 0
    assert len(made) == 1 and made[0].started and made[0].stopped
    assert made[0].args[3] is ai and made[0].kw == {"backlog": False}


def test_watch_without_credentials_warns_and_watches(tmp_path, monkeypatch, capsys):
    from rg2watch.cli import main

    db, made = _watch_env(tmp_path, monkeypatch, None)
    assert main(["watch", "--parent", P, "--db", str(db), "--analyze"]) == 0
    assert made == []
    err = capsys.readouterr().err
    assert err.count("WARNING rg2watch.cli: analysis disabled: no credentials") == 1


def test_watch_no_analyze_skips_credentials(tmp_path, monkeypatch):
    from rg2watch import cli

    db, made = _watch_env(tmp_path, monkeypatch, FakeAnthropic(None))
    monkeypatch.setattr(cli, "_anthropic_client", lambda: pytest.fail("must not build a client"))
    assert cli.main(["watch", "--parent", P, "--db", str(db), "--no-analyze"]) == 0
    assert made == []
    cfg = tmp_path / "c.toml"
    cfg.write_text("analysis_enabled = true\n")
    assert cli.main(["watch", "--parent", P, "--db", str(db), "--config", str(cfg),
                     "--no-analyze"]) == 0  # the flag overrides the config
    assert made == []


def test_watch_analysis_is_opt_in(tmp_path, monkeypatch, capsys):
    from rg2watch import cli

    db, made = _watch_env(tmp_path, monkeypatch, FakeAnthropic(None))
    monkeypatch.setattr(cli, "_anthropic_client", lambda: pytest.fail("must not build a client"))
    assert cli.main(["watch", "--parent", P, "--db", str(db)]) == 0
    assert made == []
    assert "analysis off" in capsys.readouterr().err


def test_watch_analysis_enabled_by_config_or_backlog_flag(tmp_path, monkeypatch):
    from rg2watch import cli

    db, made = _watch_env(tmp_path, monkeypatch, FakeAnthropic(None))
    cfg = tmp_path / "c.toml"
    cfg.write_text("analysis_enabled = true\n")
    assert cli.main(["watch", "--parent", P, "--db", str(db), "--config", str(cfg)]) == 0
    assert cli.main(["watch", "--parent", P, "--db", str(db), "--analyze", "--analyze-backlog"]) == 0
    assert [m.kw for m in made] == [{"backlog": False}, {"backlog": True}]


def test_default_cap_is_20_and_analysis_off():
    assert Config().max_spend_usd == 20.0 and Config().analysis_enabled is False


def test_start_logs_cap_spend_queue_and_backlog(tmp_path, caplog):
    import logging

    e = Env(tmp_path, resp(clean_report()))
    s = Store(":memory:")  # a DB where forms were recorded before analysis was ever enabled
    s.upsert_election(ELECTION)
    old, new = doc("a.pdf", no="1"), doc("b.pdf", no="2")
    s.apply_events(P, s.start_sweep(P, "t"), [Event("added", old)])
    s._db.execute("UPDATE documents SET first_seen_at='2026-10-19T18:00:00Z'")
    s.save_failed_analysis(old.url, "m", "v1", 1.25, "x")
    e.store = s
    e.pipe = AnalysisPipeline(e.rik, s, e.notifier, e.ai, e.cfg, e.on_findings, tmp_path / "c",
                              clock=e.clock)
    with caplog.at_level(logging.INFO, logger="rg2watch.pipeline"):
        e.pipe.start()
        e.pipe.stop()
    msg = [r.getMessage() for r in caplog.records if "spend cap" in r.getMessage()]
    assert msg == [f"analysis on for election {P}: spend cap $20.00, $1.25 spent so far on this "
                   "election, 0 form(s) queued, 1 form(s) seen before analysis was enabled "
                   "not analysed (--analyze-backlog to include)"]
    assert e.status(old.url) == ("skipped", 0)
    e.pipe = AnalysisPipeline(e.rik, s, e.notifier, e.ai, e.cfg, e.on_findings, tmp_path / "c",
                              clock=e.clock)  # a stopped pipeline takes no work: drain with a new one
    e.publish(new)
    assert e.drain() == 1 and e.status(new.url) == ("done", 1) and e.ai.calls == 1


def test_backlog_flag_analyses_older_forms(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    s = Store(":memory:")
    s.upsert_election(ELECTION)
    old = doc("a.pdf", no="1")
    s.apply_events(P, s.start_sweep(P, "t"), [Event("added", old)])
    s._db.execute("UPDATE documents SET first_seen_at='2026-10-19T18:00:00Z'")
    e.store = s
    e.pipe = AnalysisPipeline(e.rik, s, e.notifier, e.ai, e.cfg, e.on_findings, tmp_path / "c",
                              clock=e.clock, backlog=True)
    e.pipe.start()
    e.pipe.stop()
    e.pipe = AnalysisPipeline(e.rik, s, e.notifier, e.ai, e.cfg, e.on_findings, tmp_path / "c",
                              clock=e.clock)
    e.drain()
    assert e.status(old.url) == ("done", 1)


def test_spend_cap_counts_only_this_election(tmp_path):
    e = Env(tmp_path, resp(clean_report()), max_spend_usd=3.0)
    e.store.save_failed_analysis("file:///x.pdf", "m", "v2", 50.0, "x", parent="eval")
    e.publish(doc())
    assert e.drain() == 1


def test_analyze_local_pdf_json(tmp_path, monkeypatch, capsys):
    from rg2watch import cli

    pdf = make_pdf(tmp_path / "x.pdf")
    ai = FakeAnthropic(resp(overwritten_report()))
    monkeypatch.setattr(cli, "_anthropic_client", lambda: (ai, None))
    assert cli.main(["analyze", str(pdf), "--json", "--db", str(tmp_path / "d.sqlite")]) == 0
    s = Store(tmp_path / "d.sqlite")
    assert s.total_spend("adhoc") == pytest.approx((1000 * 4 + 500 * 20) / 1e6)
    s.close()
    out = json.loads(capsys.readouterr().out)
    assert out["report"]["numbers"]["voted"]["mark"] == "overwritten"
    assert [f["rule"] for f in out["findings"]] == ["V1"]
    assert out["cost_usd"] > 0 and out["model"] == "claude-opus-5-5"


def test_analyze_human_summary(tmp_path, monkeypatch, capsys):
    from rg2watch import cli

    pdf = make_pdf(tmp_path / "x.pdf")
    monkeypatch.setattr(cli, "_anthropic_client", lambda: (FakeAnthropic(resp(overwritten_report())), None))
    assert cli.main(["analyze", str(pdf), "--db", str(tmp_path / "d.sqlite")]) == 0
    out = capsys.readouterr().out
    assert "[HIGH] V1" in out and "confidence high" in out


def test_analyze_without_credentials_exits_2(tmp_path, monkeypatch, capsys):
    from rg2watch import cli

    monkeypatch.setattr(cli, "_anthropic_client", lambda: (None, "no credentials"))
    assert cli.main(["analyze", str(make_pdf(tmp_path / "x.pdf"))]) == 2
    assert "no credentials" in capsys.readouterr().err


def test_analyze_url_of_unregistered_host_refused(tmp_path, monkeypatch, capsys):
    from rg2watch import cli

    ai = FakeAnthropic(resp(clean_report()))
    monkeypatch.setattr(cli, "_anthropic_client", lambda: (ai, None))
    monkeypatch.setattr(cli, "fetch_pdf", lambda *a: pytest.fail("must not download"))
    assert cli.main(["analyze", "https://evil.example/x.pdf", "--db", str(tmp_path / "d.sqlite")]) == 2
    assert "registered election" in capsys.readouterr().err and ai.calls == 0


def test_analyze_url_of_known_document(tmp_path, monkeypatch, capsys):
    from rg2watch import cli

    db = tmp_path / "d.sqlite"
    s = Store(db)
    s.upsert_election(ELECTION)
    d = doc(no="7")
    s.apply_events(P, s.start_sweep(P, "t"), [Event("added", d)])
    s.close()
    pdf, seen = make_pdf(tmp_path / "x.pdf"), []

    def fake_fetch(client, url, cache_dir):
        seen.append((client.election.parent, url))
        return pdf

    report = clean_report()  # station 0012 on the form vs 7 in the listing -> V9
    monkeypatch.setattr(cli, "_anthropic_client", lambda: (FakeAnthropic(resp(report)), None))
    monkeypatch.setattr(cli, "fetch_pdf", fake_fetch)
    assert cli.main(["analyze", d.url, "--json", "--db", str(db)]) == 0
    s = Store(db)
    assert s.total_spend(P) == 0 and s.total_spend("adhoc") > 0  # never counts against the cap
    s.close()
    assert seen == [(P, d.url)]
    assert [f["rule"] for f in json.loads(capsys.readouterr().out)["findings"]] == ["V9"]


# fix round 1 -------------------------------------------------------------------

def api_error(cls, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("nope", response=httpx.Response(status, request=req), body=None)


def alerts(e, text):
    return e.out.getvalue().count(text)


def test_bad_key_pauses_alerts_once_no_attempts_and_recovers(tmp_path):
    e = Env(tmp_path, api_error(anthropic.AuthenticationError, 401), analysis_pause_s=600)
    a, b = doc("a.pdf", no="1"), doc("b.pdf", no="2")
    e.publish(a, b)
    assert e.pipe.process_next() is True  # newest (b) first -> global failure
    assert e.status(b.url) == ("new", 0) and e.status(a.url) == ("new", 0)
    for _ in range(3):
        assert e.pipe.process_next() is False  # paused: nothing claimed
    assert e.ai.calls == 1
    e.clock.now += 600
    assert e.pipe.process_next() is True  # one probe, still failing: it pays its attempt
    assert e.status(b.url) == ("failed", 1) and e.status(a.url) == ("new", 0)
    assert e.pipe.process_next() is False  # paused again
    assert e.ai.calls == 2
    assert alerts(e, "[rg2-watch] analysis paused: AuthenticationError") == 1
    e.ai.reply = resp(clean_report())
    e.clock.now += 600
    assert e.drain() == 2
    assert e.status(a.url) == ("done", 1) and e.status(b.url) == ("done", 2)
    assert alerts(e, "[rg2-watch] analysis resumed") == 1
    assert [f.rule for f in e.findings if f.rule.startswith("X")] == []


@pytest.mark.parametrize("cls,status", [(anthropic.PermissionDeniedError, 403),
                                        (anthropic.NotFoundError, 404)])
def test_other_global_errors_pause(tmp_path, cls, status):
    e = Env(tmp_path, api_error(cls, status))
    d = doc()
    e.publish(d)
    e.drain()
    assert e.status(d.url) == ("new", 0) and e.ai.calls == 1
    assert alerts(e, f"[rg2-watch] analysis paused: {cls.__name__}") == 1


def test_only_one_probe_while_paused(tmp_path):
    e = Env(tmp_path, api_error(anthropic.AuthenticationError, 401))
    e.publish(doc("a.pdf", no="1"), doc("b.pdf", no="2"))
    e.pipe.process_next()
    e.clock.now += 600
    gate, entered = threading.Event(), threading.Event()

    def slow():
        entered.set()
        gate.wait(5)
        return resp(clean_report())

    e.ai.reply = slow
    t = threading.Thread(target=e.pipe.process_next)
    t.start()
    assert entered.wait(5)
    assert e.pipe.process_next() is False  # second worker waits for the probe
    gate.set()
    t.join(5)
    assert e.drain() == 1 and e.ai.calls == 3


def overload_errors():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    mid_stream = anthropic.APIStatusError(
        "overloaded", response=httpx.Response(200, request=req),
        body={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    return [api_error(anthropic.RateLimitError, 429), api_error(anthropic.InternalServerError, 500),
            api_error(anthropic.OverloadedError, 529), anthropic.APIConnectionError(request=req),
            anthropic.APITimeoutError(request=req), mid_stream]


@pytest.mark.parametrize("err", overload_errors(), ids=lambda e: type(e).__name__)
def test_overload_refunds_and_defers_120s(tmp_path, err):
    e = Env(tmp_path, err)
    d = doc()
    e.publish(d)
    for _ in range(6):  # never runs out of attempts while Anthropic is overloaded
        assert e.drain() == 1
        assert e.status(d.url) == ("failed", 0)
        e.clock.now += 119
        assert e.drain() == 0
        e.clock.now += 1
    assert e.findings == [] and "analysis paused" not in e.out.getvalue()
    e.ai.reply = resp(clean_report())
    assert e.drain() == 1 and e.status(d.url) == ("done", 1)


def test_overload_alerts_once_after_5_and_recovers_once(tmp_path):
    e = Env(tmp_path, api_error(anthropic.OverloadedError, 529))
    pdf_dir = tmp_path / "pdfs"
    docs = [doc(f"{n}.pdf", no=str(n)) for n in range(1, 9)]
    for n in range(1, 9):
        make_pdf(pdf_dir / f"{n}.pdf")
    e.publish(*docs[:4])
    e.drain()
    assert e.ai.calls == 4 and "analysis delayed" not in e.out.getvalue()
    e.publish(*docs[4:])
    e.drain()
    hhmm = time.strftime("%H:%M", time.localtime(e.clock.now))
    assert alerts(e, f"[rg2-watch] analysis delayed: Anthropic overloaded since {hhmm}") == 1
    assert alerts(e, "analysis delayed") == 1
    e.ai.reply = resp(clean_report())
    e.clock.now += 120
    assert e.drain() == 8
    assert alerts(e, "[rg2-watch] analysis no longer delayed") == 1
    assert alerts(e, "analysis delayed:") == 1


@pytest.mark.parametrize("fail", [403, 503])
def test_rik_unavailable_download_consumes_no_attempt(tmp_path, fail):
    e = Env(tmp_path, resp(clean_report()))
    d = doc()
    e.publish(d)
    e.fake.fail_status = fail
    for _ in range(4):
        e.drain()
        e.clock.now += 121
    assert e.status(d.url) == ("failed", 0) and e.findings == []
    assert e.rik.blocked_streak == 0 and e.rik.last_ok is None  # watcher health untouched
    e.fake.fail_status = None
    e.clock.now += e.cfg.blocked_pause_s  # 403s also start the download pause
    assert e.drain() == 1
    assert e.status(d.url) == ("done", 1)


def test_download_retry_waits_120s(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    d = doc()
    e.publish(d)
    e.fake.fail_status = 403
    e.drain()
    e.fake.fail_status = None
    e.clock.now += 119
    assert e.drain() == 0
    e.clock.now += 1
    assert e.drain() == 1


def test_downloads_keep_their_own_health_and_limiter(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    e.rik.blocked_streak = 2
    used = []
    for name in ("limiter", "download_limiter"):
        lim = getattr(e.rik, name)
        orig = lim.acquire
        lim.acquire = lambda orig=orig, name=name: (used.append(name), orig())
    e.publish(doc())
    e.drain()
    assert used == ["download_limiter"]
    assert e.rik.download_limiter is not e.rik.limiter
    assert e.rik.download_limiter.max_rps == 0.5
    assert e.rik.blocked_streak == 2 and e.rik.last_ok is None
    assert e.rik.download_last_ok is not None


def test_download_limiter_ignores_slow_success():
    lim = AdaptiveLimiter(0.5, slow_is_overload=False)
    lim.on_success(30.0)
    assert lim.current_rps == 0.5
    lim.on_overload()
    assert lim.current_rps == 0.25
    lim.on_success(30.0)
    assert lim.current_rps == pytest.approx(0.35)


def test_cli_download_limiter_uses_config():
    from rg2watch.rik_client import download_limiter_for

    assert download_limiter_for(Config(download_max_rps=0.25)).max_rps == 0.25


def test_watcher_blocked_pause_stops_workers(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    d = doc()
    e.publish(d)
    e.rik.paused_until = e.rik.limiter.clock() + 100
    assert e.pipe.process_next() is False
    assert e.status(d.url) == ("new", 0) and e.pdf_requests() == []
    e.rik.paused_until = float("-inf")
    assert e.drain() == 1


def test_pause_starting_after_claim_refunds(tmp_path, monkeypatch):
    e = Env(tmp_path, resp(clean_report()))
    d = doc()
    e.publish(d)
    real = e.store.next_for_analysis

    def claim_then_pause(*a, **kw):
        out = real(*a, **kw)
        e.rik.paused_until = e.rik.limiter.clock() + 100
        return out

    monkeypatch.setattr(e.store, "next_for_analysis", claim_then_pause)
    assert e.pipe.process_next() is True
    assert e.status(d.url) == ("new", 0) and e.pdf_requests() == []


def test_save_failure_does_not_leave_running(tmp_path, monkeypatch, caplog):
    e = Env(tmp_path, resp(clean_report()))
    d = doc()
    e.publish(d)

    def boom(*a):
        raise RuntimeError("disk full")

    monkeypatch.setattr(e.store, "save_analysis", boom)
    e.drain()
    assert e.status(d.url) == ("failed", 1)
    assert "could not be recorded" in caplog.text and "$0.014" in caplog.text


def test_shutdown_waits_for_in_flight_and_records_cost(tmp_path):
    gate, entered = threading.Event(), threading.Event()

    def slow():
        entered.set()
        gate.wait(5)
        return resp(clean_report())

    e = Env(tmp_path, slow, analysis_workers=1, stop_wait_s=10)
    d = doc()
    e.publish(d)
    e.pipe.start()
    assert entered.wait(5)
    threading.Timer(0.2, gate.set).start()
    e.pipe.stop()  # returns only once the in-flight analysis is recorded
    assert e.status(d.url) == ("done", 1)
    assert e.store.total_spend() > 0


def test_store_closed_before_worker_finishes_logs_cost(tmp_path, caplog):
    gate, entered = threading.Event(), threading.Event()

    def slow():
        entered.set()
        gate.wait(5)
        return resp(clean_report())

    e = Env(tmp_path, slow, analysis_workers=1, stop_wait_s=0)
    db = tmp_path / "w.sqlite"
    e.store = e.pipe.store = Store(db)
    e.store.upsert_election(ELECTION)
    e.store.enable_analysis(P)
    d = doc()
    e.publish(d)
    e.pipe.start()
    assert entered.wait(5)
    e.pipe.stop()
    e.store.close()
    gate.set()
    e.pipe._threads[0].join(5)
    assert "could not be recorded" in caplog.text and "$0.014" in caplog.text
    s = Store(db)
    assert s._db.execute("SELECT analysis_status FROM documents").fetchone()[0] == "running"
    assert s.requeue_running(P) == []
    assert s._db.execute("SELECT analysis_status FROM documents").fetchone()[0] == "new"


def test_requeue_running_only_own_parent_and_crash_loop_gives_x2(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    mine, crashed = doc("a.pdf", no="1"), doc("b.pdf", no="2")
    other = Doc("Beta", "1", "S", "lbl", 6, f"{BASE}/extfile/other.pdf", "2026-10-19 20:00:00")
    e.publish(mine, crashed)
    e.store.apply_events("999", e.store.start_sweep("999", "t"), [Event("added", other)])
    e.store._db.execute("UPDATE documents SET analysis_status='running'")
    e.store._db.execute("UPDATE documents SET analysis_attempts=3 WHERE url=?", (crashed.url,))
    e.pipe.start()
    e.pipe.stop()
    assert e.status(crashed.url) == ("failed", 3)
    assert [(f.rule, f.doc.url) for f in e.findings if f.rule == "X2"] == [("X2", crashed.url)]
    assert e.status(other.url)[0] == "running"
    assert e.status(mine.url)[0] in ("new", "done")


def test_requeue_cli(tmp_path, capsys):
    from rg2watch.cli import main

    db = tmp_path / "d.sqlite"
    s = Store(db)
    s.upsert_election(ELECTION)
    x2, d3, ok = doc("a.pdf", no="1"), doc("b.pdf", no="2"), doc("c.pdf", no="3")
    s.apply_events(P, s.start_sweep(P, "t"), [Event("added", x) for x in (x2, d3, ok)])
    s._db.execute("UPDATE documents SET analysis_status='failed', analysis_attempts=3 WHERE url=?", (x2.url,))
    s._db.execute("UPDATE documents SET analysis_status='failed', analysis_attempts=1 WHERE url=?", (d3.url,))
    s._db.execute("UPDATE documents SET analysis_status='done', analysis_attempts=1 WHERE url=?", (ok.url,))
    s._db.commit()
    s.close()
    assert main(["requeue", "--db", str(db), "--parent", "999"]) == 0
    assert "requeued 0" in capsys.readouterr().out
    assert main(["requeue", "--db", str(db), "--parent", P]) == 0
    assert "requeued 1" in capsys.readouterr().out
    assert main(["requeue", "--db", str(db), "--failed"]) == 0
    assert "requeued 1" in capsys.readouterr().out
    s = Store(db)
    rows = dict(s._db.execute("SELECT url, analysis_status || analysis_attempts FROM documents"))
    assert rows == {x2.url: "new0", d3.url: "new0", ok.url: "done1"}


def test_watch_once_keeps_analysis_off(tmp_path, monkeypatch, capsys):
    from rg2watch.cli import main

    db, made = _watch_env(tmp_path, monkeypatch, FakeAnthropic(None))
    assert main(["watch", "--parent", P, "--db", str(db), "--once"]) == 0
    assert made == []
    assert capsys.readouterr().err.count("INFO rg2watch.cli: watch --once is a one-shot diff check") == 1


# fix round 2 -------------------------------------------------------------------

def test_poison_bad_request_doc_goes_x2_others_analysed(tmp_path):
    poison = doc("b.pdf", no="2")  # newest
    good = doc("a.pdf", no="1")
    e = Env(tmp_path, None)

    def reply():  # the document being analysed is the one marked running
        running = e.store._db.execute(
            "SELECT url FROM documents WHERE analysis_status='running'").fetchone()[0]
        return api_error(anthropic.BadRequestError, 400) if running == poison.url else resp(clean_report())

    e.ai.reply = reply
    e.publish(good)
    e.store.apply_events(P, e.store.start_sweep(P, "t"), [Event("added", poison)])
    e.pipe.submit([Event("added", poison)])
    assert e.drain() == 2  # poison fails once, good doc analysed anyway
    assert e.status(good.url) == ("done", 1) and e.status(poison.url) == ("failed", 1)
    for _ in range(2):
        e.clock.now += 200
        e.drain()
    assert e.status(poison.url) == ("failed", 3)
    assert [f.rule for f in e.findings if f.doc.url == poison.url] == ["X2"]
    assert "analysis paused" not in e.out.getvalue()


def test_three_distinct_bad_requests_pause_globally(tmp_path):
    e = Env(tmp_path, api_error(anthropic.BadRequestError, 400))
    docs = [doc(f"{n}.pdf", no=str(n)) for n in (1, 2, 3, 4)]
    pdf_dir = tmp_path / "pdfs"
    for n in (1, 2, 3, 4):
        make_pdf(pdf_dir / f"{n}.pdf")
    e.publish(*docs)
    e.drain()
    assert e.ai.calls == 3
    assert alerts(e, "[rg2-watch] analysis paused: BadRequestError") == 1
    # first two used an attempt, the third (account-wide) was refunded, the fourth never claimed
    assert [e.status(d.url) for d in reversed(docs)] == [("failed", 1), ("failed", 1), ("new", 0), ("new", 0)]
    e.clock.now += 600
    assert e.pipe.process_next() is True  # probe: still 400 -> paused again, pays its attempt
    assert e.pipe.process_next() is False
    assert e.ai.calls == 4 and alerts(e, "analysis paused") == 1
    assert e.status(docs[3].url) == ("failed", 2)  # newest due one (retry time passed)


def test_bad_request_streak_reset_by_success(tmp_path):
    e = Env(tmp_path, None)
    replies = [api_error(anthropic.BadRequestError, 400), api_error(anthropic.BadRequestError, 400),
               resp(clean_report()), api_error(anthropic.BadRequestError, 400)]
    e.ai.reply = lambda: replies.pop(0)
    pdf_dir = tmp_path / "pdfs"
    for n in (1, 2, 3, 4):
        make_pdf(pdf_dir / f"{n}.pdf")
    e.publish(*[doc(f"{n}.pdf", no=str(n)) for n in (1, 2, 3, 4)])
    e.drain()
    assert e.ai.calls == 4 and "analysis paused" not in e.out.getvalue()


def test_extended_pause_realerts_hourly(tmp_path):
    e = Env(tmp_path, api_error(anthropic.AuthenticationError, 401), analysis_pause_s=600)
    pdf_dir = tmp_path / "pdfs"
    docs = [doc(f"{n}.pdf", no=str(n)) for n in range(1, 13)]
    for n in range(1, 13):
        make_pdf(pdf_dir / f"{n}.pdf")
    e.publish(*docs)
    e.pipe.process_next()
    for _ in range(12):  # 12 failed probes = 2 hours
        e.clock.now += 600
        e.pipe.process_next()
    assert alerts(e, "[rg2-watch] analysis paused: AuthenticationError") == 1
    assert alerts(e, "[rg2-watch] analysis still paused since ") == 2


def test_stop_before_api_call_refunds(tmp_path, monkeypatch):
    e = Env(tmp_path, resp(clean_report()))
    from rg2watch import pipeline
    real = pipeline.render_tiles

    def render_then_stop(path):
        out = real(path)
        e.pipe._stop.set()
        return out

    monkeypatch.setattr(pipeline, "render_tiles", render_then_stop)
    d = doc()
    e.publish(d)
    assert e.pipe.process_next() is True
    assert e.ai.calls == 0 and e.status(d.url) == ("new", 0)


def test_pdf_download_blocked_alert_and_recovery(tmp_path):
    e = Env(tmp_path, resp(clean_report()))
    pdf_dir = tmp_path / "pdfs"
    docs = [doc(f"{n}.pdf", no=str(n)) for n in range(1, 13)]
    for n in range(1, 13):
        make_pdf(pdf_dir / f"{n}.pdf")
    e.publish(*docs)
    e.fake.fail_status = 503  # busy, not blocking: alert after 10 deferrals
    for _ in range(9):
        e.pipe.process_next()
    assert "PDF downloads" not in e.out.getvalue()
    for _ in range(3):
        e.pipe.process_next()
    assert alerts(e, "[rg2-watch] PDF downloads blocked/unavailable since ") == 1
    e.fake.fail_status = None
    e.clock.now += 121
    e.drain()
    assert alerts(e, "[rg2-watch] PDF downloads available again") == 1
    assert e.rik.blocked_streak == 0


def test_pdf_downloads_blocked_3_times_pause_workers(tmp_path):
    e = Env(tmp_path, resp(clean_report()), blocked_pause_s=300)
    pdf_dir = tmp_path / "pdfs"
    docs = [doc(f"{n}.pdf", no=str(n)) for n in range(1, 7)]
    for n in range(1, 7):
        make_pdf(pdf_dir / f"{n}.pdf")
    e.publish(*docs)
    e.fake.fail_status = 403
    for _ in range(3):
        assert e.pipe.process_next() is True
    assert e.rik.download_blocked_streak == 3
    assert alerts(e, "[rg2-watch] PDF downloads blocked/unavailable since ") == 1
    tried = len(e.pdf_requests())
    e.clock.now += 299
    for _ in range(5):
        assert e.pipe.process_next() is False  # paused: no download attempted
    assert len(e.pdf_requests()) == tried
    assert e.rik.blocked_streak == 0  # the watcher's own health is untouched
    e.clock.now += 1
    e.pipe.process_next()  # pause over: still blocked -> paused again, no second alert
    assert len(e.pdf_requests()) > tried and e.pipe.process_next() is False
    assert alerts(e, "PDF downloads blocked") == 1
    e.fake.fail_status = None
    e.clock.now += 300
    assert e.drain() == 6
    assert alerts(e, "[rg2-watch] PDF downloads available again") == 1


def test_requeue_skipped_cli(tmp_path, capsys):
    from rg2watch.cli import main

    db = tmp_path / "d.sqlite"
    s = Store(db)
    s.upsert_election(ELECTION)
    d = doc()
    s.apply_events(P, s.start_sweep(P, "t"), [Event("added", d)])
    s._db.execute("UPDATE documents SET analysis_status='skipped'")
    s._db.commit()
    s.close()
    assert main(["requeue", "--db", str(db)]) == 0
    assert "requeued 0" in capsys.readouterr().out
    assert main(["requeue", "--db", str(db), "--skipped"]) == 0
    assert "requeued 1" in capsys.readouterr().out


def test_analyze_backlog_needs_analysis_on(tmp_path, monkeypatch, capsys):
    from rg2watch import cli

    db, made = _watch_env(tmp_path, monkeypatch, FakeAnthropic(None))
    assert cli.main(["watch", "--parent", P, "--db", str(db), "--analyze-backlog"]) == 2
    assert made == [] and "--analyze-backlog needs analysis on" in capsys.readouterr().err
