import copy
import json
from types import SimpleNamespace

import pytest

from rg2watch.analyze import RG7_SCHEMA, AnalysisResult, _normalise_rg7, analyze_document
from rg2watch.browse import build_data
from rg2watch.config import Config
from rg2watch.models import Finding
from rg2watch.models import Event
from rg2watch.pipeline import PROMPT_VERSION, RG7_PROMPT_VERSION, AnalysisPipeline
from rg2watch.rules import RULE_INFO, evaluate_rg7
from rg2watch.store import Store
from rg2watch.tally import build_tally
from test_browse import P as BP
from test_browse import add, doc as bdoc, store as bstore
from test_pipeline import ELECTION, Env, FakeAnthropic, PRE_CALIBRATION, doc as pdoc
from test_rules import clean_report, nf

M = "claude-sonnet-5-5"


def rg7(changes=(), table=(), conf="high", date="14.10.2026", station="37"):
    return {"decision_date": date, "station_no": station, "reason": "arithmetic error", "confidence": conf,
            "changes": [{"item": i, "old": o, "new": n} for i, o, n in changes],
            "table": [{"item": i, "value": v} for i, v in table]}


NEGOTIN = rg7([("10.2", 180, 178), ("10.4", 177, 175), ("list:1", 146, 144)])  # from the RG-7-2 of BM 37
ZAJECAR = rg7([("3", 1704, 1703), ("10.3", 17, 16)])


def cfg():
    return Config(**PRE_CALIBRATION)


def d7(no="1", dt=20, url="/e/r7.pdf", up="2026-10-20T10:00:00Z"):
    return bdoc(url, no=no, dt=dt, up=up)


def test_schema_small_flat_strict_and_nameless():
    from anthropic.lib._parse._transform import transform_schema

    transform_schema(RG7_SCHEMA)
    assert len(json.dumps(RG7_SCHEMA)) < 1500
    assert "name" not in json.dumps(RG7_SCHEMA).replace("station_no", "")

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object":
                assert n["additionalProperties"] is False and set(n["required"]) == set(n["properties"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(RG7_SCHEMA)


def test_normalise_canonical_items_and_first_entry_wins():
    raw = rg7([("List 2", 5, 6), ("10,2.", 180, 178), ("10.2", 1, 2), ("row:3", None, 4)],
              [("LISTA 1", 144), ("10.4", 175), ("10.4", 9)])
    n = _normalise_rg7(raw)
    assert [(c["item"], c["old"], c["new"]) for c in n["changes"]] == [
        ("list:2", 5, 6), ("10.2", 180, 178), ("list:3", None, 4)]
    assert [(e["item"], e["value"]) for e in n["table"]] == [("list:1", 144), ("10.4", 175)]


def test_analyze_document_rg7_kind_uses_its_schema_and_prompt():
    seen = {}

    def stream(**kw):
        import contextlib

        seen.update(kw)

        @contextlib.contextmanager
        def cm():
            yield SimpleNamespace(get_final_message=lambda: SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(rg7([("List 1", 146, 144)])))],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5), stop_reason="end_turn", model="m"))
        return cm()

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    res = analyze_document([SimpleNamespace(name="p1", jpeg=b"x")], client, Config(fallbacks=False), "rg7")
    assert seen["output_config"]["format"]["schema"] == RG7_SCHEMA
    assert "RG-7" in seen["messages"][0]["content"][-1]["text"]
    assert res.report["changes"][0]["item"] == "list:1"


def test_r1_high_for_results_and_lists_medium_for_other_items():
    f = evaluate_rg7(NEGOTIN, d7(), cfg())
    assert [(x.rule, x.severity) for x in f] == [("R1", "HIGH")]
    assert f[0].summary == "RG-7 corrected 10.2 180->178; 10.4 177->175; list:1 146->144"
    assert evaluate_rg7(ZAJECAR, d7(), cfg())[0].severity == "HIGH"  # 10.3 is a result item
    only3 = evaluate_rg7(rg7([("3", 489, 488)]), d7(), cfg())
    assert [(x.rule, x.severity) for x in only3] == [("R1", "MEDIUM")]
    other = evaluate_rg7(rg7([("11", 5, 4)]), d7(), cfg())
    assert other[0].severity == "MEDIUM"


def test_low_confidence_downgrades_high():
    f = evaluate_rg7(rg7([("10.2", 180, 178)], conf="low"), d7(), cfg())[0]
    assert f.severity == "MEDIUM" and f.summary.endswith("(low-confidence read)")


def test_r3_when_no_changes():
    f = evaluate_rg7(rg7(), d7(), cfg())
    assert [(x.rule, x.severity) for x in f] == [("R3", "LOW")]


def test_r2_compares_old_with_the_rg2_reading():
    rg2 = clean_report()
    rg2["numbers"]["ballots_in_box"] = nf(180)
    rg2["numbers"]["valid"] = nf(170)
    rg2["numbers"]["list_votes"] = [{"row": 1, "field": nf(150)}]
    f = {x.rule: x for x in evaluate_rg7(NEGOTIN, d7(), cfg(), rg2)}
    assert f["R2"].severity == "MEDIUM"
    assert f["R2"].summary == "RG-7 vs RG-2 - 10.4: decision old 177, RG-2 read 170; list:1: decision old 146, RG-2 read 150"
    rg2["numbers"]["valid"] = nf(None, legible=False)  # unreadable: nothing to compare
    rg2["numbers"]["list_votes"] = [{"row": 1, "field": nf(146)}]
    assert "R2" not in {x.rule for x in evaluate_rg7(NEGOTIN, d7(), cfg(), rg2)}
    assert [x.rule for x in evaluate_rg7(NEGOTIN, d7(), cfg())] == ["R1"]  # no RG-2 yet: skipped


def test_rule_info_has_r_codes():
    assert {"R1", "R2", "R3"} <= set(RULE_INFO)


# tally ------------------------------------------------------------------------------------

def put(s, d, rep, model=M, version=PROMPT_VERSION):
    if "header" in rep:
        rep = {**rep, "header": {**rep["header"], "station_no": d.station_no}}
    s.save_analysis(d.url, AnalysisResult(rep, 0, 0, 0.0, model, "stop"), version, parent="adhoc")


def station(s, rep, dt7=(), no="37", **num):
    r = copy.deepcopy(rep)
    r["numbers"].update(num)
    d = bdoc(f"/e/rg2-{no}.pdf", no=no)
    add(s, d)
    put(s, d, r)
    for i, (rep7, up) in enumerate(dt7):
        x = d7(no, dt=20 + i, url=f"/e/rg7-{no}-{i}.pdf", up=up)  # RG-7-2, RG-7-3: one active per type
        add(s, x)
        put(s, x, rep7, version=RG7_PROMPT_VERSION)
    return d


def tally(s):
    return build_tally(s, Config(tally_scope={BP: "national"}), BP, M)


def test_tally_applies_corrections_and_rechecks_arithmetic():
    s = bstore()
    # RG-2 as filed: 178 voted but 180 in the box (A1 HIGH); the decision fixes the box
    station(s, clean_report(), [(rg7([("10.2", 180, 178), ("10.3", 20, 18), ("10.4", 160, 160)]), "2026-10-20T10:00:00Z")],
            voted=nf(178), ballots_in_box=nf(180), invalid=nf(20), valid=nf(160),
            list_votes=[{"row": 1, "field": nf(100)}, {"row": 2, "field": nf(60)}])
    t = tally(s)
    o = t["overall"]
    assert o["clean"] == 1 and t["excluded"] == []  # 18 + 160 = 178: arithmetic ok after correction
    assert o["all"]["ballots_in_box"] == 178 and o["all"]["invalid"] == 18
    assert t["corrected"] == [{"id": t["corrected"][0]["id"], "m": "Сечањ", "no": "37", "rg7": [
        {"d": "20/10", "ch": ["10.2 180->178", "10.3 20->18", "10.4 160->160"]}]}]
    assert o["corrected"] == 1


def test_tally_latest_decision_wins_and_table_is_final():
    s = bstore()
    station(s, clean_report(), [
        (rg7([("10.3", 20, 19), ("10.4", 780, 781)]), "2026-10-20T10:00:00Z"),
        (rg7([("10.3", 19, 18)], [("10.4", 782)]), "2026-10-27T10:00:00Z")],
        invalid=nf(20), valid=nf(780), ballots_in_box=nf(800))
    t = tally(s)
    assert (t["overall"]["all"]["invalid"], t["overall"]["all"]["valid"]) == (18, 782)
    assert [d["d"] for d in t["corrected"][0]["rg7"]] == ["20/10", "27/10"]


def test_tally_keeps_visual_alerts_but_not_corrected_arithmetic():
    s = bstore()
    station(s, clean_report(), [(rg7([("10.2", 810, 800)]), "2026-10-20T10:00:00Z")],
            ballots_in_box=nf(810), voted=nf(800, mark="overwritten", original=700))
    t = tally(s)
    assert t["overall"]["clean"] == 0
    assert t["excluded"][0]["why"] == ["HIGH alert: V1"]  # no A1: the box now reads 800


def test_tally_ignores_unanalysed_and_other_station_decisions():
    s = bstore()
    station(s, clean_report(), [(rg7([("10.4", 780, 700)]), "2026-10-20T10:00:00Z")], no="1")
    d = station(s, clean_report(), no="2")
    x = d7("2", url="/e/never.pdf")
    add(s, x)  # an RG-7 not analysed yet
    t = tally(s)
    assert [c["no"] for c in t["corrected"]] == ["1"] and d is not None


def test_tally_cache_follows_new_decisions():
    s = bstore()
    station(s, clean_report(), no="1")
    cache: dict = {}
    c = Config(tally_scope={BP: "national"})
    assert build_tally(s, c, BP, M, cache=cache)["overall"]["all"]["valid"] == 780
    x = d7("1", url="/e/late.pdf")
    add(s, x)
    put(s, x, rg7([("10.4", 780, 700), ("10.3", 20, 100), ("10.2", 800, 800)]), version=RG7_PROMPT_VERSION)
    assert build_tally(s, c, BP, M, cache=cache)["overall"]["all"]["valid"] == 700


def test_cli_format_marks_corrected_with_star():
    from rg2watch.tally import format_tally

    s = bstore()
    station(s, clean_report(), [(NEGOTIN, "2026-10-20T10:00:00Z")], no="1")
    text = format_tally(tally(s))
    assert "1*" in text and "* corrected by RG-7" in text and "RG-7 20/10: 10.2 180->178" in text


# page -------------------------------------------------------------------------------------

def test_page_data_marks_corrections(tmp_path):
    s = bstore()
    station(s, clean_report(), [(NEGOTIN, "2026-10-20T10:00:00Z")], no="1")
    x = d7("1", dt=21, url="/e/pending.pdf", up="2026-10-21T10:00:00Z")
    add(s, x)  # not read yet
    s.add_finding(BP, Finding("R1", "HIGH", d7("1", url="/e/rg7-1-0.pdf"), "RG-7 corrected x"), "/e/rg7-1-0.pdf")
    data = build_data(s, tmp_path, cfg=Config(model=M))
    assert [r["no"] for r in data["rows"]] == ["1"]  # decisions are badges, not rows
    r7 = data["rows"][0]["r7"]
    assert [(x["d"], x["ch"]) for x in r7] == [
        ("20/10", ["10.2 180->178", "10.4 177->175", "list:1 146->144"]), ("21/10", None)]
    assert r7[0]["f"] == [["R1", "HIGH", "RG-7 corrected x"]] and r7[0]["rik"].endswith("rg7-1-0.pdf")
    assert "R1" in data["rules"]


# pipeline ---------------------------------------------------------------------------------

def rg7_reply(report):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(report))],
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
                           stop_reason="end_turn", model="claude-opus-5-5")


def test_pipeline_analyses_rg7_with_its_own_prompt_version_and_rules(tmp_path):
    e = Env(tmp_path, rg7_reply(NEGOTIN))
    d = pdoc(dt=20)
    e.publish(d)
    assert e.drain() == 1
    assert e.rules() == ["R1"] and e.status(d.url) == ("done", 1)
    assert e.store._db.execute("SELECT prompt_version FROM analyses").fetchone()[0] == RG7_PROMPT_VERSION == "r1"
    assert e.store.total_spend() > 0


def test_pipeline_rg2_unchanged_and_other_types_not_analysed(tmp_path):
    e = Env(tmp_path, rg7_reply(NEGOTIN))
    e.publish(pdoc("b.pdf", dt=8))  # RG-8 stays out
    assert e.drain() == 0


def _rg2_reply():
    from test_pipeline import resp

    rg2 = clean_report()
    rg2["header"]["station_no"] = "1"  # ballots in box 800
    return resp(rg2)


def test_pipeline_r2_when_rg2_was_read_first(tmp_path):
    changed = rg7([("10.2", 810, 800)])
    e = Env(tmp_path, _rg2_reply())
    e.publish(pdoc("a.pdf", dt=6))
    e.drain()
    e.ai.reply = rg7_reply(changed)
    e.publish(pdoc("b.pdf", dt=20))
    e.drain()
    assert e.rules() == ["R1", "R2"]


def test_pipeline_r2_when_rg2_arrives_after_the_decision(tmp_path):
    changed = rg7([("10.2", 810, 800)])
    e = Env(tmp_path, rg7_reply(changed))
    e.publish(pdoc("b.pdf", dt=20))
    e.drain()
    assert e.rules() == ["R1"]
    e.ai.reply = _rg2_reply()
    e.publish(pdoc("a.pdf", dt=6))
    e.drain()
    assert e.rules() == ["R1", "R2"]


def test_pipeline_queues_rg7_recorded_before_the_feature(tmp_path):
    e = Env(tmp_path, rg7_reply(NEGOTIN))
    s = Store(":memory:")  # a rehearsal DB: RG-7 recorded (skipped) when analysis was enabled earlier
    s.upsert_election(ELECTION)
    old, other, done = pdoc("a.pdf", dt=20, no="1"), pdoc("b.pdf", dt=20, no="2"), pdoc("c.pdf", dt=21, no="3")
    s.apply_events(P := ELECTION.parent, s.start_sweep(P, "t"), [Event("added", x) for x in (old, other, done)])
    s.enable_analysis(P)
    s._db.execute("UPDATE documents SET analysis_status='skipped', first_seen_at='2026-10-01T00:00:00Z'")
    s._db.execute("UPDATE documents SET analysis_status=NULL WHERE url=?", (other.url,))
    s.save_analysis(done.url, AnalysisResult(NEGOTIN, 0, 0, 0.0, "m", "stop"), RG7_PROMPT_VERSION)
    e.store = s
    e.pipe = AnalysisPipeline(e.rik, s, e.notifier, e.ai, e.cfg, e.on_findings, tmp_path / "c", clock=e.clock)
    e.pipe._stop.set()  # start() queues, no worker thread takes the work
    e.pipe.start()
    assert e.status(old.url)[0] == "new" and e.status(other.url)[0] == "new"
    assert e.status(done.url)[0] == "skipped"  # already analysed: left alone
    assert s.unfinished_analysis(P) == 2  # --until-done waits for them
    e.pipe._stop.clear()
    assert e.drain() == 2 and e.ai.calls == 2


def test_cli_analyze_doc_type_rg7_prints_changes(tmp_path, monkeypatch, capsys):
    from rg2watch import cli
    from test_pipeline import make_pdf

    pdf = make_pdf(tmp_path / "x.pdf")
    monkeypatch.setattr(cli, "_anthropic_client", lambda: (FakeAnthropic(rg7_reply(NEGOTIN)), None))
    assert cli.main(["analyze", str(pdf), "--doc-type", "rg7", "--db", str(tmp_path / "d.sqlite")]) == 0
    out = capsys.readouterr().out
    assert "changes: 10.2 180->178; 10.4 177->175; list:1 146->144" in out and "[HIGH] R1" in out
    s = Store(tmp_path / "d.sqlite")
    assert s._db.execute("SELECT prompt_version FROM analyses").fetchone()[0] == "r1"
    s.close()
