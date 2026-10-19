import io
import json

import pytest
from test_pipeline import FakeAnthropic, make_pdf, resp
from test_rules import clean_report

from rg2watch import cli, render
from rg2watch.cli import main
from rg2watch.config import Config
from rg2watch.evaluate import estimate_cost, load_labels, run_eval, score
from rg2watch.store import Store


@pytest.fixture(autouse=True)
def no_osd(monkeypatch):
    monkeypatch.setattr(render, "_osd_rotation", lambda img, timeout=10: 0)


def label(file, expect, **kw):
    return {"file": file, "municipality": "Alpha", "station_no": "1", "expect": expect,
            "verified": True, "note": "", **kw}


def overwritten():
    r = clean_report()
    r["numbers"]["voted"] = {"value": 800, "legible": True, "mark": "overwritten", "note": ""}
    return r


def test_score_counts():
    labels = [
        label("a", {"V1": True, "V3": False}),
        label("b", {"V1": True, "V3": True}),
        label("c", {"V1": False, "V3": False}),
        label("d", {"V1": False}),
        label("e", {}),
        label("missing", {"V1": True}),  # not analysed: not scored
    ]
    found = {"a": {"V1", "V3"}, "b": {"V3"}, "c": {"V1"}, "d": set(), "e": {"V1"}}
    s = score(labels, found)
    assert s["V1"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1, "precision": 0.5, "recall": 0.5}
    assert s["V3"] == {"tp": 1, "fp": 1, "fn": 0, "tn": 1, "precision": 0.5, "recall": 1.0}
    assert set(s) == {"V1", "V3"}


def test_score_undefined_precision_none():
    s = score([label("a", {"V1": True}), label("b", {"V2": False})], {"a": set(), "b": set()})
    assert s["V1"]["precision"] is None and s["V1"]["recall"] == 0.0
    assert s["V2"]["precision"] is None and s["V2"]["recall"] is None


def env(tmp_path, reply):
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    make_pdf(pdfs / "a.pdf")
    make_pdf(pdfs / "b.pdf")
    (pdfs / "html.pdf").write_bytes(b"<!doctype html><html>404</html>")
    labels = tmp_path / "labels.jsonl"
    entries = [label("a.pdf", {"V1": True}), label("html.pdf", {"V1": True}),
               label("gone.pdf", {"V1": True}), label("b.pdf", {"V1": False})]
    labels.write_text("\n".join(json.dumps(e) for e in entries))
    return pdfs, labels, entries, FakeAnthropic(reply)


def test_run_eval_scores_records_and_survives_bad_files(tmp_path):
    pdfs, _, entries, ai = env(tmp_path, resp(overwritten()))
    store = Store(":memory:")
    out = run_eval(entries, pdfs, store, ai, Config(), cached=False, out=io.StringIO())
    assert ai.calls == 2
    assert set(out["files"]) == {"a.pdf", "b.pdf"}
    assert set(out["errors"]) == {"html.pdf", "gone.pdf"}
    assert out["score"]["V1"]["tp"] == 1 and out["score"]["V1"]["fp"] == 1
    assert store.total_spend() == pytest.approx(out["cost_usd"]) and out["cost_usd"] > 0


def test_run_eval_cached_never_calls_api(tmp_path):
    pdfs, _, entries, ai = env(tmp_path, resp(overwritten()))
    store = Store(":memory:")
    sink = io.StringIO()
    run_eval(entries[:1], pdfs, store, ai, Config(), cached=False, out=sink)
    again = run_eval(entries, pdfs, store, None, Config(), cached=True, out=sink)
    assert ai.calls == 1
    assert set(again["files"]) == {"a.pdf"}
    assert again["errors"]["b.pdf"].startswith("no stored analysis")
    assert again["cost_usd"] == 0


def test_run_eval_stops_at_spend_cap(tmp_path):
    pdfs, _, entries, ai = env(tmp_path, resp(overwritten()))
    out = run_eval([entries[0], entries[3]], pdfs, Store(":memory:"), ai, Config(max_spend_usd=0.001),
                   cached=False, out=io.StringIO())
    assert ai.calls == 1 and out["errors"]["b.pdf"] == "spend cap reached"


def test_run_eval_cap_counts_only_eval_spend(tmp_path):
    pdfs, _, entries, ai = env(tmp_path, resp(overwritten()))
    store = Store(":memory:")
    store.save_failed_analysis("file:///x.pdf", "m", "v2", 50.0, "x", parent="adhoc")
    out = run_eval([entries[0]], pdfs, store, ai, Config(max_spend_usd=3.0), cached=False,
                   out=io.StringIO())
    assert ai.calls == 1 and out["errors"] == {}
    assert store.total_spend("eval") == pytest.approx(out["cost_usd"])


def cli_args(tmp_path, labels, pdfs, *extra):
    return ["eval", "--labels", str(labels), "--pdf-dir", str(pdfs),
            "--db", str(tmp_path / "db" / "x.sqlite"), *extra]


def test_cli_refuses_without_yes(tmp_path, monkeypatch, capsys):
    pdfs, labels, _, ai = env(tmp_path, resp(overwritten()))
    monkeypatch.setattr(cli, "_anthropic_client", lambda: (ai, None))
    assert main(cli_args(tmp_path, labels, pdfs)) == 2
    cap = capsys.readouterr()
    assert "estimated cost" in cap.out and "--yes" in cap.err and ai.calls == 0


def test_cli_without_credentials_exits_2(tmp_path, capsys):
    pdfs, labels, _, _ = env(tmp_path, None)
    assert main(cli_args(tmp_path, labels, pdfs, "--yes")) == 2
    assert "evaluation unavailable" in capsys.readouterr().err


def test_cli_yes_prints_table_and_writes_json(tmp_path, monkeypatch, capsys):
    pdfs, labels, _, ai = env(tmp_path, resp(overwritten()))
    monkeypatch.setattr(cli, "_anthropic_client", lambda: (ai, None))
    assert main(cli_args(tmp_path, labels, pdfs, "--yes", "--limit", "1")) == 0
    assert "precision" in capsys.readouterr().out
    written = list((tmp_path / "db" / "eval").glob("*.json"))
    data = json.loads(written[0].read_text())
    assert data["score"]["V1"]["tp"] == 1 and "a.pdf" in data["files"]


def test_cli_cached_with_empty_expectations(tmp_path, capsys):
    pdfs, _, _, _ = env(tmp_path, None)
    labels = tmp_path / "scaffold.jsonl"
    labels.write_text(json.dumps(label("a.pdf", {}, verified=False)))
    assert main(cli_args(tmp_path, labels, pdfs, "--cached")) == 1
    out = capsys.readouterr().out
    assert "no verified labels yet - fill tests/golden/labels.jsonl" in out


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_cli_limit_must_be_positive(tmp_path, capsys, limit):
    pdfs, labels, _, _ = env(tmp_path, None)
    with pytest.raises(SystemExit) as ei:
        main(cli_args(tmp_path, labels, pdfs, "--cached", "--limit", limit))
    assert ei.value.code == 2 and "--limit" in capsys.readouterr().err


@pytest.mark.parametrize("expect", ['{"V1": "yes"}', '{"V1": 1}', '["V1"]', 'null'])
def test_load_labels_rejects_non_bool_expect(tmp_path, expect):
    p = tmp_path / "l.jsonl"
    p.write_text('{"file": "a.pdf", "expect": {}}\n{"file": "b.pdf", "expect": %s}\n' % expect)
    with pytest.raises(ValueError, match=r"l\.jsonl:2: .*expect"):
        load_labels(p)


def test_estimate_uses_planned_tiles_and_10k_output(tmp_path):
    from PIL import Image

    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    a3 = [Image.new("RGB", (3306, 2338), "white") for _ in range(2)]  # 2 pages -> 4 tiles
    a3[0].save(pdfs / "a3.pdf", "PDF", save_all=True, append_images=a3[1:], resolution=200)
    make_pdf(pdfs / "a4.pdf")  # 1 page -> 1 tile
    forms, tiles, usd = estimate_cost([{"file": "a3.pdf"}, {"file": "a4.pdf"}, {"file": "x.pdf"}],
                                      pdfs, Config())
    assert (forms, tiles) == (2, 5)
    assert usd == pytest.approx((5 * 5000 * 4 + 2 * 10_000 * 20) / 1e6)


def test_cli_estimate_over_remaining_cap_refused_without_force(tmp_path, monkeypatch, capsys):
    pdfs, labels, _, ai = env(tmp_path, resp(overwritten()))
    monkeypatch.setattr(cli, "_anthropic_client", lambda: (ai, None))
    db = tmp_path / "db" / "x.sqlite"
    db.parent.mkdir()
    s = Store(db)
    s.save_failed_analysis("file:///x.pdf", "m", "v2", 19.9, "x", parent="eval")
    s.close()
    assert main(cli_args(tmp_path, labels, pdfs, "--yes")) == 2
    cap = capsys.readouterr()
    assert "estimate, actual may differ" in cap.out
    assert "remaining cap $0.10" in cap.out and "--force-over-cap" in cap.err and ai.calls == 0
    assert main(cli_args(tmp_path, labels, pdfs, "--yes", "--force-over-cap")) == 1
    assert ai.calls == 2  # forced: runs (and would still stop once the cap is reached)
