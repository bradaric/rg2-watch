import copy
import json
import subprocess

import pytest

from rg2watch.analyze import AnalysisResult
from rg2watch.cli import main
from rg2watch.config import Config, load_config
from rg2watch.browse import STATIC, build_data
from rg2watch.pipeline import PROMPT_VERSION
from rg2watch.store import Store
from rg2watch.tally import _contrib, build_tally, disagreements, extract, format_tally
from rg2watch.models import Event
from test_browse import BASE, P, add, doc, store
from test_rules import clean_report, nf

M = "claude-sonnet-5-5"


NAT = Config(tally_scope={P: "national"})  # the test election has a made-up type


def report(**kw):
    r = copy.deepcopy(clean_report())
    for k, v in kw.items():
        r["numbers"][k] = v
    return r


def put(s, d, rep, model=M):
    rep = {**rep, "header": {**rep["header"], "station_no": d.station_no}}  # no V9 from the fixture itself
    s.save_analysis(d.url, AnalysisResult(rep, 0, 0, 0.0, model, "stop"), PROMPT_VERSION, parent="adhoc")


def form(s, no, rep=None, muni="Alpha", model=M, **kw):
    d = doc(BASE + f"/e/{muni}{no}.pdf", muni=muni, no=str(no), **kw)
    add(s, d)
    if rep is not None:
        put(s, d, rep, model)
    return d


def tally(s, **kw):
    return build_tally(s, kw.pop("cfg", NAT), P, M, **kw)


def why(t):
    return {e["no"]: e["why"] for e in t["excluded"]}


def test_clean_form_counts_and_totals_and_turnout():
    s = store()
    form(s, 12, clean_report())
    form(s, 13, report(registered=nf(500), voted=nf(400), ballots_in_box=nf(400), invalid=nf(10),
                       valid=nf(390), list_votes=[{"row": 1, "field": nf(300)}, {"row": 2, "field": nf(90)}]))
    t = tally(s)
    o = t["overall"]
    assert (o["counted"], o["clean"], o["excluded"]) == (2, 2, 0)
    assert o["cl"]["registered"] == 1500 and o["cl"]["voted"] == 1200 and o["cl"]["valid"] == 1170
    assert o["cl"]["turnout"] == pytest.approx(0.8) and o["cl"]["lists"] == [700, 470]
    assert o["cl"] == o["all"] and t["excluded"] == [] and t["nlists"] == 2


@pytest.mark.parametrize("name, rep, expect", [
    ("low", {**clean_report(), "confidence": "low"}, "low confidence"),
    ("poor", None, "page quality poor"),
    ("illegible", report(valid=nf(None, legible=False)), "valid illegible"),
    ("empty", report(registered=nf(None, legible=True, mark="empty")), "registered empty"),
    ("a1", report(ballots_in_box=nf(900)), "arithmetic A1"),
])
def test_excluded_reasons(name, rep, expect):
    s = store()
    if rep is None:
        rep = copy.deepcopy(clean_report())
        rep["visual"]["page_quality"] = "poor"
    form(s, 12, rep)
    t = tally(s)
    assert any(expect in w for w in why(t)["12"]), why(t)
    o = t["overall"]
    assert o["clean"] == 0 and o["excluded"] == 1 and o["cl"]["voted"] == 0
    assert o["all"]["voted"] > 0 or name in ("illegible", "empty")  # "all forms" still counts what was read


def test_high_alert_flags_form_and_medium_does_not():
    s = store()
    hi = report()
    hi["numbers"]["valid"] = nf(780, mark="crossed_out", original=790)
    form(s, 12, hi)
    mid = report()
    mid["signatures"] = {**mid["signatures"], "rows_name_without_signature": 3}  # V4 is MEDIUM
    form(s, 13, mid)
    t = tally(s)
    assert why(t) == {"12": ["HIGH alert: V1"]}
    assert t["overall"]["clean"] == 1


def test_empty_outside_voted_is_not_a_reason_but_illegible_is():
    s = store()
    form(s, 12, report(outside_voted=nf(None, legible=False, mark="empty")))
    form(s, 13, report(outside_voted=nf(None, legible=False, mark="clear")))
    assert list(why(tally(s))) == ["13"]


def test_unreadable_list_row_excludes_form():
    s = store()
    form(s, 12, report(list_votes=[{"row": 1, "field": nf(400)}, {"row": 2, "field": nf(None, legible=False)}]))
    assert "list row 2 illegible" in why(tally(s))["12"][0]


def test_disagreement_between_models_marks_form_and_shows_values():
    s = store()
    d = form(s, 12, clean_report())
    put(s, d, report(voted=nf(790), ballots_in_box=nf(790), invalid=nf(10)), "other")
    put(s, d, clean_report(), "third")
    t = tally(s)
    (e,) = t["excluded"]
    assert any("models disagree on voted: 800 (" + M + ") vs 790 (other)" in w for w in e["why"])
    assert t["overall"]["cl"]["voted"] == 0 and t["overall"]["all"]["voted"] == 800  # the watch model's value


def test_disagreement_unit_fields_and_list_rows():
    cfg = Config()
    mine = extract(clean_report(), doc(BASE + "/x.pdf", no="12"), cfg)
    same = extract(clean_report(), doc(BASE + "/x.pdf", no="12"), cfg)
    other = extract(report(list_votes=[{"row": 1, "field": nf(400)}, {"row": 2, "field": nf(381)}]),
                    doc(BASE + "/x.pdf", no="12"), cfg)
    assert disagreements(mine, {"b": same}, "a") == ([], [])
    (d,), notes = disagreements(mine, {"b": other}, "a")
    assert d == {"f": "list row 2", "v": {"a": 380, "b": 381}} and notes == []
    blind = extract(report(voted=nf(None, legible=False)), doc(BASE + "/x.pdf", no="12"), cfg)
    assert disagreements(mine, {"b": blind}, "a") == ([], ["b could not read voted"])


def test_forms_without_watch_model_analysis_are_not_counted():
    s = store()
    form(s, 12, clean_report(), model="someone-else")
    form(s, 13)
    t = tally(s)
    assert t["overall"]["counted"] == 0 and t["overall"]["unanalysed"] == 2


def test_removed_and_repeat_vote_forms_are_left_out():
    s = store()
    d = form(s, 12, clean_report())
    s.apply_events(P, s.start_sweep(P, "t"), [Event("removed", d)])
    form(s, 13, clean_report(), dt=10)  # repeat vote
    assert tally(s)["overall"]["counted"] == 0


def test_per_municipality_coverage_and_unknown_municipality():
    s = store()
    s.save_municipalities(P, {"1": "Alpha", "2": "Beta"})
    s.save_stations(P, "1", {str(i): f"a{i}" for i in range(1, 5)})
    s.save_stations(P, "2", {"1": "b1"})
    form(s, 1, clean_report())
    form(s, 2, clean_report())
    form(s, 3, clean_report(), muni="Beta")
    t = tally(s)
    by = {m["m"]: m for m in t["munis"]}
    assert by["Alpha"]["stations"] == 4 and by["Alpha"]["counted"] == 2 and by["Beta"]["stations"] == 1
    assert t["overall"]["stations"] == 5 and t["overall"]["counted"] == 3
    assert tally(s, municipalities={"Beta"})["overall"]["counted"] == 1


def test_null_municipality_and_station_key():
    s = store()
    form(s, 12, clean_report(), muni=None)
    s._db.execute("UPDATE documents SET station_key=NULL")
    s._db.commit()
    row = tally(s)["munis"][0]
    assert row["m"] == "(unknown)" and row["generated_unknown_muni"] is True
    assert tally(s)["overall"]["m"] == "(all)"
    assert tally(s)["overall"]["generated_unknown_muni"] is False


def test_list_names_mapping_and_default(tmp_path):
    s = store()
    form(s, 12, clean_report())
    t = tally(s, cfg=Config(tally_scope=NAT.tally_scope, list_names={P: ["Zeta", " "]}))
    assert t["names"] == ["Zeta", "List 2"]
    assert t["fallback_name_indices"] == [1]
    assert tally(s)["names"] == ["List 1", "List 2"]
    literal = tally(s, cfg=Config(tally_scope=NAT.tally_scope, list_names={P: ["List 1", " "]}))
    assert literal["names"] == ["List 1", "List 2"]
    assert literal["fallback_name_indices"] == [1]
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[list_names]\n"{P}" = ["A", "B"]\n')
    assert load_config(cfg).list_names == {P: ["A", "B"]}
    for bad in ('list_names = ["x"]', f'[list_names]\n"{P}" = "x"', f'[list_names]\n"{P}" = [1]'):
        cfg.write_text(bad)
        with pytest.raises(ValueError):
            load_config(cfg)


def test_tally_in_page_data_and_error_does_not_break_build(tmp_path, monkeypatch):
    s = store()
    form(s, 12, clean_report())
    data = build_data(s, tmp_path, P, Config(model=M))
    assert data["tally"]["overall"]["counted"] == 1
    overall = data["tally"]["overall"]
    municipality = data["tally"]["munis"][0]
    assert municipality["all"]["valid"] == municipality["all"]["lists_valid"] == sum(municipality["all"]["lists"])
    assert overall["all"]["vote_forms"] == 1
    assert overall["all"]["withheld_valid"] == 0
    assert overall["fall"]["vote_forms"] == 1
    assert overall["pair"]["all"]["pair_selected"] == 1
    assert overall["pair"]["all"]["pair_vote_forms"] == 1
    import rg2watch.browse as br
    monkeypatch.setattr(br, "build_tally", lambda *a, **k: 1 / 0)
    assert build_data(s, tmp_path, P, Config())["tally"] is None


def test_page_csv_is_formula_safe_and_labelled():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/^[=+\\-@\\t\\r]/.test(x)) x = \"'\" + x" in html
    assert "Unofficial - numbers read by a model from the scanned forms" in html
    assert 'new Intl.NumberFormat("en-GB")' in html
    assert 'id="tview"' in html and 'id="hview"' in html and 'p.set("nv", NV())' in html  # numbers view, in the hash
    for label in ("Vote forms", "Withheld readable valid", "Unreconciled turnout gap",
                  "Selected in both views", "Paired vote forms", "Vote forms not paired",
                  "Corrected-only eligible", "Filed-only eligible"):
        assert label in html
    assert "invalid counts are limited to forms with reconciled valid/list totals" in html


def test_cli_tally_prints_all_then_clean(tmp_path, capsys):
    db = tmp_path / "w.sqlite"
    s = Store(db)
    s.upsert_election(store().get_election(P))
    form(s, 12, clean_report())
    form(s, 13, report(ballots_in_box=nf(900)))
    s.close()
    assert main(["tally", "--db", str(db), "--parent", P, "--model", M]) == 0
    all_out = capsys.readouterr().out
    assert "UNOFFICIAL" in all_out and "all counted forms" in all_out and "List 1" in all_out
    assert "2 form(s) counted, 1 clean, 1 flagged" in all_out and "BM 13: arithmetic A1" in all_out
    assert "2,000" in all_out
    assert main(["tally", "--db", str(db), "--parent", P, "--model", M, "--clean"]) == 0
    out = capsys.readouterr().out
    assert "clean forms only (leave out flagged)" in out and "1,000" in out and "80.0%" in out
    assert main(["tally", "--db", str(db), "--parent", P, "--model", M, "--as-filed"]) == 0
    assert "numbers as filed on the RG-2 forms" in capsys.readouterr().out
    assert main(["tally", "--db", str(db), "--parent", P, "--model", M, "--diff"]) == 0
    assert "numbers difference (corrected - as filed" in capsys.readouterr().out
    assert main(["tally", "--db", str(db), "--parent", P, "--model", M, "--municipality", "Nowhere"]) == 2
    assert main(["tally", "--db", str(db), "--parent", P, "--model", M, "--municipality", "alp"]) == 0


def test_format_tally_empty():
    s = store()
    text = format_tally(tally(s))
    assert "0 form(s) counted" in text
    assert "Unreconciled turnout gap: -" in text


def test_snapshot_turnout_gap_formats_zero_and_signed_values():
    s = store()
    form(s, 1, clean_report())
    zero = format_tally(tally(s))
    assert "Unreconciled turnout gap: 0" in zero
    assert "Unreconciled turnout gap: ." not in zero

    positive = report(voted=nf(801), ballots_in_box=nf(800))
    s = store()
    form(s, 1, positive)
    assert "Unreconciled turnout gap: +1" in format_tally(tally(s))

    negative = report(voted=nf(799), ballots_in_box=nf(799))
    s = store()
    form(s, 1, negative)
    assert "Unreconciled turnout gap: -1" in format_tally(tally(s))
    assert "unreconciled turnout gap difference: ." in format_tally(tally(s), view="diff")


def test_hero_markup_labelled_and_safe():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="hero"' in html and "НЕЗВАНИЧНО" in html and "Веома рано" in html
    assert "#E69F00" in html and "createElementNS" not in html and "innerHTML" not in html.replace("never innerHTML", "")
    assert "Esc" in html and 'get("hero") === "1"' in html and 'ev.key === "Escape"' in html
    assert 'id="hs-forms"' in html
    for text in ("Општина", "Обухваћени записници", "Приказ бројева", "Режим презентације",
                 "Излаз (Esc)", "Излазност", "Обрађена бирачка места", "Записници", "Важећи гласови"):
        assert text in html
    assert "изабраних записника допринело је збировима гласова" in html
    assert "Неусаглашена разлика излазности" in html
    assert "прихватљиво само са исправкама" in html and "прихватљиво само како је предато" in html


def test_hero_warning_translates_every_current_tally_warning_and_preserves_unknown_text():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    source = (STATIC.parent / "tally.py").read_text(encoding="utf-8")
    warnings = [
        "unknown election type - set tally_scope",
        "municipality unknown - forms may be from different ballots, no list totals",
        "no expected ballot rows - comparable list totals unavailable",
        "list_names must be a plain list for a national election",
        "list_names must map municipality -> names for a municipal election",
        "list_names has {len(conf)} names, forms have {nrows} lists - check config",
    ]
    assert all(message in source for message in warnings)
    start = html.index("function heroWarning")
    end = html.index("function initHero", start)
    helper = html[start:end]
    script = r'''
const assert = require("assert");
''' + helper + r'''
const cases = [
  ["unknown election type - set tally_scope", "Непозната врста избора — подесите tally_scope"],
  ["municipality unknown - forms may be from different ballots, no list totals", "Општина није позната — записници могу бити са различитих гласачких листића, без збирова по листама"],
  ["no expected ballot rows - comparable list totals unavailable", "Нема очекиваних редова на гласачком листићу — упоредиви збирови по листама нису доступни"],
  ["list_names must be a plain list for a national election", "list_names мора бити обична листа за републичке изборе"],
  ["list_names must map municipality -> names for a municipal election", "list_names мора да пресликава општину у називе за локалне изборе"],
  ["list_names has 3 names, forms have 2 lists - check config", "list_names има 3 назива, записници имају 2 листе — проверите конфигурацију"],
];
for (const [input, expected] of cases) assert.equal(heroWarning(input), expected);
assert.equal(heroWarning(cases[0][0] + "; " + cases[2][0]), cases[0][1] + "; " + cases[2][1]);
assert.equal(heroWarning("future <opaque> warning"), "future <opaque> warning");
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_unreadable_by_other_model_is_a_note_not_an_exclusion():
    s = store()
    d = form(s, 12, clean_report())
    put(s, d, report(voted=nf(None, legible=False)), "other")
    t = tally(s)
    assert t["excluded"] == [] and t["overall"]["clean"] == 1
    assert t["notes"][0]["note"] == "other could not read voted"


def test_overall_stations_cover_municipalities_without_forms():
    s = store()
    s.save_municipalities(P, {"1": "Alpha", "2": "Beta"})
    s.save_stations(P, "1", {"1": "a1", "2": "a2"})
    s.save_stations(P, "2", {str(i): f"b{i}" for i in range(1, 9)})
    form(s, 1, clean_report())
    t = tally(s)
    assert t["overall"]["stations"] == 10 and t["overall"]["counted"] == 1
    assert [m["m"] for m in t["munis"]] == ["Alpha", "Beta"] and t["munis"][1]["counted"] == 0
    assert tally(s, municipalities={"Alpha"})["overall"]["stations"] == 2


def test_all_mode_denominators_are_paired():
    s = store()
    form(s, 1, clean_report())
    # registered unreadable: its voted must not enter turnout, nor its lists enter shares without valid
    form(s, 2, report(registered=nf(None, legible=False), valid=nf(None, legible=False)))
    a = tally(s)["overall"]["all"]
    assert a["registered"] == 1000 and a["voted"] == 800 and a["turnout"] == pytest.approx(0.8)
    assert a["lists"] == [400, 380] and a["lists_valid"] == 780


def test_vote_totals_only_include_reconciled_forms_but_turnout_stays_independent():
    s = store()
    form(s, 1, clean_report())
    form(s, 2, report(valid=nf(770)))
    t = tally(s)
    a = t["overall"]["all"]
    assert a["voted"] == 1600
    assert a["valid"] == a["lists_valid"] == sum(a["lists"]) == 780
    assert a["invalid"] == 20
    assert a["vote_forms"] == 1
    assert a["withheld_valid"] == 770
    assert t["overall"]["counted"] == 2
    assert any(reason.startswith("arithmetic") and "A3" in reason for reason in why(t)["2"])
    text = format_tally(t)
    assert "1 of 2 selected forms contributed reconciled vote totals" in text
    assert "770 readable valid votes withheld" in text
    assert "Unreconciled turnout gap: +800" in text


def test_vote_eligibility_edge_cases_and_non_vote_flags():
    rows = [1, 2]
    missing_valid = extract(report(valid=nf(None, legible=False)), doc(BASE + "/mv.pdf"), Config())
    unreadable_list = extract(report(list_votes=[{"row": 1, "field": nf(400)},
                                                  {"row": 2, "field": nf(None, legible=False)}]),
                              doc(BASE + "/ul.pdf"), Config())
    wrong_rows = extract(report(list_votes=[{"row": 1, "field": nf(780)}]), doc(BASE + "/wr.pdf"), Config())
    unreadable_invalid = extract(report(invalid=nf(None, legible=False)), doc(BASE + "/ui.pdf"), Config())
    non_vote_flag = extract({**clean_report(), "confidence": "low"}, doc(BASE + "/nf.pdf"), Config())

    assert "withheld_valid" not in _contrib(missing_valid, rows)
    assert _contrib(unreadable_list, rows)["withheld_valid"] == 780
    assert _contrib(wrong_rows, rows)["withheld_valid"] == 780
    assert _contrib(unreadable_invalid, rows)["vote_forms"] == 1
    assert "invalid" not in _contrib(unreadable_invalid, rows)
    assert _contrib(non_vote_flag, rows)["vote_forms"] == 1


def test_malformed_row_set_is_excluded_and_never_widens_the_table():
    s = store()
    form(s, 1, clean_report())
    form(s, 2, clean_report())
    form(s, 3, report(list_votes=[{"row": i, "field": nf(10)} for i in range(1, 6)]))
    t = tally(s)
    assert t["nlists"] == 2 and why(t)["3"][0] == "list rows differ (got 5, expected 2)"
    assert t["overall"]["all"]["lists"] == [800, 760]  # the odd form's lists are not mixed in
    assert t["names_note"] is None


def test_tally_municipalities_for_cli_names():
    s = store()
    s.save_municipalities(P, {"1": "Alpha", "2": "Beta"})
    form(s, 1, clean_report(), muni="Gamma")
    assert s.tally_municipalities(P) == ["Alpha", "Beta", "Gamma"]


def test_page_rebuild_csv_and_filter_details():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '"Note"' in html and "title: esc(c.t)" in html
    assert tally(store())["overall"]["m"] == "(all)"
    assert "other filters cleared" in html and "replace(/[^\\w.-]+/g" in html
    assert "draw(false)" in html and "tkey" in html


def test_list_names_length_mismatch_only_warns_and_names_the_lists(caplog):
    import rg2watch.tally as tl
    tl._warned.clear()
    s = store()
    form(s, 1, clean_report())
    form(s, 2, clean_report())
    cfg = Config(tally_scope=NAT.tally_scope, list_names={P: ["A", "B", "C"]})
    t = tally(s, cfg=cfg)
    assert t["nlists"] == 2 and t["excluded"] == [] and t["overall"]["clean"] == 2  # forms decide
    assert t["names"] == ["A", "B"]  # extra names ignored
    assert t["names_note"] == "list_names has 3 names, forms have 2 lists - check config"
    tally(s, cfg=cfg)
    assert caplog.text.count("check config") == 1  # once per change
    t = tally(s, cfg=Config(tally_scope=NAT.tally_scope, list_names={P: ["A"]}))
    assert t["names"] == ["A", "List 2"] and "has 1 names" in t["names_note"]
    assert "warning: list_names has 1" in format_tally(t)
    assert 'id="heronames"' in (STATIC / "index.html").read_text(encoding="utf-8")


def muni_cfg(**kw):
    return Config(tally_scope={P: "municipal"}, **kw)


def two_ballots(s):
    form(s, 1, clean_report(), muni="Alpha")
    form(s, 2, clean_report(), muni="Alpha")
    r3 = report(list_votes=[{"row": 1, "field": nf(100)}, {"row": 2, "field": nf(280)}, {"row": 3, "field": nf(400)}])
    form(s, 3, r3, muni="Beta")


def test_municipal_scope_per_municipality_lists_and_overall_without_lists():
    s = store()
    two_ballots(s)
    cfg = muni_cfg(list_names={P: {"Beta": ["X", "Y", "Z"]}})
    t = tally(s, cfg=cfg)
    assert t["scope"] == "municipal" and t["scope_note"] is None and t["nlists"] == 0
    a, b = t["munis"]
    assert (a["nlists"], b["nlists"]) == (2, 3)
    assert a["names"] == ["List 1", "List 2"] and b["names"] == ["X", "Y", "Z"]
    assert a["fallback_name_indices"] == [0, 1] and b["fallback_name_indices"] == []
    assert b["all"]["lists"] == [100, 280, 400] and b["all"]["lists_valid"]
    assert len(a["all"]["lists"]) == 2 and a["names_note"] is None
    for k in ("all", "cl"):
        assert "lists" not in t["overall"][k] and "lists_valid" not in t["overall"][k]
    assert t["overall"]["counted"] == 3 and t["excluded"] == []  # row sets are per municipality
    assert t["overall"]["all"]["valid"] == sum(m["all"]["valid"] for m in t["munis"])
    assert t["overall"]["all"]["vote_forms"] == 3
    assert t["overall"]["pair"]["all"]["pair_selected"] == 3
    assert t["overall"]["pair"]["all"]["pair_vote_forms"] == 3


def test_municipal_row_set_is_modal_per_municipality():
    s = store()
    two_ballots(s)
    form(s, 4, clean_report(), muni="Beta")
    form(s, 5, report(list_votes=[{"row": 1, "field": nf(100)}, {"row": 2, "field": nf(280)}, {"row": 3, "field": nf(400)}]), muni="Beta")
    t = tally(s, cfg=muni_cfg())
    assert {m["m"]: m["nlists"] for m in t["munis"]} == {"Alpha": 2, "Beta": 3}
    assert [e["no"] for e in t["excluded"]] == ["4"]
    assert t["excluded"][0]["why"][0] == "list rows differ (got 2, expected 3)"


def test_municipal_names_match_latin_cyrillic_and_note_wrong_shape():
    s = store()
    form(s, 1, clean_report(), muni="Нови Сад")
    t = tally(s, cfg=muni_cfg(list_names={P: {"Novi Sad": ["A", "B"]}}))
    assert t["munis"][0]["names"] == ["A", "B"] and t["munis"][0]["names_note"] is None
    t = tally(s, cfg=muni_cfg(list_names={P: {"Novi Sad": ["A"]}}))
    assert "has 1 names" in t["munis"][0]["names_note"]
    t = tally(s, cfg=muni_cfg(list_names={P: ["A", "B"]}))
    assert t["munis"][0]["names"] == ["List 1", "List 2"] and "municipal election" in t["munis"][0]["names_note"]
    t = tally(s, cfg=Config(tally_scope=NAT.tally_scope, list_names={P: {"Novi Sad": ["A"]}}))
    assert t["names"] == ["List 1", "List 2"] and "plain list" in t["names_note"]


@pytest.mark.parametrize("etype, scope, note", [("2", "national", None), ("3", "municipal", None),
                                                ("9", "municipal", "unknown election type - set tally_scope")])
def test_scope_from_election_type_and_override(etype, scope, note):
    from rg2watch.models import Election
    s = store()
    s.upsert_election(Election(P, "1", etype, "T", BASE))
    form(s, 1, clean_report())
    t = build_tally(s, Config(), P, M)
    assert (t["scope"], t["scope_note"]) == (scope, note)
    other = "national" if scope == "municipal" else "municipal"
    t = build_tally(s, Config(tally_scope={P: other}), P, M)
    assert (t["scope"], t["scope_note"]) == (other, None)


def test_no_elections_row_is_unknown_municipal():
    s = Store(":memory:")
    form(s, 1, clean_report())  # forms of P, but no elections row (as in the calib DBs)
    t = build_tally(s, Config(), P, M)
    assert t["scope"] == "municipal" and t["scope_note"] == "unknown election type - set tally_scope"
    assert build_tally(s, Config(tally_scope={P: "national"}), P, M)["scope"] == "national"


def test_cli_tally_municipal_prints_list_table_per_municipality(tmp_path, capsys):
    db = tmp_path / "w.sqlite"
    s = Store(db)
    s.upsert_election(store().get_election(P))
    two_ballots(s)
    s.close()
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'tally_scope = {{"{P}" = "municipal"}}\n')
    assert main(["tally", "--config", str(cfg), "--db", str(db), "--parent", P, "--model", M]) == 0
    out = capsys.readouterr().out
    assert "Alpha: votes per list" in out and "Beta: votes per list" in out and "List 3" in out
    head = out.split("Alpha: votes per list")[0]
    assert "List 1" not in head


def test_page_handles_municipal_scope():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'T.scope === "municipal"' in html and "изаберите општину за резултате по листама" in html
    assert '"Share %"' in html and "listsOf(" in html and 'id="tscope"' in html


def test_municipal_unknown_municipality_gets_no_list_totals():
    s = store()
    two_ballots(s)
    form(s, 9, clean_report(), muni=None)
    t = tally(s, cfg=muni_cfg())
    u = next(m for m in t["munis"] if m["m"] == "(unknown)")
    assert u["nlists"] == 0 and u["names"] == [] and "different ballots" in u["names_note"]
    assert u["fallback_name_indices"] == [] and u["generated_unknown_muni"] is True
    assert u["counted"] == 1 and u["all"]["valid"] == 0 and u["all"]["withheld_valid"] == 780
    assert "lists" not in u["all"] and "lists" not in u["cl"]
    assert "(unknown): warning: municipality unknown" in format_tally(t)
    nat = next(m for m in tally(s)["munis"] if m["m"] == "(unknown)")
    assert "lists" in nat["all"]  # national scope: one ballot, lists kept


def test_literal_unknown_municipality_is_not_marked_as_generated_even_when_grouped_with_missing():
    s = store()
    form(s, 1, clean_report(), muni=None)
    form(s, 2, clean_report(), muni="(unknown)")
    row = tally(s)["munis"][0]
    assert row["m"] == "(unknown)" and row["counted"] == 2
    assert row["generated_unknown_muni"] is False


def test_named_and_empty_municipal_rows_always_have_additive_name_metadata():
    s = store()
    s.save_municipalities(P, {"1": "Alpha", "2": "Beta"})
    s.save_stations(P, "2", {"1": "beta-1"})
    form(s, 1, clean_report(), muni="Alpha")
    t = tally(s, cfg=muni_cfg(list_names={P: {"Alpha": ["Configured", " "]}}))
    by = {r["m"]: r for r in t["munis"]}
    assert by["Alpha"]["names"] == ["Configured", "List 2"]
    assert by["Alpha"]["fallback_name_indices"] == [1]
    assert by["Alpha"]["generated_unknown_muni"] is False
    assert by["Beta"]["names"] == [] and by["Beta"]["fallback_name_indices"] == []
    assert by["Beta"]["generated_unknown_muni"] is False


def test_empty_expected_ballot_rows_are_explicitly_unavailable_in_national_and_named_municipal_scopes():
    empty_ballot = report(list_votes=[])

    s = store()
    form(s, 1, empty_ballot)
    national = tally(s)
    assert national["nlists"] == 0
    assert national["names_note"] == "no expected ballot rows - comparable list totals unavailable"
    assert "warning: no expected ballot rows - comparable list totals unavailable" in format_tally(national)

    municipal = tally(s, cfg=muni_cfg())
    alpha = municipal["munis"][0]
    assert alpha["m"] == "Alpha" and alpha["nlists"] == 0
    assert alpha["names_note"] == "no expected ballot rows - comparable list totals unavailable"
    assert "Alpha: warning: no expected ballot rows - comparable list totals unavailable" in format_tally(municipal)


def test_page_diff_view_uses_paired_numbers_and_explains_unmatched_terms():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "deltaOf(r.pair.all.c, r.pair.all.f)" in html and "terms only in corrected/filed view" in html
    assert "vote-reconciliation eligibility" in html and "deltaOf(r.all, r.fall)" not in html
    assert "pair_selected" in html and "pair_vote_forms" in html and "pair_not_contributing" in html
    assert "t(r).turnout == null ? null" in html
    assert "term-specific comparable cohorts" in html


def test_cli_diff_heading_describes_term_specific_comparable_cohorts():
    s = store()
    form(s, 1, clean_report())
    text = format_tally(tally(s), view="diff")
    assert "over term-specific comparable cohorts" in text
    assert "over the stations counted in both" not in text


def test_results_javascript_uses_selected_view_and_exports_explicit_unavailable_lists():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    helpers = html[html.index('const nf = new Intl.NumberFormat("en-GB")'):html.index("function showForm")]
    hero = html[html.index("const OKABE") : html.index('$("herobtn").onclick')]
    script = r'''
const assert = require("assert");
const els = {};
function el(id) { return els[id] ||= {textContent: "", hidden: false, value: "", style: {}, children: [],
  classList: {remove() {}, add() {}, toggle() {}}, append(...x) { this.children.push(...x); },
  replaceChildren(...x) { this.children = x; }, get options() { return this.children; }}; }
const $ = el;
function mk(tag, text, cls) { return {tag, textContent: text == null ? "" : String(text), className: cls || "", hidden: false, style: {}}; }
function epoch() { return "20:00"; }
function shortTitle(x) { return x; }
function Option(text, value) { this.textContent = text; this.value = value; }
const STALE_S = 999999;
let S = null, D = {built: "2026-10-20T18:00:00Z"};
let bodyHero = false, selectedTab = "";
const document = {body: {classList: {toggle(name, on) { if (name === "hero") bodyHero = on; }, contains() { return bodyHero; }}}};
function setTab(name) { selectedTab = name; }
els.tview = {value: "corrected"};
els.tmode = {value: "all"};
''' + helpers + hero + r'''
for (let i = 0; i < 9; i++) slots.push({row: {hidden: true, title: ""}, no: {}, nm: {}, fill: {style: {}}, val: {}});
function totals(valid, lists) {
  const o = {registered: 1000, voted: 100, turnout: .1, ballots_in_box: 100, invalid: 0,
    valid, outside_voted: 0, vote_forms: lists ? 1 : 0, withheld_valid: lists ? 0 : 100,
    lists: lists || [0, 0], lists_valid: valid};
  return o;
}
function row(correctedLists, filedLists) {
  const c = totals(correctedLists ? 100 : 0, correctedLists);
  const f = totals(filedLists ? 100 : 0, filedLists);
  const cohort = {c, f, in: 0, out: 0, pair_selected: 1, pair_vote_forms: correctedLists && filedLists ? 1 : 0,
    pair_not_contributing: correctedLists && filedLists ? 0 : 1,
    corrected_only_vote_forms: correctedLists && !filedLists ? 1 : 0,
    filed_only_vote_forms: filedLists && !correctedLists ? 1 : 0};
  return {m: "Alpha", stations: 1, counted: 1, clean: 1, excluded: 0, fclean: 1, fexcluded: 0,
    names: ["A", "B"], nlists: 2, all: c, cl: c, fall: f, fcl: f, pair: {all: cohort, cl: cohort}};
}
function setData(r, scope="national") {
  T = {model: "opaque-model-v1", title: "Избори Ћирилица", scope, scope_note: null,
    names: scope === "national" ? ["Листа А", "List 2"] : [], fallback_name_indices: scope === "national" ? [1] : [],
    names_note: null, munis: [r], overall: r};
}
function csvValue(csv, title) {
  const lines = csv.trim().split("\r\n"), headings = lines[0].split(","), cells = lines[1].split(",");
  return cells[headings.indexOf(title)];
}

let r = row([60, 40], null);
setData(r);
assert.equal(vrow(r).all.valid, 100);
let csv = tallyCsv();
assert.equal(csvValue(csv, "Reconciled valid"), "100");
assert.equal(csvValue(csv, "1 · Листа А votes"), "60");
assert.equal(csvValue(csv, "Unreconciled turnout gap (signed)"), "0");
renderHero();
assert(els["hs-valid"].textContent.includes("Неусаглашена разлика излазности: 0."));
assert(els.herofoot.children[0].includes("opaque-model-v1"));
assert(els.herotitle.textContent.includes("Избори Ћирилица"));
assert.equal(slots[0].nm.textContent, "Листа А");
assert.equal(slots[1].nm.textContent, "Листа 2");
els.tmode.value = "cl";
renderHero();
assert(els["hs-forms"].textContent.includes("чистих"));
els.tmode.value = "all";
let gapCol = resultCols("all").find(x => x.t === "Unreconciled turnout gap (signed)");
assert.equal(gapCol.f(gapCol.v(vrow(r))), "0");
r.all.voted = r.cl.voted = 101;
assert.equal(gapCol.f(gapCol.v(vrow(r))), "+1");
r.all.voted = r.cl.voted = 99;
assert.equal(gapCol.f(gapCol.v(vrow(r))), "−1");
r.all.voted = r.cl.voted = 100;
els.tview.value = "filed";
assert.equal(vrow(r).all.valid, 0);
csv = tallyCsv();
assert.equal(csvValue(csv, "Reconciled valid"), "0");
assert.equal(csvValue(csv, "1 · Листа А votes"), "0");
renderHero();
assert(slots.every(x => x.row.hidden), "filed-ineligible selected view must hide the chart");
assert(!els["hs-valid"].textContent.includes("NaN"));

const many = row([10,9,8,7,6,5,4,3,2,46], [10,9,8,7,6,5,4,3,2,46]);
setData(many);
T.names = Array.from({length: 10}, (_, i) => "Назив " + (i + 1));
T.fallback_name_indices = [];
renderHero();
assert.equal(slots[8].nm.textContent, "Остале (2 листа)");

r = row(null, [55, 45]);
setData(r);
renderHero();
assert.equal(slots[0].row.hidden, false, "filed-only eligible selected view must show the chart");
assert(!slots[0].val.textContent.includes("NaN"));

els.tview.value = "diff";
r = row([60, 40], [55, 45]);
setData(r);
renderHero();
assert(els["hs-valid"].textContent.includes("изабраних записника допринело је збировима гласова у оба приказа"));
assert(slots[0].val.textContent.includes("п. п."));
const diffCols = resultCols("all");
const selected = diffCols.find(x => x.t === "Selected in both views");
assert.equal(selected.f(selected.v(vrow(r))), "1");
gapCol = diffCols.find(x => x.t === "Unreconciled turnout gap (difference)");
assert.equal(gapCol.f(gapCol.v(vrow(r))), "·");
csv = tallyCsv();
assert.equal(csvValue(csv, "1 · Листа А votes"), "5");
assert(csv.includes("Paired cohort columns are unsigned actual sizes"));

els.tview.value = "corrected";
r = row(null, null);
setData(r);
renderHero();
assert(slots.every(x => x.row.hidden), "zero eligible forms must hide the chart");
assert(!els["hs-valid"].textContent.includes("NaN"));

const alpha = row([60, 40], [60, 40]);
const unknown = row(null, null); unknown.m = "(unknown)"; unknown.names = []; unknown.nlists = 0;
const overall = row(null, null); overall.m = "(all)"; overall.names = []; overall.nlists = 0;
for (const r of [unknown, overall]) for (const k of ["all", "cl", "fall", "fcl"]) {
  delete r[k].lists; delete r[k].lists_valid;
}
T = {model: "test", title: "Local", scope: "municipal", scope_note: null, names: [], names_note: null,
  munis: [alpha, unknown], overall};
csv = tallyCsv();
assert(csv.includes("Alpha,1,A,60,60.0"), "municipal list rows must be long format");
assert(csv.includes("Unavailable - no comparable ballot"), "unknown ballot must be explicit");
assert(csv.includes("Unavailable - no universal ballot"), "municipal overall list fields must be explicit");

unknown.generated_unknown_muni = true;
unknown.names = ["List 1", "Стварна листа"]; unknown.fallback_name_indices = [0]; unknown.nlists = 2;
for (const k of ["all", "cl", "fall", "fcl"]) { unknown[k].lists = [60, 40]; unknown[k].lists_valid = 100; unknown[k].vote_forms = 1; }
heroMuni = "(unknown)"; heroPicked = true;
renderHero();
assert.equal(els.hmuni.options[2].textContent, "Непозната општина");
assert(els.herotitle.textContent.startsWith("Непозната општина"));
assert.equal(slots[0].nm.textContent, "Листа 1");
unknown.generated_unknown_muni = false; delete unknown.fallback_name_indices;
renderHero();
assert.equal(els.hmuni.options[2].textContent, "(unknown)");
assert(els.herotitle.textContent.startsWith("(unknown)"));
assert.equal(slots[0].nm.textContent, "List 1");
heroMuni = ""; heroPicked = true;
renderHero();
assert(els.herotitle.textContent.startsWith("Све општине"));

T.scope_note = "future <opaque> warning";
renderHero();
assert(els.heronames.textContent.includes("future <opaque> warning"));
S = {watching: false, ts: 0};
renderHero();
assert(els.herofoot.children[1].textContent.includes("можда застарели"));
S = null;

els.tview.value = "corrected";
const emptyNational = row(null, null);
emptyNational.names = []; emptyNational.nlists = 0;
emptyNational.names_note = "no expected ballot rows - comparable list totals unavailable";
for (const k of ["all", "cl", "fall", "fcl"]) { emptyNational[k].lists = []; emptyNational[k].lists_valid = 0; }
setData(emptyNational);
T.names = []; T.names_note = emptyNational.names_note;
renderHero();
assert(els["hs-valid"].textContent.includes("Упоредиви збирови по листама нису доступни за овај обухват."));
let availability = resultCols("all").find(x => x.t === "List totals availability");
assert(availability, "national empty ballot needs an explicit results column");
assert(availability.f(availability.v(vrow(emptyNational))).includes("Unavailable - no expected ballot rows"));
csv = tallyCsv();
assert(csv.includes("List totals availability"));
assert(csv.includes("Unavailable - no expected ballot rows"));

const emptyMunicipal = row(null, null);
emptyMunicipal.names = []; emptyMunicipal.nlists = 0;
emptyMunicipal.names_note = "no expected ballot rows - comparable list totals unavailable";
for (const k of ["all", "cl", "fall", "fcl"]) { delete emptyMunicipal[k].lists; delete emptyMunicipal[k].lists_valid; }
T = {model: "test", title: "Local", scope: "municipal", scope_note: null, names: [], names_note: null,
  munis: [emptyMunicipal], overall};
availability = resultCols("all").find(x => x.t === "List totals availability");
assert(availability.f(availability.v(vrow(emptyMunicipal))).includes("Unavailable - no expected ballot rows"));
csv = tallyCsv();
assert(csv.includes("Unavailable - no expected ballot rows"));
setHero(true);
assert(bodyHero && selectedTab === "results" && els.herobtn.textContent === "Излаз (Esc)");
setHero(false);
assert(!bodyHero && els.herobtn.textContent === "Режим презентације");
console.log(JSON.stringify({ok: true}));
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}
