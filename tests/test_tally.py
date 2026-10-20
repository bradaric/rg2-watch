import copy

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
    assert tally(s)["munis"][0]["m"] == "(unknown)"


def test_list_names_mapping_and_default(tmp_path):
    s = store()
    form(s, 12, clean_report())
    t = tally(s, cfg=Config(tally_scope=NAT.tally_scope, list_names={P: ["Zeta", " "]}))
    assert t["names"] == ["Zeta", "List 2"]
    assert tally(s)["names"] == ["List 1", "List 2"]
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
    assert "0 form(s) counted" in format_tally(tally(s))


def test_hero_markup_labelled_and_safe():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="hero"' in html and "UNOFFICIAL - numbers read by" in html and "Very early" in html
    assert "#E69F00" in html and "createElementNS" not in html and "innerHTML" not in html.replace("never innerHTML", "")
    assert "Escape" in html and 'get("hero") === "1"' in html
    assert 'id="hs-forms"' in html
    assert "selected forms contributed vote totals" in html
    assert "Unreconciled turnout gap" in html
    assert "corrected-only eligible" in html and "filed-only eligible" in html


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
    assert 'T.scope === "municipal"' in html and "pick a municipality for list results" in html
    assert '"Share %"' in html and "listsOf(" in html and 'id="tscope"' in html


def test_municipal_unknown_municipality_gets_no_list_totals():
    s = store()
    two_ballots(s)
    form(s, 9, clean_report(), muni=None)
    t = tally(s, cfg=muni_cfg())
    u = next(m for m in t["munis"] if m["m"] == "(unknown)")
    assert u["nlists"] == 0 and u["names"] == [] and "different ballots" in u["names_note"]
    assert u["counted"] == 1 and u["all"]["valid"] == 0 and u["all"]["withheld_valid"] == 780
    assert "lists" not in u["all"] and "lists" not in u["cl"]
    assert "(unknown): warning: municipality unknown" in format_tally(t)
    nat = next(m for m in tally(s)["munis"] if m["m"] == "(unknown)")
    assert "lists" in nat["all"]  # national scope: one ballot, lists kept


def test_page_diff_view_uses_paired_numbers_and_explains_unmatched_terms():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "deltaOf(r.pair.all.c, r.pair.all.f)" in html and "terms only in corrected/filed view" in html
    assert "vote-reconciliation eligibility" in html and "deltaOf(r.all, r.fall)" not in html
    assert "pair_selected" in html and "pair_vote_forms" in html and "pair_not_contributing" in html
