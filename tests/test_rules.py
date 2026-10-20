import copy

import pytest

from rg2watch import analyze
from rg2watch.analyze import SCHEMA
from rg2watch.config import Config
from rg2watch.models import Doc
from rg2watch.rules import evaluate_report

DOC = Doc("Test", "12", "School", "label", 6, "http://x/a.pdf", "2026-10-19")
CFG = Config()


def nf(value=100, legible=True, mark="clean", original=None):
    return {"value": value, "original_value": original, "legible": legible, "mark": mark, "note": ""}


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


_NF = {"type": "object", "properties": {
    "value": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "original_value": {"anyOf": [{"type": "integer"}, {"type": "null"}]}, "legible": {"type": "boolean"},
    "mark": {"type": "string", "enum": list(analyze.MARKS)}, "note": {"type": "string"}}}
# normalised report shape (what analyze_document returns), not the API wire schema
REPORT_SHAPE = {**SCHEMA, "properties": {**SCHEMA["properties"],
    "numbers": {"type": "object", "properties": {
        **{k: _NF for k in analyze.NUM_KEYS},
        "list_votes": {"type": "array", "items": {"type": "object", "properties": {
            "row": {"type": "integer"}, "field": _NF}}}}},
    "yesno": {"type": "object", "properties": {k: {"type": "object", "properties": {
        "answer": {"type": "string", "enum": list(analyze.ANSWERS)}}} for k in analyze.YESNO_KEYS}}}}


def to_wire(report):
    """Normalised report -> the flat array form the model returns."""
    nums = [{"field": k, "row": None, **v} for k, v in report["numbers"].items() if k != "list_votes"]
    nums += [{"field": "list_row", "row": r["row"], **r["field"]} for r in report["numbers"]["list_votes"]]
    yesno = [{"item": k, "answer": v["answer"]} for k, v in report["yesno"].items()]
    return {**report, "numbers": nums, "yesno": yesno}


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
    _check(clean_report(), REPORT_SHAPE)


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
    ("A9", lambda r: r["numbers"]["outside_voted"].update(value=120), "HIGH"),
    ("A9", lambda r: r["numbers"]["outside_voted"].update(value=80), "MEDIUM"),
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


def _adversarial(quality, confidence, many_illegible):
    r = clean_report()
    if many_illegible:
        for k in ("registered", "voted", "ballots_in_box"):
            r["numbers"][k].update(value=None, legible=False, mark="empty")
    else:
        for k, f in r["numbers"].items():
            if k != "list_votes":
                f.update(value=None, legible=False, mark="empty")
    r["numbers"]["list_votes"] = [{"row": 1, "field": nf(None, False, "empty")}]
    for k in r["yesno"]:
        r["yesno"][k]["answer"] = "unclear"
    r["times"] = {"open_time": None, "close_time": None, "completed_at": None}
    r["header"].update(form_code=None, station_no=None, date=None, sections_present=[],
                       pages_match_same_form=False)
    r["signatures"].update(president_signed=False, signatures_look_same_hand=True)
    r["visual"].update(results_block_different_pen=True, results_block_different_hand=True,
                       pasted_or_patched_area=True, page_quality=quality)
    r["confidence"] = confidence
    _check(r, REPORT_SHAPE)
    return r


@pytest.mark.parametrize("confidence", ["medium", "low"])
@pytest.mark.parametrize("quality,many", [("unreadable", False), ("good", True), ("unreadable", True)])
def test_illegible_report_only_v8_v10(quality, many, confidence):
    found = set(rules(_adversarial(quality, confidence, many)))
    assert found <= {"V8", "V10"} and "V8" in found
    assert ("V10" in found) == (quality != "good")


def test_poor_quality_caps_physical_rules_at_medium():
    def fn(r):
        r["visual"].update(page_quality="poor", pasted_or_patched_area=True,
                           results_block_different_pen=True)
        r["signatures"].update(president_signed=False, signatures_look_same_hand=True)
        r["header"].update(sections_present=["I"])
    found = rules(mutated(fn))
    for rule in ("V3", "V5", "V6", "V7", "V9"):
        assert found[rule].severity == "MEDIUM"
    assert rules(mutated(lambda r: (r["visual"].update(page_quality="poor"),
                                    r["header"].update(station_no="99"))))["V9"].severity == "HIGH"


def test_v1_fires_for_corrected_unreadable_field():
    r = mutated(lambda r: r["numbers"]["voted"].update(mark="overwritten", value=None, legible=False))
    found = rules(r)
    assert found["V1"].severity == "HIGH" and "V8" in found
    assert "10.1 voted overwritten (value unreadable)" in found["V1"].summary
    r = mutated(lambda r: r["numbers"]["assisted"].update(mark="correction_fluid", value=None,
                                                          legible=False))
    assert "11 assisted correction_fluid (value unreadable)" in rules(r)["V2"].summary


def test_v1_v2_capped_at_medium_on_unreliable_scan():
    def fn(r):
        r["visual"]["page_quality"] = "unreadable"
        r["numbers"]["voted"].update(mark="overwritten", value=None, legible=False)
        r["numbers"]["registered"].update(mark="crossed_out")
    found = rules(mutated(fn))
    assert found["V1"].severity == "MEDIUM" and found["V2"].severity == "MEDIUM"


def test_dash_means_zero():
    def fn(r):
        r["numbers"]["invalid"].update(value=None, legible=False, mark="dash")
        r["numbers"]["valid"].update(value=800)
        r["numbers"]["list_votes"][1]["field"].update(value=None, legible=False, mark="dash")
        r["numbers"]["list_votes"][0]["field"].update(value=800)
        r["numbers"]["outside_voted"].update(value=None, legible=True, mark="dash")
        r["numbers"]["outside_confirmations"].update(value=0)
        r["numbers"]["assisted"].update(value=None, legible=False, mark="dash")
    found = rules(mutated(fn))
    # dash is a legible 0: no V8, A2/A3/A5 hold with 0, A7 sees one list at 100%
    assert "V8" not in found and "V1" not in found
    assert not {"A2", "A3", "A5"} & set(found)
    assert "A7" in found
    # a dash used as 0 still feeds the arithmetic rules
    found = rules(mutated(lambda r: r["numbers"]["invalid"].update(value=None, mark="dash")))
    assert "invalid 0 + valid 780 != ballots in box 800" in found["A2"].summary


def test_v11_none_on_follow_up_not_anomalous_when_parent_is_ne():
    def fn(r):
        r["yesno"]["control_sheet_found"]["answer"] = "NE"
        r["yesno"]["control_sheet_signed"]["answer"] = "none"
        r["yesno"]["observers_present"]["answer"] = "NE"
        r["yesno"]["observer_form_filled"]["answer"] = "none"
    assert "V11" not in rules(mutated(fn))
    found = rules(mutated(lambda r: r["yesno"]["control_sheet_signed"].update(answer="none")))
    assert "9.1 control_sheet_signed none" in found["V11"].summary
    found = rules(mutated(lambda r: r["yesno"]["observer_form_filled"].update(answer="none")))
    assert "13.1 observer_form_filled none" in found["V11"].summary
    def both(r):
        r["yesno"]["observers_present"]["answer"] = "NE"
        r["yesno"]["observer_form_filled"]["answer"] = "both"
    assert "V11" in rules(mutated(both))


@pytest.mark.parametrize("answer", ["none", "both"])
def test_v11_never_fires_for_committee_remarks(answer):
    assert "V11" not in rules(mutated(lambda r: r["yesno"]["committee_remarks"].update(answer=answer)))


def test_a9_summary_and_thresholds():
    def out(v, voted=600, **kw):
        def fn(r):
            r["numbers"]["voted"].update(value=voted)
            r["numbers"]["outside_voted"].update(value=v, **kw)
        return rules(mutated(fn)).get("A9")
    assert out(60).summary == "outside-station voting 60 of 600 (10.0%)"
    assert out(90).severity == "HIGH"
    assert out(60).severity == "MEDIUM"
    assert out(9, voted=80) is None  # share 11% but under 10 voters
    assert out(10, voted=100).severity == "MEDIUM"
    assert out(20) is None  # 3.3% below medium
    assert out(None, legible=False) is None
    assert out(None, mark="dash") is None  # dash = 0
    assert out(60, voted=0) is None


def test_a9_seven_percent_does_not_fire():
    fn = mutated(lambda r: (r["numbers"]["voted"].update(value=600),
                            r["numbers"]["outside_voted"].update(value=42)))
    assert "A9" not in rules(fn)


def test_v8_fires_for_illegible_outside_voted():
    found = rules(mutated(lambda r: r["numbers"]["outside_voted"].update(legible=False)))
    assert found["V8"].severity == "MEDIUM"
    assert found["V8"].summary == "12 outside_voted illegible"


def test_v8_silent_for_empty_outside_voted():
    found = rules(mutated(lambda r: r["numbers"]["outside_voted"].update(value=None, legible=False, mark="empty")))
    assert "V8" not in found


@pytest.mark.parametrize("close,done,fires", [
    ("20:00", "01:30", False), ("20:00", "19:30", True), ("20:00", "07:59", False),
    ("20:00", "08:00", True), ("20:00", "20:00", False)])
def test_p4_completed_before_close(close, done, fires):
    f = rules(mutated(lambda r: r["times"].update(close_time=close, completed_at=done)))
    assert ("P4" in f) == fires


@pytest.mark.parametrize("text,expected", [
    ("24:00", None), ("8", None), ("", None), ("20.00h", 1200), ("20h00", 1200),
    ("20:00:00", 1200), ("2000", 1200), ("20 00", 1200), ("20:00 ч", 1200), (None, None)])
def test_parse_time(text, expected):
    from rg2watch.rules import _parse_time
    assert _parse_time(text) == expected


@pytest.mark.parametrize("form,listing,fires", [
    ("12/1", "12", False), (" 012 ", "12", False), ("abc", "12", False), ("13", "12", True)])
def test_station_normalisation(form, listing, fires):
    doc = Doc("Test", listing, "School", "label", 6, "http://x/a.pdf", "2026-10-19")
    fs = {f.rule for f in evaluate_report(mutated(lambda r: r["header"].update(station_no=form)), doc, CFG)}
    assert ("V9" in fs) == fires


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
    r = mutated(fn)
    r["confidence"] = "low"
    s = rules(r)["V1"].summary
    assert len(s) <= 300 and s.endswith("... (low-confidence read)")


def test_thresholds_configurable():
    r = mutated(lambda r: r["signatures"].update(rows_name_without_signature=1))
    assert "V4" in rules(r, Config(unsigned_rows_threshold=1))


def test_report_not_mutated():
    r = clean_report()
    before = copy.deepcopy(r)
    evaluate_report(r, DOC, CFG)
    assert r == before


def _outside(value, original, voted=417, **kw):
    def fn(r):
        r["numbers"]["voted"].update(value=voted)
        r["numbers"]["outside_voted"].update(value=value, original_value=original, **kw)
    return rules(mutated(fn))


def test_v1_summary_shows_original():
    def fn(r):
        r["numbers"]["invalid"].update(value=14, original_value=18, mark="crossed_out")
    assert "10.3 invalid crossed_out (18 -> 14)" in rules(mutated(fn))["V1"].summary


def test_a9_driven_by_original():
    a9 = _outside(10, 44)["A9"]
    assert a9.severity == "MEDIUM"
    assert a9.summary == "outside-station voting 10 of 417 (2.4%), originally 44 (10.6%)"
    assert "A9" not in _outside(10, None)
    assert "originally" not in _outside(60, 20)["A9"].summary


def test_v12_fires_on_large_correction():
    f = _outside(10, 44, mark="crossed_out")["V12"]
    assert f.severity == "HIGH"
    assert f.summary == "item 12 outside_voted 44 -> 10 (-77%)"


def test_v12_not_fired_below_thresholds():
    assert "V12" not in _outside(14, 18, mark="crossed_out")
    assert "V12" not in _outside(312, 313, mark="overwritten")
    assert "V12" not in _outside(10, None)


def test_v12_capped_at_medium_when_unreliable():
    r = _adversarial("unreadable", "high", False)
    r["numbers"]["outside_voted"].update(value=10, original_value=44, legible=True)
    assert rules(r)["V12"].severity == "MEDIUM"


def test_a5_only_when_outside_votes_exceed_confirmations():
    assert "A5" not in _outside(4, None)
    assert "A5" not in _outside(5, None)
    a5 = _outside(6, None)["A5"]
    assert a5.severity == "MEDIUM"
    assert a5.summary == "outside voted 6 > confirmations 5"
