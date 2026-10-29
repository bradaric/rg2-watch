import json
from types import SimpleNamespace

import pytest

from rg2watch.analyze import AnalysisResult, TYPE13_SCHEMA, _normalise_type13, analyze_document
from rg2watch.config import Config
from rg2watch.browse import build_data
from rg2watch.browse import STATIC
from rg2watch.findings import findings_for
from rg2watch.models import Doc, Event, RARE_TYPES
from rg2watch.models import Finding
from rg2watch.operator_corrections import Type13Corrections
from rg2watch.pipeline import TYPE13_PROMPT_VERSION
from rg2watch.store import Store
from rg2watch.tally import build_tally
from test_browse import BASE, P, add, doc, store as browse_store
from test_operator_corrections import MODEL
from test_operator_corrections import correction_request
from rg2watch.server import start_server
from test_rules import clean_report


def correction_doc(url="https://x/type13.pdf"):
    return Doc("Градска општина Савски Венац", "32", 'МЗ "ДЕДИЊЕ"',
               "Исправка решења", 13, url, "2026-10-28T18:14")


def raw_type13(target="rg2", confidence="high"):
    return {
        "target": target,
        "evidence": "Decision corrects three RG-2 result fields.",
        "decision_date": "27.10.2026",
        "station_no": "32",
        "changes": [
            {"field": "voted", "row": None, "label": "", "item": "11.1", "old": 701, "new": 697},
            {"field": "ballots_in_box", "row": None, "label": "", "item": "11.2", "old": 701, "new": 697},
            {"field": "valid", "row": None, "label": "", "item": "11.4", "old": 697, "new": 693},
        ],
        "table": [],
        "reason": "A serious completion error was found.",
        "confidence": confidence,
        "corrected_decision": "RG-2 polling-station minutes",
    }


def test_type13_is_rare_and_alerted():
    assert 13 in RARE_TYPES
    [finding] = findings_for([Event("added", correction_doc())])
    assert (finding.rule, finding.severity, finding.summary) == (
        "D1", "MEDIUM", "ISPRAVKA published")


def test_type13_normalises_rg2_changes_and_sanitises_text():
    report = _normalise_type13(raw_type13() | {"evidence": "  correction\n decision  "})
    assert report["target"] == "rg2"
    assert report["evidence"] == "correction decision"
    assert [(x["item"], x["old"], x["new"]) for x in report["changes"]] == [
        ("voted", 701, 697), ("ballots_in_box", 701, 697), ("valid", 697, 693)]


def test_analyze_document_type13_uses_dedicated_schema_and_prompt():
    seen = {}

    def stream(**kwargs):
        import contextlib
        seen.update(kwargs)

        @contextlib.contextmanager
        def cm():
            yield SimpleNamespace(get_final_message=lambda: SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(raw_type13()))],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                stop_reason="end_turn", model="m"))
        return cm()

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    result = analyze_document([SimpleNamespace(name="p1", jpeg=b"x")], client,
                              Config(fallbacks=False), "type13")

    assert seen["output_config"]["format"]["schema"] == TYPE13_SCHEMA
    assert "Исправка решења" in seen["messages"][0]["content"][-1]["text"]
    assert result.report["target"] == "rg2"


def test_type13_document_enters_analysis_queue():
    store = Store(":memory:")
    source = correction_doc()
    store.apply_events("e", store.start_sweep("e", "rare"), [Event("added", source)])

    [queued] = store.next_for_analysis(1, parent="e")

    assert queued == source


def test_pipeline_analyses_type13_with_dedicated_prompt(tmp_path, monkeypatch):
    from rg2watch import render
    from test_pipeline import Env

    monkeypatch.setattr(render, "_osd_rotation", lambda image, timeout=10: 0)
    reply = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(raw_type13()))],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        stop_reason="end_turn", model="claude-opus-5-5")
    env = Env(tmp_path, reply)
    source = correction_doc("https://www.rik.parlament.gov.rs/extfile/a.pdf")

    env.publish(source)
    assert env.drain() == 1

    report = env.store.latest_report(source.url, "claude-opus-5-5", TYPE13_PROMPT_VERSION)
    assert report["target"] == "rg2"
    assert env.status(source.url)[0] == "done"


def test_type13_seen_before_analysis_is_requeued_without_backlog_flag():
    store = Store(":memory:")
    source = correction_doc()
    store.apply_events("e", store.start_sweep("e", "rare"), [Event("added", source)])
    store._db.execute("UPDATE documents SET analysis_status='skipped' WHERE url=?", (source.url,))

    assert store.requeue_type13("e", prompt_version=TYPE13_PROMPT_VERSION) == 1
    assert [doc.url for doc in store.next_for_analysis(1, parent="e")] == [source.url]


def correction_workflow_store():
    store = browse_store()
    target = doc(BASE + "/rg2-32.pdf", no="32", muni="Градска општина Савски Венац")
    add(store, target)
    store.save_analysis(target.url, AnalysisResult(clean_report(), 0, 0, 0.0, MODEL, "stop"),
                        "v10", parent=P)
    source = correction_doc(BASE + "/type13-32.pdf")
    store.apply_events(P, store.start_sweep(P, "rare"), [Event("added", source)])
    report = _normalise_type13(raw_type13() | {
        "changes": [
            {"field": "voted", "row": None, "label": "", "item": "11.1", "old": 800, "new": 790},
            {"field": "ballots_in_box", "row": None, "label": "", "item": "11.2", "old": 800, "new": 790},
            {"field": "valid", "row": None, "label": "", "item": "11.4", "old": 780, "new": 770},
        ],
        "table": [
            {"field": "list", "row": 1, "label": "", "item": "11.5", "value": 390},
            {"field": "list", "row": 2, "label": "", "item": "11.5", "value": 380},
        ],
    })
    store.save_analysis(source.url, AnalysisResult(report, 0, 0, 0.0, MODEL, "stop"),
                        TYPE13_PROMPT_VERSION, parent=P)
    return store, target, source


def test_type13_state_attaches_to_rg2_and_prefills_existing_snapshot():
    store, target, source = correction_workflow_store()
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)

    state = service.state(source.url)

    assert state["target_url"] == target.url
    assert state["classification"] == "rg2"
    assert state["can_approve"] is True
    assert state["proposal"]["voted"] == 790
    assert state["proposal"]["valid"] == 770
    assert state["proposal"]["list_votes"] == [{"row": 1, "value": 390}, {"row": 2, "value": 380}]


def test_type13_approval_uses_operator_snapshot_and_keeps_review_separate():
    store, target, source = correction_workflow_store()
    store.set_review(target.url, "escalate", "check correction")
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)
    state = service.state(source.url)

    saved = service.approve(source.url, state["proposal"], "official type-13 correction",
                            state["source_token"])

    assert saved["resolution"] == "rg2_correction"
    tally = build_tally(store, Config(model=MODEL, tally_scope={P: "national"}), P, MODEL)
    assert tally["overall"]["all"]["voted"] == 0
    assert tally["held"] == [{"id": tally["held"][0]["id"], "m": "Градска општина Савски Венац",
                              "no": "32", "verdict": "escalate"}]
    store.set_review(target.url, "ok", "official correction checked")
    released = build_tally(store, Config(model=MODEL, tally_scope={P: "national"}), P, MODEL)
    assert released["overall"]["all"]["voted"] == 790


def test_operator_can_mark_type13_as_other_without_tally_effect():
    store, target, source = correction_workflow_store()
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)
    state = service.state(source.url)

    saved = service.resolve(source.url, "other_decision", "corrects a different decision",
                            state["source_token"])

    assert saved["resolution"] == "other_decision"
    assert saved["target_state"]["status"] == "none"
    tally = build_tally(store, Config(model=MODEL, tally_scope={P: "national"}), P, MODEL)
    assert tally["overall"]["all"]["voted"] == 800


def test_type13_revoke_uses_existing_complete_snapshot_semantics():
    store, target, source = correction_workflow_store()
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)
    state = service.state(source.url)
    approved = service.approve(source.url, state["proposal"], "official correction",
                               state["source_token"])

    revoked = service.revoke(source.url, "approval entered in error", approved["source_token"])

    assert revoked["resolution"] == "revoke"
    assert revoked["target_state"]["status"] == "revoked"
    tally = build_tally(store, Config(model=MODEL, tally_scope={P: "national"}), P, MODEL)
    assert tally["overall"]["all"]["voted"] == 800


def test_approved_type13_must_be_revoked_before_reclassification():
    store, target, source = correction_workflow_store()
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)
    state = service.state(source.url)
    approved = service.approve(source.url, state["proposal"], "official correction",
                               state["source_token"])

    with pytest.raises(ValueError, match="revoke"):
        service.resolve(source.url, "other_decision", "wrong type", approved["source_token"])


def test_removed_type13_source_suspends_approved_snapshot():
    store, target, source = correction_workflow_store()
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)
    state = service.state(source.url)
    service.approve(source.url, state["proposal"], "official correction", state["source_token"])

    store.apply_events(P, store.start_sweep(P, "rare"), [Event("removed", source)])

    suspended = service.operator.state(target.url)
    assert suspended["status"] == "suspended"
    tally = build_tally(store, Config(model=MODEL, tally_scope={P: "national"}), P, MODEL)
    assert tally["overall"]["all"]["voted"] == 800


def test_type13_equal_to_repeat_round_start_blocks_numeric_target():
    store, target, source = correction_workflow_store()
    repeat = doc(BASE + "/repeat-32.pdf", no="32", muni="Градска општина Савски Венац",
                 dt=15, up=source.uploaded_at)
    add(store, repeat)
    store.save_analysis(repeat.url, AnalysisResult(clean_report(), 0, 0, 0.0, MODEL, "stop"),
                        "v10", parent=P)
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)

    state = service.state(source.url)

    assert state["target_url"] is None
    assert state["can_edit"] is False


def test_manual_type13_correction_works_without_model_analysis():
    store, target, source = correction_workflow_store()
    store._db.execute("DELETE FROM analyses WHERE document_id=(SELECT id FROM documents WHERE url=?)",
                      (source.url,))
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)
    state = service.state(source.url)
    manual = state["target_state"]["snapshot"] | {
        "voted": 790, "ballots_in_box": 790, "valid": 770,
        "list_votes": [{"row": 1, "value": 390}, {"row": 2, "value": 380}],
    }

    saved = service.approve(source.url, manual, "manually read official correction",
                            state["source_token"])

    assert saved["resolution"] == "rg2_correction"


def test_browse_attaches_type13_to_matching_rg2_row(tmp_path):
    store, target, source = correction_workflow_store()

    [row] = build_data(store, tmp_path, parent=P, cfg=Config(model=MODEL))["rows"]

    assert row["u"] == target.url
    assert row["t13"] == [{
        "u": source.url,
        "rik": source.url,
        "up": source.uploaded_at,
        "pdf": None,
        "classification": "rg2",
        "evidence": "Decision corrects three RG-2 result fields.",
        "confidence": "high",
        "resolution": None,
        "can_approve": True,
        "can_edit": True,
        "source_token": row["t13"][0]["source_token"],
    }]


def test_type13_http_reuses_correction_snapshot_contract(tmp_path):
    store, target, source = correction_workflow_store()
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)
    server = start_server(tmp_path, tmp_path, 0, type13_api=service)
    try:
        status, initial = correction_request(server, "GET", "/type13?url=" + source.url)
        assert status == 200 and initial["target_url"] == target.url

        status, saved = correction_request(server, "POST", "/type13", {
            "url": source.url, "action": "correction", "snapshot": initial["proposal"],
            "reason": "official correction", "source_token": initial["source_token"]})
        assert status == 200 and saved["resolution"] == "rg2_correction"
    finally:
        server.shutdown()
        server.server_close()


def test_page_reuses_existing_correction_dialog_for_type13():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "function type13Box(" in html
    assert "function openType13Correction(" in html
    assert 'ocFetch("type13?url="' in html
    assert 'ocEndpoint = "type13"' in html
    assert "state.proposal || state.active_snapshot" in html
    assert "Mark as other correction" in html and "Mark uncertain" in html
    open_type13 = html[html.index("function openType13Correction("):html.index("function updateCorrectionSummary(")]
    assert ".catch(e =>" in open_type13


def test_approved_type13_marks_fixed_numeric_finding_resolved(tmp_path):
    store, target, source = correction_workflow_store()
    faulty = clean_report()
    faulty["numbers"]["list_votes"][1]["field"]["value"] = 370
    store.save_analysis(target.url, AnalysisResult(faulty, 0, 0, 0.0, MODEL, "stop"),
                        "v10", parent=P)
    store.add_finding(P, Finding("A3", "HIGH", target, "list votes do not sum to valid"), target.url)
    service = Type13Corrections(store, Config(model=MODEL, tally_scope={P: "national"}), MODEL)
    state = service.state(source.url)
    service.approve(source.url, state["proposal"], "official correction", state["source_token"])

    [row] = build_data(store, tmp_path, parent=P, cfg=Config(model=MODEL))["rows"]

    assert row["f"] == []
    assert row["resolved"] == [["A3", "HIGH", "list votes do not sum to valid"]]
    assert row["todo"] is False
