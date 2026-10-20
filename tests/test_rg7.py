import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from rg2watch.analyze import RG7_SCHEMA, AnalysisResult, _normalise_rg7, analyze_document
from rg2watch.browse import build_data, build_page
from rg2watch.config import Config
from rg2watch.models import Finding
from rg2watch.models import Event
from rg2watch.pipeline import PROMPT_VERSION, RG7_PROMPT_VERSION, AnalysisPipeline
from rg2watch.rules import RULE_INFO, evaluate_rg7
from rg2watch.store import Store
from rg2watch.status import build_status
from rg2watch.tally import build_tally
from test_browse import P as BP
from test_browse import add, doc as bdoc, store as bstore
from test_pipeline import ELECTION, Env, FakeAnthropic, PRE_CALIBRATION, doc as pdoc
from test_rules import clean_report, nf

M = "claude-sonnet-5-5"


def rg7(changes=(), table=(), conf="high", date=None, station="37"):
    return {"decision_date": date, "station_no": station, "reason": "arithmetic error", "confidence": conf,
            "changes": [{"item": i, "old": o, "new": n} for i, o, n in changes],
            "table": [{"item": i, "value": v} for i, v in table]}


NEGOTIN = rg7([("10.2", 180, 178), ("10.4", 177, 175), ("list:1", 146, 144)], date="14.10.2026")  # BM 37, RG-7-2
# the RG-2 it corrects, as filed
NEG_RG2 = dict(voted=nf(180), ballots_in_box=nf(180), invalid=nf(3), valid=nf(177),
               list_votes=[{"row": 1, "field": nf(146)}, {"row": 2, "field": nf(31)}])
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
        ("list:2", 5, 6), ("ballots_in_box", 180, 178), ("list:3", None, 4)]  # old r1 numbering maps
    assert [(e["item"], e["value"]) for e in n["table"]] == [("list:1", 144), ("valid", 175)]
    legacy_other = _normalise_rg7(rg7([("11.1", 6, 73), ("12", 1, 2)]))
    assert [c["item"] for c in legacy_other["changes"]] == ["other: 11.1", "other: 12"]


def r2_entry(field, old, new, item="", row=None, label=""):
    return {"field": field, "row": row, "label": label, "item": item, "old": old, "new": new}


def test_normalise_r2_maps_by_field_meaning_not_item_number():
    raw = rg7() | {"changes": [r2_entry("voted", 6, 73, "11.1"), r2_entry("valid", 5, 6, "10.4"),
                               r2_entry("list", 1, 2, "11.5", row=3), r2_entry("list", 1, 2, row=None, label="x"),
                               r2_entry("other", 1, 2, "14", label="Posmatraci"),
                               r2_entry("outside_voted", 1, 2, "12")],
                  "table": [{"field": "voted", "row": None, "label": "", "item": "11.1", "value": 73}]}
    n = _normalise_rg7(raw)
    assert [(c["item"], c["printed"]) for c in n["changes"]] == [
        ("voted", "11.1"), ("valid", "10.4"), ("list:3", "11.5"), ("other: x", ""),
        ("other: Posmatraci", "14"), ("outside_voted", "12")]
    assert n["table"] == [{"item": "voted", "printed": "11.1", "value": 73}]


def test_2026_numbering_voted_11_1_is_a_high_applied_result_change():
    from rg2watch.rules import RG7_FIELDS as F
    assert "voted" in F
    n = _normalise_rg7(rg7() | {"changes": [r2_entry("voted", 6, 73, "11.1")]})
    f = evaluate_rg7(n, bdoc("/e/r7.pdf", no="42"), cfg())
    assert [(x.rule, x.severity) for x in f] == [("R1", "HIGH")] and "voted (item 11.1) 6->73" in f[0].summary
    s = bstore()
    station(s, clean_report(), [(n, "2026-10-20T10:00:00Z")], voted=nf(6), ballots_in_box=nf(6), invalid=nf(0),
            valid=nf(6), list_votes=[{"row": 1, "field": nf(6)}])
    o = tally(s)["overall"]
    assert o["all"]["voted"] == 73 and o["fall"]["voted"] == 6


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
        {"d": "20/10/2026", "ch": ["10.2 180->178", "10.3 20->18", "10.4 160->160"],
         "u": t["corrected"][0]["rg7"][0]["u"]}]}]
    assert t["corrected"][0]["rg7"][0]["u"]
    assert o["corrected"] == 1


def test_tally_latest_decision_wins_and_table_is_final():
    s = bstore()
    station(s, clean_report(), [
        (rg7([("10.3", 20, 19), ("10.4", 780, 781)]), "2026-10-20T10:00:00Z"),
        (rg7([("10.3", 19, 18)], [("10.4", 782)]), "2026-10-27T10:00:00Z")],
        invalid=nf(20), valid=nf(780), ballots_in_box=nf(800))
    t = tally(s)
    assert (t["overall"]["all"]["invalid"], t["overall"]["all"]["valid"]) == (0, 0)
    assert t["overall"]["all"]["withheld_valid"] == 782
    assert [d["d"] for d in t["corrected"][0]["rg7"]] == ["20/10/2026", "27/10/2026"]


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
    changed = build_tally(s, c, BP, M, cache=cache)["overall"]["all"]
    assert changed["valid"] == 0 and changed["withheld_valid"] == 700


def test_cli_format_marks_corrected_with_star():
    from rg2watch.tally import format_tally

    s = bstore()
    station(s, clean_report(), [(NEGOTIN, "2026-10-20T10:00:00Z")], no="1", **NEG_RG2)
    text = format_tally(tally(s))
    assert "1*" in text and "* corrected by RG-7" in text and "RG-7 14/10/2026: 10.2 180->178" in text


# page -------------------------------------------------------------------------------------

def test_page_data_marks_corrections(tmp_path):
    s = bstore()
    station(s, clean_report(), [(NEGOTIN, "2026-10-20T10:00:00Z")], no="1", **NEG_RG2)
    x = d7("1", dt=21, url="/e/pending.pdf", up="2026-10-21T10:00:00Z")
    add(s, x)  # not read yet
    s.add_finding(BP, Finding("R1", "HIGH", d7("1", url="/e/rg7-1-0.pdf"), "RG-7 corrected x"), "/e/rg7-1-0.pdf")
    data = build_data(s, tmp_path, cfg=Config(model=M))
    assert [r["no"] for r in data["rows"]] == ["1"]  # decisions are badges, not rows
    r7 = data["rows"][0]["r7"]
    assert [(x["d"], x["ch"]) for x in r7] == [
        ("14/10/2026", ["10.2 180->178", "10.4 177->175", "list:1 146->144"]), ("21/10/2026", None)]
    assert r7[0]["f"] == [["R1", "HIGH", "RG-7 corrected x"]] and r7[0]["rik"].endswith("rg7-1-0.pdf")
    assert "R1" in data["rules"]


def test_rg7_alert_links_nested_orphan_and_other_types(tmp_path):
    s = bstore()
    station(s, clean_report(), [(NEGOTIN, "2026-10-20T10:00:00Z")], no="1", **NEG_RG2)
    nested_url = "/e/rg7-1-0.pdf"
    nested = d7("1", url=nested_url)
    s.add_finding(BP, Finding("R1", "HIGH", nested, "nested"), nested_url)

    orphan_url = "/e/orphan.pdf"
    orphan = d7("99", url=orphan_url)
    add(s, orphan)
    s.add_finding(BP, Finding("R1", "HIGH", orphan, "orphan"), orphan_url)
    unsafe = d7("98", url="javascript:alert(1)")
    add(s, unsafe)
    s.add_finding(BP, Finding("R1", "HIGH", unsafe, "unsafe"), unsafe.url)
    rg8 = d7("97", dt=8, url="/e/rg8.pdf")
    add(s, rg8)
    s.add_finding(BP, Finding("X8", "LOW", rg8, "rg8"), rg8.url)
    rg2 = bdoc("/e/rg2-plain.pdf", no="96")
    add(s, rg2)
    s.add_finding(BP, Finding("X2", "LOW", rg2, "rg2"), rg2.url)

    tmp_path.mkdir(exist_ok=True)
    cached = {}
    for url in (nested_url, orphan_url, rg8.url):
        digest = hashlib.sha256(url.encode()).hexdigest()
        (tmp_path / (digest + ".pdf")).write_bytes(b"%PDF")
        cached[url] = digest

    data = build_data(s, tmp_path)
    nested_row = next(r for r in data["rows"] if r["no"] == "1")
    decision = next(x for x in nested_row["r7"] if x["u"] == nested_url)
    assert decision["rik"] == "https://rik.example/e/rg7-1-0.pdf"
    assert decision["pdf"] == cached[nested_url]
    assert not any(r["no"] in ("98", "99") for r in data["rows"])
    assert not any(x["u"] in (orphan_url, unsafe.url) for r in data["rows"] for x in r["r7"])

    alerts = {x[5]: x for x in build_status(s, BP, cache_dir=tmp_path)["alerts"]}
    assert alerts["nested"][7:] == [nested_url, cached[nested_url], "https://rik.example/e/rg7-1-0.pdf"]
    assert alerts["orphan"][7:] == [orphan_url, cached[orphan_url], "https://rik.example/e/orphan.pdf"]
    assert alerts["unsafe"][8:] == [None, None]
    assert alerts["rg8"][8:] == [None, None]
    assert alerts["rg2"][8:] == [None, None] and len(alerts["rg2"]) == 10

    out = tmp_path / "web" / "index.html"
    build_page(s, out, tmp_path)
    written = {x[5]: x for x in json.loads((out.parent / "status.json").read_text())["alerts"]}
    assert written["orphan"][8] == cached[orphan_url]

    (tmp_path / (cached[orphan_url] + ".pdf")).unlink()
    alerts = {x[5]: x for x in build_status(s, BP, cache_dir=tmp_path)["alerts"]}
    assert alerts["orphan"][8] is None


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
    assert e.store._db.execute("SELECT prompt_version FROM analyses").fetchone()[0] == RG7_PROMPT_VERSION == "r2"
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
    assert "changes: ballots_in_box (item 10.2) 180->178; valid (item 10.4) 177->175; list:1 146->144" in out and "[HIGH] R1" in out
    s = Store(tmp_path / "d.sqlite")
    assert s._db.execute("SELECT prompt_version FROM analyses").fetchone()[0] == "r2"
    s.close()


# review fixes -------------------------------------------------------------------------------

def test_parse_decision_date_and_ordering_keys():
    from datetime import date

    from rg2watch.rules import decision_label, decision_sort_key, parse_decision_date as p

    assert p("14.10.2026") == p("14.10.2026. године") == p(" 14. 10. 2026.") == date(2026, 10, 14)
    assert p("9. јуна 2025. године") == p("9 juna 2025") == p("9. Јуна 2025") == date(2025, 6, 9)
    assert p("31.02.2026") is None and p("sometime") is None and p(None) is None and p("5. foo 2025") is None
    assert decision_label("2026-10-20T10:00:00Z", rg7(date="9. јуна 2025.")) == "09/06/2025"
    assert decision_label("2026-10-20T10:00:00Z", rg7()) == "20/10/2026"
    assert decision_label(None, rg7()) == "?"
    keys = [decision_sort_key(None, 1, rg7()), decision_sort_key("2026-10-20", 3, rg7()),
            decision_sort_key("2026-10-21", 2, rg7(date="01.10.2026")), decision_sort_key("2026-10-20", 2, rg7())]
    assert sorted(range(4), key=keys.__getitem__) == [2, 3, 1, 0]  # parsed date, upload, id; NULL upload last


def test_normalise_restricts_items_and_cleans_text():
    n = _normalise_rg7(rg7([("10.5 row 2", 1, 2), ("10.5.3", 1, 2), ("10.5/4", 1, 2), ("10.5", 1, 2),
                            ("11", 5, 4), ("X" * 100, 1, 2), ("3", 1, 2)])
                       | {"reason": "a\x00b\nc " + "z" * 300, "station_no": "3\t7"})
    assert [c["item"] for c in n["changes"]] == ["list:2", "list:3", "list:4", "other: 10.5", "other: 11",
                                                  "other: " + "x" * 40, "registered"]
    assert len(n["reason"]) == 200 and "\x00" not in n["reason"] and "\n" not in n["reason"]
    assert n["station_no"] == "3 7"


def test_low_confidence_decision_is_not_applied_and_flags_the_form():
    s = bstore()
    station(s, clean_report(), [(rg7([("10.4", 780, 700)], conf="low"), "2026-10-20T10:00:00Z")])
    t = tally(s)
    assert t["overall"]["all"]["valid"] == 780 and t["corrected"] == [] and t["overall"]["clean"] == 0
    assert t["excluded"][0]["why"] == ["unverified RG-7 correction, RG-2 numbers kept (20/10/2026: low-confidence read)"]


def test_decision_contradicting_the_rg2_is_not_applied_and_flags_the_form():
    s = bstore()
    station(s, clean_report(), [(rg7([("10.4", 900, 700)]), "2026-10-20T10:00:00Z")])
    t = tally(s)
    assert t["overall"]["all"]["valid"] == 780 and t["corrected"] == []
    assert "unverified RG-7 correction" in t["excluded"][0]["why"][0] and "old 900, RG-2 read 780" in t["excluded"][0]["why"][0]


def test_null_new_number_is_not_a_correction():
    s = bstore()
    station(s, clean_report(), [(rg7([("10.4", 780, None)]), "2026-10-20T10:00:00Z")])
    t = tally(s)
    assert t["corrected"] == [] and t["overall"]["all"]["valid"] == 780 and t["overall"]["clean"] == 1


def test_correction_does_not_rescue_an_illegible_field_unless_the_table_gives_it():
    s = bstore()
    station(s, clean_report(), [(rg7([("10.4", None, 700)]), "2026-10-20T10:00:00Z")],
            valid=nf(None, legible=False))
    t = tally(s)
    assert t["corrected"] == [] and "illegible or empty: valid illegible" in t["excluded"][0]["why"][0]
    s2 = bstore()
    station(s2, clean_report(), [(rg7([("10.4", None, 700)], [("10.4", 700)]), "2026-10-20T10:00:00Z")],
            valid=nf(None, legible=False))
    t2 = tally(s2)
    assert t2["overall"]["all"]["valid"] == 0 and t2["overall"]["all"]["withheld_valid"] == 700
    assert [c["rg7"][0]["ch"] for c in t2["corrected"]] == [["10.4 table 700"]]


def test_decisions_are_ordered_by_decision_date_not_upload_order():
    s = bstore()
    # uploaded first but dated later; by upload order its old (19) would contradict the RG-2 (20)
    later = rg7([("10.3", 19, 18)], date="05.10.2026")
    earlier = rg7([("10.3", 20, 19)], date="01.10.2026")
    station(s, clean_report(), [(later, "2026-10-20T10:00:00Z"), (earlier, "2026-10-21T10:00:00Z")], invalid=nf(20))
    t = tally(s)
    assert t["overall"]["all"]["invalid"] == 18 and t["excluded"][0]["why"] == ["arithmetic A2"]  # applied, 18 + 780 != 800
    assert [d["d"] for d in t["corrected"][0]["rg7"]] == ["01/10/2026", "05/10/2026"]


def test_failure_while_applying_falls_back_to_the_rg2_numbers(monkeypatch, caplog):
    import rg2watch.tally as tl

    monkeypatch.setattr(tl, "apply_rg7", lambda *a: 1 / 0)
    s = bstore()
    station(s, clean_report(), [(rg7([("10.4", 780, 700)]), "2026-10-20T10:00:00Z")])
    with caplog.at_level("WARNING", logger="rg2watch.tally"):
        t = tally(s)
    assert t["overall"]["counted"] == 1 and t["overall"]["all"]["valid"] == 780 and t["corrected"] == []
    assert "could not be applied" in caplog.text


def test_r2_compares_with_the_previous_decision_when_there_is_one():
    rg2 = clean_report()  # ballots in box 800
    first = rg7([("10.2", 800, 790)])
    second = rg7([("10.2", 790, 780)])
    assert [f.rule for f in evaluate_rg7(second, d7(), cfg(), rg2, [first])] == ["R1"]  # agrees with 790
    bad = rg7([("10.2", 800, 780)])  # agrees with the RG-2 but not with the previous decision
    f = evaluate_rg7(bad, d7(), cfg(), rg2, [first])
    assert [x.rule for x in f] == ["R1", "R2"] and "previous decision new 790" in f[1].summary
    assert [x.rule for x in evaluate_rg7(bad, d7(), cfg(), rg2)] == ["R1"]
    assert [x.rule for x in evaluate_rg7(bad, d7(), cfg(), None, [first])] == ["R1", "R2"]  # no RG-2 yet


def test_pipeline_recheck_failure_never_touches_the_rg2_outcome(tmp_path, monkeypatch):
    e = Env(tmp_path, _rg2_reply())
    monkeypatch.setattr(e.pipe, "_rg7_rechecks", lambda *a: 1 / 0)
    d = pdoc("a.pdf", dt=6)
    e.publish(d)
    assert e.drain() == 1 and e.status(d.url) == ("done", 1)


def test_pipeline_queries_only_the_stations_decisions(tmp_path, monkeypatch):
    e = Env(tmp_path, rg7_reply(NEGOTIN))
    seen = []
    orig = e.store.rg7_reports
    monkeypatch.setattr(e.store, "rg7_reports", lambda *a, **kw: seen.append(kw) or orig(*a, **kw))
    e.publish(pdoc("b.pdf", dt=20))
    e.drain()
    e.ai.reply = _rg2_reply()
    e.publish(pdoc("a.pdf", dt=6))
    e.drain()
    assert seen and all(kw.get("station_key") == "Alpha|1" for kw in seen)


def test_pipeline_prior_decision_feeds_r2(tmp_path):
    e = Env(tmp_path, rg7_reply(rg7([("10.2", 800, 790)], date="01.10.2026")))
    e.publish(pdoc("a.pdf", dt=20))
    e.drain()
    e.ai.reply = rg7_reply(rg7([("10.2", 800, 780)], date="05.10.2026"))  # contradicts the previous new 790
    e.publish(pdoc("b.pdf", dt=21))
    e.drain()
    assert e.rules() == ["R1", "R1", "R2"]


def test_rg7_stays_out_of_rg2_backlog_and_counts_separately(tmp_path):
    s = bstore()
    r2, x = bdoc("/e/a.pdf", no="1"), d7("1", url="/e/x.pdf")
    add(s, r2)
    add(s, x)
    s._db.execute("UPDATE documents SET analysis_status='skipped'")
    assert s.analysis_backlog(BP) == 1 and s.queued_for_analysis(BP) == 0
    assert s.status_counts(BP)["rg7"] == {"read": 0, "pending": 0}
    assert s.requeue_skipped(BP) == 1  # the RG-7 is not part of the RG-2 backlog
    s.requeue_rg7(BP)
    assert s.status_counts(BP)["rg7"] == {"read": 0, "pending": 1}
    s.mark_analysis(x.url, "done")
    assert s.status_counts(BP)["rg7"] == {"read": 1, "pending": 0}
    assert s.run_summary(BP)["skipped"] == 0


def test_page_shows_which_model_read_a_decision(tmp_path):
    s = bstore()
    station(s, clean_report(), [], no="1")
    x = d7("1", url="/e/other.pdf")
    add(s, x)
    put(s, x, NEGOTIN, model="some-other-model", version=RG7_PROMPT_VERSION)
    r7 = build_data(s, tmp_path, cfg=Config(model=M))["rows"][0]["r7"]
    assert r7[0]["ch"] is None and r7[0]["by"] == ["some-other-model"]


def test_rg7_analysed_with_the_rg2_prompt_is_reread_and_bogus_findings_removed(tmp_path, caplog):
    e = Env(tmp_path, rg7_reply(NEGOTIN))
    s = Store(":memory:")
    s.upsert_election(ELECTION)
    P = ELECTION.parent
    bad, ok, failed, rg2, wrong_failed = (pdoc("a.pdf", dt=20, no="1"), pdoc("b.pdf", dt=19, no="2"),
                                          pdoc("c.pdf", dt=21, no="3"), pdoc("d.pdf", dt=6, no="4"),
                                          pdoc("e.pdf", dt=22, no="5"))
    s.apply_events(P, s.start_sweep(P, "t"), [Event("added", x) for x in (bad, ok, failed, rg2, wrong_failed)])
    s.enable_analysis(P)
    # a half-applied run: the RG-7 went through the RG-2 prompt and was marked done
    s.save_analysis(bad.url, AnalysisResult(clean_report(), 1, 1, 0.0, "claude-opus-5-5", "stop"), PROMPT_VERSION)
    s.save_analysis(ok.url, AnalysisResult(NEGOTIN, 1, 1, 0.0, "claude-opus-5-5", "stop"), RG7_PROMPT_VERSION)
    for x in (bad, ok, rg2):
        s.mark_analysis(x.url, "done")
    s.mark_analysis(failed.url, "failed", "boom")  # genuinely failed RG-7: stays failed
    s.save_failed_analysis(wrong_failed.url, "m", PROMPT_VERSION, 0.1, "x")  # wrong-path signature
    s.mark_analysis(wrong_failed.url, "failed", "boom")
    for rule, sev in (("V3", "HIGH"), ("V9", "HIGH"), ("D1", "MEDIUM"), ("R3", "LOW"), ("X1", "LOW")):
        s.add_finding(P, Finding(rule, sev, bad, rule), bad.url)
    s.add_finding(P, Finding("V3", "HIGH", rg2, "real"), rg2.url)
    e.store = s
    e.pipe = AnalysisPipeline(e.rik, s, e.notifier, e.ai, e.cfg, e.on_findings, tmp_path / "c", clock=e.clock)
    e.pipe._stop.set()
    e.pipe.start()
    assert e.status(bad.url)[0] == "new" and e.status(wrong_failed.url) == ("new", 0)
    assert e.status(failed.url)[0] == "failed"  # not retried (and billed) on every start
    assert e.status(ok.url)[0] == "done" and e.status(rg2.url)[0] == "done"  # r1-read and RG-2 left alone
    e.pipe._stop.clear()
    with caplog.at_level("WARNING", logger="rg2watch.pipeline"):
        e.drain()
    assert e.ai.calls == 1  # the v7-read decision (the wrong-path failed one has no PDF in the fake: D3)
    rules = lambda d: sorted(r[0] for r in s._db.execute(
        "SELECT rule FROM findings WHERE document_id=(SELECT id FROM documents WHERE url=?)", (d.url,)))
    assert rules(bad) == ["D1", "R1", "R3", "X1"] and rules(rg2) == ["V3"]  # R1 from the re-read
    assert "removed 2 RG-2 finding(s)" in caplog.text
    prompts = {r[0] for r in s._db.execute(
        "SELECT prompt_version FROM analyses a JOIN documents d ON d.id=a.document_id WHERE d.url=?", (bad.url,))}
    assert prompts == {PROMPT_VERSION, RG7_PROMPT_VERSION}


def test_rg7_never_takes_the_rg2_path(tmp_path):
    e = Env(tmp_path, _rg2_reply())  # the reply is an RG-2 report: an RG-7 must be rejected, not stored as one
    d = pdoc("a.pdf", dt=20)
    e.publish(d)
    e.drain()
    assert e.status(d.url)[0] != "done"
    assert e.store._db.execute("SELECT COUNT(*) FROM analyses WHERE prompt_version=?", (PROMPT_VERSION,)).fetchone()[0] == 0


def test_tally_three_views_corrected_filed_and_diff():
    from rg2watch.tally import _delta, format_tally

    s = bstore()
    station(s, clean_report(), [(rg7([("10.2", 180, 178), ("10.3", 20, 18), ("10.4", 160, 160)]), "2026-10-20T10:00:00Z")],
            voted=nf(178), ballots_in_box=nf(180), invalid=nf(20), valid=nf(160),
            list_votes=[{"row": 1, "field": nf(100)}, {"row": 2, "field": nf(60)}])
    t = tally(s)
    o = t["overall"]
    assert o["all"]["ballots_in_box"] == 178 and o["fall"]["ballots_in_box"] == 180  # corrected vs as filed
    assert o["all"]["invalid"] == 18 and o["fall"]["invalid"] == 20
    assert o["clean"] == 1 and o["fclean"] == 0 and o["fexcluded"] == 1  # as filed, A1 flags it
    assert t["excluded"] == [] and "arithmetic" in t["excluded_filed"][0]["why"][0]
    assert o["fcl"]["valid"] == 0 and o["cl"]["valid"] == 160
    d = _delta(o["all"], o["fall"])
    assert d["ballots_in_box"] == -2 and d["invalid"] == -2 and d["valid"] == 0 and d["lists"] == [0, 0]
    assert all(d[k] == o["all"][k] - o["fall"][k] for k in ("registered", "voted", "valid", "outside_voted"))
    assert "numbers with RG-7 corrections" in format_tally(t)
    assert "as filed on the RG-2" in format_tally(t, view="filed") and "*" not in format_tally(t, view="filed").split("\n")[4]
    dt = format_tally(t, view="diff")
    assert "difference" in dt and "-2" in dt


def test_unverified_correction_changes_neither_view():
    from rg2watch.tally import _delta

    s = bstore()
    station(s, clean_report(), [(rg7([("10.2", 999, 178)]), "2026-10-20T10:00:00Z")],  # old number contradicts the RG-2
            voted=nf(178), ballots_in_box=nf(178), invalid=nf(18), valid=nf(160),
            list_votes=[{"row": 1, "field": nf(100)}, {"row": 2, "field": nf(60)}])
    t = tally(s)
    o = t["overall"]
    assert t["corrected"] == [] and o["all"] == o["fall"]  # the numbers are the RG-2 in both views
    assert any("unverified RG-7" in w for w in t["excluded"][0]["why"])  # corrected view flags it
    assert o["cl"]["valid"] == 0 and o["fcl"]["valid"] == 160  # only the flag differs
    assert t["excluded_filed"] == [] and o["fclean"] == 1  # as filed: judged on the RG-2 alone
    assert set(_delta(o["all"], o["fall"])["lists"]) == {0}


def test_requeue_rg7_upgrades_r1_reads_but_never_rebills_failed_docs():
    s = bstore()
    r1_done, r1_failed, r2_done, r2_failed_after_r1 = (d7(str(i), url=f"/e/{i}.pdf") for i in range(4))
    for x in (r1_done, r1_failed, r2_done, r2_failed_after_r1):
        add(s, x)
    ok = lambda x, v: s.save_analysis(x.url, AnalysisResult(NEGOTIN, 1, 1, 0.0, M, "stop"), v)
    ok(r1_done, "r1")
    ok(r2_done, "r2")
    ok(r2_failed_after_r1, "r1")
    s.save_failed_analysis(r1_failed.url, M, "r1", 0.1, "boom")
    s.save_failed_analysis(r2_failed_after_r1.url, M, "r2", 0.1, "boom")
    for x in (r1_done, r2_done):
        s.mark_analysis(x.url, "done")
    for x in (r1_failed, r2_failed_after_r1):
        s.mark_analysis(x.url, "failed", "boom")
    assert s.requeue_rg7(BP, None, "r2") == 1
    st = lambda x: s._db.execute("SELECT analysis_status FROM documents WHERE url=?", (x.url,)).fetchone()[0]
    assert [st(x) for x in (r1_done, r1_failed, r2_done, r2_failed_after_r1)] == ["new", "failed", "done", "failed"]


def test_diff_is_like_for_like_stations_joining_a_total_are_counted_separately():
    from rg2watch.tally import format_tally

    s = bstore()
    station(s, clean_report(), no="1")  # untouched: in both views
    # registered unreadable as filed, given by the RG-7 table: joins the registered/voted total
    station(s, clean_report(), [(rg7(table=[("registered", 500), ("voted", 400)]), "2026-10-20T10:00:00Z")], no="2",
            registered=nf(None, legible=False, mark="empty"))
    t = tally(s)
    m = t["munis"][0]
    base = clean_report()["numbers"]
    assert m["all"]["registered"] == base["registered"]["value"] + 500 and m["fall"]["registered"] == base["registered"]["value"]
    p = m["pair"]["all"]
    assert (p["in"], p["out"]) == (1, 0)
    assert p["c"]["registered"] == p["f"]["registered"] == base["registered"]["value"]  # only station 1 is paired
    assert p["c"]["voted"] == p["f"]["voted"]
    assert t["overall"]["pair"]["all"]["in"] == 1
    text = format_tally(t, view="diff")
    assert "terms corrected-only/filed-only" in text and "+1 / -0" in text
    assert "terms present only in corrected/filed view: +1 / -0" in text
    row = next(r for r in text.splitlines() if r.startswith(m["m"]))
    assert "+500" not in row and "+400" not in row  # not shown as voter deltas


def test_diff_clean_view_status_flip_is_a_station_change_not_a_delta():
    s = bstore()
    station(s, clean_report(), [(rg7([("10.2", 180, 178), ("10.3", 20, 18), ("10.4", 160, 160)]), "2026-10-20T10:00:00Z")],
            voted=nf(178), ballots_in_box=nf(180), invalid=nf(20), valid=nf(160),
            list_votes=[{"row": 1, "field": nf(100)}, {"row": 2, "field": nf(60)}])  # flagged as filed, clean corrected
    o = tally(s)["overall"]["pair"]
    assert (o["cl"]["in"], o["cl"]["out"]) == (1, 0) and o["cl"]["c"]["valid"] == o["cl"]["f"]["valid"] == 0
    assert (o["all"]["in"], o["all"]["out"]) == (0, 0) and o["all"]["c"]["ballots_in_box"] == 178
    assert o["all"]["f"]["ballots_in_box"] == 180


def test_rg7_vote_reconciliation_change_is_one_sided_in_paired_difference():
    from rg2watch.tally import format_tally

    s = bstore()
    station(s, clean_report(), [(rg7([("list:2", 370, 380)]), "2026-10-20T10:00:00Z")],
            list_votes=[{"row": 1, "field": nf(400)}, {"row": 2, "field": nf(370)}])
    o = tally(s)["overall"]
    assert o["all"]["valid"] == 780 and o["fall"]["valid"] == 0
    assert o["fall"]["withheld_valid"] == 780
    p = o["pair"]["all"]
    assert (p["pair_selected"], p["pair_vote_forms"], p["pair_not_contributing"]) == (1, 0, 1)
    assert (p["corrected_only_vote_forms"], p["filed_only_vote_forms"]) == (1, 0)
    assert p["c"]["valid"] == p["f"]["valid"] == 0
    assert p["c"]["lists_valid"] == p["f"]["lists_valid"] == 0
    assert (p["in"], p["out"]) == (1, 1)
    clean = o["pair"]["cl"]
    assert clean["pair_selected"] == clean["pair_not_contributing"] == 0
    text = format_tally(tally(s), view="diff")
    assert "0 of 1 forms selected in both views contributed to compared vote totals" in text
    assert "1 not paired (1 corrected-only eligible, 0 filed-only eligible, 0 neither)" in text
    assert "terms present only in corrected/filed view" in text
    assert "vote-reconciliation eligibility" in text


def test_repeated_identical_correction_is_agreement_and_applied_once():
    from rg2watch.rules import rg7_mismatch
    from rg2watch.tally import apply_rg7

    first = rg7([("10.1", 470, 471)], date="01.10.2026")
    again = rg7([("10.1", 470, 471)], date="02.10.2026")  # Bor BM29: RG-7-2 and a supplementary RG-7-3
    rg2 = clean_report()
    rg2["numbers"]["voted"] = nf(470)
    assert rg7_mismatch(again, rg2, [first]) == []
    assert rg7_mismatch(rg7([("10.1", 470, 472)]), rg2, [first]) != []  # a different new value still mismatches
    out, applied, unverified = apply_rg7(rg2, [{"d": "a", "rep": first}, {"d": "b", "rep": again}])
    assert out["numbers"]["voted"]["value"] == 471 and unverified == []
    assert [a["d"] for a in applied] == ["a"]
    f = evaluate_rg7(again, bdoc("/e/r7.pdf", no="29"), cfg(), rg2, [first])
    assert [x.rule for x in f] == ["R1"]  # no R2
