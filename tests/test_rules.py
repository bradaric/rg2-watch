import copy

import pytest

from rg2watch.analyze import SCHEMA
from rg2watch.config import Config
from rg2watch.models import Doc
from rg2watch.rules import evaluate_report

DOC = Doc("Test", "12", "School", "label", 6, "http://x/a.pdf", "2026-10-19")
CFG = Config()


def nf(value=100, legible=True, mark="clean"):
    return {"value": value, "legible": legible, "mark": mark, "note": ""}


def yn(answer="NE"):
    return {"answer": answer}


def clean_report():
    return {
        "header": {"form_code": "RG-2", "station_no": "0012", "date": "19.10.2026",
                   "sections_present": ["I", "II", "III", "IV", "V"],
                   "pages_match_same_form": True, "rotated_or_upside_down": False},
        "numbers": {
            "registered": nf(1000), "first_voter_number": nf(1), "voted": nf(800),
            "ballots_in_box": nf(800), "invalid": nf(20), "valid": nf(780),
            "assisted": nf(10), "outside_voted": nf(5), "outside_confirmations": nf(5),
            "list_votes": [{"row": 1, "field": nf(400)}, {"row": 2, "field": nf(380)}],
        },
        "yesno": {"interruption": yn("NE"), "control_sheet_found": yn("DA"),
                  "control_sheet_signed": yn("DA"), "observers_present": yn("DA"),
                  "observer_form_filled": yn("DA"), "committee_remarks": yn("NE")},
        "times": {"open_time": "7:00", "close_time": "20:00", "completed_at": "22:30"},
        "remarks_text_present": False,
        "remarks_summary": "",
        "signatures": {"rows_total": 7, "rows_with_name": 7, "rows_with_signature": 7,
                       "rows_name_without_signature": 0, "president_signed": True,
                       "signatures_look_same_hand": False, "note": ""},
        "visual": {"results_block_different_pen": False, "results_block_different_hand": False,
                   "pasted_or_patched_area": False, "page_quality": "good", "other_anomalies": []},
        "confidence": "high",
    }


def _check(value, schema):
    if "anyOf" in schema:
        assert any(_try(value, s) for s in schema["anyOf"]), (value, schema)
        return
    t = schema["type"]
    if t == "object":
        assert set(value) == set(schema["properties"])
        for k, s in schema["properties"].items():
            _check(value[k], s)
    elif t == "array":
        assert isinstance(value, list)
        for v in value:
            _check(v, schema["items"])
    elif t == "string":
        assert isinstance(value, str)
        if "enum" in schema:
            assert value in schema["enum"]
    elif t == "integer":
        assert isinstance(value, int) and not isinstance(value, bool)
    elif t == "boolean":
        assert isinstance(value, bool)
    elif t == "null":
        assert value is None


def _try(value, schema):
    try:
        _check(value, schema)
        return True
    except AssertionError:
        return False


def rules(report, cfg=CFG):
    return {f.rule: f for f in evaluate_report(report, DOC, cfg)}


def mutated(fn):
    r = clean_report()
    fn(r)
    return r


def test_clean_report_matches_schema():
    _check(clean_report(), SCHEMA)


def test_clean_report_no_findings():
    assert evaluate_report(clean_report(), DOC, CFG) == []


# (rule, mutation, expected severity); each must fire, and the clean report must not
CASES = [
    ("V1", lambda r: r["numbers"]["ballots_in_box"].update(mark="overwritten", value=834), "HIGH"),
    ("V1", lambda r: r["numbers"]["list_votes"][0]["field"].update(mark="digit_altered"), "HIGH"),
    ("V2", lambda r: r["numbers"]["registered"].update(mark="correction_fluid"), "MEDIUM"),
    ("V3", lambda r: r["signatures"].update(president_signed=False), "HIGH"),
    ("V4", lambda r: r["signatures"].update(rows_name_without_signature=2), "MEDIUM"),
    ("V5", lambda r: r["signatures"].update(signatures_look_same_hand=True), "HIGH"),
    ("V6", lambda r: r["visual"].update(results_block_different_pen=True), "HIGH"),
    ("V6", lambda r: r["visual"].update(results_block_different_hand=True), "HIGH"),
    ("V7", lambda r: r["visual"].update(pasted_or_patched_area=True), "HIGH"),
    ("V8", lambda r: r["numbers"]["valid"].update(value=None, legible=False, mark="empty"), "MEDIUM"),
    ("V8", lambda r: r["numbers"]["list_votes"][1]["field"].update(legible=False), "MEDIUM"),
    ("V9", lambda r: r["header"].update(station_no="13"), "HIGH"),
    ("V9", lambda r: r["header"].update(sections_present=["I", "II", "III", "IV"]), "HIGH"),
    ("V9", lambda r: r["header"].update(pages_match_same_form=False), "HIGH"),
    ("V10", lambda r: r["visual"].update(page_quality="poor"), "LOW"),
    ("V11", lambda r: r["yesno"]["interruption"].update(answer="both"), "MEDIUM"),
    ("V11", lambda r: r["yesno"]["committee_remarks"].update(answer="none"), "MEDIUM"),
    ("P1", lambda r: r["yesno"]["control_sheet_found"].update(answer="NE"), "HIGH"),
    ("P1", lambda r: r["yesno"]["control_sheet_signed"].update(answer="NE"), "HIGH"),
    ("P2", lambda r: r["yesno"]["interruption"].update(answer="DA"), "MEDIUM"),
    ("P3", lambda r: r["yesno"]["committee_remarks"].update(answer="DA"), "MEDIUM"),
    ("P3", lambda r: r.update(remarks_text_present=True), "MEDIUM"),
    ("P4", lambda r: r["times"].update(close_time="19:30"), "MEDIUM"),
    ("P4", lambda r: r["times"].update(completed_at="19 00"), "MEDIUM"),
    ("A1", lambda r: r["numbers"]["ballots_in_box"].update(value=836), "HIGH"),
    ("A2", lambda r: r["numbers"]["invalid"].update(value=25), "MEDIUM"),
    ("A3", lambda r: r["numbers"]["list_votes"][0]["field"].update(value=390), "MEDIUM"),
    ("A4", lambda r: r["numbers"]["registered"].update(value=700), "HIGH"),
    ("A5", lambda r: r["numbers"]["outside_voted"].update(value=6), "MEDIUM"),
    ("A6", lambda r: r["numbers"]["registered"].update(value=840), "MEDIUM"),
    ("A7", lambda r: r["numbers"]["list_votes"].__setitem__(0, {"row": 1, "field": nf(710)}), "MEDIUM"),
    ("A8", lambda r: r["numbers"]["assisted"].update(value=41), "LOW"),
]


@pytest.mark.parametrize("rule,fn,sev", CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)])
def test_rule_fires(rule, fn, sev):
    found = rules(mutated(fn))
    assert rule in found
    assert found[rule].severity == sev
    assert found[rule].doc is DOC


def test_one_finding_per_rule_and_aggregated_summary():
    def fn(r):
        r["numbers"]["ballots_in_box"].update(mark="overwritten", value=834)
        r["numbers"]["valid"].update(mark="crossed_out", value=780)
    fs = evaluate_report(mutated(fn), DOC, CFG)
    v1 = [f for f in fs if f.rule == "V1"]
    assert len(v1) == 1
    assert "10.2 ballots_in_box overwritten (834)" in v1[0].summary
    assert "10.4 valid crossed_out (780)" in v1[0].summary


def test_summary_values():
    found = rules(mutated(lambda r: r["numbers"]["ballots_in_box"].update(value=836)))
    assert found["A1"].summary == "ballots in box 836 > voted 800"


def test_not_fire_boundaries():
    # empty/dash/clean marks are not V1; one unsigned row is below threshold; leading zeros ok
    r = clean_report()
    r["numbers"]["voted"]["mark"] = "dash"
    r["signatures"]["rows_name_without_signature"] = 1
    r["times"]["completed_at"] = "2000"
    r["times"]["close_time"] = "20 00"
    assert "V1" not in rules(r) and "V4" not in rules(r) and "P4" not in rules(r)
    # unclear yes/no never fires P1-P3
    r = clean_report()
    for k in r["yesno"]:
        r["yesno"][k]["answer"] = "unclear"
    assert rules(r) == {}
    # unparseable time skipped
    assert "P4" not in rules(mutated(lambda r: r["times"].update(close_time="evening", completed_at=None)))
    # ratio rules respect minimum sizes
    def small(r):
        r["numbers"]["registered"].update(value=40)
        r["numbers"]["voted"].update(value=40)
        r["numbers"]["ballots_in_box"].update(value=40)
        r["numbers"]["valid"].update(value=40)
        r["numbers"]["invalid"].update(value=0)
        r["numbers"]["list_votes"][:] = [{"row": 1, "field": nf(40)}]
        r["numbers"]["assisted"].update(value=0)
    assert rules(mutated(small)) == {}


def test_arithmetic_skipped_when_illegible():
    def fn(r):
        r["numbers"]["ballots_in_box"].update(value=900, legible=False)
        r["numbers"]["registered"].update(value=None)
        r["numbers"]["outside_voted"].update(value=9, legible=False)
    found = rules(mutated(fn))
    assert not {"A1", "A2", "A4", "A5", "A6"} & set(found)


def test_illegible_report_only_v8_v10():
    r = clean_report()
    for k, f in r["numbers"].items():
        if k != "list_votes":
            f.update(value=None, legible=False, mark="empty")
    r["numbers"]["list_votes"] = [{"row": 1, "field": nf(None, False, "empty")}]
    for k in r["yesno"]:
        r["yesno"][k]["answer"] = "unclear"
    r["times"] = {"open_time": None, "close_time": None, "completed_at": None}
    r["header"].update(form_code=None, station_no=None, date=None)
    r["visual"]["page_quality"] = "unreadable"
    r["confidence"] = "low"
    _check(r, SCHEMA)
    assert set(rules(r)) == {"V8", "V10"}


def test_low_confidence_downgrades_high():
    r = mutated(lambda r: (r["signatures"].update(president_signed=False),
                           r["visual"].update(page_quality="poor"),
                           r.update(confidence="low")))
    found = rules(r)
    assert found["V3"].severity == "MEDIUM"
    assert found["V3"].summary.endswith(" (low-confidence read)")
    assert found["V10"].severity == "LOW"


def test_summary_truncated():
    def fn(r):
        r["numbers"]["list_votes"][:] = [
            {"row": i, "field": nf(i, True, "overwritten")} for i in range(1, 60)]
    s = rules(mutated(fn))["V1"].summary
    assert len(s) <= 300 and s.endswith("...")


def test_thresholds_configurable():
    r = mutated(lambda r: r["signatures"].update(rows_name_without_signature=1))
    assert "V4" in rules(r, Config(unsigned_rows_threshold=1))


def test_report_not_mutated():
    r = clean_report()
    before = copy.deepcopy(r)
    evaluate_report(r, DOC, CFG)
    assert r == before
