import http.client
import json
import sqlite3

import pytest

from rg2watch.analyze import AnalysisResult
from rg2watch.browse import STATIC, build_data
from rg2watch.config import Config
from rg2watch.operator_corrections import (CorrectionConflict, OperatorCorrections,
                                           apply_operator_snapshot, validate_snapshot)
from rg2watch.pipeline import PROMPT_VERSION
from rg2watch.server import start_server
from rg2watch.store import Store
from rg2watch.tally import build_tally, format_tally
from test_browse import BASE, P, add, doc, store
from test_rules import clean_report


URL = BASE + "/extfile/operator-correction.pdf"
MODEL = "claude-sonnet-5-5"


def snapshot(**changes):
    out = {
        "registered": 1000,
        "voted": 800,
        "ballots_in_box": 800,
        "invalid": 20,
        "valid": 780,
        "outside_voted": 5,
        "outside_confirmations": 5,
        "list_votes": [{"row": 1, "value": 400}, {"row": 2, "value": 380}],
    }
    out.update(changes)
    return out


def correction_store():
    s = store()
    add(s, doc(URL))
    return s


def analysed_store(report=None):
    s = correction_store()
    s.save_analysis(URL, AnalysisResult(report or clean_report(), 0, 0, 0.0, MODEL, "stop"),
                    PROMPT_VERSION, parent=P)
    return s


def test_validate_full_snapshot_and_apply_it_without_mutating_model_report():
    original = clean_report()
    original["numbers"]["voted"].update(value=1762, mark="crossed_out", original_value=853)
    corrected = snapshot(voted=853, ballots_in_box=853, invalid=26, valid=827,
                         list_votes=[{"row": 1, "value": 427}, {"row": 2, "value": 400}])

    assert validate_snapshot(corrected, [1, 2]) == corrected
    effective = apply_operator_snapshot(original, corrected)

    assert original["numbers"]["voted"]["value"] == 1762
    assert effective["numbers"]["voted"] == {
        "value": 853, "original_value": None, "legible": True,
        "mark": "clean", "note": "operator-entered correction",
    }
    assert [r["field"]["value"] for r in effective["numbers"]["list_votes"]] == [427, 400]


@pytest.mark.parametrize("change, message", [
    ({"ballots_in_box": 801}, "ballots in box cannot exceed voted"),
    ({"invalid": 21}, "invalid plus valid must equal ballots in box"),
    ({"list_votes": [{"row": 1, "value": 399}, {"row": 2, "value": 380}]},
     "list votes must sum to valid"),
    ({"voted": 1001, "ballots_in_box": 1001, "valid": 981,
      "list_votes": [{"row": 1, "value": 500}, {"row": 2, "value": 481}]},
     "voted cannot exceed registered"),
    ({"outside_voted": 6}, "outside voted cannot exceed confirmations"),
])
def test_invalid_arithmetic_is_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        validate_snapshot(snapshot(**change), [1, 2])


@pytest.mark.parametrize("bad", [
    {**snapshot(), "extra": 1},
    {k: v for k, v in snapshot().items() if k != "valid"},
    snapshot(voted=True),
    snapshot(invalid=-1),
    snapshot(list_votes=[{"row": 1, "value": 780}]),
    snapshot(list_votes=[{"row": 1, "value": 400}, {"row": 1, "value": 380}]),
])
def test_snapshot_shape_must_be_complete_nonnegative_and_have_exact_rows(bad):
    with pytest.raises(ValueError):
        validate_snapshot(bad, [1, 2])


def test_store_keeps_append_only_revision_and_revocation_history():
    s = correction_store()
    first = s.add_operator_correction(URL, "correction", snapshot(), "read from corrected form", "source-a")
    second_values = snapshot(voted=799, ballots_in_box=799, invalid=19)
    second = s.add_operator_correction(URL, "correction", second_values, "fixed my entry", "source-a")
    revoked = s.add_operator_correction(URL, "revoke", None, "use source values", "source-a")

    assert first["id"] < second["id"] < revoked["id"]
    assert s.latest_operator_corrections()[0]["action"] == "revoke"
    history = s.operator_correction_history(URL)
    assert [r["action"] for r in history] == ["correction", "correction", "revoke"]
    assert json.loads(history[1]["snapshot_json"])["voted"] == 799
    assert s.add_operator_correction(BASE + "/missing.pdf", "correction", snapshot(), "x", "t") is None
    assert s.latest_operator_corrections("other") == []


def test_store_rejects_bad_correction_record_fields():
    s = correction_store()
    for action, values, reason, token in (
        ("bad", snapshot(), "why", "t"),
        ("correction", None, "why", "t"),
        ("revoke", snapshot(), "why", "t"),
        ("correction", snapshot(), "", "t"),
        ("correction", snapshot(), "why", ""),
    ):
        with pytest.raises(ValueError):
            s.add_operator_correction(URL, action, values, reason, token)


def test_operator_corrections_table_is_created_on_an_old_database(tmp_path):
    path = tmp_path / "old.sqlite"
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE operator_corrections")
    db.commit()
    db.close()

    Store(path).close()
    names = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert {"operator_corrections", "operator_corrections_url"} <= names


def test_service_saves_revision_separately_from_review_and_revokes_it():
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    before = service.state(URL)
    assert before["status"] == "none" and before["snapshot"] == snapshot()

    active = service.save(URL, snapshot(), "verified directly on the scan", before["source_token"])
    assert active["status"] == "active" and active["reason"] == "verified directly on the scan"
    assert s.latest_verdict(URL) is None

    revoked = service.revoke(URL, "entered for the wrong station", active["source_token"])
    assert revoked["status"] == "revoked"
    assert [x["action"] for x in revoked["history"]] == ["correction", "revoke"]


def test_service_calls_refresh_after_each_successful_change():
    s = analysed_store()
    refreshed = []
    service = OperatorCorrections(s, Config(model=MODEL), MODEL, on_change=lambda: refreshed.append(True))
    state = service.state(URL)
    active = service.save(URL, snapshot(), "checked", state["source_token"])
    service.revoke(URL, "withdrawn", active["source_token"])
    assert refreshed == [True, True]


def test_service_rejects_stale_save_and_preserves_existing_history():
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    stale = service.state(URL)["source_token"]
    changed = clean_report()
    changed["numbers"]["registered"]["value"] = 1001
    s.save_analysis(URL, AnalysisResult(changed, 0, 0, 0.0, MODEL, "stop"), PROMPT_VERSION, parent=P)

    with pytest.raises(CorrectionConflict, match="source changed"):
        service.save(URL, snapshot(), "stale draft", stale)
    assert s.operator_correction_history(URL) == []


def test_service_detects_a_competing_revision_during_the_save(monkeypatch):
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    state = service.state(URL)
    real_add = s.add_operator_correction

    def racing_add(url, action, values, reason, source_token, **kwargs):
        real_add(url, "correction", snapshot(), "competing operator", source_token)
        return real_add(url, action, values, reason, source_token, **kwargs)

    monkeypatch.setattr(s, "add_operator_correction", racing_add)
    with pytest.raises(CorrectionConflict, match="revision changed"):
        service.save(URL, snapshot(), "my stale edit", state["source_token"])
    assert [r["reason"] for r in s.operator_correction_history(URL)] == ["competing operator"]


def test_service_detects_source_change_during_the_save(monkeypatch):
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    state = service.state(URL)
    real_source = service._source
    calls = 0

    def changing_source(url):
        nonlocal calls
        calls += 1
        row, report, token, rows = real_source(url)
        return row, report, token if calls == 1 else "changed-during-save", rows

    monkeypatch.setattr(service, "_source", changing_source)
    with pytest.raises(CorrectionConflict, match="source changed"):
        service.save(URL, snapshot(), "raced source", state["source_token"])
    assert s.operator_correction_history(URL) == []


def test_service_suspends_an_active_snapshot_when_source_changes_and_allows_reconfirmation():
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    state = service.state(URL)
    service.save(URL, snapshot(), "first check", state["source_token"])

    changed = clean_report()
    changed["numbers"]["registered"]["value"] = 1001
    s.save_analysis(URL, AnalysisResult(changed, 0, 0, 0.0, MODEL, "stop"), PROMPT_VERSION, parent=P)
    suspended = service.state(URL)
    assert suspended["status"] == "suspended" and suspended["active_snapshot"] is None

    restored = service.save(URL, snapshot(registered=1001), "reconfirmed after source update",
                            suspended["source_token"])
    assert restored["status"] == "active" and len(restored["history"]) == 2


def test_new_unanalysed_rg7_suspends_active_operator_correction():
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    service.save(URL, snapshot(), "checked before decision", service.state(URL)["source_token"])

    add(s, doc(BASE + "/extfile/new-rg7.pdf", dt=19))
    state = service.state(URL)
    assert state["status"] == "suspended" and state["active_snapshot"] is None


def test_editor_uses_ballot_rows_even_when_target_model_read_omitted_one():
    missing = clean_report()
    missing["numbers"]["list_votes"] = [{"row": 1, "field": missing["numbers"]["list_votes"][0]["field"]}]
    s = analysed_store(missing)
    other = doc(BASE + "/extfile/complete.pdf", no="2")
    add(s, other)
    s.save_analysis(other.url, AnalysisResult(clean_report(), 0, 0, 0.0, MODEL, "stop"),
                    PROMPT_VERSION, parent=P)

    state = OperatorCorrections(s, Config(model=MODEL), MODEL).state(URL)
    assert state["expected_rows"] == [1, 2]
    assert state["snapshot"]["list_votes"] == [{"row": 1, "value": 400},
                                                  {"row": 2, "value": None}]


def test_replacement_url_starts_without_correction_and_old_history_is_retained():
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    service.save(URL, snapshot(), "old version", service.state(URL)["source_token"])
    replacement = doc(BASE + "/extfile/replacement.pdf", up="2026-10-20T10:00")
    add(s, replacement, "replaced", URL)
    s.save_analysis(replacement.url, AnalysisResult(clean_report(), 0, 0, 0.0, MODEL, "stop"),
                    PROMPT_VERSION, parent=P)

    assert service.state(URL)["active"] is False
    new_state = service.state(replacement.url)
    assert new_state["status"] == "none" and new_state["history"] == []
    tally = build_tally(s, Config(model=MODEL, tally_scope={P: "national"}), P, MODEL)
    assert tally["operator_corrected"] == []


def test_tally_uses_active_operator_snapshot_and_clears_corrected_numeric_alerts():
    report = clean_report()
    report["numbers"].update(
        registered={**report["numbers"]["registered"], "value": 1763},
        voted={**report["numbers"]["voted"], "value": 1762, "mark": "crossed_out"},
        ballots_in_box={**report["numbers"]["ballots_in_box"], "value": 1762, "mark": "crossed_out"},
        invalid={**report["numbers"]["invalid"], "value": 26},
        valid={**report["numbers"]["valid"], "value": 1737, "mark": "crossed_out"},
        list_votes=[{"row": 1, "field": {**report["numbers"]["list_votes"][0]["field"], "value": 428}},
                    {"row": 2, "field": {**report["numbers"]["list_votes"][1]["field"], "value": 400}}],
    )
    s = analysed_store(report)
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    state = service.state(URL)
    corrected = snapshot(registered=1763, voted=853, ballots_in_box=853, invalid=26, valid=827,
                         outside_voted=0, outside_confirmations=0,
                         list_votes=[{"row": 1, "value": 427}, {"row": 2, "value": 400}])
    service.save(URL, corrected, "read corrected entries", state["source_token"])

    tally = build_tally(s, Config(model=MODEL, tally_scope={P: "national"}), P, MODEL)
    totals = tally["overall"]
    assert totals["clean"] == 1 and tally["excluded"] == []
    assert totals["all"]["voted"] == totals["cl"]["voted"] == 853
    assert totals["all"]["valid"] == 827 and totals["all"]["lists"] == [427, 400]
    rec = tally["reconciliation"][0]
    assert rec["status"] == "ok" and rec["rules"] == []
    assert tally["operator_corrected"] == [{"id": tally["operator_corrected"][0]["id"],
                                             "m": "Сечањ", "no": "1",
                                             "reason": "read corrected entries",
                                             "at": tally["operator_corrected"][0]["at"]}]
    text = format_tally(tally)
    assert "RG-7 and operator-entered corrections" in text
    assert "operator correction: read corrected entries" in text


def test_operator_snapshot_does_not_clear_unrelated_high_alert_and_suspension_stops_application():
    report = clean_report()
    report["signatures"]["president_signed"] = False
    report["numbers"]["voted"].update(value=900, mark="crossed_out")
    report["numbers"]["ballots_in_box"].update(value=900)
    report["numbers"]["invalid"].update(value=120)
    s = analysed_store(report)
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    service.save(URL, snapshot(), "verified numbers", service.state(URL)["source_token"])

    active = build_tally(s, Config(model=MODEL, tally_scope={P: "national"}), P, MODEL)
    assert active["overall"]["all"]["voted"] == 800
    assert active["overall"]["clean"] == 0
    assert active["excluded"][0]["why"] == ["HIGH alert: V3"]

    changed = clean_report()
    changed["numbers"]["registered"]["value"] = 1001
    s.save_analysis(URL, AnalysisResult(changed, 0, 0, 0.0, MODEL, "stop"), PROMPT_VERSION, parent=P)
    suspended = build_tally(s, Config(model=MODEL, tally_scope={P: "national"}), P, MODEL)
    assert suspended["overall"]["all"]["registered"] == 1001
    assert suspended["operator_corrected"] == []


def test_browse_row_has_compact_operator_correction_status(tmp_path):
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    service.save(URL, snapshot(), "checked against scan", service.state(URL)["source_token"])

    row = build_data(s, tmp_path, cfg=Config(model=MODEL))["rows"][0]
    assert row["oc"] == {"status": "active", "reason": "checked against scan",
                         "at": row["oc"]["at"]}
    assert row["rec"]["status"] == "ok"


def correction_request(server, method, path, body=None, headers=None):
    host = f"127.0.0.1:{server.server_port}"
    base = {"Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"}
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        base.update({"Content-Type": "application/json", "X-RG2W": "1"})
    base.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request(method, path, body=raw, headers=base)
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


def test_correction_http_get_save_revise_and_revoke(tmp_path):
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    server = start_server(tmp_path, tmp_path, 0, correction_api=service)
    try:
        status, initial = correction_request(server, "GET", "/correction?url=" + URL)
        assert status == 200 and initial["status"] == "none"

        status, active = correction_request(server, "POST", "/correction", {
            "url": URL, "action": "correction", "snapshot": snapshot(),
            "reason": "checked scan", "source_token": initial["source_token"]})
        assert status == 200 and active["status"] == "active"

        status, revoked = correction_request(server, "POST", "/correction", {
            "url": URL, "action": "revoke", "reason": "wrong form",
            "source_token": active["source_token"]})
        assert status == 200 and revoked["status"] == "revoked"
    finally:
        server.shutdown()
        server.server_close()


def test_correction_http_rejects_cross_origin_malformed_and_stale_requests(tmp_path):
    s = analysed_store()
    service = OperatorCorrections(s, Config(model=MODEL), MODEL)
    server = start_server(tmp_path, tmp_path, 0, correction_api=service)
    try:
        _, initial = correction_request(server, "GET", "/correction?url=" + URL)
        assert correction_request(server, "POST", "/correction", {
            "url": URL, "action": "correction", "snapshot": snapshot(),
            "reason": "x", "source_token": initial["source_token"]},
            {"Origin": "http://evil.example"})[0] == 403
        assert correction_request(server, "POST", "/correction", {"url": URL})[0] == 400

        changed = clean_report()
        changed["numbers"]["registered"]["value"] = 1001
        s.save_analysis(URL, AnalysisResult(changed, 0, 0, 0.0, MODEL, "stop"),
                        PROMPT_VERSION, parent=P)
        status, body = correction_request(server, "POST", "/correction", {
            "url": URL, "action": "correction", "snapshot": snapshot(),
            "reason": "stale", "source_token": initial["source_token"]})
        assert status == 409 and "source changed" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_page_has_refresh_safe_themed_operator_correction_dialog():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<dialog id="ocdlg"' in html
    assert "function openCorrection(" in html and "function correctionBox(" in html
    assert "sessionStorage.setItem(ocDraftKey" in html
    assert "source_token" in html and 'submitCorrection("correction")' in html and 'submitCorrection("revoke")' in html
    assert "var(--panel)" in html and "var(--accent)" in html and "var(--hi)" in html
    assert "Operator correction" in html and "Validate and save revision" in html
    assert "Corrections counted (RG-7 / operator-entered)" in html
    assert "T.operator_corrected.map" in html
    # The dialog is a static body child, not a DataTable child-row element rebuilt by loadData.
    assert html.index('<dialog id="ocdlg"') < html.index('<div id="formsview">')
    render = html[html.index("function renderCorrectionEditor("):html.index("function openCorrection(")]
    assert "refreshCorrectionValidation();" in render
    assert "saveCorrectionDraft();" not in render  # a successful save must not recreate a cleared draft
    failure = html[html.index("}, e => {", html.index("function submitCorrection(")):html.index("});\n}", html.index("function submitCorrection("))]
    assert failure.index("saveCorrectionDraft();") < failure.index("Not saved:")
    success = html[html.index(".then(state => {", html.index("function submitCorrection(")):html.index("}, e => {", html.index("function submitCorrection("))]
    assert success.index("dataBuilt = null;") < success.index("loadData();")
    assert '$("ocdlg").close();' in success
    assert '$("ocdlg").close();' not in failure
    assert 'id="nc"' in html and "number check" in html
    assert '<option value="gap">turnout gap</option>' in html
    assert "function reconciliationEl(" in html
    assert "rec-ok" in html and "rec-fail" in html and "rec-unavailable" in html
    assert 'data: "rec", title: "Numbers"' in html and "r.rec.rules.includes(nc)" in html
    assert 'nc === "gap"' in html and '"Gap " + snum(rec.gap)' in html
