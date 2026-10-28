import copy
import hashlib
import json
import os
import subprocess
import threading
import time

import rg2watch.cli as cli
from rg2watch.pipeline import PROMPT_VERSION
from rg2watch.analyze import AnalysisResult
from rg2watch.browse import STATIC, STATIC_FILES, build_page
from rg2watch.cli import main
from rg2watch.config import Config
from rg2watch.models import Doc, Election, Event, Finding
from rg2watch.store import Store
from test_rules import clean_report, nf

real_sleep = time.sleep
P = "662405"
BASE = "https://rik.example"


def doc(url, muni="Сечањ", no="1", dt=6, up="2026-10-19T20:15", label="lbl"):
    return Doc(muni, no, "ОШ", label, dt, url, up)


def store():
    s = Store(":memory:")
    s.upsert_election(Election(P, "1", "t", "Title", BASE))
    return s


def add(s, d, kind="added", prev=None):
    s.apply_events(P, s.start_sweep(P, "rg2"), [Event(kind, d, prev)])


def rows_of(path):
    return json.loads((path.parent / "data.json").read_text(encoding="utf-8"))["rows"]


def test_groups_versions_and_states(tmp_path):
    s = store()
    old, new = doc(BASE + "/extfile/old.pdf"), doc(BASE + "/extfile/new.pdf", up="2026-10-19T21:00")
    add(s, old)
    add(s, new, "replaced", old.url)
    gone = doc(BASE + "/extfile/g.pdf", no="2")
    add(s, gone)
    add(s, gone, "removed")
    add(s, doc(BASE + "/extfile/seven.pdf", dt=7, no="3"))
    out = tmp_path / "index.html"
    assert build_page(s, out, tmp_path / "pdfs") == 2
    by_no = {r["no"]: r for r in rows_of(out)}
    assert by_no["1"]["state"] == "replaced" and by_no["1"]["up"] == "2026-10-19T21:00"
    assert by_no["1"]["doc_type"] == 6 and by_no["1"]["doc_type_label"] == "RG-2"
    assert [v["rik"] for v in by_no["1"]["v"]] == [old.url]
    assert by_no["2"]["state"] == "removed"


def test_form_rows_include_optional_domestic_and_foreign_settlement_labels(tmp_path):
    s = store()
    add(s, doc(BASE + "/extfile/domestic.pdf", muni="Градска општина Гроцка", no="7"))
    add(s, doc(BASE + "/extfile/foreign.pdf", muni="Иностранство", no="79"))
    add(s, doc(BASE + "/extfile/unknown.pdf", muni="Непостојећа општина", no="1"))
    out = tmp_path / "index.html"

    build_page(s, out, tmp_path / "pdfs")

    by_muni = {row["muni"]: row for row in rows_of(out)}
    assert by_muni["Градска општина Гроцка"]["settlement"] == "ВИНЧА"
    assert by_muni["Иностранство"]["settlement"] == "ПАРИЗ · ФРАНЦУСКА"
    assert "settlement" not in by_muni["Непостојећа општина"]


def test_severity_order_and_rik_link_built_from_relative_url(tmp_path):
    s = store()
    d = doc("/extfile/a.pdf")
    add(s, d)
    for rule, sev in (("X1", "LOW"), ("X2", "HIGH"), ("X3", "MEDIUM")):
        s.add_finding(P, Finding(rule, sev, d, rule + " s"), d.url)
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    (r,) = rows_of(out)
    assert [f[1] for f in r["f"]] == ["HIGH", "MEDIUM", "LOW"] and r["sev"] == 3
    assert r["rik"] == BASE + "/extfile/a.pdf"


def test_non_http_url_gets_no_rik_link(tmp_path):
    s = store()
    add(s, doc("javascript:alert(1)"))
    add(s, doc("file:///tmp/x.pdf", no="2"))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    assert all(r["rik"] is None for r in rows_of(out))


def test_pdf_link_only_when_cached(tmp_path):
    s = store()
    d = doc(BASE + "/extfile/a.pdf")
    add(s, d)
    out = tmp_path / "web" / "index.html"
    cache = tmp_path / "pdfs"
    build_page(s, out, cache)
    assert rows_of(out)[0]["pdf"] is None
    cache.mkdir()
    (cache / (hashlib.sha256(d.url.encode()).hexdigest() + ".pdf")).write_bytes(b"%PDF")
    build_page(s, out, cache)
    assert rows_of(out)[0]["pdf"] == hashlib.sha256(d.url.encode()).hexdigest()


def test_selected_repeat_rg2_offers_operator_corrections(tmp_path):
    s = store()
    first = doc(BASE + "/e/first.pdf", no="1", dt=6)
    repeat = doc(BASE + "/e/repeat.pdf", no="1", dt=10, up="2026-10-20T20:15")
    add(s, first)
    add(s, repeat)
    model = "claude-sonnet-5-5"
    for d in (first, repeat):
        s.save_analysis(d.url, AnalysisResult(clean_report(), 0, 0, 0, model, "stop"),
                        PROMPT_VERSION, parent="adhoc")
    out = tmp_path / "index.html"

    build_page(s, out, tmp_path / "pdfs", cfg=Config(model=model))

    by_type = {r["doc_type"]: r for r in rows_of(out)}
    assert by_type[6]["correctable"] is False
    assert by_type[6]["tally_state"] == "superseded"
    assert by_type[10]["correctable"] is True
    assert by_type[10]["tally_state"] == "selected"
    assert by_type[10]["doc_type_label"] == "RG-2-R"
    assert by_type[6]["rec"]["result"] == {
        "corrected": {"included": None, "reason": "superseded by repeat vote"},
        "filed": {"included": None, "reason": "superseded by repeat vote"},
    }
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    correction_box = html[html.index("function correctionBox("):html.index("function reconciliationEl(")]
    assert 'v.ast === "done" && v.correctable' in correction_box


def test_browse_exposes_quarantined_content_classification(tmp_path):
    s = store()
    d = doc(BASE + "/e/mislabeled.pdf", no="1", dt=15)
    add(s, d)
    rep = clean_report()
    rep["document_classification"] = {"kind": "rg7", "evidence": "typed correction decision"}
    s.save_analysis(d.url, AnalysisResult(rep, 0, 0, 0, M := "claude-sonnet-5-5", "stop"),
                    PROMPT_VERSION, parent="adhoc")

    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs", cfg=Config(model=M))

    (row,) = rows_of(out)
    assert row["classification"] == {
        "kind": "rg7", "evidence": "typed correction decision", "override": False,
        "override_action": None, "legacy": False, "status": "quarantined_rg7",
    }
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "function classificationBox(" in html
    assert 'fetch("classification"' in html
    assert "confirm as RG-2" in html and "revoke RG-2 confirmation" in html
    assert "mark as mislabeled RG-7" in html


def test_successful_legacy_analysis_displays_as_rg2_without_reanalysis(tmp_path):
    s = store()
    d = doc(BASE + "/e/legacy.pdf", no="1", dt=15)
    add(s, d)
    rep = clean_report()
    rep.pop("document_classification")
    model = "claude-sonnet-5-5"
    s.save_analysis(d.url, AnalysisResult(rep, 0, 0, 0, model, "stop"), "v9", parent="adhoc")

    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs", cfg=Config(model=model))

    (row,) = rows_of(out)
    assert row["classification"]["status"] == "confirmed_rg2"
    assert row["classification"]["evidence"] == "successful legacy analysis"
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'selected: "selected for tally"' in html
    assert 'pending: "classification pending"' in html
    assert 'analysis " + ({done: "complete"' in html


def test_result_inclusion_analysis_status_filter_and_models_column_are_present():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'id="ri"' in html
    inclusion = html[html.index('result inclusion <select id="ri"'):html.index('</select>', html.index('result inclusion <select id="ri"'))]
    assert all(f'<option value="{value}">{label}</option>' in inclusion for value, label in (
        ("counted-both", "counted in both"),
        ("corrected-only", "corrected only"),
        ("filed-only", "as filed only"),
        ("not-counted", "not counted"),
        ("not-in-tally", "not in tally"),
        ("excluded-filed", "excluded as filed"),
    ))
    assert 'not counted with corrections' not in inclusion
    assert 'not counted as filed' not in inclusion
    assert ('analysis status <select id="ast"><option value="">any</option>' in html and
            all(f'<option value="{status}">{status}</option>' in html
                for status in ("pending", "done", "failed", "off")))
    assert '"nc", "ri", "an", "ast", "fl"' in html
    assert '(!sel("ast") || r.ast === sel("ast"))' in html
    assert 'title: "Result inclusion"' in html
    assert 'data: "mods", title: "Models", visible: false' in html
    assert "d.rgColumns = 3" in html and "d.rgColumns !== 3" in html


def test_result_inclusion_filter_uses_the_same_five_states_as_the_column():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    start = html.index("function resultInclusionStatus(")
    code = html[start:html.index("function resultInclusionEl(", start)]
    script = f"""
const assert = require("assert");
{code}
const result = (corrected, filed) => ({{corrected: {{included: corrected}}, filed: {{included: filed}}}});
assert.equal(resultInclusionStatus(result(true, true)), "counted-both");
assert.equal(resultInclusionStatus(result(true, false)), "corrected-only");
assert.equal(resultInclusionStatus(result(false, true)), "filed-only");
assert.equal(resultInclusionStatus(result(false, false)), "not-counted");
assert.equal(resultInclusionStatus(result(null, null)), "not-in-tally");
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'ri === "excluded-filed" ? r.rec.result.filed.included === false' in html


def test_document_type_and_decision_status_filters_are_in_their_workflow_sections():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    scope = html[html.index('id="scopefilters"'):html.index('id="reviewfilters"')]
    review = html[html.index('id="reviewfilters"'):html.index('id="morefilters"')]
    assert 'document type <select id="dt">' in scope
    assert 'decision status <select id="an">' in review
    assert '<option value="undetermined">cannot be determined (RG-8)</option>' in review
    assert '<option value="annulled">annulled by RG-9</option>' in review
    assert '<option value="not">no RG-8/RG-9 decision</option>' in review
    assert 'id="ud"' not in review
    assert '"e", "dt", "mod"' in html and '"ri", "an", "ast"' in html
    assert '(!sel("dt") || String(r.doc_type) === sel("dt"))' in html
    assert '(!sel("an") || (r.rec.annulled ? "annulled" : r.rec.undetermined ? "undetermined" : "not") === sel("an"))' in html
    assert 'optsOf($("dt"), "all document types"' in html


def test_common_review_filter_presets_preserve_scope_and_auto_refresh_setting():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert '<div id="presetbar" class="presetbar"' in html
    assert '<button type="button" data-preset="review-filed">Review excluded as filed</button>' in html
    assert '<button type="button" data-preset="review-v13">Review V13</button>' in html
    assert '<button id="clearfilters" type="button">Clear all filters</button>' in html
    start = html.index("const FILTER_PRESETS =")
    code = html[start:html.index("// rule codes", start)]
    script = f"""
const assert = require("assert");
{code}
assert.deepEqual(presetControls("review-filed", {{e: "779676", m: "Alpha", auto: false}}),
  {{e: "779676", m: "Alpha", auto: false, rv: "todo", ri: "excluded-filed", ast: "done", an: "not"}});
assert.deepEqual(presetControls("review-v13", {{e: "779676", m: "Alpha", auto: false}}),
  {{e: "779676", m: "Alpha", auto: false, rv: "todo", ast: "done", rules: ["V13"]}});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'setCtl(presetControls(name, {e: sel("e"), m: sel("m"), auto: $("auto").checked}));' in html
    assert 'document.querySelectorAll("[data-preset]")' in html
    assert 'setCtl({auto: $("auto").checked});' in html


def test_form_filters_are_visually_grouped_by_workflow():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert '<div id="filtergroups">' in html
    assert '<div id="scopefilters" class="filtergroup"' in html
    assert '<div id="reviewfilters" class="filtergroup"' in html
    assert '<div id="morefilters" class="filtergroup"' in html
    assert all(f'id="{group}title" class="filtertitle">{label}</span>' in html for group, label in (
        ("preset", "Common workflows"), ("scope", "Scope"),
        ("review", "Review filters"), ("more", "More")))
    assert ".presetbar{" in html and ".filtergroup{" in html
    assert "border-left:4px solid var(--accent)" in html
    assert "@media (max-width:700px)" in html and ".filtertitle{flex-basis:100%}" in html

    review_start = html.index('<div id="reviewfilters"')
    review = html[review_start:html.index('<div id="morefilters"', review_start)]
    assert [review.index(f'id="{control}"') for control in
            ("ast", "an", "sev", "rulesbox", "nc", "ri", "rv")] == sorted(
                review.index(f'id="{control}"') for control in
                ("ast", "an", "sev", "rulesbox", "nc", "ri", "rv"))

    more = html[html.index('<div id="morefilters"'):html.index('<div id="onlyid"')]
    assert [more.index(f'id="{control}"') for control in
            ("rp", "rm", "fl", "c7", "dis", "loc", "legendbox", "esccsv", "colbox")] == sorted(
                more.index(f'id="{control}"') for control in
                ("rp", "rm", "fl", "c7", "dis", "loc", "legendbox", "esccsv", "colbox"))
    assert 'id="auto"' not in more
    assert more.index('id="colbox"') > more.index('id="esccsv"')
    assert "#colbox{margin-left:auto}" in html
    assert "#colbox #cols{left:auto;right:0}" in html

    preset = html[html.index('<div id="presetbar"'):html.index('<div id="filtergroups"')]
    assert preset.index('id="auto"') < preset.index('id="clearfilters"')
    assert "#presetutils{margin-left:auto;display:flex" in html


def test_alert_action_links_stay_intact_on_their_own_mobile_row():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert ".lk a,.lk .chip{display:inline-block;white-space:nowrap" in html
    assert ".card.wide td.lk{flex:0 0 100%;text-align:left}" in html


def test_operator_page_uses_adaptive_navy_serbian_theme():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "--bg:#eaf0f6" in html and "--bg:#061321" in html
    assert "@media (prefers-color-scheme:dark)" in html


def test_analysis_status_uses_compact_grouped_labels():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    for label in ("primary queue", "primary results", "arithmetic results", "pace", "ETA"):
        assert f'["{label}"' in html
    assert '" waiting · "' in html and '" done · "' in html and '" s/form"' in html
    assert '"queued / running"' not in html and '"time to clear queue"' not in html


def test_review_and_alert_status_share_one_card():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'card("Review & alerts"' in html
    assert 'card("Review"' not in html and 'card("Alerts"' not in html


def test_results_details_and_audit_are_secondary_collapsed_groups():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert '<details id="tdetails" class="resultgroup">' in html
    assert '<summary>Detailed totals <span id="tdetailcount"></span></summary>' in html
    assert '<details id="taudit" class="resultgroup">' in html
    assert '<summary>Result audit trail <span id="tauditcount"></span></summary>' in html
    assert '<details id="tdetails" class="resultgroup" open>' not in html
    assert '<details id="taudit" class="resultgroup" open>' not in html

    detailed = html[html.index('<details id="tdetails"'):html.index('</details>', html.index('<details id="tdetails"'))]
    audit = html[html.index('<details id="taudit"'):html.index('</details>', html.index('<details id="taudit"'))]
    assert all(marker in detailed for marker in ('id="tctl"', 'id="csv"', 'id="toverall"', 'id="tmunis"'))
    assert all(marker in audit for marker in ('id="texcl"', 'id="tcor"', 'id="tnotes"'))
    assert '$("tdetailcount").textContent = ' in html
    assert '$("tauditcount").textContent = ' in html


def test_station_results_are_collapsed_with_compact_controls_and_columns():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert '<details id="stationresults" hidden>' in html
    assert '<details id="stationresults" hidden open>' not in html
    assert all(f'id="{control}"' in html for control in (
        "stationsort", "stationbars", "stationtoggle", "stationmore"))
    assert "#stationmore[hidden]{display:none}" in html
    assert all(f'<option value="{n}">' in html for n in (1, 2, 3))
    assert '<option value="1">1 листа</option>' in html
    assert '<option value="2" selected>2 листе</option>' in html
    assert '<option value="3">3 листе</option>' in html
    assert 'id="stationfilters"' in html
    assert all(f'data-station-filter="{value}"' in html for value in (
        "all", "out", "escalate", "unsure", "undecided"))
    assert all(f'value="{value}"' in html for value in (
        "margin-close", "margin-wide", "turnout-high", "turnout-low", "review"))
    assert 'id="sortturnouthigh"' in html and 'id="sortturnoutlow"' in html
    assert all(label in html for label in (
        "Бирачко место", "Излазност", "Важећи", "Победник", "Удео", "Маргина", "Статус"))
    assert 'Прошири све' in html and 'Скупи све' in html
    assert '.stationstatus{display:grid;grid-template-columns:auto auto auto;align-items:center;justify-content:end;' in html
    assert '.stationmenubtn{display:inline-grid;place-items:center;' in html
    assert '.stationmenubtn::before{content:"";' in html
    assert '.stationreview{font-size:.8em;' in html
    assert '.stationreview.hasnote:hover::after,.stationreview.hasnote:focus::after{display:block}' in html
    assert '.stationstatusmain.hasreason:hover::after,.stationstatusmain.hasreason:focus::after{display:block}' in html
    assert '.stationmarginfill{display:block;height:100%;background:var(--bar)' in html


def test_collapsed_station_results_defer_card_rendering_and_initial_tally_renders_once():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    station = html[html.index("function renderStationResults("):html.index("function renderHero(")]
    assert station.index("if (!section.open)") < station.index("let records = stations.map")
    assert '$("stationresults").addEventListener("toggle"' in station

    apply = html[html.index("function applyData("):html.index("// ---- results")]
    assert apply.count("renderTally();") == 1
    assert "const first = !table;" in apply


def test_forms_table_renders_settlement_on_a_small_separate_line():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "function stationCellHtml" in html
    render = html[html.index("function stationCellHtml"):html.index("function optsOf")]
    script = r'''
const assert = require("assert");
const esc = value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
''' + render + r'''
const shown = stationCellHtml({muni: "Иностранство", no: "79", name: "Амбасада", settlement: "ПАРИЗ · ФРАНЦУСКА"});
assert.equal(shown, "Иностранство<br>BM 79 - Амбасада<span class=\"stationplace\">ПАРИЗ · ФРАНЦУСКА</span>");
assert.equal(stationCellHtml({muni: "Unknown", no: "1", name: "School"}), "Unknown<br>BM 1 - School");
assert(stationCellHtml({muni: "M", no: "1", name: "N", settlement: "<Paris>"}).includes("&lt;Paris&gt;"));
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert ".stationplace{display:block;color:var(--mut);font-size:12px" in html


def test_current_escalation_note_is_available_from_forms_table_pill():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    verdict = html[html.index("function verdictHtml"):html.index("function verdictEl")]
    script = r'''
const assert = require("assert");
const VERDICTS = ["ok", "escalate", "unsure"];
const VLABEL = {ok: "ok", escalate: "escalate", unsure: "unsure"};
const esc = value => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;")
  .replaceAll("<", "&lt;").replaceAll(">", "&gt;");
''' + verdict + r'''
const noted = verdictHtml({v: "escalate", n: 'Проверити "потписе" <сада>'});
assert(noted.includes('class="rv rv-escalate hasnote"'));
assert(noted.includes('data-note="Проверити &quot;потписе&quot; &lt;сада&gt;"'));
assert(noted.includes('tabindex="0"'));
assert(noted.includes('aria-label="escalate: Проверити &quot;потписе&quot; &lt;сада&gt;"'));
assert(!verdictHtml({v: "escalate"}).includes("hasnote"));
assert(!verdictHtml({v: "ok", n: "checked"}).includes("hasnote"));
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert ".rv.hasnote:hover::after,.rv.hasnote:focus::after{display:block}" in html


def test_non_default_form_select_filters_use_active_filter_colors():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    active = "#ctl select:has(option:not(:first-child):checked)"
    assert active + "{background:var(--accent-bg);border-color:var(--accent)}" in html


def test_operator_page_uses_official_static_flag_background():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'class="serbian-flag-bg" aria-hidden="true"' in html
    assert 'class="serbian-flag-art"' in html and "Flag_of_Serbia.svg" in html
    assert ".serbian-flag-bg{position:fixed;z-index:-1;left:-12vw;top:-6vh" in html
    assert "pointer-events:none" in html
    assert "mask-image:linear-gradient(90deg" in html
    assert "mask-image:linear-gradient(135deg" not in html
    assert "linear-gradient(to bottom,var(--flag-red)" not in html


def test_operator_page_has_separate_official_rik_results_tab():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="tabofficial"' in html and "RIK official results" in html
    assert 'id="officialview"' in html and "Official RIK data" in html
    assert "official-results.json" in html and "function renderOfficialResults(" in html
    assert "РИК" in html and "НЕЗВАНИЧНО" in html
    assert "#officialtable{width:100%;border-collapse:collapse;background:var(--panel);border:0;box-shadow:none}" in html


def test_operator_page_preserves_active_tab_in_url_hash():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "function activeTab()" in html
    assert 'if (activeTab() !== "forms") p.set("tab", activeTab());' in html
    assert 'setTab(p.get("tab"), false);' in html
    assert 'function setTab(name, updateHash = true)' in html


def test_operator_page_supports_shareable_form_deep_links():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'const formId = p.get("form")' in html
    assert 'showForm(form.id, form.muni + " BM " + form.no, false);' in html
    assert 'if (onlyId != null && activeTab() === "forms") p.set("form", String(onlyId));' in html


def test_json_uses_utf8_and_escapes_lone_surrogate_and_script_markup():
    from rg2watch.browse import _json
    blob = _json({"x": "Ћирилица a\ud800b\u2028</script>"})
    raw = blob.encode("utf-8")
    assert b"\\ud800" in raw and b"</script>" not in raw
    assert "Ћирилица" in blob and json.loads(blob)["x"].startswith("Ћирилица")


def test_script_close_tag_escaped_in_utf8_files(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf", muni="</script><b>x", label="Ћ<!-- y"))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    for name in ("data.js", "data.json"):
        raw = (tmp_path / name).read_bytes()
        assert "Ћ".encode() in raw and b"</script>" not in raw and b"<!--" not in raw
    assert rows_of(out)[0]["muni"] == "</script><b>x"


def test_build_writes_small_data_metadata_for_correction_refresh(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf"))
    out = tmp_path / "index.html"

    build_page(s, out, tmp_path / "pdfs")

    data = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    meta_path = tmp_path / "data-meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta == {"built": data["built"], "corrections": {}}
    assert meta_path.stat().st_size < 200


def test_atomic_write_leaves_no_temp_and_keeps_old_on_failure(tmp_path, monkeypatch):
    s = store()
    add(s, doc(BASE + "/e/a.pdf"))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    before = (tmp_path / "data.json").read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))

    def boom(*a):
        raise OSError("nope")

    monkeypatch.setattr("rg2watch.browse.os.replace", boom)
    add(s, doc(BASE + "/e/b.pdf", no="2"))
    try:
        build_page(s, out, tmp_path / "pdfs")
    except OSError:
        pass
    assert (tmp_path / "data.json").read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.tmp"))


def test_stale_tmp_files_are_removed_fresh_ones_kept(tmp_path):
    s = store()
    old, fresh = tmp_path / "x.tmp", tmp_path / "y.tmp"
    old.write_text("x")
    fresh.write_text("y")
    t = time.time() - 3600
    os.utime(old, (t, t))
    build_page(s, tmp_path / "index.html", tmp_path / "pdfs")
    assert not old.exists() and fresh.exists()


def test_rg7_decisions_carry_links_wherever_shown():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.count("linksOf(x)") >= 3  # child row, forms-table badges, results list
    assert "row.r7.find(y => y.u === d.u)" in html and "showForm(c.id" in html
    assert "for (const v of r.r7)" in html and "return [r, v]" in html
    assert "showForm(r.id" in html
    assert "x[8]" in html and "x[9]" in html


def test_static_files_ship_and_are_copied_only_when_changed(tmp_path):
    for name in STATIC_FILES:
        assert (STATIC / name).is_file()
    versions = (STATIC / "VERSIONS.txt").read_text()
    for name in STATIC_FILES[1:]:
        assert name in versions and hashlib.sha256((STATIC / name).read_bytes()).hexdigest() in versions
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "innerHTML" not in html.replace("never innerHTML", "") and "http://" not in html
    s = store()
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    jq = tmp_path / "jquery.min.js"
    assert jq.read_bytes() == (STATIC / "jquery.min.js").read_bytes()
    mtime = jq.stat().st_mtime_ns
    build_page(s, out, tmp_path / "pdfs")
    assert jq.stat().st_mtime_ns == mtime
    jq.write_text("tampered")
    build_page(s, out, tmp_path / "pdfs")
    assert jq.read_bytes() == (STATIC / "jquery.min.js").read_bytes()


def test_null_station_key_and_municipality(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf", muni=None, no="1"))
    add(s, doc(BASE + "/e/b.pdf", muni=None, no="2"))
    s._db.execute("UPDATE documents SET station_key=NULL")
    s._db.commit()
    out = tmp_path / "index.html"
    assert build_page(s, out, tmp_path / "pdfs") == 2  # not merged into one row
    assert {r["muni"] for r in rows_of(out)} == {"(unknown)"}
    assert len({r["id"] for r in rows_of(out)}) == 2


def test_document_times_include_upload_seen_cache_and_current_analysis(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", up="2026-10-19T20:15")
    add(s, d)
    s._db.execute("UPDATE documents SET first_seen_at='2026-10-19T18:30:00Z'")  # UTC, CEST = +2
    cfg = Config()
    s.save_analysis(d.url, AnalysisResult(clean_report(), 0, 0, 0.0, cfg.model, "stop"),
                    PROMPT_VERSION, parent=P)
    s.mark_analysis(d.url, "done")
    s._db.execute("UPDATE analyses SET created_at='2026-10-19T18:35:00Z'")
    s._db.commit()
    out = tmp_path / "index.html"
    cache = tmp_path / "pdfs"
    cache.mkdir()
    cached = cache / (hashlib.sha256(d.url.encode()).hexdigest() + ".pdf")
    cached.write_bytes(b"%PDF")
    timestamp = 1792434600  # 2026-10-19T18:30:00Z
    os.utime(cached, (timestamp, timestamp))
    build_page(s, out, cache, cfg=cfg)
    (r,) = rows_of(out)
    assert r["up"] == "2026-10-19T20:15" and r["seen"] == "2026-10-19T20:30"
    assert r["cache"] == "2026-10-19T20:30" and r["done"] == "2026-10-19T20:35"
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '[["up", d], ["seen", r.seen], ["cache", r.cache], ["done", r.done]]' in html
    assert 'x[0] + " " + stamp(x[1])' in html


def test_retrying_is_distinct_from_failed(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf", no="1"))
    add(s, doc(BASE + "/e/b.pdf", no="2"))
    s.mark_analysis(BASE + "/e/a.pdf", "failed", "x", retry_at=time.time() + 60)
    s.mark_analysis(BASE + "/e/b.pdf", "failed", "x")
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    assert {r["no"]: r["ast"] for r in rows_of(out)} == {"1": "retrying", "2": "failed"}


def test_older_versions_keep_their_findings(tmp_path):
    s = store()
    old, new = doc(BASE + "/e/o.pdf"), doc(BASE + "/e/n.pdf", up="2026-10-19T21:00")
    add(s, old)
    add(s, new, "replaced", old.url)
    s.add_finding(P, Finding("X9", "HIGH", old, "old problem"), old.url)
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    (r,) = rows_of(out)
    assert r["f"] == [] and r["v"][0]["f"] == [["X9", "HIGH", "old problem"]]


def _report(**numbers):
    r = copy.deepcopy(clean_report())
    r["numbers"].update(numbers)
    return r


def _analysis(s, d, model, report):
    s.save_analysis(d.url, AnalysisResult(report, 0, 0, 0.0, model, "stop"), "v6", parent="adhoc")


def test_second_opinions_merged_per_model_with_disagreement(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", no="12")
    add(s, d)
    bad = _report(ballots_in_box=nf(900))  # more ballots than voters
    _analysis(s, d, "opus", bad)
    _analysis(s, d, "sonnet", clean_report())
    _analysis(s, d, "opus", bad)  # same model twice: newest only
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    (r,) = rows_of(out)
    by = {o["m"]: o for o in r["ops"]}
    assert set(by) == {"opus", "sonnet"} and by["sonnet"]["f"] == []
    assert any(f[1] == "HIGH" for f in by["opus"]["f"])
    assert r["dis"] and r["dis"] == sorted({f[0] for f in by["opus"]["f"] if f[1] == "HIGH"})


def test_targeted_arithmetic_models_are_listed_separately_and_wrap(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", no="12")
    add(s, d)
    _analysis(s, d, "claude-haiku-5-5", clean_report())
    arithmetic = {
        "voted": {"value": 800, "legible": True},
        "ballots_in_box": {"value": 800, "legible": True},
        "invalid": {"value": 20, "legible": True},
        "valid": {"value": 780, "legible": True},
        "list_votes": [{"row": 1, "value": 780, "legible": True}],
    }
    s.save_analysis(d.url, AnalysisResult(arithmetic, 0, 0, 0, "bonsai-2:27b", "stop"), "a1")

    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    (row,) = rows_of(out)
    html = STATIC.joinpath("index.html").read_text(encoding="utf-8")

    assert [x["m"] for x in row["ars"]] == ["bonsai-2:27b"]
    assert [x["m"] for x in row["ops"]] == ["claude-haiku-5-5"]
    assert 'className: "models"' in html
    assert '.models{white-space:normal;max-width:' in html


def test_no_disagreement_when_models_agree_or_single(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", no="12")
    add(s, d)
    _analysis(s, d, "opus", _report(ballots_in_box=nf(900)))
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    assert rows_of(out)[0]["dis"] == []
    _analysis(s, d, "sonnet", _report(ballots_in_box=nf(900)))
    build_page(s, out, tmp_path / "pdfs")
    assert rows_of(out)[0]["dis"] == []


def test_unevaluable_stored_report_is_skipped(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", no="12")
    add(s, d)
    _analysis(s, d, "weird", {"nonsense": 1})
    _analysis(s, d, "ok", clean_report())
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    assert [o["m"] for o in rows_of(out)[0]["ops"]] == ["ok"]


def test_status_for_browse_says_watcher_not_running_with_db_counts(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf"))
    add(s, doc(BASE + "/e/r.pdf", dt=9, no="3"))
    s.add_finding(P, Finding("X9", "HIGH", doc(BASE + "/e/a.pdf"), "boom"), BASE + "/e/a.pdf")
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["watching"] is False and st["watcher"] is None and abs(st["ts"] - time.time()) < 60
    assert st["docs"]["rg2_seen"] == 1 and st["docs"]["new10"] == 1 and st["docs"]["rare"] == {"RG-9": 1}
    assert st["analysis"]["on"] is False and st["analysis"]["queued"] == 1
    assert st["alerts"][0][1:3] == ["HIGH", "X9"]
    assert st["alerts"][0][7].startswith(BASE)  # document URL: the panel links to the form
    assert st["alerts"][0][8:] == [None, None]
    assert (tmp_path / "status.js").read_text().startswith("window.RG2_STATUS = {")


def test_page_marks_stale_status():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "STALE_S = 60" in html and "watcher not running / stalled since" in html


def test_status_refresh_preserves_unchanged_alerts_and_skips_static_repaints():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    status = html[html.index("let statusKey"):html.index("// ---- table")]
    script = r'''
const assert = require("assert");
const els = {};
function el(id) { return els[id] ||= {children: [], style: {}, textContent: "", replaceCount: 0,
  append(...x) { this.children.push(...x); },
  replaceChildren(...x) { this.children = x; this.replaceCount++; }}; }
const $ = el;
function mk(tag, text, cls) { return {tag, textContent: text == null ? "" : String(text), className: cls || "",
  children: [], append(...x) { this.children.push(...x); }}; }
function card(title) { return mk("div", title, "card"); }
function setTitle() {}
function epoch() { return "20:00"; }
function ago() { return "now"; }
function dur() { return "-"; }
function reviewCounts() { return {todo: 0, escalate: 0, unsure: 0, ok: 0}; }
function ruleEl(code) { return mk("span", code); }
function when(x) { return x; }
function alertForm() { return null; }
function localHref() { return null; }
function rikHref() { return null; }
function link() { return mk("a"); }
function showForm() {}
const STALE_S = 60, SEV = ["HIGH", "MEDIUM", "LOW"];
let heroPaints = 0, slots = [{}], rows = [], D = {db: "db", built: "one"}, S = null;
function renderHero() { heroPaints++; }
''' + status + r'''
const view = {
  ts: 1, watching: false, db: "db", watcher: null,
  docs: {rg2_seen: 1, stations: 1, rg2_rik: null, new10: 0, new60: 0, replaced: 0, removed: 0, rg7: null, rare: {}},
  analysis: {on: false, paused: null, done: 0, failed: 0, spend: 0},
  ntfy: null, alerts: []
};
S = view;
renderStatus();
const alertCard = els["status-alerts"].children[0];
assert(alertCard, "alerts render in their own stable container");
const alertReplaces = els["status-alerts"].replaceCount;
renderStatus();
assert.strictEqual(els["status-alerts"].children[0], alertCard, "unchanged alerts keep the same DOM node");
assert.equal(els["status-alerts"].replaceCount, alertReplaces, "unchanged alerts are not replaced");
assert.equal(heroPaints, 0, "status-only updates must not repaint election results");

let paints = 0;
renderStatus = () => { paints++; };
applyStatus(view);
applyStatus(JSON.parse(JSON.stringify(view)));
assert.equal(paints, 1, "an unchanged fetched view snapshot is not repainted");
tickStatus();
assert.equal(paints, 1, "the clock does not repaint a non-watching snapshot");
S = {...view, watching: true};
tickStatus();
assert.equal(paints, 2, "the clock still repaints live watcher status");
console.log(JSON.stringify({ok: true}));
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


def test_page_sets_tab_title_from_status_and_data():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "function setTitle()" in html and "document.title = " in html and " | rg2-watch" in html
    assert html.count("setTitle();") == 2  # renderStatus and applyData
    assert "shortTitle(T.title)" in html and "shortTitle(D.elections" in html


def test_status_with_live_snapshots(tmp_path):
    from rg2watch.status import build_status
    s = store()
    add(s, doc(BASE + "/e/a.pdf"))
    live = {"watcher": {"health": "ok"}, "ntfy": {"enabled": False},
            "analysis": {"model": "m", "paused": ["cap", 1.0], "avg_s": 30.0, "workers": 2, "cap": 20.0}}
    st = build_status(s, P, live)
    assert st["watching"] and st["watcher"] == {"health": "ok"}
    a = st["analysis"]
    assert a["on"] and a["model"] == "m" and a["paused"] == ["cap", 1.0] and a["cap"] == 20.0
    assert a["queued"] == 1 and a["eta_s"] == 15


def test_cli_default_path_is_next_to_db(tmp_path, capsys):
    db = tmp_path / "d" / "w.sqlite"
    db.parent.mkdir()
    Store(db).close()
    assert main(["browse", "--db", str(db)]) == 0
    page = tmp_path / "d" / "w" / "index.html"
    assert page.exists() and not (tmp_path / "d" / "index.html").exists()
    assert str(page) in capsys.readouterr().out


def _watch(tmp_path, monkeypatch, interval, ticks, extra=(), run_once=lambda self: None, build=None):
    s = Store(tmp_path / "w.sqlite")
    s.upsert_election(Election(P, "660578", "3", "Test", "http://127.0.0.1:9"))
    s.close()
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"browse_interval_s = {interval}\n")
    built, sleeps = [], []

    def fake_sleep(sec):
        real_sleep(0.05)  # lets the background threads start
        sleeps.append(sec)
        if len(sleeps) >= ticks:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "build_page", build or (lambda *a: built.append(a)))
    monkeypatch.setattr(cli, "write_status", lambda *a, **k: built.append(("status", a, k)))
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli.Watcher, "run_once", run_once)
    assert main(["watch", "--parent", P, "--db", str(tmp_path / "w.sqlite"),
                 "--config", str(cfg), "--serve-port", "0", *extra]) == 0
    real_sleep(0.2)  # the daemon threads' first runs
    return [b for b in built if not (isinstance(b, tuple) and b[0] == "status")], built


def test_watch_builds_in_background_at_start_and_status_is_written(tmp_path, monkeypatch):
    pages, everything = _watch(tmp_path, monkeypatch, 120, 3)
    status = [x for x in everything if x[0] == "status"]
    assert len(pages) == 1 and status
    assert status[0][1][4] == tmp_path / "pdfs"
    assert pages[0][3] is None and pages[0][6] is False  # all elections' rows; the status thread owns status


def test_background_update_can_be_woken_before_its_period():
    stop, wake, ran = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def update():
        calls.append(time.monotonic())
        ran.set()

    thread = cli._background("test-refresh", stop, 60, update, wake=wake)
    try:
        assert ran.wait(1)
        ran.clear()
        wake.set()
        assert ran.wait(1)
        assert len(calls) == 2
    finally:
        stop.set()
        wake.set()
        thread.join(1)


def test_watch_browse_off_with_zero(tmp_path, monkeypatch):
    pages, everything = _watch(tmp_path, monkeypatch, 0, 3)
    assert pages == [] and any(x[0] == "status" for x in everything)


def test_watch_once_builds_and_serves_nothing(tmp_path, monkeypatch):
    s = Store(tmp_path / "w.sqlite")
    s.upsert_election(Election(P, "1", "3", "T", "http://127.0.0.1:9"))
    s.close()
    calls = []
    for name in ("build_page", "write_status", "copy_static", "start_server"):
        monkeypatch.setattr(cli, name, lambda *a, n=name, **k: calls.append(n))
    monkeypatch.setattr(cli.Watcher, "run_once", lambda self: None)
    assert main(["watch", "--once", "--parent", P, "--db", str(tmp_path / "w.sqlite")]) == 0
    time.sleep(0.1)
    assert calls == []


def test_slow_build_does_not_delay_run_once(tmp_path, monkeypatch):
    release, ticks = threading.Event(), []
    t0 = time.monotonic()
    try:
        _watch(tmp_path, monkeypatch, 120, 5, run_once=lambda self: ticks.append(time.monotonic()),
               build=lambda *a: release.wait(2))
        assert len(ticks) == 5 and ticks[-1] - t0 < 1.5  # ticks ran while the build was blocked
    finally:
        release.set()


def test_watch_browse_failure_does_not_stop_loop(tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(cli, "build_page", boom)
    monkeypatch.setattr(cli, "write_status", boom)
    monkeypatch.setattr(cli.Watcher, "run_once", lambda self: None)
    s = Store(tmp_path / "w.sqlite")
    s.upsert_election(Election(P, "1", "3", "T", "http://127.0.0.1:9"))
    s.close()
    n = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: n.append(1) if len(n) < 2 else (_ for _ in ()).throw(KeyboardInterrupt))
    assert main(["watch", "--parent", P, "--db", str(tmp_path / "w.sqlite"), "--serve-port", "0"]) == 0
    assert len(n) == 2


def test_serve_port_config_and_cli(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(cli, "PORT_TRIES", 1)

    import pytest

    from rg2watch.config import Config, load_config
    assert Config().serve_port == 8770
    bad = tmp_path / "bad.toml"
    bad.write_text("serve_port = 70000\n")
    with pytest.raises(ValueError):
        load_config(bad)
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        db = tmp_path / "w.sqlite"
        Store(db).close()
        assert main(["serve", "--db", str(db), "--port", str(busy.getsockname()[1])]) == 1
        assert (tmp_path / "w" / "index.html").exists()
        # watch keeps going when its port is taken
        monkeypatch.setattr(cli.Watcher, "run_once", lambda self: None)
        s = Store(db)
        s.upsert_election(Election(P, "1", "3", "T", "http://127.0.0.1:9"))
        s.close()
        monkeypatch.setattr(cli.time, "sleep", lambda s_: (_ for _ in ()).throw(KeyboardInterrupt))
        assert main(["watch", "--parent", P, "--db", str(db),
                     "--serve-port", str(busy.getsockname()[1])]) == 0
    finally:
        busy.close()


def test_status_without_registered_election_uses_documents_parent(tmp_path):
    s = Store(":memory:")  # an evaluation-style DB: documents but no elections row
    s.apply_events("eval", s.start_sweep("eval", "t"), [Event("added", doc(BASE + "/e/a.pdf"))])
    build_page(s, tmp_path / "index.html", tmp_path / "pdfs")
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["parent"] == "eval" and st["docs"]["rg2_seen"] == 1


def test_opinions_cached_across_builds_and_logged_once(tmp_path, monkeypatch, caplog):
    import rg2watch.browse as br
    s = store()
    d = doc(BASE + "/e/a.pdf", no="12")
    add(s, d)
    _analysis(s, d, "ok", clean_report())
    _analysis(s, d, "weird", {"nonsense": 1})
    calls = []
    real = br.evaluate_report
    monkeypatch.setattr(br, "evaluate_report", lambda *a: calls.append(1) or real(*a))
    cache = {}
    out = tmp_path / "index.html"
    for _ in range(3):
        build_page(s, out, tmp_path / "pdfs", cache=cache)
    assert len(calls) == 2  # one evaluation per analysis, not per build
    assert caplog.text.count("could not be re-evaluated") == 1
    assert [o["m"] for o in rows_of(out)[0]["ops"]] == ["ok"]


def test_current_prompt_version_preferred_per_model(tmp_path):
    s = store()
    d = doc(BASE + "/e/a.pdf", no="12")
    add(s, d)
    s.save_analysis(d.url, AnalysisResult(_report(ballots_in_box=nf(900)), 0, 0, 0.0, "m", "stop"), PROMPT_VERSION)
    s.save_analysis(d.url, AnalysisResult(clean_report(), 0, 0, 0.0, "m", "stop"), "v5")  # newer, old prompt
    out = tmp_path / "index.html"
    build_page(s, out, tmp_path / "pdfs")
    (o,) = rows_of(out)[0]["ops"]
    assert any(f[1] == "HIGH" for f in o["f"])
    assert len(s.analysis_reports([1, 2])) == 2


def test_browse_rows_are_chunked(monkeypatch):
    import rg2watch.store as st
    monkeypatch.setattr(st, "BROWSE_CHUNK", 2)
    s = store()
    for i in range(5):
        d = doc(BASE + f"/e/{i}.pdf", no=str(i))
        add(s, d)
        s.add_finding(P, Finding("X1", "LOW", d, "s"), d.url)
    docs, found = s.browse_rows(P)
    assert [d["station_no"] for d in docs] == ["0", "1", "2", "3", "4"] and len(found) == 5


def test_watch_style_build_writes_no_status(tmp_path):
    s = store()
    add(s, doc(BASE + "/e/a.pdf"))
    build_page(s, tmp_path / "index.html", tmp_path / "pdfs", with_status=False)
    assert (tmp_path / "data.json").exists() and not (tmp_path / "status.json").exists()


def test_ports_validated_and_serve_needs_one(tmp_path, capsys):
    import pytest
    for argv in (["serve", "--port", "70000"], ["watch", "--serve-port", "-1"], ["serve", "--port", "x"]):
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 2
    assert main(["serve", "--db", str(tmp_path / "w.sqlite"), "--port", "0"]) == 2


def test_serve_always_copies_static(tmp_path, monkeypatch):
    db = tmp_path / "w.sqlite"
    s = Store(db)
    add_el = s.upsert_election(Election(P, "1", "t", "T", BASE))
    s.close()
    page = tmp_path / "w"
    page.mkdir()
    (page / "index.html").write_text("old")
    (page / "data.json").write_text("{}")
    monkeypatch.setattr(cli, "start_server", lambda *a: None)
    assert main(["serve", "--db", str(db), "--port", "8123"]) == 1
    assert (page / "index.html").read_bytes() == (STATIC / "index.html").read_bytes()


def test_page_filters_include_versions_and_models():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "only in" in html and 'r.sev = r._sev' in html and "hashchange" in html


def _el_db(tmp_path, stem, parent):
    s = Store(tmp_path / f"{stem}.sqlite")
    s.upsert_election(Election(parent, "1", "t", "T" + parent, BASE))
    return s


def test_two_dbs_in_one_dir_build_separate_page_dirs(tmp_path):
    for stem, parent in (("election-1", "1"), ("election-2", "2")):
        s = _el_db(tmp_path, stem, parent)
        assert main(["browse", "--db", str(tmp_path / f"{stem}.sqlite")]) == 0
        s.close()
    for stem in ("election-1", "election-2"):
        d = tmp_path / stem
        assert (d / "index.html").is_file() and (d / "jquery.min.js").is_file()
        assert json.loads((d / "data.json").read_text())["db"] == stem
        assert json.loads((d / "status.json").read_text())["db"] == stem
    assert not (tmp_path / "index.html").exists() and not (tmp_path / "data.json").exists()


def test_page_dir_default_and_memory(tmp_path):
    from rg2watch.config import Config
    assert cli._browse_path(Config(db_path="data/rg2watch.sqlite")) == cli.Path("data/rg2watch/index.html")
    assert cli._browse_path(Config(db_path=":memory:")) == cli.Path("data/memory/index.html")


def test_start_web_falls_back_to_next_free_port(tmp_path):
    import socket
    s = Store(":memory:")
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    server = cli._start_web(tmp_path, tmp_path, port, s)
    try:
        assert server is not None and port < server.server_port <= port + 10
    finally:
        server.shutdown()
        server.server_close()
        busy.close()


def test_start_web_none_when_all_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "start_server", lambda *a: None)
    assert cli._start_web(tmp_path, tmp_path, 9000, Store(":memory:")) is None


def test_status_prints_page_url_only_while_running(tmp_path):
    from rg2watch.status import format_status
    s = store()
    f = tmp_path / "status.json"
    f.write_text(json.dumps({"watching": True, "parent": P, "ts": 1000.0, "url": "http://127.0.0.1:8771"}))
    assert "page http://127.0.0.1:8771" in format_status(s, P, f, 20.0, now=1010.0)
    assert "page http" not in format_status(s, P, f, 20.0, now=2000.0)
    assert "page http" not in format_status(s, P, tmp_path / "none.json", 20.0)


def test_page_banner_on_status_data_db_mismatch():
    html = (STATIC / "index.html").read_text()
    assert "status and data are from different databases" in html and "D.db !== S.db" in html


def test_alert_links_are_built_from_url_in_status_and_data():
    html = (STATIC / "index.html").read_text()
    assert "alertForm(x[7])" in html and 'link("PDF", loc)' in html and "showForm(r.id" in html
    assert "v ? v.pdf : x[8]" in html and "v ? v.rik : x[9]" in html
    assert 'showForm(r.id, r.muni + " BM " + r.no)' in html


def test_browse_leaves_a_live_watchers_status_alone(tmp_path):
    db = tmp_path / "w.sqlite"
    Store(db).close()
    page_dir = tmp_path / "w"
    page_dir.mkdir()
    status = page_dir / "status.json"
    live = json.dumps({"watching": True, "parent": P, "ts": time.time(), "marker": "live"})
    status.write_text(live)
    assert main(["browse", "--db", str(db)]) == 0
    assert (page_dir / "data.json").exists() and status.read_text() == live  # data only
    status.write_text(json.dumps({"watching": True, "parent": P, "ts": time.time() - 120}))
    assert main(["browse", "--db", str(db)]) == 0  # stale: rewritten as "not watching"
    assert json.loads(status.read_text())["watching"] is False
