import json
import os
import re
import subprocess
from pathlib import Path

from rg2watch.config import Config
from rg2watch.cli import main
from rg2watch.models import Election
from rg2watch.public_results import build_public_results, render_public_results, write_public_results
from rg2watch.store import Store
from test_browse import BASE, P, add, doc, store
from test_rules import clean_report, nf
from test_tally import M, form, put, report


ROOT = Path(__file__).parents[1]


def config(**kw):
    return Config(model=M, tally_scope={P: "national"}, list_names={P: ["Листа А", "Листа Б"]}, **kw)


def test_public_payload_has_only_sanitized_corrected_results_and_review_counts():
    s = store()
    form(s, 1, clean_report())
    flagged = form(s, 2, report(ballots_in_box=nf(900)))
    s.set_review(flagged.url, "ok", "private operator note")

    data = build_public_results(s, config(), P)

    assert data["version"] == 2 and data["title"] == "Title" and data["scope"] == "national"
    assert data["names"] == ["Листа А", "Листа Б"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", data["built"])
    row = data["overall"]
    assert row["counted"] == 2 and row["clean"] == 1 and row["flagged"] == 1
    assert row["review"] == {"ok": 1, "escalate": 0, "unsure": 0, "undecided": 0}
    assert row["totals"]["voted"] == 1600 and row["totals"]["valid"] == 1560
    assert row["totals"]["lists"] == [800, 760]
    assert data["municipalities"][0]["m"] == "Alpha"
    assert len(data["stations"]) == 2
    assert set(data["stations"][0]) == {
        "m", "no", "name", "included", "registered", "voted", "valid", "lists",
    }
    assert data["stations"][0] == {
        "m": "Alpha", "no": "1", "name": "ОШ", "included": True,
        "registered": 1000, "voted": 800, "valid": 780, "lists": [400, 380],
    }

    blob = json.dumps(data, ensure_ascii=False)
    for private in (BASE, flagged.url, "private operator note", "claude", "A1", "arithmetic",
                    '"id"', '"reason"', '"filed"', '"rv"',
                    '"db"', '"model"', '"operator_corrected"'):
        assert private not in blob


def test_public_payload_sorts_municipalities_by_serbian_cyrillic_alphabet():
    s = store()
    for no, municipality in enumerate(
            ["Љиг", "Ћуприја", "Ада", "Шид", "Лапово", "Тител", "Чока", "Џеп"], 1):
        form(s, no, clean_report(), muni=municipality)

    data = build_public_results(s, config(), P)

    assert [row["m"] for row in data["municipalities"]] == [
        "Ада", "Лапово", "Љиг", "Тител", "Ћуприја", "Чока", "Џеп", "Шид",
    ]


def test_public_payload_removes_records_prefix_from_election_title():
    s = store()
    s.upsert_election(Election(P, "1", "t", "Записници Парламентарни избори 2026", BASE))
    form(s, 1, clean_report())

    data = build_public_results(s, config(), P)

    assert data["title"] == "Парламентарни избори 2026"


def test_public_review_breakdown_uses_current_version_and_marks_missing_verdict_undecided():
    s = store()
    old = form(s, 1, report(ballots_in_box=nf(900)))
    s.set_review(old.url, "ok")
    new = doc(BASE + "/new.pdf", muni="Alpha", no="1", up="2026-10-20T22:00")
    add(s, new, "replaced", old.url)
    put(s, new, report(ballots_in_box=nf(900)))

    data = build_public_results(s, config(), P)

    assert data["overall"]["flagged"] == 1
    assert data["overall"]["review"] == {"ok": 0, "escalate": 0, "unsure": 0, "undecided": 1}


def test_public_results_hold_escalated_form_out_of_totals():
    s = store()
    form(s, 1, clean_report())
    held = form(s, 2, report(registered=nf(500), voted=nf(400), ballots_in_box=nf(400),
                              invalid=nf(10), valid=nf(390),
                              list_votes=[{"row": 1, "field": nf(300)},
                                          {"row": 2, "field": nf(90)}]))
    s.set_review(held.url, "escalate")

    data = build_public_results(s, config(), P)

    assert data["overall"]["counted"] == 2
    assert data["overall"]["held"] == 1
    assert data["overall"]["totals"]["registered"] == 1000
    assert data["overall"]["totals"]["valid"] == 780
    assert [row["included"] for row in data["stations"]] == [True, False]
    assert "Изузето из резултата" in render_public_results(data)


def test_rendered_export_is_one_self_contained_script_safe_page(tmp_path):
    s = store()
    s.upsert_election(Election(P, "1", "t", "</script><b>Избори", BASE))
    form(s, 1, clean_report())
    data = build_public_results(s, config(), P)

    html = render_public_results(data)
    assert "</script><b>" not in html
    assert "\\u003c/script>" in html
    assert "fetch(" not in html and "XMLHttpRequest" not in html
    assert "<script src=" not in html and "<link rel=\"stylesheet\"" not in html
    assert "default-src 'none'" in html and "connect-src 'none'" in html
    assert "Општина" in html and "НЕЗВАНИЧНО" in html and "Стање у" in html
    assert "corrected" not in html.lower() and "review note" not in html.lower()

    out = write_public_results(s, config(), P, tmp_path / "public-results")
    assert out == tmp_path / "public-results" / "index.html"
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert out.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in out.parent.iterdir()] == ["index.html"]
    script = out.read_text(encoding="utf-8").rsplit("<script>", 1)[1].split("</script>", 1)[0]
    checked = subprocess.run(["node", "--check"], input=script, text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr


def test_public_page_refreshes_after_three_minutes_of_inactivity():
    s = store()
    form(s, 1, clean_report())

    html = render_public_results(build_public_results(s, config(), P))

    assert '<meta http-equiv="refresh"' not in html
    assert "const REFRESH_IDLE_MS=180000,REFRESH_RESUME_MS=60000" in html


def test_public_page_renders_controls_summary_and_every_list():
    s = store()
    form(s, 1, clean_report())
    data = build_public_results(s, config(), P)
    data["names"] = [f"Листа {i}" for i in range(1, 11)]
    data["fallback_name_indices"] = []
    data["overall"]["totals"]["lists"] = list(range(1, 11))
    data["overall"]["totals"]["lists_valid"] = 55
    html = render_public_results(data)
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    harness = r'''
const assert=require("assert"),els={};let refresh,refreshDelay,reloadedHash;
class El{constructor(){this.textContent="";this.value="";this.children=[];this.style={};this.className="";this.hidden=false;this.listeners={};this.attrs={}}append(...x){this.children.push(...x)}replaceChildren(...x){this.children=x}addEventListener(type,fn){this.listeners[type]=fn}setAttribute(k,v){this.attrs[k]=String(v)}}
global.document={hidden:false,listeners:{},getElementById:id=>els[id]??=(new El()),createElement:()=>new El(),addEventListener(type,fn){this.listeners[type]=fn}};
global.Option=function(text,value){const x=new El();x.textContent=text;x.value=value;return x};
global.location={hash:"#municipality=Alpha",pathname:"/",search:"",reload(){reloadedHash=this.hash}};
global.history={replaceState(_state,_title,url){location.hash=url.includes("#")?url.slice(url.indexOf("#")):""}};
global.setTimeout=(fn,delay)=>{refresh=fn;refreshDelay=delay};
global.clearTimeout=()=>{};
''' + script + r'''
assert.equal(els.title.textContent,"Title");
assert.equal(els.municipality.children.length,2);
assert.equal(els.municipality.value,"Alpha");
els.municipality.value="";els.municipality.listeners.change();
assert.equal(location.hash,"");
els.municipality.value="Alpha";els.municipality.listeners.change();
assert.equal(location.hash,"#municipality=Alpha");
assert.equal(refreshDelay,180000);refresh();
assert.equal(reloadedHash,"#municipality=Alpha");
els.municipality.value="";els.municipality.listeners.change();
assert.equal(els.chart.children.length,10);
assert.equal(els.chart.children[0].children[0].textContent,"Листа 10");
assert.equal(els.chart.children[9].children[0].textContent,"Листа 1");
assert(!els.chart.children.some(x=>x.children[0].textContent.startsWith("Остале")));
assert.equal(els.footer.textContent,"НЕЗВАНИЧНО — Скенирани записници обрађени помоћу вештачке интелигенције; ово нису резултати РИК-а.");
const original={counted:DATA.overall.counted,clean:DATA.overall.clean,held:DATA.overall.held,
  flagged:DATA.overall.flagged,review:DATA.overall.review,vote_forms:DATA.overall.totals.vote_forms};
Object.assign(DATA.overall,{counted:996,clean:843,held:13,flagged:151,
  review:{ok:26,escalate:11,unsure:0,undecided:114}});
DATA.overall.totals.vote_forms=983;render();
assert.deepEqual(els["forms-sub"].textContent.split("\n"),[
  "843 без уочених проблема",
  "За проверу: 151 записник — 26 прихваћено · 11 ескалирано · 114 без одлуке",
  "Изузето из резултата: 13 ескалираних записника — 11 из скупа за проверу · 2 додатно",
]);
Object.assign(DATA.overall,{counted:original.counted,clean:original.clean,held:original.held,
  flagged:original.flagged,review:original.review});
DATA.overall.totals.vote_forms=original.vote_forms;render();
DATA.scope_note="unknown election type - set tally_scope";render();
assert(els.warning.textContent.includes("Непозната врста избора"));
assert(!els.warning.textContent.includes("unknown election type"));
const counted=DATA.overall.counted;
DATA.overall.counted=0;render();
assert(els["chart-section"].hidden);
assert(els.warning.hidden);
assert(!els["empty-results"].hidden);
assert.equal(els["empty-results"].textContent,"Још нема обрађених записника — резултати још нису доступни.");
DATA.overall.counted=counted;DATA.scope="municipal";
const muni=DATA.municipalities[0];muni.counted=0;els.municipality.value=muni.m;render();
assert(els["chart-section"].hidden);
assert.equal(els["empty-results"].textContent,"За ову општину још нема обрађених записника.");
els.municipality.value="";render();
assert(!els["chart-section"].hidden);
assert(els["empty-results"].hidden);
assert.equal(els["chart-note"].textContent,"Изаберите општину за упоредиве резултате по листама.");
console.log(JSON.stringify({ok:true}));
'''
    result = subprocess.run(["node", "-e", harness], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


def test_public_page_renders_sanitized_station_results_with_global_progressive_filters():
    s = store()
    form(s, 1, clean_report())
    html = render_public_results(build_public_results(s, config(), P))

    assert 'id="station-results"' in html
    assert 'data-station-filter="in"' in html
    assert 'data-station-filter="out"' in html
    assert 'id="station-more"' in html

    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    harness = r'''
const assert=require("assert"),els={};let reloaded=0,nextTimer=1,timers=[];
class El{constructor(){this.textContent="";this.value="";this.children=[];this.style={};this.dataset={};this.className="";this.hidden=false;this.open=false;this.listeners={};this.attrs={}}
append(...x){this.children.push(...x)}replaceChildren(...x){this.children=x}addEventListener(type,fn){this.listeners[type]=fn}setAttribute(k,v){this.attrs[k]=String(v)}}
global.document={hidden:false,listeners:{},getElementById:id=>els[id]??=(new El()),createElement:()=>new El(),addEventListener(type,fn){this.listeners[type]=fn}};
global.Option=function(text,value){const x=new El();x.textContent=text;x.value=value;return x};
global.location={hash:"",pathname:"/",search:"",reload(){reloaded++}};
global.history={replaceState(){}};
global.setTimeout=(fn,delay)=>{const timer={id:nextTimer++,fn,delay,cleared:false};timers.push(timer);return timer.id};
global.clearTimeout=id=>{const timer=timers.find(item=>item.id===id);if(timer)timer.cleared=true};
const latestTimer=()=>timers.filter(timer=>!timer.cleared).at(-1);
''' + script + r'''
assert.equal(els["station-filter-in"].attrs["aria-pressed"],"true");
assert.equal(els.stationlist.children.length,1);
assert(els.stationlist.children[0].children[0].children[0].textContent.startsWith("Alpha · БМ 1"));
const initialTimer=latestTimer();document.listeners.pointerdown();
assert(initialTimer.cleared);assert.equal(latestTimer().delay,180000);
DATA.stations[0].lists=[390,390];renderStationResults();
assert.equal(els.stationlist.children[0].children[0].children[3].children[0].textContent,"=");

DATA.stations=Array.from({length:102},(_,i)=>({m:i%2?"Beta":"Alpha",no:String(i+1),name:"Школа "+(i+1),
  included:i!==101,registered:1000,voted:500+i,valid:490+i,lists:[300+i,190]}));
let stationOutcomeCalls=0;const realStationOutcome=stationOutcome;
stationOutcome=(...args)=>{stationOutcomeCalls++;return realStationOutcome(...args)};
setStationFilter("in");
assert(stationOutcomeCalls<=100,"station-order rendering computes outcomes only for visible rows");
stationOutcome=realStationOutcome;
assert.equal(els.stationlist.children.length,100);
assert.equal(els.stationcount.textContent,"100 од 101 бирачког места");
assert.equal(els["station-more"].hidden,false);
assert.equal(els["station-more"].textContent,"Прикажи још (1)");
els["station-more"].listeners.click();
assert.equal(els.stationlist.children.length,101);
assert.equal(els["station-more"].hidden,true);

setStationFilter("out");
assert.equal(els.stationlist.children.length,1);
const excluded=els.stationlist.children[0];
assert(excluded.className.includes("excluded"));
assert.equal(excluded.children[0].children.at(-1).textContent,"Није урачунато");
excluded.open=true;excluded.listeners.toggle();
assert(excluded.children[1].children.length>=2,"expanded station must show every list");
let timer=latestTimer();
assert.equal(timer.delay,180000);timer.cleared=true;timer.fn();
assert.equal(reloaded,0,"an expanded station defers refresh");
excluded.open=false;excluded.listeners.toggle();
timer=latestTimer();assert.equal(timer.delay,60000);timer.cleared=true;timer.fn();
assert.equal(reloaded,1,"refresh resumes after one idle minute");

document.hidden=true;scheduleRefresh();timer=latestTimer();timer.cleared=true;timer.fn();
assert.equal(reloaded,1,"a hidden page defers refresh");
document.hidden=false;document.listeners.visibilitychange();
assert.equal(latestTimer().delay,60000);
timer=latestTimer();timer.cleared=true;timer.fn();
assert.equal(reloaded,2);

excluded.open=true;excluded.listeners.toggle();
scheduleRefresh();timer=latestTimer();timer.cleared=true;timer.fn();
assert.equal(reloaded,2);
els.municipality.value="Alpha";render();
timer=latestTimer();assert.equal(timer.delay,60000);timer.cleared=true;timer.fn();
assert.equal(reloaded,3,"a filtered-out expanded station does not defer refresh");

setStationFilter("all");
render();
assert.equal(els.stationlist.children.length,51,"a selected municipality renders its complete station list");
assert.equal(els["station-more"].hidden,true);
assert(!els.stationlist.children[0].children[0].children[0].textContent.startsWith("Alpha ·"));

DATA.scope="municipal";els.stationsort.value="winner";els.municipality.value="";render();
assert.equal(els.sortstationwinner.hidden,true,"winner sorting is unavailable across different local ballots");
assert.equal(els.stationsort.value,"station");
assert.equal($("stationfilters").hidden,false);
console.log(JSON.stringify({ok:true}));
'''
    result = subprocess.run(["node", "-e", harness], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


def test_public_page_uses_wide_layout_nonwrapping_ratios_and_unofficial_badge():
    s = store()
    form(s, 1, clean_report())

    html = render_public_results(build_public_results(s, config(), P))

    assert "main{max-width:1600px" in html
    assert ".ratio{" in html and "white-space:nowrap" in html
    assert html.count('class="v ratio"') == 2
    assert 'class="status-badge">Незванични резултати</div>' in html
    assert ".status-badge{" in html and "background:var(--badge-bg)" in html


def test_public_page_uses_compact_municipality_select_text():
    s = store()
    form(s, 1, clean_report())

    html = render_public_results(build_public_results(s, config(), P))

    assert "select{display:block" in html
    assert "font:inherit;font-size:.9rem" in html


def test_public_page_uses_restrained_editorial_accents():
    s = store()
    form(s, 1, clean_report())

    html = render_public_results(build_public_results(s, config(), P))

    assert "header{display:flex" in html and "border-bottom:2px solid var(--accent)" in html
    assert ".tiles .tile:first-child{box-shadow:inset 0 3px var(--accent)}" in html
    assert ".chart-eyebrow{" in html
    assert '<span class="chart-eyebrow">Пресек резултата</span>' in html


def test_public_page_aligns_chart_bars_and_values_in_shared_columns():
    s = store()
    form(s, 1, clean_report())

    html = render_public_results(build_public_results(s, config(), P))

    assert "grid-template-columns:minmax(16rem,30%) minmax(8rem,1fr) 12rem" in html
    assert ".value{font-variant-numeric:tabular-nums;white-space:nowrap" in html
    assert "@media(max-width:780px)" in html
    assert ".row{grid-template-columns:1fr auto}.row>.track{grid-column:1/-1;grid-row:2}" in html


def test_public_station_span_bar_fills_are_block_level():
    s = store()
    form(s, 1, clean_report())

    html = render_public_results(build_public_results(s, config(), P))

    assert ".fill{display:block;height:100%;background:var(--bar)" in html


def test_public_page_uses_adaptive_navy_serbian_theme():
    s = store()
    form(s, 1, clean_report())

    html = render_public_results(build_public_results(s, config(), P))

    assert "--bg:#eaf0f6" in html and "--bg:#061321" in html
    assert "@media(prefers-color-scheme:dark)" in html


def test_public_page_uses_official_static_flag_background():
    s = store()
    form(s, 1, clean_report())

    html = render_public_results(build_public_results(s, config(), P))

    assert 'class="serbian-flag-bg" aria-hidden="true"' in html
    assert 'class="serbian-flag-art"' in html and "Flag_of_Serbia.svg" in html
    assert ".serbian-flag-bg{position:fixed;z-index:-1;left:-12vw;top:-6vh" in html
    assert "pointer-events:none" in html
    assert "mask-image:linear-gradient(90deg" in html
    assert "mask-image:linear-gradient(135deg" not in html
    assert "linear-gradient(to bottom,var(--flag-red)" not in html


def test_export_refuses_directory_with_other_files(tmp_path):
    s = store()
    form(s, 1, clean_report())
    out = tmp_path / "public-results"
    out.mkdir()
    (out / "private-data.json").write_text("secret", encoding="utf-8")

    try:
        write_public_results(s, config(), P, out)
    except ValueError as exc:
        assert "must contain only index.html" in str(exc)
    else:
        raise AssertionError("mixed public/private directory was accepted")
    assert (out / "private-data.json").read_text(encoding="utf-8") == "secret"
    assert not (out / "index.html").exists()


def test_export_results_cli_writes_default_directory_and_replaces_snapshot(tmp_path, monkeypatch, capsys):
    db = tmp_path / "election.sqlite"
    s = Store(db)
    s.upsert_election(Election(P, "1", "2", "Јавни резултати", BASE))
    s.upsert_election(Election("999999", "1", "2", "Новији избори", BASE))
    s.close()
    monkeypatch.chdir(tmp_path)

    assert main(["export-results", "--db", str(db)]) == 0
    out = tmp_path / "public-results" / "index.html"
    first = out.read_text(encoding="utf-8")
    assert json.dumps("Новији избори", ensure_ascii=True)[1:-1] in first
    assert str(out.relative_to(tmp_path)) in capsys.readouterr().out

    out.write_text("old export", encoding="utf-8")
    assert main(["export-results", "--db", str(db), "--parent", P,
                 "--out", str(tmp_path / "site")]) == 0
    selected = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert selected.startswith("<!doctype html>")
    assert json.dumps("Јавни резултати", ensure_ascii=True)[1:-1] in selected


def test_export_results_cli_rejects_database_without_registered_election(tmp_path, capsys):
    db = tmp_path / "empty.sqlite"
    Store(db).close()
    out = tmp_path / "site"

    assert main(["export-results", "--db", str(db), "--out", str(out)]) == 1
    assert "no registered election" in capsys.readouterr().err
    assert not out.exists()


def test_export_results_cli_refuses_mixed_output_directory(tmp_path, capsys):
    db = tmp_path / "election.sqlite"
    s = Store(db)
    s.upsert_election(Election(P, "1", "2", "Јавни резултати", BASE))
    s.close()
    out = tmp_path / "site"
    out.mkdir()
    (out / "data.json").write_text("private", encoding="utf-8")

    assert main(["export-results", "--db", str(db), "--out", str(out)]) == 1
    assert "must contain only index.html" in capsys.readouterr().err
    assert (out / "data.json").read_text(encoding="utf-8") == "private"


def test_export_wrapper_is_executable_and_only_runs_local_export():
    script = ROOT / "scripts" / "export.sh"
    assert script.is_file() and os.access(script, os.X_OK)
    text = script.read_text(encoding="utf-8")
    assert 'election_id "$@"; shift' in text and "require_db" in text
    assert 'rg2watch export-results --parent "$ID" --db "$DB"' in text
    assert all(command not in text for command in ("wrangler", "aws ", "curl ", "scp ", "rsync "))
    result = subprocess.run([script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 2 and "usage: scripts/export.sh" in result.stderr
