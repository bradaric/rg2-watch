"""Unofficial count read from the forms: totals of what the watch model read from each RG-2.
Never RIK's official results; numbers are model readings and are marked as such."""
import copy
import json
import logging
from collections import Counter

from .config import Config
from .models import Doc
from .names import to_cyrillic
from .pipeline import PROMPT_VERSION, RG7_PROMPT_VERSION
from .rules import (RG7_ITEMS, _dash_zero, _ok, change_text, decision_label, decision_sort_key, evaluate_report,
                    rg2_value, rg7_mismatch)
from .store import Store, _doc

log = logging.getLogger("rg2watch.tally")
COUNTED = ("registered", "voted", "ballots_in_box", "invalid", "valid", "outside_voted")
ARITHMETIC = ("A1", "A2", "A3", "A4", "A5")
RG7_FIRST_ROUND = (19, 20, 21, 22)  # decisions correcting a first-round RG-2
FIRST_ROUND = 6  # repeat-vote forms (10, 15, 16) are another round: never mixed into the totals


UNKNOWN_TYPE_NOTE = "unknown election type - set tally_scope"
UNKNOWN_MUNI = "(unknown)"
UNKNOWN_MUNI_NOTE = "municipality unknown - forms may be from different ballots, no list totals"


def scope_of(store: Store, cfg: Config, parent: str) -> tuple[str, str | None]:
    """("national" | "municipal", note). Config override first, then RIK's election type
    (2 parliamentary, 3 local); anything else (or no elections row) is municipal plus a note."""
    if parent in cfg.tally_scope:
        return cfg.tally_scope[parent], None
    e = store.get_election(parent)
    kind = {"2": "national", "3": "municipal"}.get(e.election_type if e else None)
    return (kind, None) if kind else ("municipal", UNKNOWN_TYPE_NOTE)


def configured_names(cfg: Config, parent: str, scope: str, muni: str | None) -> tuple[list[str], str | None]:
    """(names from list_names for the national ballot or one municipality, note on a wrong shape).
    Municipality keys match as Latin or Cyrillic, case-insensitive."""
    v = cfg.list_names.get(parent)
    if v is None:
        return [], None
    if scope == "national":
        return (v, None) if isinstance(v, list) else ([], "list_names must be a plain list for a national election")
    if isinstance(v, list):
        return [], "list_names must map municipality -> names for a municipal election"
    want = to_cyrillic(muni or "")
    return next((n for k, n in v.items() if to_cyrillic(k) == want), []), None


def list_name(names: list[str], row: int) -> str:
    return names[row - 1] if 1 <= row <= len(names) and names[row - 1].strip() else f"List {row}"


def apply_rg7(report: dict, decisions: list[dict]) -> tuple[dict, list[dict], list[str]]:
    """(corrected copy of an RG-2 report, applied [{d, ch}], unverified reasons). Decisions come in
    decision order ({d: label, rep: RG-7 report}); latest wins per item and a decision's corrected
    table is final for the items it lists. A decision that is a low-confidence read, or whose old
    numbers contradict the RG-2 reading (or the previous decision), is not applied. A correction
    never fills an RG-2 field that was unreadable, unless the decision's table gives the value; a
    null new number changes nothing. Corrected values read as clean and legible."""
    out = copy.deepcopy(report)
    nums = out["numbers"]
    applied: list[dict] = []
    unverified: list[str] = []
    used: list[dict] = []

    def put(item: str, value: int | None, table: bool) -> bool:
        if value is None:
            return False
        done = {"value": value, "legible": True, "mark": "clean", "note": "corrected by RG-7",
                "original_value": None}
        if item in RG7_ITEMS:
            f = _dash_zero(nums[RG7_ITEMS[item]])
            if not table and not _ok(f):
                return False
            nums[RG7_ITEMS[item]] = done
            return True
        if item.startswith("list:") and item[5:].isdigit():
            row = int(item[5:])
            old = next((r for r in nums["list_votes"] if r["row"] == row), None)
            if old is not None:
                if not table and not _ok(_dash_zero(old["field"])):
                    return False
                old["field"] = done
            elif table:
                nums["list_votes"] = sorted([*nums["list_votes"], {"row": row, "field": done}],
                                            key=lambda r: r["row"])
            else:
                return False
            return True
        return False

    for dec in decisions:
        rep = dec["rep"]
        if rep["confidence"] == "low":
            unverified.append(f"{dec['d']}: low-confidence read")
            continue
        if bad := rg7_mismatch(rep, report, used):
            unverified.append(f"{dec['d']}: " + "; ".join(bad))
            continue
        used.append(rep)
        ch = [change_text(c) for c in rep["changes"] if put(c["item"], c["new"], False)]
        for e in rep["table"]:
            before = rg2_value(out, e["item"])
            if put(e["item"], e["value"], True) and before != e["value"]:
                ch.append(f"{e['item']} table {e['value']}")
        if ch:
            applied.append({"d": dec["d"], "ch": ch, **({"u": dec["u"]} if "u" in dec else {})})
    return out, applied, unverified


def extract(report: dict, doc: Doc, cfg: Config, rg7: list[dict] | None = None) -> dict:
    """The counted numbers of one report (None = not readable) plus what makes it unreliable.
    With `rg7` decisions the corrected numbers are counted and the arithmetic rules re-run on them;
    the physical alerts (V, P) of the original RG-2 stay."""
    original = report
    applied, unverified = [], []
    if rg7:
        try:
            report, applied, unverified = apply_rg7(report, rg7)
        except Exception:  # the form stays counted on its RG-2 numbers
            log.warning("RG-7 corrections of %s %s could not be applied", doc.municipality,
                        doc.station_no, exc_info=True)
            report, applied, unverified = original, [], []
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
    found = evaluate_report(report, doc, cfg)  # the rules re-applied to the stored report (no D-rules)
    arith = sorted({f.rule for f in found if f.rule in ARITHMETIC})
    high = {f.rule for f in found if f.severity == "HIGH"}
    if applied:  # arithmetic from the corrected numbers, everything else from the RG-2 as filed
        high = {r for r in high if r in ARITHMETIC} | {
            f.rule for f in evaluate_report(original, doc, cfg)
            if f.severity == "HIGH" and f.rule not in ARITHMETIC}
    high = sorted(high)
    return {"n": n, "l": lists, "bad": bad, "conf": report["confidence"],
            "pq": report["visual"]["page_quality"], "arith": arith, "high": high,
            "rg7": applied, "rg7_bad": unverified}


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
    if ex["rg7_bad"]:
        out.append("unverified RG-7 correction, RG-2 numbers kept (" + "; ".join(ex["rg7_bad"]) + ")")
    if ex["arith"]:
        out.append("arithmetic " + ", ".join(ex["arith"]))
    if ex["high"]:
        out.append("HIGH alert: " + ", ".join(ex["high"]))
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
    corr: dict[str, list[dict]] = {}  # station_key -> RG-7 decisions in decision order
    for row, rep in store.rg7_reports(parent, model, RG7_PROMPT_VERSION):
        if row["doc_type"] in RG7_FIRST_ROUND and row["station_key"] is not None:
            corr.setdefault(row["station_key"], []).append(
                ({"d": decision_label(row["uploaded_at"], rep), "rep": rep, "u": row["url"]},
                 decision_sort_key(row["uploaded_at"], row["id"], rep)))
    corr = {k: [d for d, _ in sorted(v, key=lambda x: x[1])] for k, v in corr.items()}
    sigs = {k: hash(json.dumps(v, sort_keys=True)) for k, v in corr.items()}

    def key_of(a) -> object:
        sig = sigs.get(docobj[a["document_id"]].station_key) if a["document_id"] in docobj else None
        return a["id"] if sig is None else (a["id"], sig)

    docobj = {d["id"]: _doc(d) for d in mains}
    need = [a["id"] for d in mains for m, a in by_doc.get(d["id"], {}).items()
            if key_of(a) not in cache and (m == model or a["prompt_version"] == PROMPT_VERSION)]
    reports = store.analysis_reports(need)

    def ex_of(a) -> dict | None:
        k = key_of(a)
        if k not in cache:
            doc = docobj[a["document_id"]]
            try:
                cache[k] = extract(json.loads(reports[a["id"]]), doc, cfg, corr.get(doc.station_key))
            except Exception:
                log.warning("stored analysis %s could not be read for the tally", a["id"], exc_info=True)
                cache[k] = None
        return cache[k]

    live = {a["id"] for a in arows} | {key_of(a) for a in arows}
    for gone in set(cache) - live:
        del cache[gone]
    forms, unanalysed = [], {}
    for d in mains:
        muni = d["municipality"] or UNKNOWN_MUNI
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
    scope, scope_note = scope_of(store, cfg, parent)
    national = scope == "national"
    by_muni: dict[str, list] = {}
    for f in forms:
        by_muni.setdefault(f["muni"], []).append(f)
    if national:
        rows = expected_rows([f["ex"] for f in forms])
        rows_of = {m: rows for m in by_muni}
    else:  # every municipality has its own ballot: its own modal row set
        rows_of = {m: expected_rows([f["ex"] for f in fs]) for m, fs in by_muni.items()}
    info: dict[str, dict] = {}  # per ballot ("" = the national one): nlists, names, names_note

    def ballot(muni: str, nrows: int) -> dict:
        key = "" if national else muni
        if key not in info:
            conf, shape = configured_names(cfg, parent, scope, None if national else muni)
            note = shape
            if not note and conf and nrows and len(conf) != nrows:
                note = f"list_names has {len(conf)} names, forms have {nrows} lists - check config"
            if note and (parent, key, len(conf), nrows) not in _warned:
                _warned.add((parent, key, len(conf), nrows))
                log.warning("%s (election %s%s)", note, parent, "" if national else f", {muni}")
            info[key] = {"nlists": nrows, "names": [list_name(conf, r) for r in range(1, nrows + 1)],
                         "names_note": note}
        return info[key]

    notes = []
    for f in forms:
        f["rows"] = rows_of[f["muni"]]
        dis, note = disagreements(f["ex"], f["others"], model)
        f["why"] = reasons(f["ex"], dis, f["rows"])
        notes += [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"], "note": n} for n in note]
    stations = {m: n for m, n in store.station_counts(parent).items()
                if municipalities is None or m in municipalities}
    names = sorted({*by_muni, *unanalysed, *stations})
    nl = len(rows) if national else 0
    overall = {"all": _acc(nl), "cl": _acc(nl)}
    counts = {"stations": 0, "counted": 0, "clean": 0, "unanalysed": 0}
    munis = []
    for name in names:
        mine = by_muni.get(name, [])
        # "(unknown)" in municipal scope may mix ballots: shared totals only, no list totals
        mixed = not national and name == UNKNOWN_MUNI
        mrows = rows if national else [] if mixed else rows_of.get(name, [])
        acc = {"all": _acc(len(mrows)), "cl": _acc(len(mrows))}
        for f in mine:
            for key, take in (("all", True), ("cl", not f["why"])):
                if take:
                    _add(acc[key], f["ex"], mrows)
                    _add(overall[key], f["ex"], rows if national else [])
        row = {"m": name, "stations": stations.get(name, 0), "counted": len(mine),
               "corrected": sum(1 for f in mine if f["ex"]["rg7"]),
               "clean": sum(1 for f in mine if not f["why"]), "unanalysed": unanalysed.get(name, 0),
               "all": _tot(acc["all"]), "cl": _tot(acc["cl"])}
        if mixed:
            row.update({"nlists": 0, "names": [], "names_note": UNKNOWN_MUNI_NOTE})
            for key in ("all", "cl"):
                del row[key]["lists"], row[key]["lists_valid"]
        elif not national:
            row.update(ballot(name, len(mrows)) if mine else {"nlists": 0, "names": [], "names_note": None})
        row["excluded"] = row["counted"] - row["clean"]
        for k in counts:
            counts[k] += row[k]
        munis.append(row)
    total = {"m": "(all)", **counts, "corrected": sum(r["corrected"] for r in munis), "all": _tot(overall["all"]), "cl": _tot(overall["cl"])}
    if not national:
        for key in ("all", "cl"):
            del total[key]["lists"], total[key]["lists_valid"]
    total["excluded"] = total["counted"] - total["clean"]
    e = store.get_election(parent)
    # top-level nlists/names/names_note describe the national ballot; in municipal scope they are
    # empty and each municipality row carries its own
    top = ballot("", len(rows)) if national else {"nlists": 0, "names": [], "names_note": None}
    return {"model": model, "parent": parent, "title": e.title if e else parent, "scope": scope,
            "scope_note": scope_note, **top, "munis": munis, "overall": total, "notes": notes,
            "corrected": [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
                           "rg7": f["ex"]["rg7"]} for f in forms if f["ex"]["rg7"]],
            "excluded": [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
                          "why": f["why"]} for f in forms if f["why"]]}


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.1%}"


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    w = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    line = lambda r: "  ".join(c.ljust(w[i]) if i == 0 else c.rjust(w[i]) for i, c in enumerate(r))
    return [line(head), *map(line, rows)]


def _list_table(names: list[str], rows: list[dict], key: str) -> list[str]:
    body = [[r["m"], *(f"{v:,} ({_pct(v / r[key]['lists_valid'] if r[key]['lists_valid'] else None)})"
                       for v in r[key]["lists"])] for r in rows]
    return _table(["municipality", *names], body)


def format_tally(t: dict, all_forms: bool = True) -> str:
    """Plain-text report of build_tally(): totals, list votes, flagged forms."""
    key = "all" if all_forms else "cl"
    rows = [*t["munis"], t["overall"]]
    out = [f"UNOFFICIAL - numbers read by {t['model']} from the scanned forms, not RIK results",
           f"{t['title']} ({t['parent']}): "
           + ("all counted forms" if all_forms else "clean forms only (leave out flagged)"), ""]
    head = ["municipality", "stations", "counted", "clean", "flagged", "cover", "registered", "voted",
            "turnout", "in box", "invalid", "valid", "outside"]
    body = [[r["m"], str(r["stations"]), str(r["counted"]) + ("*" if r["corrected"] else ""), str(r["clean"]), str(r["excluded"]),
             _pct(r["counted"] / r["stations"] if r["stations"] else None),
             *(f"{r[key][k]:,}" for k in ("registered", "voted")), _pct(r[key]["turnout"]),
             *(f"{r[key][k]:,}" for k in ("ballots_in_box", "invalid", "valid", "outside_voted"))]
            for r in rows]
    out += _table(head, body)
    if t["scope"] == "national":
        if t["nlists"]:
            out += ["", "votes per list (share of valid votes)"]
            out += _list_table(t["names"], rows, key)
        if t["names_note"]:
            out += ["", "warning: " + t["names_note"]]
    else:
        if t["scope_note"]:
            out += ["", "warning: " + t["scope_note"]]
        for r in t["munis"]:
            if r["nlists"]:
                out += ["", f"{r['m']}: votes per list (share of valid votes)"]
                out += _list_table(r["names"], [r], key)
                if r["names_note"]:
                    out += ["warning: " + r["names_note"]]
            elif r["names_note"]:
                out += ["", f"{r['m']}: warning: {r['names_note']}"]
    pending = t["overall"]["unanalysed"]
    out += ["", f"{t['overall']['counted']} form(s) counted, {t['overall']['clean']} clean, "
            f"{t['overall']['excluded']} flagged, {pending} not analysed by {t['model']}"]
    if t["corrected"]:
        out += ["", "* corrected by RG-7 decisions (corrected numbers counted):"]
        out += [f"  {c['m']} BM {c['no']}: " + "; ".join(f"RG-7 {d['d']}: {', '.join(d['ch'])}" for d in c["rg7"])
                for c in t["corrected"]]
    if t["notes"]:
        out += ["", "notes (not flagged):"] + [f"  {n['m']} BM {n['no']}: {n['note']}" for n in t["notes"]]
    if t["excluded"]:
        out += ["", "flagged forms (reason):"]
        out += [f"  {e['m']} BM {e['no']}: " + "; ".join(e["why"]) for e in t["excluded"]]
    return "\n".join(out)
