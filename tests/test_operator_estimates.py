import json
import subprocess

import pytest

from rg2watch.browse import STATIC, build_data, build_page
from rg2watch.config import Config
from rg2watch.estimates import make_sample, sample_path, store_frame
from rg2watch.public_results import build_public_results, render_public_results
from rg2watch.store import Store
from test_browse import BASE, P, add, doc
from test_estimate_replay import make_db
from test_rules import clean_report, nf
from test_tally import form, M, report


def populate(db):
    make_db(db, False)
    s = Store(db)
    for m, count in (("A", 6), ("B", 4)):
        for no in range(1, count + 1):
            form(s, no, report(list_votes=[{"row": 1, "field": nf(300 + no * 10)},
                                           {"row": 2, "field": nf(480 - no * 10)}]), muni=m)
    return s


def test_private_snapshot_shares_counted_readings_and_public_path_never_estimates(tmp_path, monkeypatch):
    s = populate(tmp_path / "election.sqlite")
    cfg = Config(model=M)
    sample = make_sample(store_frame(s, P), P, 6, "s", retrospective=True)
    sample_path(s).write_text(json.dumps(sample))
    data = build_data(s, tmp_path, P, cfg)
    estimates = data["tally"]["estimates"]
    assert estimates["built"] == data["built"]
    assert estimates["arrivals"]["status"] == "complete"
    assert estimates["sample"]["status"] == "experimental"
    assert estimates["sample"]["retrospective"] is True
    assert estimates["arrivals"]["results"][0]["share"] == pytest.approx(
        data["tally"]["overall"]["all"]["lists"][0] / data["tally"]["overall"]["all"]["lists_ballots_in_box"])
    monkeypatch.setattr("rg2watch.estimates.operator_estimates", lambda *a: pytest.fail("public estimates"))
    public = build_public_results(s, cfg, P)
    assert "estimates" not in json.dumps(public)
    html = render_public_results(public)
    assert "tabsample" not in html and "All-arrivals projection" not in html
    s.close()


def test_held_and_annulled_forms_remain_missing_in_both_methods(tmp_path):
    s = populate(tmp_path / "election.sqlite")
    sample = make_sample(store_frame(s, P), P, 10, "s", retrospective=True)
    sample_path(s).write_text(json.dumps(sample))
    d = next(d for d in s.browse_rows(P)[0] if d["municipality"] == "A")
    s.set_review(d["url"], "escalate")
    add(s, doc(BASE + "/rg9.pdf", muni="B", no="1", dt=9))
    estimates = build_data(s, tmp_path, P, Config(model=M))["tally"]["estimates"]
    assert estimates["sample"]["reporting"] == estimates["arrivals"]["reporting"] == 8
    assert len(estimates["sample"]["sample_stations"]) == 10
    s.close()


def test_absent_malformed_or_stale_sample_does_not_hide_arrivals_or_tally(tmp_path):
    s = populate(tmp_path / "election.sqlite")
    cfg = Config(model=M)
    first = build_data(s, tmp_path, P, cfg)
    assert first["tally"]["estimates"]["sample"]["status"] == "not_configured"
    sample_path(s).write_text('{"broken": true}')
    out = build_data(s, tmp_path, P, cfg)["tally"]
    assert out["estimates"]["sample"]["status"] == "invalid_sample"
    assert out["estimates"]["arrivals"]["status"] == "complete"
    assert out["overall"] == first["tally"]["overall"]
    s.close()


def test_nonnational_and_incomplete_or_outdated_rosters_fail_closed(tmp_path):
    s = populate(tmp_path / "election.sqlite")
    cfg = Config(model=M, tally_scope={P: "municipal"})
    out = build_data(s, tmp_path, P, cfg)["tally"]["estimates"]
    assert out["arrivals"]["status"] == "unsupported"
    form(s, 99, clean_report(), muni="A")
    out = build_data(s, tmp_path, P, Config(model=M))["tally"]["estimates"]
    assert out["arrivals"]["status"] == out["sample"]["status"] == "invalid_frame"
    s.close()


def test_new_frozen_sample_refreshes_operator_snapshot_without_new_votes_or_feed_reset(tmp_path):
    from rg2watch.cli import _snapshot_stale
    from rg2watch.operator_feed import generation_token
    s = populate(tmp_path / "election.sqlite")
    cfg = Config(model=M)
    out = tmp_path / "page" / "index.html"
    build_page(s, out, tmp_path, P, cfg)
    assert _snapshot_stale(s, out, cfg) is False
    generation = generation_token(s, cfg, P)
    revision = s.operator_feed_state(P)[1]
    sample_path(s).write_text(json.dumps(make_sample(store_frame(s, P), P, 6, "s", retrospective=True)))
    assert s.operator_feed_state(P)[1] == revision
    assert generation_token(s, cfg, P) == generation  # no needless reset of the live form feed
    assert _snapshot_stale(s, out, cfg) is True
    build_page(s, out, tmp_path, P, cfg)
    assert _snapshot_stale(s, out, cfg) is False
    # Roster metadata changes must also invalidate sample/projection inputs before new forms arrive.
    s.save_stations(P, "a", {"99": "new-station"})
    assert _snapshot_stale(s, out, cfg) is True
    s.close()


def test_estimate_ui_text_rendering_coverage_gate_csv_and_tabs():
    html = (STATIC / "index.html").read_text()
    assert 'id="tabsample"' in html and 'id="tabprojection"' in html
    assert 'renderEstimates();' in html
    code = html[html.index("// ---- operator estimates"):html.index("// ---- results (unofficial tally)")]
    tabs = html[html.index("function activeTab()"):html.index('$("tmode").addEventListener')]
    csv = html[html.index("function csvCell("):html.index("function tallyCsv(")]
    invalidation = html[html.index("function staleTallyRow("):html.index("function beginBootstrap(")]
    script = r'''
const assert=require("assert");
const els={};
function mk(tag,text){return {tag,textContent:text||"",children:[],hidden:false,
  dataset:{},querySelectorAll(){return this.children.flatMap(n => [
    ...(n.tag==="details"&&n.dataset.estimateSection ? [n] : []),...n.querySelectorAll()])},
  append(...nodes){this.children.push(...nodes)},replaceChildren(...nodes){this.children=nodes},
  classList:{toggle(){}}};}
function $(id){return els[id]||(els[id]=mk("div"));}
function epoch(at){return String(at)}
function writeHash(){};function loadOfficialResults(){};
let tmTable=null,teTable=null,feedRevision=2,tallyRevision=1;
let rows=[],D={},table=null,acceptedSnapshot={},feedGeneration=null,feedOrderSignature=null;
let T={scope:"national",parent:"P",estimates:{built:"2026-11-07T10:00:00Z",sample:{
  status:"insufficient_coverage",blockers:["Missing municipality"],warnings:["Experimental"],
  reporting:1,population:10,selected:6,retrospective:true,strata:[],results:[],
  sample_stations:[{m:"=EVIL",no:"1",population:10,selected:6,design_weight:10/6,
    response_weight:10,reporting:false}]
},arrivals:{status:"experimental",blockers:[],warnings:[],reporting:4,population:10,selected:10,
strata:[{m:"<b>unsafe</b>",population:10,reporting:4,weight:2.5}],
results:[{row:1,name:"<img onerror=bad()>",share:.6,low:.5,high:.7}],sample_stations:[]}}};
''' + csv + code + tabs + invalidation + r'''
renderEstimates();
function texts(el){return [el.textContent,...el.children.flatMap(texts)].join(" ")}
assert(texts($("sampleview")).includes("Missing municipality"));
assert(!texts($("sampleview")).includes("60.00%"));
assert(texts($("sampleview")).toLowerCase().includes("retrospective"));
assert(texts($("projectionview")).includes("<img onerror=bad()>"));
assert(texts($("projectionview")).includes("60.00%"));
assert(texts($("projectionview")).includes("50.00% – 70.00%"));
assert(texts($("projectionview")).includes("Updating"));
assert(sampleCsv(T.estimates.sample).includes("'=EVIL"));
setTab("sample",false);assert.equal(activeTab(),"sample");
assert($("formsview").hidden&&$("resview").hidden&&$("officialview").hidden);
setTab("projection",false);assert.equal(activeTab(),"projection");
invalidateTally();
assert(!texts($("projectionview")).includes("60.00%"));
assert(texts($("projectionview")).toLowerCase().includes("snapshot"));
T={scope:"national",parent:"NEXT",estimates:{built:"2026-11-07T10:00:00Z",sample:{
  status:"experimental",blockers:[],warnings:[],reporting:8,population:20,selected:8,
  source_parent:"PRIOR",design_kind:"prior_election_early",early_strength:2,
  interval_kind:"historical_design_unvalidated",sample_stations:[],strata:[],
  results:[{row:1,name:"One",share:.6,low:null,high:null,se:null}]
}}};
renderEstimates();
assert(texts($("sampleview")).includes("PRIOR"));
assert(texts($("sampleview")).includes("historical"));
assert(!texts($("sampleview")).includes("no observed variance"));
assert(!texts($("sampleview")).includes("Geography-only"));
assert(texts($("sampleview")).includes("not validated"));
T.estimates.sample.strata=[{m:"geo: north / west",population:20,selected:8,reporting:8,weight:2.5}];
renderEstimates();
assert(texts($("sampleview")).includes("Coverage by sampling stratum"));
assert(!texts($("sampleview")).includes("Coverage by municipality"));
assert(sampleCsv({...T.estimates.sample,sample_stations:[{m:"A",no:"1",stratum:"geo: north / west",
  population:20,selected:8,design_weight:2.5,response_weight:2.5,reporting:true}]}).includes("geo: north / west"));
T.estimates.sample.prior_electorate_response=.85;
T.estimates.sample.historical_balance={registered_relative_error:.01,geography_error_pp:.5,geography:[{
  geography:"geo: north / west",population:20,selected:8,prior_electorate_share:.5,sample_prior_electorate_share:.505
}]};
renderEstimates();
assert(texts($("sampleview")).includes("85.00%"));
assert(texts($("sampleview")).includes("Historical geographic balance"));
assert(texts($("sampleview")).includes("50.50%"));
T.estimates.sample.interval_kind="conditional_95_pivotal_hh";
T.estimates.sample.response_rate=1;
T.estimates.sample.results=[{row:1,name:"One",share:.6,se:.02,low:.5608,high:.6392,margin_pp:3.92}];
T.estimates.sample.historical_benchmark={source_parent:"<b>PRIOR</b>",sample_max_error_pp:.3301,
  design_p95_max_error_pp:.8461,design_trials:30,reference_usable:8385,reference_population:8392};
renderEstimates();
const explanatory=texts($("sampleview"));
assert(explanatory.includes("Approx. 95% sampling interval"));
assert(explanatory.includes("56.08% – 63.92%"));
assert(explanatory.includes("±3.92 pp"));
assert(explanatory.includes("0.33 pp"));
assert(explanatory.includes("0.85 pp"));
assert(explanatory.includes("30 trial"));
assert(explanatory.includes("not a forecast margin"));
assert(explanatory.includes("not independent"));
assert(explanatory.includes("8,385 / 8,392"));
assert(explanatory.includes("Partial"));
assert(explanatory.includes("Completeness, not accuracy"));
assert(explanatory.includes("<b>PRIOR</b>"));
T.estimates.sample.historical_benchmark.design_p95_max_error_pp=null;
T.estimates.sample.historical_benchmark.design_trials=null;
T.estimates.sample.results[0]={row:1,name:"One",share:.6,se:0,low:null,high:null,margin_pp:null};
renderEstimates();
assert(texts($("sampleview")).includes("No observed variation"));
assert(!texts($("sampleview")).includes("±0.00"));
assert(texts($("sampleview")).includes("Trial benchmark unavailable"));
T.estimates.sample.sample_stations=[{m:"A",no:"1",design_weight:1,reporting:true,
  prior_first_upload:"2026-10-25T19:56:00Z"}];
T.estimates.sample.selected=1;
renderEstimates();
assert(texts($("sampleview")).includes("Historical reporting times"));
assert(texts($("sampleview")).includes("Europe/Belgrade"));
assert(texts($("sampleview")).includes("20:56"));
assert(texts($("sampleview")).includes("25 Oct 2026"));
assert(texts($("sampleview")).includes("not a prediction"));
assert(texts($("sampleview")).includes("80%"));
const quick={...T.estimates.sample,design_kind:"speed_balanced_nonprobability",sample_label:"Quick",sample_role:"quick",
  interval_kind:"not_probability_sample",completion_target:"2026-10-25T23:00:00+01:00",sample_complete:true,
  speed_fit:{eligible_count:2132,eligible_prior_electorate_fraction:.144,pool_missing_domains:["geo: foreign"],
    registered_relative_error:.001},sample_stations:[{m:"A",no:"1",prior_registered:1000,
    prior_first_upload:"2026-10-25T19:56:00Z",reporting:true}],
  results:[{row:1,name:"One",share:.6,se:null,low:null,high:null,margin_pp:null}]};
const morning={...quick,sample_label:"Morning",sample_role:"morning",completion_target:"2026-10-26T08:00:00+01:00"};
T.estimates.sample=quick;T.estimates.samples={quick,morning};
renderEstimates();
assert(texts($("sampleview")).includes("Quick sample estimate"));
assert(texts($("morningview")).includes("Morning sample estimate"));
assert(texts($("sampleview")).includes("23:00"));
assert(texts($("morningview")).includes("08:00"));
assert(texts($("sampleview")).includes("all selected stations"));
assert(texts($("sampleview")).includes("No validated error margin"));
assert(!texts($("sampleview")).includes("Hansen"));
assert(!texts($("sampleview")).includes("Approx. sampling margin"));
assert(texts($("sampleview")).includes("14.40%"));
assert(texts($("sampleview")).includes("geo: foreign"));
assert(!sampleCsv(quick).includes("Design weight 1/pi"));
assert(sampleCsv(quick).includes("nonprobability"));
assert.equal($("tabmorning").hidden,false);
setTab("morning",false);assert.equal(activeTab(),"morning");
assert($("sampleview").hidden&&!$("morningview").hidden);
T.estimates.samples=null;renderEstimates();assert($("tabmorning").hidden);
'''
    result = subprocess.run(["node"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_speed_panel_puts_results_before_collapsed_method_details():
    html = (STATIC / "index.html").read_text()
    code = html[html.index("// ---- operator estimates"):html.index("// ---- results (unofficial tally)")]
    script = r'''
const assert=require("assert");
function mk(tag,text){return {tag,textContent:text||"",children:[],dataset:{},open:false,
  append(...nodes){this.children.push(...nodes)},replaceChildren(...nodes){this.children=nodes},
  querySelectorAll(){return this.children.flatMap(n => [
    ...(n.tag==="details" ? [n] : []),...n.querySelectorAll()])}};}
const elements={};const $=id=>elements[id]||(elements[id]=mk("section"));
function epoch(v){return "10:06";}
let feedRevision=1,tallyRevision=1;
const quick={design_kind:"speed_balanced_nonprobability",sample_label:"Quick",sample_role:"quick",
  status:"experimental",reporting:2,selected:2,sample_size:2,population:8392,sample_complete:true,
  response_rate:1,prior_electorate_response:1,retrospective:true,seed:"secret-debug-seed",frame_hash:"framehash",
  source_parent:"779676",completion_target:"2026-10-25T23:00:00+01:00",warnings:["Technical limitations"],
  historical_benchmark:{source_parent:"779676",sample_max_error_pp:.06,reference_usable:8385,reference_population:8392},
  blockers:[],strata:[],results:[{row:1,name:"One",share:.6,low:null,high:null,se:null,margin_pp:null}],
  sample_stations:[{m:"A",no:"1",prior_registered:1000,prior_first_upload:"2026-10-25T19:46:00Z",reporting:true},
    {m:"A",no:"2",prior_registered:1000,prior_first_upload:"2026-10-25T22:00:00Z",reporting:true}]};
let T={parent:"779676",estimates:{built:"2026-11-08T09:06:00Z",sample:quick,
  samples:{quick,morning:{...quick,sample_label:"Morning",sample_role:"morning",
    completion_target:"2026-10-26T08:00:00+01:00"}}}};
''' + code + r'''
function visible(el){return [el.textContent,...(el.tag==="details"&&!el.open ?
  el.children.filter(c=>c.tag==="summary") : el.children).flatMap(visible)].join(" ");}
renderEstimates();
for(const id of ["sampleview","morningview"]){
  const panel=$(id), table=panel.children.find(c=>c.tag==="table");
  assert(table,"results must be visible without opening anything");
  const prefix=panel.children.slice(0,panel.children.indexOf(table));
  assert(prefix.filter(c=>c.tag==="p").length<=3,"at most three short status/caution lines before results");
  assert(prefix.map(visible).join(" ").split(/\s+/).length<80,"intro must be brief");
  const method=panel.querySelectorAll().find(d=>d.dataset.estimateSection==="method");
  assert(method&&!method.open,"technical detail must start collapsed");
  assert(panel.children.indexOf(method)>panel.children.indexOf(table),"method belongs after results");
  assert.equal(table.children[0].children[0].children.length,2,"no repeated empty interval column");
  const text=visible(panel);
  assert(text.includes("2 / 2 stations"));assert(text.includes("No validated error margin"));
  assert(text.includes("Retrospective test"));assert(text.includes("10:06"));
  assert(!text.includes("secret-debug-seed"));assert(!text.includes("0.06 pp"));
  assert(!text.includes("Technical limitations"));
}
assert(visible($("sampleview")).includes("23:00"));
assert(visible($("morningview")).includes("08:00 next morning"));
const method=$("sampleview").querySelectorAll().find(d=>d.dataset.estimateSection==="method");
method.open=true;
assert(visible($("sampleview")).includes("secret-debug-seed"));
assert(visible($("sampleview")).includes("0.06 pp"));
T.estimates.built="2026-11-08T09:07:00Z";renderEstimates();
assert($("sampleview").querySelectorAll().find(d=>d.dataset.estimateSection==="method").open);
$("sampleview").querySelectorAll().find(d=>d.dataset.estimateSection==="method").open=false;
feedRevision=2;renderEstimates();assert(visible($("sampleview")).includes("Updating"));
quick.blockers=["Waiting for selected stations"];quick.results=[];renderEstimates();
assert(visible($("sampleview")).includes("Waiting for selected stations"));
assert(!visible($("sampleview")).includes("60.00%"));
'''
    result = subprocess.run(["node"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_estimate_refresh_preserves_expanded_sections_and_skips_unchanged_dom():
    html = (STATIC / "index.html").read_text()
    code = html[html.index("// ---- operator estimates"):html.index("// ---- results (unofficial tally)")]
    script = r'''
const assert=require("assert");
function mk(tag,text){return {tag,textContent:text||"",children:[],dataset:{},open:false,
  append(...nodes){this.children.push(...nodes)},replaceChildren(...nodes){this.children=nodes},
  querySelectorAll(){return this.children.flatMap(n => [
    ...(n.tag==="details" ? [n] : []),...n.querySelectorAll()])}};}
const elements={};
function $(id){return elements[id]||(elements[id]=mk("section"));}
function epoch(v){return String(v);}
let feedRevision=1,tallyRevision=1;
const sample={status:"experimental",design_kind:"prior_election_early",source_parent:"PRIOR",
  reporting:2,selected:2,population:4,response_rate:1,prior_electorate_response:1,
  strata:[{m:"A",population:4,selected:2,reporting:2,weight:2}],results:[],
  historical_balance:{geography:[{geography:"geo",population:4,selected:2,
    prior_electorate_share:1,sample_prior_electorate_share:1}]},
  sample_stations:[{m:"A",no:"1",design_weight:2,reporting:true,prior_first_upload:"2026-10-25T19:56:00Z"}]};
let T={parent:"ONE",estimates:{built:"2026-11-08T00:00:00Z",sample,
  arrivals:{...sample,design_kind:null}}};
''' + code + r'''
const sections=id => Object.fromEntries($(id).querySelectorAll().map(d => [d.dataset.estimateSection ||
  (d.children[0].textContent.startsWith("Coverage") ? "coverage" :
    d.children[0].textContent.startsWith("Historical") ? "geography" : "stations"),d]));
renderEstimates();
assert.equal(Object.keys(sections("sampleview")).length,4);
sections("sampleview").coverage.open=true;
sections("sampleview").geography.open=true;
sections("sampleview").timing.open=true;
sections("projectionview").coverage.open=true;
const oldFirst=$("sampleview").children[0], oldCoverage=sections("sampleview").coverage;
renderEstimates();
assert.strictEqual($("sampleview").children[0],oldFirst,"an unchanged feed poll must not replace text/selection");
assert.strictEqual(sections("sampleview").coverage,oldCoverage);
// A new but equivalent snapshot object must not force a redraw either.
T=JSON.parse(JSON.stringify(T));renderEstimates();
assert.strictEqual(sections("sampleview").coverage,oldCoverage);
// A real snapshot update must display new contents without collapsing open or opening closed sections.
T.estimates.sample.reporting=1;T.estimates.built="2026-11-08T00:01:00Z";
renderEstimates();
assert.notStrictEqual($("sampleview").children[0],oldFirst);
assert(sections("sampleview").coverage.open);
assert(sections("sampleview").geography.open);
assert(sections("sampleview").timing.open);
assert(!sections("sampleview").stations.open);
assert(sections("projectionview").coverage.open);
// Closing a previously expanded note must be remembered, not just the set of ever-opened sections.
sections("sampleview").coverage.open=false;
feedRevision=2;renderEstimates();
assert(!sections("sampleview").coverage.open);
assert(sections("sampleview").geography.open);
// Temporary snapshot invalidation must hide stale numbers, then restore the same-election reading state.
const saved=T;T=null;renderEstimates();
assert.equal($("sampleview").querySelectorAll().length,0);
T=saved;renderEstimates();
assert(sections("sampleview").geography.open);
assert(!sections("sampleview").coverage.open);
assert(sections("projectionview").coverage.open);
// A different election must not inherit the prior election's expanded notes.
T.parent="TWO";renderEstimates();
assert(Object.values(sections("sampleview")).every(d => !d.open));
assert(Object.values(sections("projectionview")).every(d => !d.open));
'''
    result = subprocess.run(["node"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
