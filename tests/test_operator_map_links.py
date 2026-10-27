import json
import subprocess

import rg2watch.browse as browse
import rg2watch.station_locations as locations
from rg2watch.public_results import build_public_results, render_public_results
from rg2watch.tally import build_tally
from test_browse import P, store
from test_public_results import config
from test_rules import clean_report
from test_tally import M, form


def test_operator_station_results_include_cached_coordinates(tmp_path):
    s = store()
    form(s, 11, clean_report(), muni="Градска општина Гроцка")
    result = browse.build_data(s, tmp_path, parent=P, cfg=config())

    assert result["tally"]["station_results"][0]["coordinates"] == {
        "latitude": 44.664, "longitude": 20.59089, "location_confidence": 0,
    }


def test_operator_stations_without_coordinates_have_no_map_data(tmp_path):
    s = store()
    form(s, 13, clean_report(), muni="Иностранство")
    result = browse.build_data(s, tmp_path, parent=P, cfg=config())

    assert "coordinates" not in result["tally"]["station_results"][0]


def test_unavailable_coordinate_cache_does_not_hide_operator_results(tmp_path, monkeypatch):
    s = store()
    form(s, 11, clean_report(), muni="Градска општина Гроцка")

    def unavailable(*args):
        raise FileNotFoundError("coordinate cache missing")

    monkeypatch.setattr(browse, "station_coordinates", unavailable, raising=False)
    result = browse.build_data(s, tmp_path, parent=P, cfg=config())

    assert result["tally"]["overall"]["all"]["valid"] == 780
    assert "coordinates" not in result["tally"]["station_results"][0]


def test_public_exports_never_read_or_publish_coordinates(monkeypatch):
    s = store()
    form(s, 11, clean_report(), muni="Градска општина Гроцка")

    original_data = locations._data

    def addresses_only(path=None):
        if path == locations.COORDINATES:
            raise AssertionError("public export must not read the coordinate cache")
        return original_data(path)

    monkeypatch.setattr(locations, "_data", addresses_only)
    data = build_public_results(s, config(), P)
    html = render_public_results(data)
    for field in ("coordinates", "latitude", "longitude", "location_confidence"):
        assert field not in json.dumps(data)
    assert "openstreetmap.org" not in html and "stationmap" not in html
    assert "44.664" not in html and "20.59089" not in html
    # The shared tally remains coordinate-free even when station results are requested.
    shared = build_tally(s, config(), P, M, include_station_results=True)
    assert "coordinates" not in shared["station_results"][0]


def test_operator_globe_links_are_safe_accessible_and_preserve_location_warnings():
    html = (browse.STATIC / "index.html").read_text(encoding="utf-8")
    assert 'stationMapLink(station)' in html
    assert '.stationname.hasmap{' in html
    start = html.index("function stationMapLink(")
    code = html[start:html.index("function renderStationResults(", start)]
    link = html[html.index("function link("):html.index("const HEX =")]
    script = r'''
const assert=require("assert");
function mk(tag,text){return {tag,textContent:text,children:[],append(...nodes){this.children.push(...nodes)}}}
''' + link + code + r'''
const station={m:"Гроцка",no:"11",coordinates:{latitude:44.664,longitude:20.59089,location_confidence:0}};
const globe=stationMapLink(station);
assert.equal(globe.textContent,"🌐");
assert.equal(globe.href,"https://www.openstreetmap.org/?mlat=44.664&mlon=20.59089#map=18/44.664/20.59089");
assert.equal(globe.target,"_blank");
assert.equal(globe.rel,"noopener noreferrer");
assert(globe.ariaLabel.includes("OpenStreetMap")&&globe.ariaLabel.includes("БМ 11"));
assert(globe.title.includes("ИТ Блокада"));
assert(!globe.title.includes("Непроверена")&&!globe.title.includes("Приближна"));
let stopped=0;
globe.onclick({stopPropagation(){stopped++},preventDefault(){throw Error("must allow opening new tab")}});
globe.onkeydown({key:"Enter",stopPropagation(){stopped++},preventDefault(){throw Error("must allow keyboard navigation")}});
assert.equal(stopped,2);
station.coordinates.location_confidence=1;
assert(stationMapLink(station).title.includes("Непроверена"));
station.coordinates.location_confidence=2;
assert(stationMapLink(station).title.includes("Приближна"));
for(const coordinates of [null,{}, {latitude:null,longitude:20}, {latitude:44,longitude:"20"},
  {latitude:NaN,longitude:20}, {latitude:Infinity,longitude:20}, {latitude:91,longitude:20},
  {latitude:44,longitude:-181}, {latitude:0,longitude:0}]){
  assert.equal(stationMapLink({...station,coordinates}),null);
}
assert(stationMapLink({...station,coordinates:{latitude:0,longitude:20,location_confidence:0}}));
'''
    result = subprocess.run(["node"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
