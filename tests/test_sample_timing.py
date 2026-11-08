import subprocess

from rg2watch.browse import STATIC


def test_historical_sample_timing_uses_full_selected_denominator_and_never_invents_missing_times():
    html = (STATIC / "index.html").read_text()
    code = html[html.index("// ---- operator estimates"):html.index("// ---- results (unofficial tally)")]
    script = 'const assert=require("assert");\n' + code + r'''
const s={selected:4,sample_stations:[
  {prior_first_upload:"2026-10-25T19:56:00Z"},
  {prior_first_upload:"2026-10-26T00:36:00Z"},
  {prior_first_upload:"2026-10-26T03:17:00Z"},
  {prior_first_upload:"2026-10-28T17:18:00Z"}]};
const t=sampleUploadTiming(s);
assert.equal(t.known,4);assert.equal(t.total,4);
assert.equal(t.milestones[0].at,"2026-10-25T19:56:00.000Z");
assert.equal(t.milestones.find(r=>r.fraction===.5).at,"2026-10-26T00:36:00.000Z");
assert.equal(t.milestones.find(r=>r.fraction===1).at,"2026-10-28T17:18:00.000Z");
const reversed=sampleUploadTiming({...s,sample_stations:[...s.sample_stations].reverse()});
assert.deepStrictEqual(reversed,t);
const missing=sampleUploadTiming({...s,sample_stations:[s.sample_stations[0],s.sample_stations[1],
  {prior_first_upload:null},{prior_first_upload:"not-a-date"}]});
assert.equal(missing.known,2);assert.equal(missing.total,4);
assert(missing.milestones.find(r=>r.fraction===.5).at);
assert.equal(missing.milestones.find(r=>r.fraction===.8).at,null);
assert.equal(missing.milestones.find(r=>r.fraction===1).at,null);
assert.equal(sampleUploadTiming({selected:200,sample_stations:[]}).known,0);
'''
    result = subprocess.run(["node"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
