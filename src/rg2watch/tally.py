"""Unofficial count read from the forms: totals of what the watch model read from each RG-2.
Never RIK's official results; numbers are model readings and are marked as such."""
import json
import logging
from collections import Counter

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


def disagreements(mine: dict, others: dict[str, dict], model: str) -> tuple[list[dict], list[str]]:
    """(fields on which readable values differ between models, fields another model could not read).
    Only the first excludes a form."""
    diff, notes = [], []
    for key in (*COUNTED, *sorted(set(mine["l"]).union(*(o["l"] for o in others.values())))):
        get = (lambda e, k=key: e["n"][k]) if isinstance(key, str) else (lambda e, k=key: e["l"].get(k))
        name = key if isinstance(key, str) else f"list row {key}"
        vals = {model: get(mine), **{m: get(o) for m, o in others.items()}}
        if len({v for v in vals.values() if v is not None}) > 1:
            diff.append({"f": name, "v": vals})
        elif get(mine) is not None:
            notes += [f"{m} could not read {name}" for m, v in vals.items() if v is None and m != model]
    return diff, notes


def reasons(ex: dict, dis: list[dict], rows: list[int]) -> list[str]:
    out = []
    if sorted(ex["l"]) != rows:
        out.append(f"list rows differ (got {len(ex['l'])}, expected {len(rows)})")
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
    return {**{k: 0 for k in COUNTED}, "lists": [0] * nlists, "lv": 0, "tv": 0, "tr": 0}


def _add(acc: dict, ex: dict, rows: list[int]) -> None:
    """Every sum only takes forms where all its terms are known, so ratios share a denominator:
    registered and voted come from forms with both, list votes from forms with every list row
    (of the expected set) and valid."""
    for k in COUNTED:
        if ex["n"][k] is not None and k not in ("registered", "voted"):
            acc[k] += ex["n"][k]
    if ex["n"]["voted"] is not None and ex["n"]["registered"] is not None:
        acc["registered"] += ex["n"]["registered"]
        acc["voted"] += ex["n"]["voted"]
    vals = [ex["l"].get(r) for r in rows]
    if sorted(ex["l"]) == rows and rows and None not in vals and ex["n"]["valid"] is not None:
        acc["lists"] = [a + v for a, v in zip(acc["lists"], vals)]
        acc["lv"] += ex["n"]["valid"]


def _tot(acc: dict) -> dict:
    return {**{k: acc[k] for k in COUNTED}, "lists": acc["lists"], "lists_valid": acc["lv"],
            "turnout": acc["voted"] / acc["registered"] if acc["registered"] else None}


def expected_rows(exs: list[dict]) -> list[int]:
    """The list rows every form should have: the most common row set (ties: the larger). A stray
    extra row on one form never widens the table; list_names only supplies names."""
    common = Counter(tuple(sorted(e["l"])) for e in exs).most_common()
    if not common:
        return []
    top = max(n for _, n in common)
    return list(max((r for r, n in common if n == top), key=len))


_warned: set[tuple] = set()


def build_tally(store: Store, cfg: Config, parent: str, model: str,
                municipalities: set[str] | None = None, cache: dict | None = None,
                docs=None, arows=None) -> dict:
    """Totals per municipality and overall, over all counted forms and over clean ones only.
    `model` is the stored model name; `cache` keeps extracts by analysis id between builds;
    `docs`/`arows` let the caller share its reads (browse_rows / browse_analyses)."""
    cache = {} if cache is None else cache
    docs = store.browse_rows(parent)[0] if docs is None else docs
    # Only doc type 6 (the first-round RG-2): the repeat-vote RG-2 types (10, 15, 16) are another
    # round and would double count stations. Assumes RIK files first-round forms under type 6.
    docs = [d for d in docs if d["doc_type"] == FIRST_ROUND and d["parent"] == parent]
    ids = {d["id"] for d in docs}
    arows = [a for a in (store.browse_analyses(parent, PROMPT_VERSION) if arows is None else arows)
             if a["document_id"] in ids]
    by_doc: dict[int, dict[str, object]] = {}
    for a in arows:
        by_doc.setdefault(a["document_id"], {})[a["model"]] = a
    groups: dict[object, list] = {}
    for d in docs:  # a NULL station_key cannot be grouped: the document stands alone
        groups.setdefault(d["station_key"] if d["station_key"] is not None else ("id", d["id"]), []).append(d)
    mains = []
    for ds in groups.values():
        active = [d for d in ds if d["active"]]
        if active:  # removed forms are not counted
            mains.append(max(active, key=lambda d: (d["uploaded_at"] or "", d["id"])))
    need = [a["id"] for d in mains for m, a in by_doc.get(d["id"], {}).items()
            if a["id"] not in cache and (m == model or a["prompt_version"] == PROMPT_VERSION)]
    reports = store.analysis_reports(need)
    docobj = {d["id"]: _doc(d) for d in mains}

    def ex_of(a) -> dict | None:
        if a["id"] not in cache:
            try:
                cache[a["id"]] = extract(json.loads(reports[a["id"]]), docobj[a["document_id"]], cfg)
            except Exception:
                log.warning("stored analysis %s could not be read for the tally", a["id"], exc_info=True)
                cache[a["id"]] = None
        return cache[a["id"]]

    for gone in set(cache) - {a["id"] for a in arows}:
        del cache[gone]
    forms, unanalysed = [], {}
    for d in mains:
        muni = d["municipality"] or "(unknown)"
        if municipalities is not None and muni not in municipalities:
            continue
        got = by_doc.get(d["id"], {})
        mine = ex_of(got[model]) if model in got else None
        if mine is None:
            unanalysed[muni] = unanalysed.get(muni, 0) + 1
            continue
        others = {m: e for m, a in got.items() if m != model and a["prompt_version"] == PROMPT_VERSION
                  and (e := ex_of(a)) is not None}
        forms.append({"d": d, "muni": muni, "ex": mine, "others": others})
    rows = expected_rows([f["ex"] for f in forms])
    nlists = len(rows)
    configured = len(cfg.list_names.get(parent, []))
    names_note = None
    if configured and nlists and configured != nlists:
        names_note = f"list_names has {configured} names, forms have {nlists} lists - check config"
        if (parent, configured, nlists) not in _warned:
            _warned.add((parent, configured, nlists))
            log.warning("%s (election %s)", names_note, parent)
    notes = []
    for f in forms:
        dis, note = disagreements(f["ex"], f["others"], model)
        f["why"] = reasons(f["ex"], dis, rows)
        notes += [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"], "note": n} for n in note]
    stations = {m: n for m, n in store.station_counts(parent).items()
                if municipalities is None or m in municipalities}
    by_muni: dict[str, list] = {}
    for f in forms:
        by_muni.setdefault(f["muni"], []).append(f)
    names = sorted({*by_muni, *unanalysed, *stations})
    overall = {"all": _acc(nlists), "cl": _acc(nlists)}
    counts = {"stations": 0, "counted": 0, "clean": 0, "unanalysed": 0}
    munis = []
    for name in names:
        mine = by_muni.get(name, [])
        acc = {"all": _acc(nlists), "cl": _acc(nlists)}
        for f in mine:
            for key, take in (("all", True), ("cl", not f["why"])):
                if take:
                    _add(acc[key], f["ex"], rows)
                    _add(overall[key], f["ex"], rows)
        row = {"m": name, "stations": stations.get(name, 0), "counted": len(mine),
               "clean": sum(1 for f in mine if not f["why"]), "unanalysed": unanalysed.get(name, 0),
               "all": _tot(acc["all"]), "cl": _tot(acc["cl"])}
        row["excluded"] = row["counted"] - row["clean"]
        for k in counts:
            counts[k] += row[k]
        munis.append(row)
    total = {"m": "(all)", **counts, "all": _tot(overall["all"]), "cl": _tot(overall["cl"])}
    total["excluded"] = total["counted"] - total["clean"]
    e = store.get_election(parent)
    return {"model": model, "parent": parent, "title": e.title if e else parent, "nlists": nlists,
            "names": [list_name(cfg, parent, r) for r in range(1, nlists + 1)],
            "munis": munis, "overall": total, "notes": notes, "names_note": names_note,
            "excluded": [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
                          "why": f["why"]} for f in forms if f["why"]]}


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.1%}"


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    w = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    line = lambda r: "  ".join(c.ljust(w[i]) if i == 0 else c.rjust(w[i]) for i, c in enumerate(r))
    return [line(head), *map(line, rows)]


def format_tally(t: dict, all_forms: bool = True) -> str:
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
        body = [[r["m"], *(f"{v:,} ({_pct(v / r[key]['lists_valid'] if r[key]['lists_valid'] else None)})"
                           for v in r[key]["lists"])] for r in rows]
        out += _table(["municipality", *t["names"]], body)
    if t["names_note"]:
        out += ["", "warning: " + t["names_note"]]
    pending = t["overall"]["unanalysed"]
    out += ["", f"{t['overall']['counted']} form(s) counted, {t['overall']['clean']} clean, "
            f"{t['overall']['excluded']} excluded, {pending} not analysed by {t['model']}"]
    if t["notes"]:
        out += ["", "notes (not excluded):"] + [f"  {n['m']} BM {n['no']}: {n['note']}" for n in t["notes"]]
    if t["excluded"]:
        out += ["", "excluded forms (reason):"]
        out += [f"  {e['m']} BM {e['no']}: " + "; ".join(e["why"]) for e in t["excluded"]]
    return "\n".join(out)
