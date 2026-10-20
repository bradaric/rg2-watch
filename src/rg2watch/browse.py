import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from .store import Store

SEV_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
AST = {"new": "pending", "running": "pending", "failed": "failed", "done": "done"}


def _rik_link(url: str, base: str | None) -> str | None:
    full = urljoin(base + "/", url) if base else url
    return full if urlsplit(full).scheme in ("http", "https") else None


def _version(d, found: dict, cache_dir: Path, out_dir: Path, base: str | None) -> dict:
    pdf = cache_dir / (hashlib.sha256(d["url"].encode()).hexdigest() + ".pdf")
    f = [[x["rule"], x["severity"], x["summary"]] for x in found.get(d["id"], [])]
    f.sort(key=lambda x: -SEV_RANK.get(x[1], 0))
    return {"up": d["uploaded_at"], "seen": d["first_seen_at"], "f": f,
            "ast": AST.get(d["analysis_status"], "off"),
            "pdf": quote(os.path.relpath(pdf, out_dir).replace(os.sep, "/")) if pdf.is_file() else None,
            "rik": _rik_link(d["url"], base)}


def build_page(store: Store, out_path: Path, cache_dir: Path, parent: str | None = None) -> int:
    out_path = Path(out_path)
    out_dir = out_path.resolve().parent
    cache_dir = cache_dir.resolve()
    docs, findings = store.browse_rows(parent)
    found: dict[int, list] = {}
    for f in findings:
        found.setdefault(f["document_id"], []).append(f)
    elections = {e.parent: e for e in store.elections()}
    groups: dict[tuple, list] = {}
    for d in docs:
        groups.setdefault((d["parent"], d["station_key"], d["doc_type"]), []).append(d)
    rows = []
    for (par, _, _), ds in groups.items():
        ds.sort(key=lambda d: (d["uploaded_at"] or "", d["id"]), reverse=True)
        main = next((d for d in ds if d["active"]), ds[0])
        base = elections[par].base_url if par in elections else None
        row = _version(main, found, cache_dir, out_dir, base)
        row.update(
            el=par, muni=main["municipality"], no=main["station_no"], name=main["station_name"],
            label=main["label"],
            state="removed" if not main["active"] else "replaced" if len(ds) > 1 else "current",
            sev=max((SEV_RANK.get(x[1], 0) for x in row["f"]), default=0),
            v=[_version(d, found, cache_dir, out_dir, base) for d in ds if d is not main])
        rows.append(row)
    data = {"built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elections": {p: e.title for p, e in elections.items()}, "rows": rows}
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_HTML.replace("__DATA__", blob))
        os.chmod(tmp, 0o644)
        os.replace(tmp, out_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(rows)


_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rg2-watch</title>
<style>
:root{--bg:#fff;--fg:#1b1f24;--mut:#667;--line:#d8dce1;--card:#f5f6f8;--hi:#c62828;--md:#e08600;--lo:#2e7d32;--link:#0b5fc0}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--fg:#e4e7ea;--mut:#98a;--line:#2d3238;--card:#1d2125;--hi:#ef5350;--md:#ffb74d;--lo:#81c784;--link:#7ab6ff}}
*{box-sizing:border-box}
body{margin:0;padding:12px 16px;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
a{color:var(--link)}
h1{font-size:18px;margin:0 0 8px}
#ctl{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
#ctl label{white-space:nowrap}
input,select,button{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:4px;padding:3px 6px}
#rules{height:4.5em}
#wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{cursor:pointer;white-space:nowrap;user-select:none}
tr.v td{background:var(--card);color:var(--mut)}
.b{display:inline-block;padding:0 6px;border-radius:8px;color:#fff;font-size:12px;margin-right:4px}
.b.HIGH{background:var(--hi)}.b.MEDIUM{background:var(--md)}.b.LOW{background:var(--lo)}
.mut{color:var(--mut)}
.f{margin:2px 0}
</style></head><body>
<h1>rg2-watch <span class="mut" id="built"></span></h1>
<div id="ctl">
<input id="q" type="search" placeholder="search">
<select id="m"></select><select id="e"></select>
<label>min severity <select id="sev"><option value="0">any</option><option value="1">LOW</option><option value="2">MEDIUM</option><option value="3">HIGH</option></select></label>
<select id="rules" multiple title="rules"></select>
<label><input type="checkbox" id="fl"> with findings</label>
<label><input type="checkbox" id="rp"> replaced</label>
<label><input type="checkbox" id="rm"> removed</label>
<label><input type="checkbox" id="fa"> analysis failed</label>
<label><input type="checkbox" id="auto" checked> auto-reload</label>
<span id="count"></span>
</div>
<div id="wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const $ = id => document.getElementById(id);
const COLS = [["el","Election"],["muni","Municipality"],["no","BM"],["name","Station"],["label","Document"],
  ["up","Uploaded"],["seen","First seen"],["state","State"],["ast","Analysis"],["sev","Findings"],["pdf","Links"]];
const SEV = ["", "LOW", "MEDIUM", "HIGH"];
const pad = n => String(n).padStart(2, "0");
function when(s) {
  const d = new Date(s);
  if (!s || isNaN(d)) return s || "";
  return pad(d.getDate()) + "/" + pad(d.getMonth() + 1) + "/" + d.getFullYear() + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function mk(tag, text, cls) {
  const e = document.createElement(tag);
  if (text != null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function opts(sel, first, items) {
  sel.append(new Option(first, ""));
  for (const [v, t] of items) sel.append(new Option(t, v));
}
const rows = D.rows;
opts($("m"), "all municipalities", [...new Set(rows.map(r => r.muni))].sort().map(m => [m, m]));
const parents = [...new Set(rows.map(r => r.el))].sort();
opts($("e"), "all elections", parents.map(p => [p, D.elections[p] || p]));
$("e").hidden = parents.length < 2;
for (const r of [...new Set(rows.flatMap(r => r.f.map(f => f[0])))].sort()) $("rules").append(new Option(r, r));
$("built").textContent = "built " + when(D.built);
$("rules").hidden = !$("rules").options.length;

let sortKey = "sev", sortDir = -1;
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  $("q").value = p.get("q") || "";
  for (const k of ["m", "e", "sev"]) if ([...$(k).options].some(o => o.value === p.get(k))) $(k).value = p.get(k);
  for (const k of ["fl", "rp", "rm", "fa"]) $(k).checked = p.get(k) === "1";
  $("auto").checked = p.get("auto") !== "0";
  const rs = (p.get("rules") || "").split(",");
  for (const o of $("rules").options) o.selected = rs.includes(o.value);
  if (COLS.some(c => c[0] === p.get("sort"))) sortKey = p.get("sort");
  sortDir = p.get("dir") === "asc" ? 1 : -1;
}
function writeHash() {
  const p = new URLSearchParams();
  if ($("q").value) p.set("q", $("q").value);
  for (const k of ["m", "e"]) if ($(k).value) p.set(k, $(k).value);
  if ($("sev").value !== "0") p.set("sev", $("sev").value);
  for (const k of ["fl", "rp", "rm", "fa"]) if ($(k).checked) p.set(k, "1");
  const rs = [...$("rules").selectedOptions].map(o => o.value);
  if (rs.length) p.set("rules", rs.join(","));
  if (!$("auto").checked) p.set("auto", "0");
  p.set("sort", sortKey); p.set("dir", sortDir > 0 ? "asc" : "desc");
  history.replaceState(null, "", "#" + p);
}
function haystack(r) {
  return [r.muni, r.no, r.name, r.label, ...r.f.map(f => f[0] + " " + f[2])].join("\n").toLowerCase();
}
function keep(r) {
  const q = $("q").value.trim().toLowerCase();
  const rs = [...$("rules").selectedOptions].map(o => o.value);
  return (!q || haystack(r).includes(q)) && (!$("m").value || r.muni === $("m").value)
    && (!$("e").value || r.el === $("e").value) && r.sev >= +$("sev").value
    && (!rs.length || r.f.some(f => rs.includes(f[0])))
    && (!$("fl").checked || r.f.length) && (!$("rp").checked || r.state === "replaced")
    && (!$("rm").checked || r.state === "removed") && (!$("fa").checked || r.ast === "failed");
}
function cmp(a, b) {
  let x = a[sortKey], y = b[sortKey];
  if (sortKey === "no") { x = parseInt(x) || 0; y = parseInt(y) || 0; }
  if (sortKey === "pdf") { x = !!a.pdf; y = !!b.pdf; }
  if (x == null) x = ""; if (y == null) y = "";
  return (x < y ? -1 : x > y ? 1 : 0) * sortDir || (b.sev - a.sev);
}
function links(v) {
  const td = mk("td");
  if (v.pdf) { const a = mk("a", "PDF"); a.href = v.pdf; td.append(a, " "); }
  if (v.rik) { const a = mk("a", "RIK"); a.href = v.rik; a.rel = "noopener noreferrer"; td.append(a); }
  return td;
}
function findings(v) {
  const td = mk("td");
  for (const f of v.f) {
    const d = mk("div", null, "f");
    d.append(mk("span", f[1], "b " + (SEV.includes(f[1]) ? f[1] : "")), f[0] + ": " + f[2]);
    td.append(d);
  }
  return td;
}
function render() {
  writeHash();
  const shown = rows.filter(keep).sort(cmp);
  $("count").textContent = "showing " + shown.length + " of " + rows.length;
  const tb = $("body");
  tb.replaceChildren();
  for (const r of shown) {
    const tr = mk("tr");
    tr.append(mk("td", D.elections[r.el] || r.el), mk("td", r.muni), mk("td", r.no), mk("td", r.name),
      mk("td", r.label), mk("td", when(r.up)), mk("td", when(r.seen)), mk("td", r.state), mk("td", r.ast),
      findings(r), links(r));
    tb.append(tr);
    for (const v of r.v) {
      const vr = mk("tr", null, "v");
      vr.append(mk("td", "older version", "mut"), mk("td"), mk("td"), mk("td"), mk("td"),
        mk("td", when(v.up)), mk("td", when(v.seen)), mk("td"), mk("td", v.ast), findings(v), links(v));
      tb.append(vr);
    }
  }
}
const head = $("head");
for (const [k, t] of COLS) {
  const th = mk("th", t);
  th.onclick = () => { sortDir = sortKey === k ? -sortDir : 1; sortKey = k; render(); };
  head.append(th);
}
for (const id of ["q", "m", "e", "sev", "rules", "fl", "rp", "rm", "fa", "auto"]) $(id).addEventListener("input", render);
readHash();
render();
setInterval(() => { if ($("auto").checked) location.reload(); }, 120000);
</script></body></html>
"""
