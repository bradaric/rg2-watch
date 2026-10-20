import anthropic
import pytest

from rg2watch import batch, cli
from rg2watch.models import Doc, Event, Finding
from rg2watch.names import MunicipalityError
from rg2watch.pipeline import PROMPT_VERSION
from rg2watch.rik_client import RikError
from rg2watch.store import Store
from test_pipeline import (BASE, ELECTION, P, FakeAnthropic, api_error, clean_report, make_pdf,
                           overwritten_report, resp)


def mk(name, muni="Alpha", no="1", dt=6, up="2026-10-19 20:00:00"):
    return Doc(muni, no, "School", "lbl", dt, f"{BASE}/extfile/{name}", up)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "d.sqlite"
    s = Store(path)
    s.upsert_election(ELECTION)
    s.apply_events(P, s.start_sweep(P, "t"), [Event("added", d) for d in (
        mk("a.pdf", "Alpha", "1"), mk("b.pdf", "Beta", "02"), mk("c.pdf", "Beta", "3"),
        mk("o.pdf", "Alpha", "9", dt=9))])
    s.add_finding(P, Finding("V1", "HIGH", mk("a.pdf"), "x"), mk("a.pdf").url)
    s.add_finding(P, Finding("S2", "LOW", mk("b.pdf", "Beta", "02"), "y"), mk("b.pdf").url)
    s.close()
    return path


def sel(db, **kw):
    s = Store(db)
    try:
        return [d.url.rsplit("/", 1)[1] for d in batch.select_forms(s, P, **kw).docs]
    finally:
        s.close()


def test_select_filters_anded(db):
    assert sel(db) == ["a.pdf", "b.pdf", "c.pdf"]  # RG-9 is not an analysed type
    assert sel(db, municipalities=["beta"]) == ["b.pdf", "c.pdf"]
    assert sel(db, municipalities=["beta"], stations=["2"]) == ["b.pdf"]
    assert sel(db, rules=["V1"]) == ["a.pdf"]
    assert sel(db, severity="MEDIUM") == ["a.pdf"]
    assert sel(db, severity="LOW") == ["a.pdf", "b.pdf"]
    assert sel(db, rules=["S2"], severity="HIGH") == []
    assert sel(db, limit=2) == ["a.pdf", "b.pdf"]


def test_select_state_like_browse(db):
    s = Store(db)
    new = mk("a2.pdf", up="2026-10-19 21:00:00")
    s.apply_events(P, s.start_sweep(P, "t"), [Event("replaced", new, previous_url=mk("a.pdf").url),
                                              Event("removed", mk("c.pdf", "Beta", "3"))])
    s.close()
    assert sel(db, state="replaced") == ["a2.pdf"]
    assert sel(db, state="current") == ["b.pdf"]
    assert sel(db, state="removed") == ["c.pdf"]
    assert sel(db) == ["a2.pdf", "b.pdf"]


def test_select_unknown_municipality(db):
    with pytest.raises(MunicipalityError):
        sel(db, municipalities=["Nowhere"])


@pytest.fixture
def run(tmp_path, db, monkeypatch, capsys):
    pdf = make_pdf(tmp_path / "x.pdf")
    monkeypatch.setattr(batch, "fetch_pdf", lambda client, url, cache: pdf)
    monkeypatch.setattr(batch, "_sleep", lambda s: None)

    def go(ai, *args):
        monkeypatch.setattr(cli, "_anthropic_client", lambda: (ai, None))
        code = cli.main(["analyze", "--parent", P, "--db", str(db), *args])
        return code, capsys.readouterr()
    return go


def test_both_or_neither_source(tmp_path, capsys):
    assert cli.main(["analyze", "--db", str(tmp_path / "d.sqlite")]) == 2
    assert cli.main(["analyze", "x.pdf", "--parent", P, "--db", str(tmp_path / "d.sqlite")]) == 2
    assert cli.main(["analyze", "x.pdf", "--yes", "--db", str(tmp_path / "d.sqlite")]) == 2
    assert capsys.readouterr().err.count("\n") >= 3


def test_paid_model_needs_yes(run):
    ai = FakeAnthropic(resp(clean_report()))
    code, cap = run(ai)
    assert code == 0 and ai.calls == 0
    assert "3 form(s) selected, 0 already analysed" in cap.out
    assert "estimated cost $" in cap.out and "without --yes" in cap.out


def test_batch_analyses_records_adhoc_and_caches(run, db):
    ai = FakeAnthropic(resp(overwritten_report()))
    code, cap = run(ai, "--yes", "--municipality", "Alpha")
    assert code == 0 and ai.calls == 1
    assert "Alpha BM 1 | claude-opus-5-5 | $0.014" in cap.out
    assert "[HIGH] V1" in cap.out and "1 analysed, 0 cached, 0 failed" in cap.out
    s = Store(db)
    assert s.total_spend("adhoc") > 0 and s.total_spend(P) == 0
    assert s.latest_report(mk("a.pdf").url, "claude-opus-5-5", PROMPT_VERSION) is not None
    assert s._db.execute("SELECT document_id FROM analyses").fetchone()[0] is not None
    s.close()
    code, cap = run(ai, "--yes", "--municipality", "Alpha")
    assert ai.calls == 1 and "1 already analysed" in cap.out and "cached" in cap.out
    assert "0 analysed, 1 cached" in cap.out and "[HIGH] V1" in cap.out


def test_spend_cap_stops_batch(run):
    ai = FakeAnthropic(resp(clean_report()))
    code, cap = run(ai, "--yes", "--max-usd", "0.001")
    assert ai.calls == 1 and "spend cap $0.00 reached" in cap.err and code == 1


def test_overload_retried_then_skipped(run):
    err = api_error(anthropic.InternalServerError, 529)
    ai = FakeAnthropic(err)
    code, cap = run(ai, "--yes", "--limit", "2")
    assert ai.calls == 6 and code == 1  # 3 attempts per form, batch continues
    assert cap.out.count("skipped") + cap.err.count("skipped") == 2
    assert "0 analysed, 0 cached, 2 failed" in cap.out


def test_auth_error_stops_batch(run):
    ai = FakeAnthropic(api_error(anthropic.AuthenticationError, 401))
    code, cap = run(ai, "--yes")
    assert ai.calls == 1 and code == 1 and "stopping" in cap.err


def test_download_failure_skips_form(run, monkeypatch):
    def boom(*a):
        raise RikError("gone")
    monkeypatch.setattr(batch, "fetch_pdf", boom)
    ai = FakeAnthropic(resp(clean_report()))
    code, cap = run(ai, "--yes")
    assert ai.calls == 0 and "3 failed" in cap.out


def test_local_model_needs_no_yes(run, monkeypatch):
    calls = []

    def fake(tiles, client, cfg):
        calls.append(1)
        from rg2watch.analyze import AnalysisResult
        return AnalysisResult(clean_report(), 0, 0, 0.0, "qwen", "stop")

    monkeypatch.setattr(batch, "analyze_document", fake)
    code, cap = run(None, "--model", "ollama:qwen", "--limit", "1")
    assert code == 0 and calls == [1] and "local model, no API cost" in cap.out


def test_cache_hits_when_responding_model_differs(run, db):
    r = resp(clean_report())
    r.model = "claude-opus-5-5-20261001"
    ai = FakeAnthropic(r)
    run(ai, "--yes", "--municipality", "Alpha")
    s = Store(db)
    rep = s.latest_report(mk("a.pdf").url, "claude-opus-5-5", PROMPT_VERSION)
    s.close()
    assert rep["responding_model"] == "claude-opus-5-5-20261001"
    code, cap = run(ai, "--yes", "--municipality", "Alpha")
    assert ai.calls == 1 and "0 analysed, 1 cached" in cap.out


def test_eval_cached_hits_when_responding_model_differs(tmp_path, db):
    from rg2watch.config import Config
    from rg2watch.evaluate import run_eval

    pdf = make_pdf(tmp_path / "f.pdf")
    s = Store(tmp_path / "e.sqlite")
    cfg = Config()
    ai = FakeAnthropic(resp(clean_report()))
    ai.reply.model = "other-snapshot"
    import io
    labels = [{"file": "f.pdf", "expect": {}}]
    run_eval(labels, tmp_path, s, ai, cfg, cached=False, out=io.StringIO())
    res = run_eval(labels, tmp_path, s, None, cfg, cached=True, out=io.StringIO())
    s.close()
    assert "f.pdf" in res["files"] and not res["errors"]


def test_blocked_downloads_stop_batch(run, monkeypatch):
    from rg2watch.rik_client import RikBlocked

    def boom(*a):
        raise RikBlocked("403")
    monkeypatch.setattr(batch, "fetch_pdf", boom)
    code, cap = run(FakeAnthropic(resp(clean_report())), "--yes")
    assert code == 1 and "3 downloads in a row" in cap.err and "2 failed" in cap.out


def test_bad_requests_stop_batch(run):
    ai = FakeAnthropic(api_error(anthropic.BadRequestError, 400))
    code, cap = run(ai, "--yes")
    assert ai.calls == 3 and code == 1 and "bad requests" in cap.err


def test_cap_checked_before_retry(run, monkeypatch):
    err = api_error(anthropic.InternalServerError, 529)
    err.cost_usd = 1.0
    code, cap = run(FakeAnthropic(err), "--yes", "--max-usd", "0.5")
    assert code == 1 and "spend cap" in cap.err


def test_retry_wait_capped(run, monkeypatch):
    monkeypatch.setattr(batch, "MAX_WAIT_S", 15)
    ai = FakeAnthropic(api_error(anthropic.InternalServerError, 529))
    code, cap = run(ai, "--yes")
    assert code == 1 and ai.calls == 2 and "still busy" in cap.err
