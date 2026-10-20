"""Unofficial count read from the forms: totals of what the watch model read from each RG-2.
Never RIK's official results; numbers are model readings and are marked as such."""
import json
import logging

from .config import Config
from .models import Doc
from .pipeline import PROMPT_VERSION
from .rules import _dash_zero, _ok, evaluate_report
from .store import Store, _doc

log = logging.getLogger("rg2watch.tally")
COUNTED = ("registered", "voted", "ballots_in_box", "invalid", "valid", "outside_voted")
ARITHMETIC = ("A1", "A2", "A3", "A4", "A5")
FIRST_ROUND = 6  # repeat-vote forms (10, 15, 16) are another round: never mixed into the totals


def list_name(cfg: Config, parent: str, row: int) -> str:
    names = cfg.list_names.get(parent, [])
    return names[row - 1] if 1 <= row <= len(names) and names[row - 1].strip() else f"List {row}"


def extract(report: dict, doc: Doc, cfg: Config) -> dict:
    """The counted numbers of one report (None = not readable) plus what makes it unreliable."""
    nums = {k: _dash_zero(f) for k, f in report["numbers"].items() if k != "list_votes"}
    n: dict[str, int | None] = {}
    bad: list[str] = []
    for k in COUNTED:
        f = nums[k]
        n[k] = f["value"] if _ok(f) else None
        if k == "outside_voted":  # an empty field is no problem (as in rule V8)
            if not f["legible"] and f["mark"] != "empty":
                bad.append(f"{k} illegible")
        elif not _ok(f):
            bad.append(f"{k} {'illegible' if not f['legible'] else 'empty'}")
    lists: dict[int, int | None] = {}
    for r in report["numbers"]["list_votes"]:
        f = _dash_zero(r["field"])
        lists[r["row"]] = f["value"] if _ok(f) else None
        if not _ok(f):
            bad.append(f"list row {r['row']} {'illegible' if not f['legible'] else 'empty'}")
    arith = sorted({f.rule for f in evaluate_report(report, doc, cfg) if f.rule in ARITHMETIC})
    return {"n": n, "l": lists, "bad": bad, "conf": report["confidence"],
            "pq": report["visual"]["page_quality"], "arith": arith}


def disagreements(mine: dict, others: dict[str, dict], model: str) -> list[dict]:
    """Counted fields (or list rows) on which another model's newest reading differs."""
    out = []
    for key in (*COUNTED, *sorted(set(mine["l"]).union(*(o["l"] for o in others.values())))):
        get = (lambda e, k=key: e["n"][k]) if isinstance(key, str) else (lambda e, k=key: e["l"].get(k))
        vals = {model: get(mine), **{m: get(o) for m, o in others.items()}}
        if len(set(vals.values())) > 1:
            out.append({"f": key if isinstance(key, str) else f"list row {key}", "v": vals})
    return out


def reasons(ex: dict, dis: list[dict]) -> list[str]:
    out = []
    if ex["conf"] == "low":
        out.append("low confidence")
    if ex["pq"] in ("poor", "unreadable"):
        out.append(f"page quality {ex['pq']}")
    if ex["bad"]:
        out.append("illegible or empty: " + ", ".join(ex["bad"]))
    for d in dis:
        out.append(f"models disagree on {d['f']}: " + " vs ".join(f"{v} ({m})" for m, v in d["v"].items()))
    if ex["arith"]:
        out.append("arithmetic " + ", ".join(ex["arith"]))
    return out


def _acc(nlists: int) -> dict:
    return {**{k: 0 for k in COUNTED}, "lists": {r: 0 for r in range(1, nlists + 1)}, "tv": 0, "tr": 0}


def _add(acc: dict, ex: dict) -> None:
    for k in COUNTED:
        if ex["n"][k] is not None:
            acc[k] += ex["n"][k]
    for r, v in ex["l"].items():
        if v is not None:
            acc["lists"][r] = acc["lists"].get(r, 0) + v
    if ex["n"]["voted"] is not None and ex["n"]["registered"] is not None:
        acc["tv"] += ex["n"]["voted"]
        acc["tr"] += ex["n"]["registered"]


def _tot(acc: dict, nlists: int) -> dict:
    return {**{k: acc[k] for k in COUNTED}, "turnout": acc["tv"] / acc["tr"] if acc["tr"] else None,
            "lists": [acc["lists"].get(r, 0) for r in range(1, nlists + 1)]}


def build_tally(store: Store, cfg: Config, parent: str, model: str,
                municipalities: set[str] | None = None, cache: dict | None = None) -> dict:
    """Totals per municipality and overall, over all counted forms and over clean ones only.
    `model` is the stored model name; `cache` keeps extracts by analysis id between builds."""
    cache = {} if cache is None else cache
    docs = [d for d in store.browse_rows(parent)[0] if d["doc_type"] == FIRST_ROUND]
    arows = store.browse_analyses(parent, PROMPT_VERSION)
    by_doc: dict[int, dict[str, int]] = {}
    for a in arows:
        by_doc.setdefault(a["document_id"], {})[a["model"]] = a["id"]
    groups: dict[object, list] = {}
    for d in docs:  # a NULL station_key cannot be grouped: the document stands alone
        groups.setdefault(d["station_key"] if d["station_key"] is not None else ("id", d["id"]), []).append(d)
    mains = []
    for ds in groups.values():
        active = [d for d in ds if d["active"]]
        if active:  # removed forms are not counted
            mains.append(max(active, key=lambda d: (d["uploaded_at"] or "", d["id"])))
    need = [i for d in mains for m, i in by_doc.get(d["id"], {}).items() if i not in cache]
    reports = store.analysis_reports(need)
    docobj = {d["id"]: _doc(d) for d in mains}
    owner = {i: d for d in mains for i in by_doc.get(d["id"], {}).values()}

    def ex_of(aid: int) -> dict | None:
        if aid not in cache:
            try:
                cache[aid] = extract(json.loads(reports[aid]), docobj[owner[aid]["id"]], cfg)
            except Exception:
                log.warning("stored analysis %s could not be read for the tally", aid, exc_info=True)
                cache[aid] = None
        return cache[aid]

    live = set(owner)
    for gone in set(cache) - live:
        del cache[gone]
    forms, unanalysed = [], {}
    for d in mains:
        muni = d["municipality"] or "(unknown)"
        if municipalities is not None and muni not in municipalities:
            continue
        ids = by_doc.get(d["id"], {})
        mine = ex_of(ids[model]) if model in ids else None
        if mine is None:
            unanalysed[muni] = unanalysed.get(muni, 0) + 1
            continue
        others = {m: e for m, i in ids.items() if m != model and (e := ex_of(i)) is not None}
        dis = disagreements(mine, others, model)
        forms.append({"d": d, "muni": muni, "ex": mine, "dis": dis, "why": reasons(mine, dis)})
    nlists = max((r for f in forms for r in f["ex"]["l"]), default=0)
    stations = store.station_counts(parent)
    names = sorted({f["muni"] for f in forms} | set(unanalysed))
    overall = {"all": _acc(nlists), "cl": _acc(nlists)}
    counts = {"stations": 0, "counted": 0, "clean": 0, "unanalysed": 0}
    munis = []
    for name in names:
        mine = [f for f in forms if f["muni"] == name]
        acc = {"all": _acc(nlists), "cl": _acc(nlists)}
        for f in mine:
            for key, target in (("all", True), ("cl", not f["why"])):
                if target:
                    _add(acc[key], f["ex"])
                    _add(overall[key], f["ex"])
        row = {"m": name, "stations": stations.get(name, 0), "counted": len(mine),
               "clean": sum(1 for f in mine if not f["why"]), "unanalysed": unanalysed.get(name, 0),
               "all": _tot(acc["all"], nlists), "cl": _tot(acc["cl"], nlists)}
        row["excluded"] = row["counted"] - row["clean"]
        for k in counts:
            counts[k] += row[k]
        munis.append(row)
    total = {"m": "All", **counts, "all": _tot(overall["all"], nlists), "cl": _tot(overall["cl"], nlists)}
    total["excluded"] = total["counted"] - total["clean"]
    e = store.get_election(parent)
    return {"model": model, "parent": parent, "title": e.title if e else parent, "nlists": nlists,
            "names": [list_name(cfg, parent, r) for r in range(1, nlists + 1)],
            "munis": munis, "overall": total,
            "excluded": [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
                          "why": f["why"]} for f in forms if f["why"]]}


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.1%}"


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    w = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    line = lambda r: "  ".join(c.ljust(w[i]) if i == 0 else c.rjust(w[i]) for i, c in enumerate(r))
    return [line(head), *map(line, rows)]


def format_tally(t: dict, all_forms: bool = False) -> str:
    """Plain-text report of build_tally(): totals, list votes, excluded forms."""
    key = "all" if all_forms else "cl"
    rows = [*t["munis"], t["overall"]]
    out = [f"UNOFFICIAL - numbers read by {t['model']} from the scanned forms, not RIK results",
           f"{t['title']} ({t['parent']}): " + ("all counted forms" if all_forms else "clean forms only"), ""]
    head = ["municipality", "stations", "counted", "clean", "excluded", "cover", "registered", "voted",
            "turnout", "in box", "invalid", "valid", "outside"]
    body = [[r["m"], str(r["stations"]), str(r["counted"]), str(r["clean"]), str(r["excluded"]),
             _pct(r["counted"] / r["stations"] if r["stations"] else None),
             *(f"{r[key][k]:,}" for k in ("registered", "voted")), _pct(r[key]["turnout"]),
             *(f"{r[key][k]:,}" for k in ("ballots_in_box", "invalid", "valid", "outside_voted"))]
            for r in rows]
    out += _table(head, body)
    if t["nlists"]:
        out += ["", "votes per list (share of valid votes)"]
        body = [[r["m"], *(f"{v:,} ({_pct(v / r[key]['valid'] if r[key]['valid'] else None)})"
                           for v in r[key]["lists"])] for r in rows]
        out += _table(["municipality", *t["names"]], body)
    pending = t["overall"]["unanalysed"]
    out += ["", f"{t['overall']['counted']} form(s) counted, {t['overall']['clean']} clean, "
            f"{t['overall']['excluded']} excluded, {pending} not analysed by {t['model']}"]
    if t["excluded"]:
        out += ["", "excluded forms (reason):"]
        out += [f"  {e['m']} BM {e['no']}: " + "; ".join(e["why"]) for e in t["excluded"]]
    return "\n".join(out)
