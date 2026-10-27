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


def test_tally_exposes_per_station_results_for_operator_drilldown():
    s = store()
    form(s, 12, clean_report())

    result = tally(s, include_station_results=True)

    assert result["station_results"] == [{
        "id": result["station_results"][0]["id"], "m": "Alpha", "no": "12", "name": "ОШ",
        "source_round": "original",
        "annulled": None, "undetermined": None,
        "corrected": {"clean": True, "included": True, "reason": None,
                      "registered": 1000, "voted": 800, "ballots_in_box": 800,
                      "invalid": 20, "valid": 780, "outside_voted": 5,
                      "lists": [400, 380]},
        "filed": None,
    }]


def test_station_results_include_optional_settlement_and_address_enrichment():
    s = store()
    form(s, 7, clean_report(), muni="Градска општина Гроцка")

    result = tally(s, include_station_results=True)

    station = result["station_results"][0]
    assert station["settlement"] == "ВИНЧА"
    assert station["address"] == "ВИНЧА, ЈОВАНА ЈОВАНОВИЋА ЗМАЈА 1"
    assert result["station_location_source"]["source"] == "https://bm.itblokada.org/"


def test_active_rg9_annuls_station_and_removes_it_from_every_tally_view(tmp_path):
    s = store()
    rg2 = form(s, 12, clean_report())
    rg9 = doc("/extfile/rg9.pdf", muni="Alpha", no="12", dt=9)
    add(s, rg9)

    result = tally(s, include_station_results=True)

    assert result["overall"]["counted"] == 1
    assert result["overall"]["clean"] == 0
    assert result["overall"]["annulled"] == 1
    assert result["munis"][0]["annulled"] == 1
    for view in ("all", "cl", "fall", "fcl"):
        assert result["overall"][view]["registered"] == 0
        assert result["overall"][view]["valid"] == 0
        assert result["overall"][view]["lists"] == [0, 0]
    assert why(result) == {"12": ["annulled by RG-9"]}
    rec = result["reconciliation"][0]
    assert rec["result"] == {
        "corrected": {"included": False, "reason": "annulled by RG-9"},
        "filed": {"included": False, "reason": "annulled by RG-9"},
    }
    station = result["station_results"][0]
    assert station["corrected"]["included"] is False
    assert station["corrected"]["clean"] is False
    assert station["annulled"]["rik"] == BASE + "/extfile/rg9.pdf"
    assert result["annulled"] == [{"id": station["id"], "m": "Alpha", "no": "12",
                                    "rik": BASE + "/extfile/rg9.pdf"}]
    text = format_tally(result)
    assert "поништена бирачка места (RG-9):" in text
    assert "Alpha БМ 12: Поништено" in text
    browse_row = build_data(s, tmp_path, parent=P, cfg=Config(model=M, tally_scope={P: "national"}))["rows"][0]
    assert browse_row["rec"]["annulled"]["rik"] == BASE + "/extfile/rg9.pdf"
    s.set_review(rg2.url, "escalate")
    overlap = tally(s)["overall"]
    assert (overlap["held"], overlap["annulled"], overlap["held_annulled"]) == (1, 1, 1)


def test_removed_rg9_does_not_annul_station():
    s = store()
    form(s, 12, clean_report())
    rg9 = doc(BASE + "/extfile/removed-rg9.pdf", muni="Alpha", no="12", dt=9)
    add(s, rg9)
    add(s, rg9, "removed")

    result = tally(s, include_station_results=True)

    assert result["overall"]["all"]["registered"] == 1000
    assert result["station_results"][0]["annulled"] is None


def test_active_rg8_marks_results_undetermined_and_removes_station_from_every_tally_view(tmp_path):
    s = store()
    form(s, 12, clean_report())
    rg8 = doc("/extfile/rg8.pdf", muni="Alpha", no="12", dt=8)
    add(s, rg8)

    result = tally(s, include_station_results=True)

    assert result["overall"]["counted"] == 1
    assert result["overall"]["undetermined"] == 1
    assert result["munis"][0]["undetermined"] == 1
    for view in ("all", "cl", "fall", "fcl"):
        assert result["overall"][view]["registered"] == 0
        assert result["overall"][view]["valid"] == 0
        assert result["overall"][view]["lists"] == [0, 0]
    assert why(result) == {"12": ["results cannot be determined by RG-8"]}
    rec = result["reconciliation"][0]
    assert rec["result"] == {
        "corrected": {"included": False, "reason": "results cannot be determined by RG-8"},
        "filed": {"included": False, "reason": "results cannot be determined by RG-8"},
    }
    station = result["station_results"][0]
    assert station["corrected"]["included"] is False
    assert station["undetermined"]["rik"] == BASE + "/extfile/rg8.pdf"
    assert result["undetermined"] == [{"id": station["id"], "m": "Alpha", "no": "12",
                                        "rik": BASE + "/extfile/rg8.pdf"}]
    text = format_tally(result)
    assert "резултати се не могу утврдити (RG-8):" in text
    assert "Alpha БМ 12: Резултати се не могу утврдити" in text
    browse_row = build_data(s, tmp_path, parent=P,
                            cfg=Config(model=M, tally_scope={P: "national"}))["rows"][0]
    assert browse_row["rec"]["undetermined"]["rik"] == BASE + "/extfile/rg8.pdf"


def test_rg9_takes_display_precedence_when_rg8_and_rg9_are_both_active():
    s = store()
    form(s, 12, clean_report())
    add(s, doc(BASE + "/extfile/rg8.pdf", muni="Alpha", no="12", dt=8))
    add(s, doc(BASE + "/extfile/rg9.pdf", muni="Alpha", no="12", dt=9))

    result = tally(s, include_station_results=True)

    assert (result["overall"]["undetermined"], result["overall"]["annulled"],
            result["overall"]["decision_excluded"]) == (1, 1, 1)
    station = result["station_results"][0]
    assert station["undetermined"] is not None and station["annulled"] is not None
    assert station["corrected"]["reason"] == "annulled by RG-9"


def test_repeat_annulment_does_not_leak_into_a_later_repeat_round():
    s = store()
    form(s, 12, clean_report())
    repeat_i = doc(BASE + "/e/repeat-I.pdf", muni="Alpha", no="12", dt=10,
                   up="2026-10-20T20:00")
    add(s, repeat_i)
    put(s, repeat_i, clean_report())
    add(s, doc(BASE + "/e/repeat-I-rg9.pdf", muni="Alpha", no="12", dt=28,
               up="2026-10-20T22:00"))
    repeat_ii = doc(BASE + "/e/repeat-II.pdf", muni="Alpha", no="12", dt=15,
                    up="2026-10-21T20:00")
    add(s, repeat_ii)
    put(s, repeat_ii, clean_report())

    result = tally(s, include_station_results=True)

    assert result["overall"]["annulled"] == 0
    assert result["overall"]["all"]["registered"] == 1000
    assert result["decision_notices"] == [{
        "id": result["station_results"][0]["id"], "m": "Alpha", "no": "12",
        "label": "RG-9-R belongs to superseded repeat vote I",
        "rik": BASE + "/e/repeat-I-rg9.pdf",
    }]
    assert result["station_results"][0]["annulled"] is None


def test_repeat_annulment_after_selected_repeat_excludes_it():
    s = store()
    repeat_ii = doc(BASE + "/e/repeat-II.pdf", muni="Alpha", no="12", dt=15,
                    up="2026-10-21T20:00")
    add(s, repeat_ii)
    put(s, repeat_ii, clean_report())
    add(s, doc(BASE + "/e/repeat-II-rg9.pdf", muni="Alpha", no="12", dt=28,
               up="2026-10-21T22:00"))

    result = tally(s, include_station_results=True)

    assert result["overall"]["annulled"] == 1
    assert result["overall"]["all"]["registered"] == 0
    assert result["station_results"][0]["annulled"]["rik"].endswith("repeat-II-rg9.pdf")


def test_first_round_decision_gets_no_repeat_round_warning():
    s = store()
    form(s, 12, clean_report())
    add(s, doc(BASE + "/e/original-rg9.pdf", muni="Alpha", no="12", dt=9,
               up="2026-10-19T22:00"))
    repeat_ii = doc(BASE + "/e/repeat-II.pdf", muni="Alpha", no="12", dt=15,
                    up="2026-10-21T20:00")
    add(s, repeat_ii)
    put(s, repeat_ii, clean_report())

    result = tally(s, include_station_results=True)

    assert result["overall"]["annulled"] == 0
    assert result["decision_warnings"] == []
    assert result["decision_notices"] == []


@pytest.mark.parametrize("decision_time", ["2026-10-21T20:00", "not-a-time", None])
def test_ambiguous_repeat_decision_time_is_not_applied(decision_time):
    s = store()
    repeat_i = doc(BASE + "/e/repeat-I.pdf", muni="Alpha", no="12", dt=10,
                   up="2026-10-20T20:00")
    add(s, repeat_i)
    put(s, repeat_i, clean_report())
    repeat_ii = doc(BASE + "/e/repeat-II.pdf", muni="Alpha", no="12", dt=15,
                    up="2026-10-21T20:00")
    add(s, repeat_ii)
    put(s, repeat_ii, clean_report())
    add(s, doc(BASE + "/e/ambiguous-rg9.pdf", muni="Alpha", no="12", dt=28,
               up=decision_time))

    result = tally(s, include_station_results=True)

    assert result["overall"]["annulled"] == 0
    assert result["overall"]["all"]["registered"] == 1000
    assert result["decision_warnings"] == [{
        "id": result["station_results"][0]["id"], "m": "Alpha", "no": "12",
        "label": "RG-9-R round ambiguous", "rik": BASE + "/e/ambiguous-rg9.pdf",
    }]


def test_same_round_replacement_keeps_decision_bound_to_that_round():
    s = store()
    old = doc(BASE + "/e/repeat-II-old.pdf", muni="Alpha", no="12", dt=15,
              up="2026-10-21T20:00")
    add(s, old)
    put(s, old, clean_report())
    add(s, doc(BASE + "/e/repeat-II-rg9.pdf", muni="Alpha", no="12", dt=28,
               up="2026-10-21T21:00"))
    replacement = doc(BASE + "/e/repeat-II-new.pdf", muni="Alpha", no="12", dt=15,
                      up="2026-10-21T22:00")
    add(s, replacement, "replaced", old.url)
    put(s, replacement, clean_report())

    result = tally(s, include_station_results=True)

    assert result["overall"]["annulled"] == 1
    assert result["station_results"][0]["id"] == next(
        r["id"] for r in s.browse_rows(P)[0] if r["url"] == replacement.url)


def test_current_escalated_form_is_held_from_every_tally_view():
    s = store()
    form(s, 12, clean_report())
    held = form(s, 13, report(registered=nf(500), voted=nf(400), ballots_in_box=nf(400),
                              invalid=nf(10), valid=nf(390),
                              list_votes=[{"row": 1, "field": nf(300)},
                                          {"row": 2, "field": nf(90)}]))
    s.set_review(held.url, "escalate")

    result = tally(s)
    overall = result["overall"]

    assert overall["counted"] == 2 and overall["held"] == 1
    assert overall["held_clean"] == 1 and overall["held_fclean"] == 1
    assert overall["all"]["registered"] == 1000
    assert overall["all"]["voted"] == 800
    assert overall["all"]["valid"] == 780
    assert overall["all"]["lists"] == [400, 380]
    assert overall["cl"] == overall["all"]
    assert [(row["no"], row["verdict"]) for row in result["held"]] == [("13", "escalate")]
    held_rec = next(row for row in result["reconciliation"] if row["no"] == "13")
    assert held_rec["result"] == {
        "corrected": {"included": False, "reason": "held by escalation"},
        "filed": {"included": False, "reason": "held by escalation"},
    }
    text = format_tally(result)
    assert "held by escalation" in text and "1 held" in text

    s.set_review(held.url, "unsure")
    released_result = tally(s)
    released = released_result["overall"]
    assert released["held"] == 0
    assert released["all"]["registered"] == 1500
    assert released["all"]["valid"] == 1170
    released_rec = next(row for row in released_result["reconciliation"] if row["no"] == "13")
    assert released_rec["result"]["corrected"] == {"included": True, "reason": None}


def test_escalation_of_replaced_version_does_not_hold_current_form():
    s = store()
    old = form(s, 12, clean_report())
    s.set_review(old.url, "escalate")
    new = doc(BASE + "/e/replacement.pdf", muni="Alpha", no="12")
    add(s, new, "replaced", old.url)
    put(s, new, clean_report())

    result = tally(s)

    assert result["overall"]["held"] == 0
    assert result["overall"]["all"]["registered"] == 1000
    assert result["held"] == []


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


def test_reconciliation_status_reports_effective_arithmetic_or_unavailable_values():
    s = store()
    form(s, 12, clean_report())
    broken = report(invalid=nf(21),
                    list_votes=[{"row": 1, "field": nf(400)}, {"row": 2, "field": nf(379)}])
    form(s, 13, broken)
    form(s, 14, report(valid=nf(None, legible=False)))
    form(s, 15, report(voted=nf(801)))

    result = tally(s)
    by_no = {x["no"]: x for x in result["reconciliation"]}
    assert by_no["12"] == {"id": by_no["12"]["id"], "m": "Alpha", "no": "12",
                            "status": "ok", "rules": [], "details": [], "gap": 0,
                            "result": {
                                "corrected": {"included": True, "reason": None},
                                "filed": {"included": True, "reason": None},
                            }}
    assert by_no["13"]["status"] == "fail" and by_no["13"]["rules"] == ["A2", "A3"]
    assert "invalid 21 + valid 780" in by_no["13"]["details"][0]
    assert by_no["14"]["status"] == "unavailable" and by_no["14"]["rules"] == []
    assert by_no["15"]["status"] == "ok" and by_no["15"]["gap"] == 1
    total = result["overall"]["all"]
    assert sum(x["gap"] for x in by_no.values()) == total["voted"] - total["valid"] - total["invalid"]


def test_empty_outside_voted_is_not_a_reason_but_illegible_is():
    s = store()
    form(s, 12, report(outside_voted=nf(None, legible=False, mark="empty")))
    form(s, 13, report(outside_voted=nf(None, legible=False, mark="clear")))
    assert list(why(tally(s))) == ["13"]


def test_unreadable_list_row_excludes_form():
    s = store()
    form(s, 12, report(list_votes=[{"row": 1, "field": nf(400)}, {"row": 2, "field": nf(None, legible=False)}]))
    assert "list row 2 illegible" in why(tally(s))["12"][0]


def test_empty_list_row_counts_as_zero_when_reconciled():
    rep = report(list_votes=[{"row": 1, "field": nf(780)},
                             {"row": 2, "field": nf(None, legible=False, mark="empty")}])
    ex = extract(rep, doc(BASE + "/blank.pdf", no="12"), Config())
    assert ex["l"] == {1: 780, 2: 0}
    assert ex["bad"] == [] and not ex["rec_bad"]
    assert _contrib(ex, [1, 2])["lists"] == ([780, 0], 780, 800)


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


def test_removed_form_is_left_out():
    s = store()
    d = form(s, 12, clean_report())
    s.apply_events(P, s.start_sweep(P, "t"), [Event("removed", d)])
    assert tally(s)["overall"]["counted"] == 0


def test_highest_repeat_vote_form_replaces_original_station_result():
    s = store()
    form(s, 12, clean_report())
    repeat = doc(BASE + "/e/repeat-II.pdf", muni="Alpha", no="12", dt=15,
                 up="2026-10-20T20:15")
    add(s, repeat)
    put(s, repeat, report(registered=nf(500), voted=nf(400), ballots_in_box=nf(400),
                          invalid=nf(10), valid=nf(390),
                          list_votes=[{"row": 1, "field": nf(300)},
                                      {"row": 2, "field": nf(90)}]))

    result = tally(s, include_station_results=True)

    assert result["overall"]["counted"] == 1
    assert result["overall"]["all"]["registered"] == 500
    assert result["station_results"][0]["source_round"] == "repeat_ii"
    assert result["station_results"][0]["id"] == next(
        r["id"] for r in s.browse_rows(P)[0] if r["url"] == repeat.url)


@pytest.mark.parametrize("kind", ["rg7", "other", "uncertain"])
def test_non_rg2_repeat_does_not_supersede_legitimate_original(kind):
    s = store()
    original = form(s, 12, clean_report())
    repeat = doc(BASE + f"/e/{kind}.pdf", muni="Alpha", no="12", dt=15,
                 up="2026-10-20T20:15")
    add(s, repeat)
    rep = report(registered=nf(500), voted=nf(400), ballots_in_box=nf(400),
                 invalid=nf(10), valid=nf(390),
                 list_votes=[{"row": 1, "field": nf(300)}, {"row": 2, "field": nf(90)}])
    rep["document_classification"] = {"kind": kind, "evidence": "not an RG-2 form"}
    put(s, repeat, rep)

    result = tally(s, include_station_results=True)

    assert result["overall"]["all"]["registered"] == 1000
    assert result["station_results"][0]["id"] == next(
        r["id"] for r in s.browse_rows(P)[0] if r["url"] == original.url)


def test_exact_url_override_can_confirm_and_revoke_quarantined_repeat():
    s = store()
    form(s, 12, clean_report())
    repeat = doc(BASE + "/e/mislabeled.pdf", muni="Alpha", no="12", dt=15,
                 up="2026-10-20T20:15")
    add(s, repeat)
    rep = report(registered=nf(500), voted=nf(400), ballots_in_box=nf(400),
                 invalid=nf(10), valid=nf(390),
                 list_votes=[{"row": 1, "field": nf(300)}, {"row": 2, "field": nf(90)}])
    rep["document_classification"] = {"kind": "rg7", "evidence": "typed correction decision"}
    put(s, repeat, rep)

    s.add_classification_override(repeat.url, "confirm_rg2")
    assert tally(s)["overall"]["all"]["registered"] == 500

    s.add_classification_override(repeat.url, "revoke")
    assert tally(s)["overall"]["all"]["registered"] == 1000


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
    assert 'SOURCE_ROUND_LABEL = {original: "original RG-2"' in html


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
    assert ('id="hero"' in html and "НЕЗВАНИЧНО" in html and
            "Рани резултати — обрађено је мало бирачких места; проценти се могу знатно променити." in html)
    assert "--bar:#b51e34" in html and "--bar:#e84a5f" in html
    assert "createElementNS" not in html and "innerHTML" not in html.replace("never innerHTML", "")
    assert "Esc" in html and 'get("hero") === "1"' in html and 'ev.key === "Escape"' in html
    assert 'id="hs-forms-primary"' in html and 'id="hs-forms-context"' in html
    assert '<span id="hv-forms">-</span><span class="of" id="hv-formstot"></span>' in html
    for text in ("Општина", "Обухваћени записници", "Приказ података", "Цео екран",
                 "Излаз (Esc)", "Излазност", "Обрађена бирачка места", "Записници", "Важећи гласови"):
        assert text in html
    assert "Режим презентације" not in html
    assert "помоћу АИ" not in html
    assert "само записници без уочених проблема (без оних за проверу)" in html
    assert "са исправкама (RG-7 / ручни унос)" in html
    assert "оригинални подаци (RG-2)" in html
    assert "разлика (са исправкама − оригинални)" in html
    assert 'id="hs-valid-primary"' in html and 'id="hs-valid-context"' in html
    assert "#hs-valid-primary,#hs-valid-context{white-space:pre-line}" in html
    assert "#hs-valid-context{border-top:1px solid var(--line2);margin-top:.7em;padding-top:.7em;font-size:.9em}" in html
    assert "#hs-forms-primary,#hs-forms-context{white-space:pre-line}" in html
    assert "#hs-forms-context{border-top:1px solid var(--line2)" in html
    assert '"За проверу: " + num(seen.size)' in html and 'serbianCount(seen.size, "записник"' in html
    assert "урачунато у резултат" in html
    assert "Held by escalation" in html and "Изузето из резултата" in html
    assert "Ван заједничког збира: " in html
    assert '"Неусаглашено: " + snum(gap)' in html
    assert "само са исправкама: " in html and "само оригинално: " in html


def test_hero_warning_translates_every_current_tally_warning_and_preserves_unknown_text():
    from rg2watch.tally import TALLY_WARNING_TEMPLATES

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert TALLY_WARNING_TEMPLATES == {
        "unknown_type": "unknown election type - set tally_scope",
        "unknown_municipality": "municipality unknown - forms may be from different ballots, no list totals",
        "empty_ballot": "no expected ballot rows - comparable list totals unavailable",
        "national_names_shape": "list_names must be a plain list for a national election",
        "municipal_names_shape": "list_names must map municipality -> names for a municipal election",
        "names_count": "list_names has {names} names, forms have {lists} lists - check config",
    }
    start = html.index("function serbianCount")
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
  ["list_names has 1 names, forms have 1 lists - check config", "list_names има 1 назив, записници имају 1 листу — проверите конфигурацију"],
  ["list_names has 2 names, forms have 2 lists - check config", "list_names има 2 назива, записници имају 2 листе — проверите конфигурацију"],
  ["list_names has 5 names, forms have 5 lists - check config", "list_names има 5 назива, записници имају 5 листа — проверите конфигурацију"],
  ["list_names has 12 names, forms have 12 lists - check config", "list_names има 12 назива, записници имају 12 листа — проверите конфигурацију"],
  ["list_names has 22 names, forms have 22 lists - check config", "list_names има 22 назива, записници имају 22 листе — проверите конфигурацију"],
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
    assert a["lists_ballots_in_box"] == 800


def test_list_share_denominator_uses_valid_plus_invalid_when_ballots_in_box_is_unavailable():
    s = store()
    form(s, 1, report(ballots_in_box=nf(None, legible=False)))

    totals = tally(s)["overall"]["all"]

    assert totals["lists"] == [400, 380]
    assert totals["lists_valid"] == 780
    assert totals["lists_ballots_in_box"] == 800
    assert totals["ballots_in_box"] == 800
    result = tally(s)
    for view in ("corrected", "filed"):
        text = format_tally(result, view=view)
        assert "share of ballots in the box for reconciled vote forms" in text
        assert "400 (50.0%)" in text


def test_form_missing_invalid_contributes_no_aggregate_numbers():
    s = store()
    form(s, 1, report(invalid=nf(None, legible=False)))

    result = tally(s)
    totals = result["overall"]["all"]

    assert totals["vote_forms"] == 0
    assert totals["lists"] == [0, 0]
    assert totals["lists_ballots_in_box"] == 0
    assert all(totals[key] == 0 for key in
               ("registered", "voted", "ballots_in_box", "invalid", "valid", "outside_voted"))
    assert result["reconciliation"][0]["result"]["corrected"] == {
        "included": False, "reason": "invalid unavailable for percentage denominator"}


def test_cli_list_shares_use_ballots_in_box_in_national_and_municipal_scopes():
    s = store()
    form(s, 1, clean_report())

    for cfg in (NAT, muni_cfg()):
        result = tally(s, cfg=cfg)
        for view in ("corrected", "filed"):
            text = format_tally(result, view=view)
            assert "400 (50.0%)" in text
            assert "380 (47.5%)" in text


def test_incomplete_forms_contribute_no_totals_including_turnout():
    s = store()
    form(s, 1, clean_report())
    form(s, 2, report(valid=nf(770)))
    t = tally(s)
    a = t["overall"]["all"]
    assert a["registered"] == 1000 and a["voted"] == 800
    assert a["valid"] == a["lists_valid"] == sum(a["lists"]) == 780
    assert a["invalid"] == 20
    assert a["vote_forms"] == 1
    assert a["withheld_valid"] == 0
    assert t["overall"]["counted"] == 2
    rec = {x["no"]: x["result"] for x in t["reconciliation"]}
    assert rec["1"]["corrected"] == {"included": True, "reason": None}
    assert rec["2"]["corrected"] == {
        "included": False, "reason": "list votes sum 780 != valid 770"}
    assert rec["2"]["filed"] == rec["2"]["corrected"]
    assert any(reason.startswith("arithmetic") and "A3" in reason for reason in why(t)["2"])
    text = format_tally(t)
    assert "1 of 2 selected forms contributed reconciled vote totals" in text
    assert "770 readable valid votes withheld" not in text
    assert "Unreconciled turnout gap: +800" not in text


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
    assert _contrib(unreadable_list, rows) == {}
    assert _contrib(wrong_rows, rows) == {}
    assert _contrib(unreadable_invalid, rows) == {}
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


def test_operator_short_title_removes_records_prefix_with_or_without_separator():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    helper = next(line for line in html.splitlines() if line.startswith("const shortTitle ="))
    script = helper + r'''
const assert = require("assert");
assert.equal(shortTitle("Записници Парламентарни избори 2026"), "Парламентарни избори 2026");
assert.equal(shortTitle("Записници - Локални избори 2026"), "Локални избори 2026");
assert.equal(shortTitle("Zapisnici: Parliamentary election 2026"), "Parliamentary election 2026");
assert.equal(shortTitle("Парламентарни избори 2026"), "Парламентарни избори 2026");
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_operator_results_use_editorial_accents():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "#herohead{" in html and "border-bottom:2px solid var(--accent)" in html
    assert ".kicker{" in html and "background:var(--accent-bg)" in html
    assert "#heronums .tile:first-child{box-shadow:inset 0 3px var(--accent)" in html
    assert '<div class="hero-eyebrow">Пресек резултата</div>' in html
    assert ".resultgroup h3{" in html and "border-left:3px solid var(--accent)" in html
    assert "body.hero>*:not(#resview):not(.serbian-flag-bg){display:none!important}" in html
    assert ".resultvotes{" in html and ".resultpct{" in html
    assert ".resultvotes{color:var(--mut)" in html
    assert ".resultpct{color:var(--fg);font-weight:800" in html
    assert ".minifill{display:block;" in html
    assert ".stationshare.single .minibar{" in html


def test_operator_station_result_names_get_more_room_than_bars():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert ".stationbar{display:grid;grid-template-columns:minmax(12rem,38%) minmax(8rem,1fr) 11rem" in html


def test_operator_station_results_use_one_attribution_instead_of_per_station_addresses():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert '<p class="note stationattribution">Подаци о насељима: <a href="https://bm.itblokada.org/"' in html
    assert '"Адреса: " + station.address' not in html


def test_operator_mobile_station_rows_use_labeled_metrics_without_compact_bars():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert ".station summary{grid-template-columns:auto repeat(3,minmax(0,1fr))" in html
    assert '.stationturnout::before{content:"ИЗЛАЗНОСТ"}' in html
    assert '.stationvalid::before{content:"ВАЖЕЋИ"}' in html
    assert '.stationwinner::before{content:"ПОБЕДНИК"}' in html
    assert ".stationshare,.stationmargin{display:none}" in html


def test_operator_results_sort_municipality_picker_by_serbian_alphabet():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'const SERBIAN_MUNICIPALITY_COLLATOR = new Intl.Collator("sr-Cyrl")' in html
    assert 'rows = sortHeroMunis(T ? T.munis : [])' in html
    helper = html[html.index("const SERBIAN_MUNICIPALITY_COLLATOR"):html.index("function heroListNames")]
    script = helper + r'''
const assert = require("assert");
const rows = ["Љиг", "Ћуприја", "Ада", "Шид", "Лапово", "Тител", "Чока", "Џеп"].map(m => ({m}));
assert.deepEqual(sortHeroMunis(rows).map(row => row.m),
  ["Ада", "Лапово", "Љиг", "Тител", "Ћуприја", "Чока", "Џеп", "Шид"]);
assert.deepEqual(rows.map(row => row.m),
  ["Љиг", "Ћуприја", "Ада", "Шид", "Лапово", "Тител", "Чока", "Џеп"], "must not mutate tally order");
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_forms_and_results_share_the_url_municipality_context():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "function setHeroMunicipality(" in html
    assert 'setHeroMunicipality(o.m || "", true);' in html
    assert '$("hmuni").addEventListener("input", () => setSharedMunicipality($("hmuni").value));' in html
    assert '$("m").value = value;' in html
    assert "redraw();" in html


def test_municipal_unknown_municipality_gets_no_list_totals():
    s = store()
    two_ballots(s)
    form(s, 9, clean_report(), muni=None)
    t = tally(s, cfg=muni_cfg())
    u = next(m for m in t["munis"] if m["m"] == "(unknown)")
    assert u["nlists"] == 0 and u["names"] == [] and "different ballots" in u["names_note"]
    assert u["fallback_name_indices"] == [] and u["generated_unknown_muni"] is True
    assert u["counted"] == 1
    assert all(u["all"][key] == 0 for key in
               ("registered", "voted", "ballots_in_box", "invalid", "valid", "withheld_valid"))
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
    assert "TOP_LISTS" not in html and 'Остале (" + rest.length' not in html
    assert "OKABE" not in html
    assert 'const RESULT_BAR_COLOR = "var(--bar)"' in html
    assert 'id="stationresults"' in html and "function renderStationResults(" in html
    assert 'data-station-filter="annulled"' in html
    assert 'data-station-filter="undetermined"' in html
    helpers = html[html.index('const nf = new Intl.NumberFormat("en-GB")'):html.index("function showForm")]
    hero = html[html.index("const RESULT_BAR_COLOR") : html.index('$("herobtn").onclick')]
    status = html[html.index("function paintHero"):html.index("// ---- table")]
    refresh = html[html.index("function rowOf"):html.index("function escalatedCsv")]
    script = r'''
const assert = require("assert");
const els = {};
function el(id) { return els[id] ||= {textContent: "", hidden: false, value: "", style: {}, children: [],
  listeners: {}, addEventListener(type, handler) { this.listeners[type] = handler; },
  classList: {remove() {}, add() {}, toggle() {}}, append(...x) { this.children.push(...x); },
  replaceChildren(...x) { this.children = x; }, setAttribute(name, value) { this[name] = value; },
  get options() { return this.children; }}; }
const $ = el;
function mk(tag, text, cls) { return {tag, textContent: text == null ? "" : String(text), className: cls || "", hidden: false, style: {}, dataset: {}, append(...nodes) { this.children = this.children || []; this.children.push(...nodes); }, replaceChildren(...nodes) { this.children = nodes; }}; }
function epoch() { return "20:00"; }
function shortTitle(x) { return x; }
function Option(text, value) { this.textContent = text; this.value = value; }
const STALE_S = 999999;
let S = null, D = {built: "2026-10-20T18:00:00Z", rows: []};
let bodyHero = false, selectedTab = "";
const document = {activeElement: null, body: {classList: {toggle(name, on) { if (name === "hero") bodyHero = on; }, contains() { return bodyHero; }}}};
let REV = null;
const LOCAL = {};
function rvOf(v) {
  return v.u in LOCAL ? LOCAL[v.u] : REV ? REV[v.u] || null : v.rv || null;
}
const HTTP = true;
let revKey = "";
let table = null;
let rows = [];
function setTitle() {}
function card() {}
function ago() {}
function dur() {}
function ruleEl() {}
function when() {}
function reviewCounts() {}
function alertForm() {}
function localHref(value) { return value ? "local:" + value : null; }
function rikHref(value) { return value ? value : null; }
function link(text, href) { const a = mk("a", text); a.href = href; a.target = "_blank"; return a; }
function setTab(name) { selectedTab = name; }
let shownForm = null;
function showForm(id, label) { shownForm = [id, label]; }
let redrawCalls = 0;
function redraw() { redrawCalls++; }
els.tview = {value: "corrected"};
els.tmode = {value: "all"};
''' + helpers + hero + status + refresh + r'''
for (let i = 0; i < 9; i++) slots.push({row: {hidden: true, title: ""}, no: {}, nm: {}, fill: {style: {}}, val: mk("div")});
function totals(valid, lists) {
  const o = {registered: 1000, voted: 100, turnout: .1, ballots_in_box: 100, invalid: 0,
    valid, outside_voted: 0, vote_forms: lists ? 1 : 0, withheld_valid: lists ? 0 : 100,
    lists: lists || [0, 0], lists_valid: valid, lists_ballots_in_box: lists ? 100 : 0};
  return o;
}
function row(correctedLists, filedLists) {
  const c = totals(correctedLists ? 100 : 0, correctedLists);
  const f = totals(filedLists ? 100 : 0, filedLists);
  const cohort = {c, f, in: 0, out: 0, pair_selected: 1, pair_vote_forms: correctedLists && filedLists ? 1 : 0,
    pair_not_contributing: correctedLists && filedLists ? 0 : 1,
    corrected_only_vote_forms: correctedLists && !filedLists ? 1 : 0,
    filed_only_vote_forms: filedLists && !correctedLists ? 1 : 0};
  return {m: "Alpha", stations: 1, counted: 1, clean: 1, excluded: 0, held: 0,
    held_clean: 0, held_fclean: 0, fclean: 1, fexcluded: 0,
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
assert.equal(csvValue(csv, "Held by escalation"), "0");
assert.equal(csvValue(csv, "Поништено (RG-9)"), "0");
assert.equal(csvValue(csv, "1 · Листа А votes"), "60");
assert.equal(csvValue(csv, "Unreconciled turnout gap (signed)"), "0");
renderHero();
assert(els["hs-valid-primary"].textContent.includes("Неусаглашено: 0"));
assert(els.herotitle.textContent.includes("Избори Ћирилица"));
assert.equal(slots[0].val.children[0].textContent, "60");
assert.equal(slots[0].val.children[0].className, "resultvotes");
assert.equal(slots[0].val.children[1].textContent, "60.0%");
assert.equal(slots[0].val.children[1].className, "resultpct");
assert.equal(slots[0].nm.textContent, "Листа А");
assert.equal(slots[1].nm.textContent, "Листа 2");
const ballotDenominator = row([400, 380], null);
for (const key of ["all", "cl"]) Object.assign(ballotDenominator[key],
  {valid: 780, ballots_in_box: 800, lists_valid: 780, lists_ballots_in_box: 800});
setData(ballotDenominator); renderHero();
assert.equal(slots[0].val.children[1].textContent, "50.0%");
assert.equal(slots[1].val.children[1].textContent, "47.5%");
assert.equal(csvValue(tallyCsv(), "1 · Листа А %"), "50.0");
ballotDenominator.all.lists_ballots_in_box = null;
renderHero();
assert.equal(slots[0].val.children[0].textContent, "400");
assert.equal(slots[0].val.children[1].textContent, "-");
ballotDenominator.all.lists_ballots_in_box = 800;
const differingDenominators = deltaOf(
  {...totals(100, [60, 40]), lists_ballots_in_box: 200},
  {...totals(100, [55, 45]), lists_ballots_in_box: 100});
assert.equal(differingDenominators.lists[0], 5);
assert(Math.abs(differingDenominators.sh[0] + .25) < 1e-12);
const missingPairedDenominator = deltaOf(
  {...totals(100, [60, 40]), lists_ballots_in_box: null},
  {...totals(100, [55, 45]), lists_ballots_in_box: 100});
assert.equal(missingPairedDenominator.lists[0], 5);
assert.equal(missingPairedDenominator.sh[0], null);
setData(r); renderHero();
T.names = ["List 1", "List 2"];
T.fallback_name_indices = [1];
renderHero();
assert.equal(slots[0].nm.textContent, "List 1", "configured literal List N must remain unchanged");
assert.equal(slots[1].nm.textContent, "Листа 2", "only the metadata-marked fallback is localized");
const renderedWarnings = [
  ["unknown election type - set tally_scope", "Непозната врста избора"],
  ["municipality unknown - forms may be from different ballots, no list totals", "Општина није позната"],
  ["no expected ballot rows - comparable list totals unavailable", "Нема очекиваних редова"],
  ["list_names must be a plain list for a national election", "list_names мора бити обична листа"],
  ["list_names must map municipality -> names for a municipal election", "list_names мора да пресликава општину"],
  ["list_names has 22 names, forms have 12 lists - check config", "22 назива, записници имају 12 листа"],
];
for (const [warning, expected] of renderedWarnings) {
  T.scope_note = warning;
  renderHero();
  assert(els.heronames.textContent.includes(expected));
  assert(!els.heronames.textContent.includes(warning));
}
T.scope_note = null;
r = row([60, 40], [55, 45]);
r.counted = 15; r.clean = 10; r.excluded = 5;
r.cl = {...r.cl};
r.all.vote_forms = 12; r.cl.vote_forms = 10;
r.all.invalid = 9; r.all.withheld_valid = 11; r.all.voted = 120;
setData(r);
els.tview.value = "corrected";
els.tmode.value = "all";
renderHero();
assert(els.herotitle.textContent.endsWith(" — са исправкама (RG-7 / ручни унос)"));
assert.equal(els["hv-forms"].textContent, "12");
assert.equal(els["hv-formstot"].textContent, " / 15");
assert.equal(els["hs-forms-primary"].textContent, "урачунато у резултат");
assert.deepEqual(els["hs-forms-context"].textContent.split("\n"), [
  "10 без уочених проблема",
  "За проверу: 0 записника — 0 прихваћено",
  "Није урачунато: 3 записника — подаци по листама нису потпуни или усаглашени",
]);
r.held = 1;
renderHero();
assert.equal(heldReviewSummary(13,11),
  "Изузето из резултата: 13 ескалираних записника — 11 из скупа за проверу · 2 додатно");
assert(els["hs-forms-context"].textContent.includes("Изузето из резултата: 1 ескалирани записник"));
assert(els["hs-forms-context"].textContent.includes(
  "Није урачунато: 2 записника — подаци по листама нису потпуни или усаглашени"));
r.held = 0;
assert.deepEqual(els["hs-valid-primary"].textContent.split("\n"), [
  "Неважећи: 9",
  "Неусаглашено: +11",
]);
assert.deepEqual(els["hs-valid-context"].textContent.split("\n"), [
  "Није урачунато: 11 важећих гласова — подаци по листама нису потпуни или усаглашени",
]);
assert(els.herofoot.children[0].includes("НЕЗВАНИЧНО — Скенирани записници обрађени помоћу вештачке интелигенције; ово нису резултати РИК-а. Стање у "));
assert(els.herofoot.children[0].includes("20:00"));
for (const absent of ["opaque-model-v1", "Сви обрађени записници.", "Са исправкама RG-7.", "(време у Србији)", "можда застарели", "АИ"])
  assert(!els.herofoot.children[0].includes(absent));
els.tmode.value = "cl";
renderHero();
assert.equal(els["hv-forms"].textContent, "10");
assert.equal(els["hv-formstot"].textContent, " / 10");
assert.deepEqual(els["hs-forms-context"].textContent.split("\n"), [
  "10 без уочених проблема",
  "За проверу: 0 записника — 0 прихваћено",
]);
assert.equal(els["hs-valid-context"].textContent, "");
assert(!els["hs-valid-context"].textContent.includes("У збиру:"));

els.tview.value = "filed";
r.fclean = 8; r.fexcluded = 7; r.fcl = {...r.fcl}; r.fall.vote_forms = 4; r.fcl.vote_forms = 3;
els.tmode.value = "all";
renderHero();
assert.equal(els.herotitle.textContent, "Све општине — Избори Ћирилица");
assert.equal(els["hv-forms"].textContent, "4");
assert.equal(els["hv-formstot"].textContent, " / 15");
assert.deepEqual(els["hs-forms-context"].textContent.split("\n"), [
  "3 без уочених проблема",
  "За проверу: 0 записника — 0 прихваћено",
  "Није урачунато: 11 записника — подаци по листама нису потпуни или усаглашени",
]);
els.tmode.value = "cl";
renderHero();
assert.equal(els["hv-forms"].textContent, "3");
assert.equal(els["hv-formstot"].textContent, " / 8");
assert.deepEqual(els["hs-forms-context"].textContent.split("\n"), [
  "3 без уочених проблема",
  "За проверу: 0 записника — 0 прихваћено",
  "Није урачунато: 5 записника — подаци по листама нису потпуни или усаглашени",
]);
assert(!els.herofoot.children[0].includes("оригинални"));

T.excluded = [{id: 1, m: "Alpha"}, {id: 2, m: "Alpha"}, {id: 3, m: "Alpha"}, {id: 4, m: "Alpha"}];
D.rows = [
  {id: 1, u: "u1", rv: {v: "ok"}, v: []},
  {id: 2, u: "u2", rv: {v: "escalate"}, v: []},
  {id: 3, u: "u3", rv: {v: "unsure"}, v: []},
  {id: 4, u: "u4", rv: null, v: [{u: "old4", rv: {v: "ok"}}]},
];
els.tview.value = "corrected";
els.tmode.value = "all";
renderHero();
assert(els["hs-forms-context"].textContent.includes(
  "За проверу: 4 записника — 1 прихваћено · 1 ескалирано · 1 несигурно · 1 без одлуке"));
REV = {u1: {v: "unsure"}, u2: {v: "ok"}, u3: {v: "ok"}};
renderHero();
assert(els["hs-forms-context"].textContent.includes(
  "За проверу: 4 записника — 2 прихваћено · 1 несигурно · 1 без одлуке"));

D.rows = [];
REV = null;
for (const k of Object.keys(LOCAL)) delete LOCAL[k];
heroMuni = "";
heroPicked = false;
els.tview.value = "corrected";
els.tmode.value = "all";

r = row([60, 40], null);
setData(r);
els.tview.value = "corrected";
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
assert(!els["hs-valid-primary"].textContent.includes("NaN"));
assert(!els["hs-valid-context"].textContent.includes("NaN"));

const values = Array.from({length: 13}, (_, i) => 13 - i);
const many = row(values, values);
setData(many);
T.names = values.map((_, i) => "Назив " + (i + 1));
T.fallback_name_indices = [];
renderHero();
assert.equal(slots.length, 13);
assert.equal(slots[0].nm.textContent, "Назив 1");
assert.equal(slots[12].nm.textContent, "Назив 13");
assert(!slots.some(x => x.nm.textContent.startsWith("Остале")));

r = row(null, [55, 45]);
setData(r);
renderHero();
assert.equal(slots[0].row.hidden, false, "filed-only eligible selected view must show the chart");
assert(!slots[0].val.children.map(x => x.textContent).join(" ").includes("NaN"));

els.tview.value = "diff";
r = row([60, 40], [55, 45]);
r.clean = 10; r.fclean = 8; r.excluded = 5; r.fexcluded = 7;
r.pair.all = {...r.pair.all, pair_selected: 13, pair_vote_forms: 5, pair_not_contributing: 8,
  corrected_only_vote_forms: 1, filed_only_vote_forms: 3, in: 2, out: 3};
r.pair.cl = {c: totals(100, [60, 40]), f: totals(100, [55, 45]), in: 4, out: 1,
  pair_selected: 6, pair_vote_forms: 2, pair_not_contributing: 4,
  corrected_only_vote_forms: 1, filed_only_vote_forms: 2};
for (const p of [r.pair.all, r.pair.cl]) {
  p.c.invalid = 10; p.f.invalid = 3;
  p.c.withheld_valid = 2; p.f.withheld_valid = 10;
  p.c.voted = 107; p.f.voted = 100;
}
setData(r);
els.tmode.value = "all";
renderHero();
assert(els.herotitle.textContent.endsWith(" — РАЗЛИКА (са исправкама RG-7 / ручни унос − оригинални RG-2)"));
assert.equal(els["hv-formstot"].textContent, "");
assert.equal(els["hs-forms-primary"].textContent, "промене броја: без уочених проблема / за проверу");
assert.equal(els["hs-forms-context"].textContent, "");
assert.deepEqual(els["hs-valid-primary"].textContent.split("\n"), [
  "Разлика неважећих: +7",
  "У оба приказа у збиру: 5 од 13 упарених записника",
  "Ван заједничког збира: 8 (само са исправкама: 1; само оригинално: 3; ни у једном: 4)",
  "Разлика неурачунатих важећих гласова: −8",
  "Разлика неусаглашене излазности: ·",
]);
assert.equal(els["hs-valid-context"].textContent, "");
assert.equal(els.heronote.textContent, "Ставке присутне само у приказу са исправкама / оригиналном приказу: +2 / −3; неупарене ставке су изостављене (можда се променила читљивост, избор записника без уочених проблема или прихватљивост усаглашених гласова).");
els.tmode.value = "cl";
renderHero();
assert.deepEqual(els["hs-valid-primary"].textContent.split("\n").slice(0, 3), [
  "Разлика неважећих: +7",
  "У оба приказа у збиру: 2 од 6 упарених записника",
  "Ван заједничког збира: 4 (само са исправкама: 1; само оригинално: 2; ни у једном: 1)",
]);
assert(els["hs-valid-primary"].textContent.includes("Разлика неурачунатих важећих гласова: −8"));
assert(els["hs-valid-primary"].textContent.includes("Разлика неусаглашене излазности: ·"));
els.tmode.value = "all";
assert(slots[0].val.children[1].textContent.includes("п. п."));
const diffCols = resultCols("all");
const selected = diffCols.find(x => x.t === "Selected in both views");
assert.equal(selected.f(selected.v(vrow(r))), "13");
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
assert(!els["hs-valid-primary"].textContent.includes("NaN"));
assert(!els["hs-valid-context"].textContent.includes("NaN"));

const zero = row(null, null);
zero.counted = zero.clean = zero.excluded = zero.fclean = zero.fexcluded = 0;
setData(zero);
renderHero();
assert.equal(els["hv-turn"].textContent, "-");
assert.equal(els.herotitle.textContent, "");
assert.equal(els["hs-valid-primary"].textContent, "");
assert.equal(els["hs-valid-context"].textContent, "");
assert(els.heronote.textContent.includes("Још нема обрађених записника"));
assert(slots.every(x => x.row.hidden));
T = null;
renderHero();
assert(els.herofoot.children[0].includes("резултати још нису доступни"));
assert(els.heronote.textContent.includes("Још нема анализираних записника"));
assert(slots.every(x => x.row.hidden));

const alpha = row([60, 40], [60, 40]);
alpha.m = "Ћуприја";
const unknown = row(null, null); unknown.m = "(unknown)"; unknown.names = []; unknown.nlists = 0;
const overall = row(null, null); overall.m = "(all)"; overall.names = []; overall.nlists = 0;
for (const r of [unknown, overall]) for (const k of ["all", "cl", "fall", "fcl"]) {
  delete r[k].lists; delete r[k].lists_valid;
}
T = {model: "test", title: "Local", scope: "municipal", scope_note: null, names: [], names_note: null,
  munis: [alpha, unknown], overall};
csv = tallyCsv();
assert(csv.includes("Ћуприја,1,A,60,60.0"), "municipal list rows must be long format");
assert(csv.includes("Unavailable - no comparable ballot"), "unknown ballot must be explicit");
assert(csv.includes("Unavailable - no universal ballot"), "municipal overall list fields must be explicit");
heroMuni = "Ћуприја"; heroPicked = true;
renderHero();
assert.equal(els.hmuni.options.find(o => o.value === "Ћуприја").textContent, "Ћуприја");
assert(els.herotitle.textContent.startsWith("Ћуприја"));
T.station_location_source = {source: "https://bm.itblokada.org/", revision: "test", retrieved: "2026-10-27"};
T.station_results = [{id: 9, m: "Ћуприја", no: "12", name: "ОШ", settlement: "СЕЊЕ", address: "СЕЊЕ, ЦЕНТАР 1",
  corrected: {clean: true, included: true, reason: null, registered: 1000, voted: 800, ballots_in_box: 800, invalid: 20, valid: 780, lists: [400, 380]},
  filed: {clean: true, included: false, reason: "list votes mismatch", registered: 1000, voted: 800, ballots_in_box: 800, invalid: 20, valid: 780, lists: [390, 390]}}];
D.rows = [{id: 9, u: "u9", rv: {v: "escalate", n: "Проверити потписе"}, pdf: "a".repeat(64), rik: "https://example.test/rg2.pdf"}];
$("stationbars").value = "2";
renderHero();
assert.equal(els.stationresults.hidden, false);
assert.equal(els.stationcount.textContent, "1 записник");
assert.equal(els.stationlist.children.length, 0, "collapsed station results must not render cards");
els.stationresults.open = true;
els.stationresults.listeners.toggle();
let stationCard = els.stationlist.children[0];
assert(stationCard.children[0].children[0].textContent.startsWith("БМ 12 · СЕЊЕ — ОШ"));
stationCard.open = true; stationCard.ontoggle();
assert(!stationCard.children[1].children.some(x => x.className === "stationlocation"));
stationCard.open = false; stationCard.ontoggle();
assert(!(stationCard.children[0].children[0].children || []).some(x => x.className === "stationmap"));
T.station_results[0].coordinates = {latitude: 44.664, longitude: 20.59089, location_confidence: 1};
renderHero();
stationCard = els.stationlist.children[0];
const stationGlobe = stationCard.children[0].children[0].children.find(x => x.className === "stationmap");
assert(stationGlobe.href.startsWith("https://www.openstreetmap.org/?mlat=44.664&mlon=20.59089"));
assert.equal(stationGlobe.target, "_blank");
assert(stationGlobe.title.includes("Непроверена"));
delete T.station_results[0].coordinates;
renderHero();
stationCard = els.stationlist.children[0];
assert(!(stationCard.children[0].children[0].children || []).some(x => x.className === "stationmap"));
const stationWinner = stationCard.children[0].children.find(x => x.className === "stationwinner");
assert.equal(stationWinner.children[0].textContent, "1");
assert.equal(stationWinner.children.length, 1, "compact winner column must show only the list number");
assert.equal(stationWinner.title, "A");
let stationShares = stationCard.children[0].children.find(x => x.className.startsWith("stationshare"));
assert.equal(stationShares.className, "stationshare");
assert.equal(stationShares.children.length, 2);
assert.equal(stationShares.children[0].children[2].textContent, "50.0%");
assert.equal(stationShares.children[1].children[0].textContent, "2");
assert.equal(stationShares.children[1].children[2].textContent, "47.5%");
const stationMarginEl = stationCard.children[0].children.find(x => x.className === "stationmargin visual");
assert(Math.abs(parseFloat(stationMarginEl.children[0].children[0].style.width) - 20 / 800 * 100) < 1e-9);
assert.equal(stationMarginEl.children[1].children[0].textContent, "+2.5 п. п.");
assert.equal(stationMarginEl.children[1].children[1].textContent, "+20");
T.station_results[0].corrected.ballots_in_box = null;
renderHero();
let unavailableCard = els.stationlist.children[0];
let unavailableShares = unavailableCard.children[0].children.find(x => x.className.startsWith("stationshare"));
assert.equal(unavailableShares.children[0].children[2].textContent, "50.0%");
T.station_results[0].corrected.invalid = null;
renderHero();
unavailableCard = els.stationlist.children[0];
unavailableShares = unavailableCard.children[0].children.find(x => x.className.startsWith("stationshare"));
assert.equal(unavailableShares.children[0].children[2].textContent, "—");
let unavailableMargin = unavailableCard.children[0].children.find(x => x.className === "stationmargin visual");
assert.equal(unavailableMargin.children[1].children[0].textContent, "—");
assert.equal(unavailableMargin.children[1].children[1].textContent, "+20");
T.station_results[0].corrected.lists = [390, 390];
renderHero();
unavailableCard = els.stationlist.children[0];
unavailableMargin = unavailableCard.children[0].children.find(x => x.className === "stationmargin visual");
assert.equal(unavailableMargin.children[1].children[0].textContent, "—");
assert.equal(unavailableMargin.children[1].children[1].textContent, "Нерешено");
T.station_results[0].corrected.ballots_in_box = 800;
T.station_results[0].corrected.invalid = 20;
T.station_results[0].corrected.lists = [400, 380];
renderHero();
stationCard = els.stationlist.children[0];
let stationStatusEl = stationCard.children[0].children.find(x => x.className.startsWith("stationstatus"));
let stationActions = (stationStatusEl.children || []).find(x => x.className === "stationactions");
assert(stationActions, "station status must have a compact actions menu");
assert.deepEqual(stationActions.menu.children.map(x => x.textContent), ["PDF", "RIK", "прикажи"]);
assert.equal(stationActions.menu.hidden, true);
T.station_results[0].annulled = {rik: "https://example.test/rg9.pdf"};
T.overall.annulled = T.munis[0].annulled = 1;
renderHero();
assert(els["hs-forms-context"].textContent.includes("Поништено: 1 бирачко место (RG-9)"));
stationCard = els.stationlist.children[0];
stationStatusEl = stationCard.children[0].children.find(x => x.className.startsWith("stationstatus"));
assert.equal(stationStatusEl.children[0].textContent, "Поништено");
stationActions = (stationStatusEl.children || []).find(x => x.className === "stationactions");
assert.deepEqual(stationActions.menu.children.map(x => x.textContent), ["PDF", "RIK", "RG-9", "прикажи"]);
assert.equal(els["stationfilter-annulled"].textContent, "Поништено 1");
setStationFilter("annulled");
assert.equal(els.stationlist.children.length, 1);
setStationFilter("all");
const formStatus = documentStatusEl({state: "current", ast: "done",
  rec: {annulled: {rik: "https://example.test/rg9.pdf"}}});
assert(formStatus.children.some(x => x.textContent === "Поништено — RG-9"));
assert.equal(formStatus.children.at(-1).textContent, "RG-9");
T.station_results[0].annulled = null;
T.overall.annulled = T.munis[0].annulled = 0;
renderHero();
T.station_results[0].undetermined = {rik: "https://example.test/rg8.pdf"};
T.overall.undetermined = T.munis[0].undetermined = 1;
T.overall.decision_excluded = T.munis[0].decision_excluded = 1;
renderHero();
assert(els["hs-forms-context"].textContent.includes("Резултати се не могу утврдити: 1 бирачко место (RG-8)"));
stationCard = els.stationlist.children[0];
stationStatusEl = stationCard.children[0].children.find(x => x.className.startsWith("stationstatus"));
assert.equal(stationStatusEl.children[0].textContent, "Неутврђено");
assert.equal(stationStatusEl.children[0].title, "Резултати се не могу утврдити (RG-8)");
stationActions = (stationStatusEl.children || []).find(x => x.className === "stationactions");
assert.deepEqual(stationActions.menu.children.map(x => x.textContent), ["PDF", "RIK", "RG-8", "прикажи"]);
assert.equal(els["stationfilter-undetermined"].textContent, "Не могу се утврдити 1");
setStationFilter("undetermined");
assert.equal(els.stationlist.children.length, 1);
const rg8FormStatus = documentStatusEl({state: "current", ast: "done",
  rec: {annulled: null, undetermined: {rik: "https://example.test/rg8.pdf"}}});
assert(rg8FormStatus.children.some(x => x.textContent === "Резултати се не могу утврдити — RG-8"));
assert.equal(rg8FormStatus.children.at(-1).textContent, "RG-8");
T.station_results[0].undetermined = null;
T.overall.undetermined = T.munis[0].undetermined = 0;
T.overall.decision_excluded = T.munis[0].decision_excluded = 0;
setStationFilter("all");
renderHero();
let prevented = 0, stopped = 0;
stationActions.button.onclick({preventDefault() { prevented++; }, stopPropagation() { stopped++; }});
assert.equal(stationActions.menu.hidden, false);
const stationShow = stationActions.menu.children.find(x => x.textContent === "прикажи");
assert.equal(stationShow.tag, "a");
assert.equal(stationShow.href, "#form=9");
assert.equal(stationShow.target, "_blank");
assert.equal(shownForm, null, "opening a deep link must leave the current Results tab untouched");
assert.equal(prevented, 1); assert.equal(stopped, 1);
function stationReviewEl() {
  const card = els.stationlist.children[0], status = card.children[0].children.find(x => x.className.startsWith("stationstatus"));
  return (status.children || []).find(x => x.className.startsWith("stationreview"));
}
els.tview.value = "filed";
renderHero();
const stationStatusMain = els.stationlist.children[0].children[0].children.find(x => x.className.startsWith("stationstatus"))
  .children.find(x => x.className.startsWith("stationstatusmain"));
assert(stationStatusMain.className.includes("hasreason"));
assert.equal(stationStatusMain.dataset.reason, "list votes mismatch");
assert.equal(stationStatusMain.tabIndex, 0);
assert.equal(stationReviewEl().textContent, "ескалиран");
assert.equal(stationReviewEl().dataset.note, "Проверити потписе");
assert.equal(stationReviewEl().tabIndex, 0);
assert(stationReviewEl().className.includes("hasnote"));
assert.equal(els["stationfilter-all"].textContent, "Сва бирачка места 1");
assert.equal(els["stationfilter-out"].textContent, "Није урачунато 1");
assert.equal(els["stationfilter-escalate"].textContent, "Ескалирано 1");
setStationFilter("escalate");
assert.equal(els.stationlist.children.length, 1);
REV = {u9: {v: "escalate", n: "Нова напомена"}}; renderHero();
assert.equal(stationReviewEl().dataset.note, "Нова напомена", "a changed note must repaint without a verdict change");
REV = {u9: {v: "unsure", n: "Поново упоредити"}}; renderHero();
assert.equal(els.stationlist.children.length, 1, "empty filtered results render one empty-state message");
assert.equal(els.stationlist.children[0].textContent, "Нема записника за изабрани филтер.");
setStationFilter("unsure");
assert.equal(stationReviewEl().textContent, "несигуран");
assert.equal(stationReviewEl().dataset.note, "Поново упоредити");
REV = {}; renderHero();
setStationFilter("undecided");
assert.equal(stationReviewEl().textContent, "без одлуке");
assert(!stationReviewEl().className.includes("hasnote"));
REV = {u9: {v: "ok", n: "Проверено"}}; renderHero();
setStationFilter("all");
assert.equal(stationReviewEl().textContent, "прихваћен");
assert.equal(stationReviewEl().dataset.note, "Проверено");
function sortableStation(id, no, voted, lists) {
  const result = {clean: true, included: false, reason: "test reason", registered: 1000,
    voted, ballots_in_box: lists[0] + lists[1], invalid: 0, valid: lists[0] + lists[1], lists};
  return {id, m: "Ћуприја", no: String(no), name: "БМ " + no, corrected: result, filed: result};
}
T.station_results = [
  sortableStation(10, 2, 500, [51, 49]),
  sortableStation(11, 10, 900, [90, 10]),
  sortableStation(12, 5, 700, [60, 40]),
  sortableStation(13, 1, 800, [55, 45]),
];
D.rows = [
  {id: 10, u: "u10", rv: {v: "unsure"}},
  {id: 11, u: "u11", rv: {v: "ok"}},
  {id: 12, u: "u12", rv: null},
  {id: 13, u: "u13", rv: {v: "escalate"}},
];
REV = null;
const stationIds = () => els.stationlist.children.filter(x => x.stationId).map(x => Number(x.stationId));
els.stationsort.value = "margin-close"; renderHero();
assert.deepEqual(stationIds(), [10, 13, 12, 11]);
els.stationsort.value = "margin-wide"; renderHero();
assert.deepEqual(stationIds(), [11, 12, 13, 10]);
els.stationsort.value = "turnout-high"; renderHero();
assert.deepEqual(stationIds(), [11, 13, 12, 10]);
els.stationsort.value = "turnout-low"; renderHero();
assert.deepEqual(stationIds(), [10, 12, 13, 11]);
els.stationsort.value = "review"; renderHero();
assert.deepEqual(stationIds(), [13, 10, 12, 11], "review sorting puts actionable states first");
assert.equal(els["stationfilter-all"].textContent, "Сва бирачка места 4");
assert.equal(els["stationfilter-out"].textContent, "Није урачунато 4");
assert.equal(els["stationfilter-unsure"].textContent, "Несигурно 1");
assert.equal(els["stationfilter-undecided"].textContent, "Без одлуке 1");
const rememberedOpen = els.stationlist.children.find(x => x.stationId === "10");
rememberedOpen.open = true; rememberedOpen.ontoggle();
setStationFilter("escalate");
assert.deepEqual(stationIds(), [13]);
setStationFilter("all");
assert.equal(els.stationlist.children.find(x => x.stationId === "10").open, true,
  "filtering a station out and back in must preserve its expansion state");
setStationFilter("escalate");
els.tview.value = "diff";
els.stationsort.value = "margin-wide";
renderHero();
assert.equal(els.stationfilters.hidden, true);
assert.equal(els.sortturnouthigh.textContent, "Највећа промена излазности");
assert.equal(els.sortturnoutlow.textContent, "Најмања промена излазности");
assert.deepEqual(stationIds(), [13, 10, 12, 11], "difference mode ignores review filters and incompatible sorts");
T.scope = "national"; T.names = ["A", "B"];
T.station_results = Array.from({length: 102}, (_, i) => {
  const station = sortableStation(1000 + i, i + 1, 500 + i, [300 + i, 190]);
  station.m = i % 2 ? "(unknown)" : "Ћуприја";
  return station;
});
D.rows = T.station_results.map(station => ({id: station.id}));
let stationSourceFinds = 0;
const nativeStationSourceFind = D.rows.find.bind(D.rows);
D.rows.find = (...args) => { stationSourceFinds++; return nativeStationSourceFind(...args); };
REV = null; els.tview.value = "corrected"; els.stationsort.value = "station";
setStationFilter("all");
let stationOutcomeCalls = 0, stationSourceCalls = 0;
const realStationOutcome = stationOutcome, realStationSource = stationSource;
stationOutcome = (...args) => { stationOutcomeCalls++; return realStationOutcome(...args); };
stationSource = (...args) => { stationSourceCalls++; return realStationSource(...args); };
heroMuni = ""; heroPicked = true; renderHero();
assert(stationSourceFinds <= 1, "all-municipality rendering must not scan every form for every station");
assert(stationOutcomeCalls <= 100, "station-order rendering computes outcomes only for visible rows");
assert(stationSourceCalls <= 205, "off-screen rows do not build action-link cache data");
stationOutcome = realStationOutcome; stationSource = realStationSource;
assert.equal(els.stationresults.hidden, false);
assert.equal(stationIds().length, 100);
assert(els.stationlist.children[0].children[0].children[0].textContent.includes(" · БМ "));
assert.equal(els.stationmore.hidden, false);
assert.equal(els.stationmore.textContent, "Прикажи још (2)");
loadMoreStations();
assert.equal(stationIds().length, 102);
assert.equal(els.stationmore.hidden, true);
heroMuni = "Ћуприја"; renderHero();
assert.equal(stationIds().length, 51, "a selected municipality renders every matching station");
assert.equal(els.stationmore.hidden, true);
assert(!els.stationlist.children[0].children[0].children[0].textContent.startsWith("Ћуприја ·"));
T.scope = "municipal"; unknown.names = ["X", "Y"]; unknown.nlists = 2;
heroMuni = ""; els.stationsort.value = "winner"; renderHero();
assert.equal(els.sortwinner.hidden, true);
assert.equal(els.stationsort.value, "station");
T.station_results = [{id: 9, m: "Ћуприја", no: "12", name: "ОШ",
  corrected: {clean: true, included: true, reason: null, registered: 1000, voted: 800, ballots_in_box: 800, invalid: 20, valid: 780, lists: [400, 380]},
  filed: {clean: true, included: false, reason: "list votes mismatch", registered: 1000, voted: 800, ballots_in_box: 800, invalid: 20, valid: 780, lists: [390, 390]}}];
setStationFilter("all");
els.stationsort.value = "station";
REV = null; els.tview.value = "corrected";
D.rows = [{id: 9}];
renderHero();
stationCard = els.stationlist.children[0];
stationStatusEl = stationCard.children[0].children.find(x => x.className.startsWith("stationstatus"));
stationActions = (stationStatusEl.children || []).find(x => x.className === "stationactions");
assert.deepEqual(stationActions.menu.children.map(x => x.textContent), ["прикажи"],
  "unavailable PDF and RIK actions must be omitted after a live refresh");
els.tview.value = "diff";
renderHero();
stationCard = els.stationlist.children[0];
stationStatusEl = stationCard.children[0].children.find(x => x.className.startsWith("stationstatus"));
assert.equal(stationStatusEl.children.find(x => x.className === "stationstatusmain").textContent, "Поређење");
assert((stationStatusEl.children || []).some(x => x.className === "stationactions"));
els.tview.value = "corrected";
els.stationbars.value = "1";
renderHero();
stationCard = els.stationlist.children[0];
stationShares = stationCard.children[0].children.find(x => x.className.startsWith("stationshare"));
assert.equal(stationShares.className, "stationshare single");
assert.equal(stationShares.children.length, 1);
assert.equal(stationCard.children[1].children, undefined, "station chart is lazy");
setAllStations(true);
assert.equal(stationCard.open, true);
assert.equal(els.stationtoggle.textContent, "Скупи све");
setAllStations(false);
assert.equal(stationCard.open, false);
assert.equal(els.stationtoggle.textContent, "Прошири све");
stationCard.open = true; stationCard.ontoggle();
assert(stationCard.children[1].children.length > 1);
const firstStationValue = stationCard.children[1].children.find(x => x.className === "stationbar").children[2];
assert.equal(firstStationValue.children[0].className, "resultvotes");
assert.equal(firstStationValue.children[1].className, "resultpct");
assert(!stationCard.children[1].children.some(x => x.textContent === "Урачунат"),
  "the inclusion status already shown in the station summary must not be repeated");
renderHero();
assert.strictEqual(els.stationlist.children[0], stationCard,
  "unchanged live data must not rebuild and collapse expanded stations");
setSharedMunicipality("Ћуприја");
assert.equal(els.m.value, "Ћуприја");
assert.equal(heroMuni, "Ћуприја");
assert.equal(redrawCalls, 1);

T.scope = "national"; T.names = ["Републичка А", "Републичка Б"]; T.fallback_name_indices = [];
alpha.names = []; alpha.fallback_name_indices = [];
renderHero();
const nationalStation = els.stationlist.children[0];
nationalStation.open = true; nationalStation.ontoggle();
assert(nationalStation.children[1].children.some(x => x.children && x.children[0].textContent.includes("Републичка А")),
  "national station charts must use national list names");
T.scope = "municipal"; T.names = []; alpha.names = ["A", "B"];

unknown.generated_unknown_muni = true;
unknown.names = ["List 1", "Стварна листа"]; unknown.fallback_name_indices = [0]; unknown.nlists = 2;
for (const k of ["all", "cl", "fall", "fcl"]) { unknown[k].lists = [60, 40]; unknown[k].lists_valid = 100; unknown[k].lists_ballots_in_box = 100; unknown[k].vote_forms = 1; }
heroMuni = "(unknown)"; heroPicked = true;
renderHero();
assert.equal(els.hmuni.options.find(o => o.value === "(unknown)").textContent, "Непозната општина");
assert(els.herotitle.textContent.startsWith("Непозната општина"));
assert.equal(slots[0].nm.textContent, "Листа 1");
unknown.generated_unknown_muni = false; delete unknown.fallback_name_indices;
renderHero();
assert.equal(els.hmuni.options.find(o => o.value === "(unknown)").textContent, "(unknown)");
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
assert.equal(els.herofoot.children.length, 1);
assert(!String(els.herofoot.children[0]).includes("можда застарели"));
setHero(true);
renderHero();
assert.equal(els.herofoot.children.length, 1);
assert(!String(els.herofoot.children[0]).includes("можда застарели"));
setHero(false);
S = null;

els.tview.value = "corrected";
const emptyNational = row(null, null);
emptyNational.names = []; emptyNational.nlists = 0;
emptyNational.names_note = "no expected ballot rows - comparable list totals unavailable";
for (const k of ["all", "cl", "fall", "fcl"]) { emptyNational[k].lists = []; emptyNational[k].lists_valid = 0; }
setData(emptyNational);
T.names = []; T.names_note = emptyNational.names_note;
renderHero();
const listWarning = "Упоредиви збирови по листама нису доступни за овај обухват.";
const correctedUnavailableLines = els["hs-valid-context"].textContent.split("\n");
assert.equal(correctedUnavailableLines.length, 2);
assert.equal(correctedUnavailableLines.at(-1), listWarning);
els.tview.value = "diff";
renderHero();
const diffUnavailableLines = els["hs-valid-primary"].textContent.split("\n");
assert.equal(diffUnavailableLines.length, 6);
assert.equal(diffUnavailableLines.at(-1), listWarning);
els.tview.value = "corrected";
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
assert(!bodyHero && els.herobtn.textContent === "Цео екран");

async function flushTurns() {
  for (let i = 0; i < 4; i++) await Promise.resolve();
}
function installReviewTable(id) {
  const api = {
    data() { return {id}; },
    index() { return 0; },
    child: {isShown() { return false; }}
  };
  table = {
    rows() { return {every(fn) { fn.call(api); }}; },
    cell() { return {invalidate() {}}; },
    cells() { return {invalidate() {}}; },
    draw() {}
  };
}
async function refreshCases() {
  const id = 7, url = "u-current";
  const base = row([60, 40], [60, 40]);
  base.counted = 15; base.clean = 10; base.excluded = 5;
  setData(base);
  T.excluded = [{id, m: "Alpha"}];
  T.excluded_filed = [{id, m: "Alpha"}];
  D.rows = [{id, u: url, rv: null, v: []}];
  heroMuni = "";
  heroPicked = true;
  els.tview.value = "corrected";
  els.tmode.value = "all";
  S = null;
  REV = null;
  revKey = "";
  for (const k of Object.keys(LOCAL)) delete LOCAL[k];
  rows = [];
  installReviewTable(id);
  const realRenderHero = renderHero;
  let heroPaints = 0;
  renderHero = function () { heroPaints++; return realRenderHero(); };

  let gate;
  fetch = () => new Promise((resolve, reject) => { gate = {resolve, reject}; });
  heroPaints = 0;
  submitReview({id, u: url}, "ok", "", null);
  assert.equal(heroPaints, 1, "submit paints the hero once before fetch settles");
  assert.equal(LOCAL[url].pending, true);
  assert(els["hs-forms-context"].textContent.includes("За проверу: 1 записник — 1 прихваћено"));
  assert(!els["hs-forms-context"].textContent.includes("0 без одлуке"));

  gate.reject(new Error("save failed"));
  await flushTurns();
  assert.equal(heroPaints, 2, "failed save paints the hero once more");
  assert.equal(url in LOCAL, false);
  assert(els["hs-forms-context"].textContent.includes("За проверу: 1 записник — 0 прихваћено · 1 без одлуке"));

  table = null;
  const body = {[url]: {v: "ok"}};
  fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve(body)});
  heroPaints = 0;
  loadReviews();
  await flushTurns();
  assert.equal(heroPaints, 1, "changed poll paints once when the table is missing");
  heroPaints = 0;
  loadReviews();
  await flushTurns();
  assert.equal(heroPaints, 0, "unchanged poll does not paint");

  const id2 = 8, url2 = "u-pending";
  T.excluded = [{id, m: "Alpha"}, {id: id2, m: "Alpha"}];
  D.rows = [{id, u: url, rv: null, v: []}, {id: id2, u: url2, rv: null, v: []}];
  LOCAL[url2] = {v: "ok", pending: true, t: 0};
  loadReviews();
  await flushTurns();
  assert.equal(LOCAL[url2].pending, true, "poll must not delete a pending verdict");

  for (const k of Object.keys(LOCAL)) delete LOCAL[k];
  REV = null;
  revKey = "";
  T.excluded = [{id, m: "Alpha"}];
  D.rows = [{id, u: url, rv: null, v: []}];
  S = {
    ts: Date.now() / 1000, watching: true, db: "election.sqlite",
    watcher: null,
    docs: {rg2_seen: 1, stations: 1, rg2_rik: null, new10: 0, new60: 0, replaced: 0, removed: 0, rg7: null, rare: {}},
    analysis: {on: false, paused: null, done: 0, failed: 0, spend: 0},
    ntfy: null, alerts: []
  };
  D.db = S.db;
  rows = [];
  installReviewTable(id);
  let settle;
  fetch = () => new Promise((resolve, reject) => { settle = {resolve, reject}; });
  heroPaints = 0;
  submitReview({id, u: url}, "ok", "", null);
  assert.equal(heroPaints, 1, "status-present submit paints once");
  settle.reject(new Error("unused"));
  await flushTurns();

  table = null;
  for (const k of Object.keys(LOCAL)) delete LOCAL[k];
  REV = null;
  revKey = "";
  fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve({[url]: {v: "ok"}})});
  heroPaints = 0;
  loadReviews();
  await flushTurns();
  assert.equal(heroPaints, 1, "status-present changed poll paints once, even without a table");
}
(async () => {
  try {
    await refreshCases();
    console.log(JSON.stringify({ok: true}));
  } catch (e) {
    console.error(e);
    process.exit(1);
  }
})();
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}
