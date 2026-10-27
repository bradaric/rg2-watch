"""Unofficial count read from the forms: totals of what the watch model read from each RG-2.
Never RIK's official results; numbers are model readings and are marked as such."""
import copy
import json
import logging
from collections import Counter

from .config import Config
from .models import Doc
from .names import to_cyrillic
from .operator_corrections import OperatorCorrections, apply_operator_snapshot
from .pipeline import PROMPT_VERSION, RG7_PROMPT_VERSION
from .rules import (RG7_FIELDS, _dash_zero, _list_zero, _ok, change_text, decision_label,
                     decision_sort_key, evaluate_report, repeats, rg2_value, rg7_field, rg7_mismatch)
from .status import _rik_link
from .store import Store, _doc

log = logging.getLogger("rg2watch.tally")
COUNTED = ("registered", "voted", "ballots_in_box", "invalid", "valid", "outside_voted")
ARITHMETIC = ("A1", "A2", "A3", "A4", "A5")
RG7_FIRST_ROUND = (19, 20, 21, 22)  # decisions correcting a first-round RG-2
FIRST_ROUND = 6  # repeat-vote forms (10, 15, 16) are another round: never mixed into the totals


TALLY_WARNING_TEMPLATES = {
    "unknown_type": "unknown election type - set tally_scope",
    "unknown_municipality": "municipality unknown - forms may be from different ballots, no list totals",
    "empty_ballot": "no expected ballot rows - comparable list totals unavailable",
    "national_names_shape": "list_names must be a plain list for a national election",
    "municipal_names_shape": "list_names must map municipality -> names for a municipal election",
    "names_count": "list_names has {names} names, forms have {lists} lists - check config",
}
UNKNOWN_TYPE_NOTE = TALLY_WARNING_TEMPLATES["unknown_type"]
UNKNOWN_MUNI = "(unknown)"
UNKNOWN_MUNI_NOTE = TALLY_WARNING_TEMPLATES["unknown_municipality"]
EMPTY_BALLOT_NOTE = TALLY_WARNING_TEMPLATES["empty_ballot"]


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
        return (v, None) if isinstance(v, list) else ([], TALLY_WARNING_TEMPLATES["national_names_shape"])
    if isinstance(v, list):
        return [], TALLY_WARNING_TEMPLATES["municipal_names_shape"]
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
        item = rg7_field(item)
        if value is None:
            return False
        done = {"value": value, "legible": True, "mark": "clean", "note": "corrected by RG-7",
                "original_value": None}
        if item in RG7_FIELDS:
            f = _dash_zero(nums[item])
            if not table and not _ok(f):
                return False
            nums[item] = done
            return True
        if item.startswith("list:") and item[5:].isdigit():
            row = int(item[5:])
            old = next((r for r in nums["list_votes"] if r["row"] == row), None)
            if old is not None:
                if not table and not _ok(_list_zero(old["field"])):
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
        ch = [change_text(c) for c in rep["changes"]
              if not repeats(c, used[:-1]) and put(c["item"], c["new"], False)]
        for e in rep["table"]:
            before = rg2_value(out, e["item"])
            if put(e["item"], e["value"], True) and before != e["value"]:
                ch.append(f"{e['item']} table {e['value']}")
        if ch:
            applied.append({"d": dec["d"], "ch": ch, **({"u": dec["u"]} if "u" in dec else {})})
    return out, applied, unverified


def extract(report: dict, doc: Doc, cfg: Config, rg7: list[dict] | None = None,
            operator_snapshot: dict | None = None) -> dict:
    """The counted numbers of one report (None = not readable) plus what makes it unreliable.
    Corrections are counted and arithmetic is re-run. RG-7 keeps physical alerts from the filed RG-2;
    a validated operator snapshot clears alerts tied to the replaced numeric fields."""
    original = report
    applied, unverified = [], []
    if rg7:
        try:
            report, applied, unverified = apply_rg7(report, rg7)
        except Exception:  # the form stays counted on its RG-2 numbers
            log.warning("RG-7 corrections of %s %s could not be applied", doc.municipality,
                        doc.station_no, exc_info=True)
            report, applied, unverified = original, [], []
    if operator_snapshot is not None:
        report = apply_operator_snapshot(report, operator_snapshot)
    n, lists, bad = _numbers(report)
    found = evaluate_report(report, doc, cfg)  # the rules re-applied to the stored report (no D-rules)
    arith = sorted({f.rule for f in found if f.rule in ARITHMETIC})
    arith_details = {f.rule: f.summary for f in found if f.rule in ARITHMETIC}
    raw_nums = {k: _dash_zero(v) for k, v in report["numbers"].items() if k != "list_votes"}
    rec_bad = any(not _ok(raw_nums[k]) for k in ("registered", "voted", "ballots_in_box", "invalid", "valid"))
    rec_bad = rec_bad or any(not _ok(_list_zero(x["field"])) for x in report["numbers"]["list_votes"])
    rec_bad = rec_bad or any(not raw_nums[k]["legible"] and raw_nums[k]["mark"] != "empty"
                             for k in ("outside_voted", "outside_confirmations"))
    rec_bad = rec_bad or (_ok(raw_nums["outside_voted"]) and not _ok(raw_nums["outside_confirmations"]))
    high = {f.rule for f in found if f.severity == "HIGH"}
    filed = None  # the RG-2 as filed, when corrections changed anything: n, l, bad, arith, high
    if applied or operator_snapshot is not None:
        orig = evaluate_report(original, doc, cfg)
        fn, fl, fb = _numbers(original)
        filed = {"n": fn, "l": fl, "bad": fb, "arith": sorted({f.rule for f in orig if f.rule in ARITHMETIC}),
                 "high": sorted({f.rule for f in orig if f.severity == "HIGH"})}
        if operator_snapshot is None:  # RG-7 does not erase physical alerts on the filed RG-2
            high = {r for r in high if r in ARITHMETIC} | {
                f.rule for f in orig if f.severity == "HIGH" and f.rule not in ARITHMETIC}
    high = sorted(high)
    return {"n": n, "l": lists, "bad": bad, "conf": report["confidence"],
            "pq": report["visual"]["page_quality"], "arith": arith, "high": high,
            "arith_details": arith_details, "rec_bad": rec_bad,
            "rg7": applied, "rg7_bad": unverified, "operator": operator_snapshot is not None,
            "filed": filed}


def _numbers(report: dict) -> tuple[dict, dict, list[str]]:
    """(counted numbers, list votes by row, what is illegible or empty) of one report."""
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
        f = _list_zero(r["field"])
        lists[r["row"]] = f["value"] if _ok(f) else None
        if not _ok(f):
            bad.append(f"list row {r['row']} {'illegible' if not f['legible'] else 'empty'}")
    return n, lists, bad


def as_filed(ex: dict) -> dict:
    """The extract judged on the RG-2 alone: no corrections, so no unverified-correction flag."""
    return {**ex, **(ex.get("filed") or {}), "rg7": [], "rg7_bad": [], "operator": False,
            "filed": None}


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
    return {**{k: 0 for k in COUNTED}, "vote_forms": 0, "withheld_valid": 0,
            "lists": [0] * nlists, "lv": 0, "lb": 0, "lb_complete": True,
            "tv": 0, "tr": 0}


def _contrib(ex: dict, rows: list[int]) -> dict:
    """What one form adds to a totals block, by term. Every term is only counted when all its numbers
    are known, so ratios share a denominator: registered and voted ("rv") come from forms with both,
    list votes, valid, and ballots in the box ("lists") from forms with every list row (of the
    expected set) and valid. A missing ballots-in-box value makes that cohort's share denominator
    unavailable rather than silently narrowing the cohort."""
    n = ex["n"]
    c = {k: n[k] for k in ("ballots_in_box", "outside_voted") if n[k] is not None}
    if n["voted"] is not None and n["registered"] is not None:
        c["rv"] = (n["registered"], n["voted"])
    vals = [ex["l"].get(r) for r in rows]
    eligible = (bool(rows) and sorted(ex["l"]) == rows and None not in vals
                and n["valid"] is not None and sum(vals) == n["valid"])
    if eligible:
        c["valid"] = n["valid"]
        c["lists"] = (vals, n["valid"], n["ballots_in_box"])
        c["vote_forms"] = 1
        if n["invalid"] is not None:
            c["invalid"] = n["invalid"]
    elif n["valid"] is not None:
        c["withheld_valid"] = n["valid"]
    return c


def _result_inclusion(ex: dict, rows: list[int]) -> dict:
    """Whether one form contributes reconciled list totals, with an operator-facing reason."""
    if not rows:
        return {"included": False, "reason": "expected list rows are unavailable"}
    if sorted(ex["l"]) != rows:
        return {"included": False,
                "reason": f"list rows differ (got {len(ex['l'])}, expected {len(rows)})"}
    missing = [r for r in rows if ex["l"].get(r) is None]
    if missing:
        details = [b for b in ex["bad"] if any(b.startswith(f"list row {r} ") for r in missing)]
        return {"included": False, "reason": ", ".join(details) or
                "list rows unavailable: " + ", ".join(map(str, missing))}
    valid = ex["n"]["valid"]
    if valid is None:
        detail = next((b for b in ex["bad"] if b.startswith("valid ")), "valid unavailable")
        return {"included": False, "reason": detail}
    total = sum(ex["l"][r] for r in rows)
    if total != valid:
        return {"included": False, "reason": f"list votes sum {total} != valid {valid}"}
    return {"included": True, "reason": None}


def _put(acc: dict, c: dict, terms=None) -> None:
    for t, v in c.items():
        if terms is not None and t not in terms:
            continue
        if t == "rv":
            acc["registered"] += v[0]
            acc["voted"] += v[1]
        elif t == "lists":
            acc["lists"] = [a + x for a, x in zip(acc["lists"], v[0])]
            acc["lv"] += v[1]
            if v[2] is None:
                acc["lb_complete"] = False
            else:
                acc["lb"] += v[2]
        else:
            acc[t] += v


def _add(acc: dict, ex: dict, rows: list[int]) -> None:
    _put(acc, _contrib(ex, rows))


def _tot(acc: dict) -> dict:
    return {**{k: acc[k] for k in COUNTED},
            **{k: acc[k] for k in ("vote_forms", "withheld_valid")},
            "lists": acc["lists"], "lists_valid": acc["lv"],
            "lists_ballots_in_box": acc["lb"] if acc["lb_complete"] else None,
            "turnout": acc["voted"] / acc["registered"] if acc["registered"] else None}


def _pair_acc(nlists: int) -> dict:
    return {"c": _acc(nlists), "f": _acc(nlists), "in": 0, "out": 0,
            "pair_selected": 0, "pair_vote_forms": 0,
            "corrected_only_vote_forms": 0, "filed_only_vote_forms": 0}


def _paired_terms(cc: dict, cf: dict) -> set[str]:
    """Terms comparable on both sides, keeping reconciled vote terms atomic."""
    both = cc.keys() & cf.keys()
    vote_terms = {"valid", "lists", "vote_forms"}
    if not vote_terms <= both:
        both -= vote_terms | {"invalid"}
    return both


def _pair_tot(p: dict) -> dict:
    missing = p["pair_selected"] - p["pair_vote_forms"]
    assert p["pair_vote_forms"] <= p["pair_selected"]
    assert p["corrected_only_vote_forms"] + p["filed_only_vote_forms"] <= missing
    return {"c": _tot(p["c"]), "f": _tot(p["f"]), "in": p["in"], "out": p["out"],
            "pair_selected": p["pair_selected"], "pair_vote_forms": p["pair_vote_forms"],
            "pair_not_contributing": missing,
            "corrected_only_vote_forms": p["corrected_only_vote_forms"],
            "filed_only_vote_forms": p["filed_only_vote_forms"]}


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
                docs=None, arows=None, include_station_results: bool = False,
                expected_rows_cache: dict | None = None) -> dict:
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
    held_ids = {r["document_id"] for r in store.latest_reviews(parent)
                if r["active"] and not r["superseded"] and r["verdict"] == "escalate"}
    annulled = {d["station_key"]: d for d in store.active_rg9(parent)
                if d["station_key"] is not None}
    corr: dict[str, list[dict]] = {}  # station_key -> RG-7 decisions in decision order
    for row, rep in store.rg7_reports(parent, model, RG7_PROMPT_VERSION):
        if row["doc_type"] in RG7_FIRST_ROUND and row["station_key"] is not None:
            corr.setdefault(row["station_key"], []).append(
                ({"d": decision_label(row["uploaded_at"], rep), "rep": rep, "u": row["url"]},
                 decision_sort_key(row["uploaded_at"], row["id"], rep)))
    corr = {k: [d for d, _ in sorted(v, key=lambda x: x[1])] for k, v in corr.items()}
    sigs = {k: hash(json.dumps(v, sort_keys=True)) for k, v in corr.items()}

    latest_operator = {r["url"]: r for r in store.latest_operator_corrections(parent)}
    operator_states = {}
    if latest_operator:
        service = OperatorCorrections(store, cfg, model,
                                      expected_rows_cache=expected_rows_cache)
        for d in mains:
            if d["url"] not in latest_operator:
                continue
            source = by_doc.get(d["id"], {}).get(model)
            if source is None or source["prompt_version"] != PROMPT_VERSION:
                continue
            try:
                operator_states[d["id"]] = service.state(d["url"])
            except Exception:
                log.warning("operator correction state of %s could not be read", d["url"], exc_info=True)

    def key_of(a) -> object:
        sig = sigs.get(docobj[a["document_id"]].station_key) if a["document_id"] in docobj else None
        op = operator_states.get(a["document_id"])
        parts = [a["id"]]
        if sig is not None:
            parts.append(sig)
        if op is not None:
            parts.append(op["source_token"])
        return parts[0] if len(parts) == 1 else tuple(parts)

    docobj = {d["id"]: _doc(d) for d in mains}
    need = [a["id"] for d in mains for m, a in by_doc.get(d["id"], {}).items()
            if key_of(a) not in cache and (m == model or a["prompt_version"] == PROMPT_VERSION)]
    reports = store.analysis_reports(need)

    def ex_of(a) -> dict | None:
        k = key_of(a)
        if k not in cache:
            doc = docobj[a["document_id"]]
            try:
                op = operator_states.get(a["document_id"])
                cache[k] = extract(json.loads(reports[a["id"]]), doc, cfg, corr.get(doc.station_key),
                                   op["active_snapshot"] if op and op["status"] == "active" else None)
            except Exception:
                log.warning("stored analysis %s could not be read for the tally", a["id"], exc_info=True)
                cache[k] = None
        return cache[k]

    live = {a["id"] for a in arows} | {key_of(a) for a in arows}
    for gone in set(cache) - live:
        del cache[gone]
    forms, unanalysed, muni_sources = [], {}, {}
    for d in mains:
        muni = d["municipality"] or UNKNOWN_MUNI
        if municipalities is not None and muni not in municipalities:
            continue
        muni_sources.setdefault(muni, []).append(d["municipality"])
        got = by_doc.get(d["id"], {})
        mine = ex_of(got[model]) if model in got else None
        if mine is None:
            unanalysed[muni] = unanalysed.get(muni, 0) + 1
            continue
        others = {m: e for m, a in got.items() if m != model and a["prompt_version"] == PROMPT_VERSION
                  and (e := ex_of(a)) is not None}
        forms.append({"d": d, "muni": muni, "ex": mine, "others": others,
                      "held": d["id"] in held_ids,
                      "annulled": annulled.get(d["station_key"]),
                      "operator": operator_states.get(d["id"])})
    scope, scope_note = scope_of(store, cfg, parent)
    e = store.get_election(parent)
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
            note = "; ".join(x for x in (shape, EMPTY_BALLOT_NOTE if not nrows else None) if x) or None
            if not note and conf and nrows and len(conf) != nrows:
                note = TALLY_WARNING_TEMPLATES["names_count"].format(names=len(conf), lists=nrows)
            if note and (parent, key, len(conf), nrows) not in _warned:
                _warned.add((parent, key, len(conf), nrows))
                log.warning("%s (election %s%s)", note, parent, "" if national else f", {muni}")
            info[key] = {"nlists": nrows, "names": [list_name(conf, r) for r in range(1, nrows + 1)],
                         "fallback_name_indices": [r - 1 for r in range(1, nrows + 1)
                                                   if not (r <= len(conf) and conf[r - 1].strip())],
                         "names_note": note}
        return info[key]

    notes = []
    for f in forms:
        f["annulment"] = ({"rik": _rik_link(f["annulled"]["url"], e.base_url if e else None)}
                           if f["annulled"] is not None else None)
        f["rows"] = rows_of[f["muni"]]
        dis, note = disagreements(f["ex"], f["others"], model)
        f["why"] = reasons(f["ex"], dis, f["rows"])
        fdis, _ = disagreements(as_filed(f["ex"]), {m: as_filed(e) for m, e in f["others"].items()}, model)
        f["why_f"] = reasons(as_filed(f["ex"]), fdis, f["rows"])
        if f["annulled"] is not None:
            f["why"].append("annulled by RG-9")
            f["why_f"].append("annulled by RG-9")
        contribution = _contrib(f["ex"], f["rows"])
        turnout_gap = (contribution.get("rv", (0, 0))[1] - contribution.get("valid", 0)
                       - contribution.get("invalid", 0))
        unavailable = (f["ex"]["rec_bad"] or not f["rows"]
                       or sorted(f["ex"]["l"]) != f["rows"])
        status = "fail" if f["ex"]["arith"] else "unavailable" if unavailable else "ok"
        inclusion = ({"included": False, "reason": "annulled by RG-9"}
                     if f["annulled"] is not None else
                     {"included": False, "reason": "held by escalation"} if f["held"] else None)
        f["rec"] = {"status": status, "rules": f["ex"]["arith"],
                     "details": [f["ex"]["arith_details"][r] for r in f["ex"]["arith"]],
                     "gap": turnout_gap,
                     "result": {"corrected": inclusion or _result_inclusion(f["ex"], f["rows"]),
                                "filed": inclusion or _result_inclusion(
                                    as_filed(f["ex"]), f["rows"])}}
        notes += [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"], "note": n} for n in note]
    stations = {m: n for m, n in store.station_counts(parent).items()
                if municipalities is None or m in municipalities}
    names = sorted({*by_muni, *unanalysed, *stations})
    nl = len(rows) if national else 0
    overall = {k: _acc(nl) for k in ("all", "cl", "fall", "fcl")}  # f*: as filed on the RG-2s
    # like-for-like difference: each term only over the stations present in it in BOTH views
    opair = {k: _pair_acc(nl) for k in ("all", "cl")}
    counts = {"stations": 0, "counted": 0, "clean": 0, "held": 0, "annulled": 0,
              "held_annulled": 0,
              "held_clean": 0, "held_fclean": 0, "unanalysed": 0}
    munis = []
    for name in names:
        mine = by_muni.get(name, [])
        # "(unknown)" in municipal scope may mix ballots: shared totals only, no list totals
        mixed = not national and name == UNKNOWN_MUNI
        mrows = rows if national else [] if mixed else rows_of.get(name, [])
        acc = {k: _acc(len(mrows)) for k in overall}
        pair = {k: _pair_acc(len(mrows)) for k in opair}
        for f in mine:
            if f["held"] or f["annulled"] is not None:
                continue
            filed = as_filed(f["ex"])
            for key, take_c, take_f in (("all", True, True), ("cl", not f["why"], not f["why_f"])):
                cc = _contrib(f["ex"], mrows) if take_c else {}
                cf = _contrib(filed, mrows) if take_f else {}
                both = _paired_terms(cc, cf)
                _put(pair[key]["c"], cc, both)
                _put(pair[key]["f"], cf, both)
                _put(opair[key]["c"], cc, both if national else both - {"lists"})
                _put(opair[key]["f"], cf, both if national else both - {"lists"})
                if take_c and take_f:
                    c_vote, f_vote = "vote_forms" in cc, "vote_forms" in cf
                    for p in (pair[key], opair[key]):
                        p["pair_selected"] += 1
                        p["pair_vote_forms"] += int(c_vote and f_vote)
                        p["corrected_only_vote_forms"] += int(c_vote and not f_vote)
                        p["filed_only_vote_forms"] += int(f_vote and not c_vote)
                joined, left = int(bool(cc.keys() - cf.keys())), int(bool(cf.keys() - cc.keys()))
                pair[key]["in"] += joined
                pair[key]["out"] += left
                opair[key]["in"] += joined
                opair[key]["out"] += left
            for key, take, ex in (("all", True, f["ex"]), ("cl", not f["why"], f["ex"]),
                                  ("fall", True, as_filed(f["ex"])), ("fcl", not f["why_f"], as_filed(f["ex"]))):
                if take:
                    contribution = _contrib(ex, mrows)
                    _put(acc[key], contribution)
                    _put(overall[key], contribution, None if national else contribution.keys() - {"lists"})
        row = {"m": name, "stations": stations.get(name, 0), "counted": len(mine),
               "generated_unknown_muni": bool(muni_sources.get(name))
               and all(not source for source in muni_sources[name]),
               "corrected": sum(1 for f in mine if f["ex"]["rg7"]),
               "operator_corrected": sum(1 for f in mine if f["ex"]["operator"]),
               "clean": sum(1 for f in mine if not f["why"]),
               "held": sum(1 for f in mine if f["held"]),
               "annulled": sum(1 for f in mine if f["annulled"] is not None),
               "held_annulled": sum(1 for f in mine if f["held"] and f["annulled"] is not None),
               "held_clean": sum(1 for f in mine if f["held"] and not f["why"]),
               "held_fclean": sum(1 for f in mine if f["held"] and not f["why_f"]),
               "unanalysed": unanalysed.get(name, 0),
               "fclean": sum(1 for f in mine if not f["why_f"]),
               **{k: _tot(acc[k]) for k in acc},
               "pair": {k: _pair_tot(p) for k, p in pair.items()}}
        if mixed:
            row.update({"nlists": 0, "names": [], "fallback_name_indices": [],
                        "names_note": UNKNOWN_MUNI_NOTE})
            for key in acc:
                del row[key]["lists"], row[key]["lists_valid"], row[key]["lists_ballots_in_box"]
            for p in row["pair"].values():
                del p["c"]["lists"], p["c"]["lists_valid"], p["c"]["lists_ballots_in_box"]
                del p["f"]["lists"], p["f"]["lists_valid"], p["f"]["lists_ballots_in_box"]
        elif not national:
            row.update(ballot(name, len(mrows)) if mine else
                       {"nlists": 0, "names": [], "fallback_name_indices": [], "names_note": None})
        row["excluded"] = row["counted"] - row["clean"]
        row["fexcluded"] = row["counted"] - row["fclean"]
        for k in counts:
            counts[k] += row[k]
        munis.append(row)
    total = {"m": "(all)", "generated_unknown_muni": False, **counts,
             "corrected": sum(r["corrected"] for r in munis), "fclean": sum(r["fclean"] for r in munis),
             "operator_corrected": sum(r["operator_corrected"] for r in munis),
             **{k: _tot(overall[k]) for k in overall},
              "pair": {k: _pair_tot(p) for k, p in opair.items()}}
    if not national:
        for key in overall:
            del total[key]["lists"], total[key]["lists_valid"], total[key]["lists_ballots_in_box"]
        for p in total["pair"].values():
            del p["c"]["lists"], p["c"]["lists_valid"], p["c"]["lists_ballots_in_box"]
            del p["f"]["lists"], p["f"]["lists_valid"], p["f"]["lists_ballots_in_box"]
    total["excluded"] = total["counted"] - total["clean"]
    total["fexcluded"] = total["counted"] - total["fclean"]
    # top-level nlists/names/names_note describe the national ballot; in municipal scope they are
    # empty and each municipality row carries its own
    top = ballot("", len(rows)) if national else {
        "nlists": 0, "names": [], "fallback_name_indices": [], "names_note": None}

    def station_view(ex: dict, expected: list[int], why: list[str], inclusion: dict) -> dict:
        return {"clean": not why, **inclusion, **{key: ex["n"][key] for key in COUNTED},
                "lists": [ex["l"].get(row) for row in expected]}

    station_results = [] if not include_station_results else [
        {"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
         "name": f["d"]["station_name"],
         "annulled": f["annulment"],
         "corrected": station_view(f["ex"], f["rows"], f["why"],
                                   f["rec"]["result"]["corrected"]),
         "filed": (station_view(as_filed(f["ex"]), f["rows"], f["why_f"],
                                f["rec"]["result"]["filed"])
                   if f["ex"].get("filed") is not None else None)}
        for f in forms]
    return {"model": model, "parent": parent, "title": e.title if e else parent, "scope": scope,
            "scope_note": scope_note, **top, "munis": munis, "overall": total, "notes": notes,
            "station_results": station_results,
            "corrected": [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
                           "rg7": f["ex"]["rg7"]} for f in forms if f["ex"]["rg7"]],
            "operator_corrected": [{"id": f["d"]["id"], "m": f["muni"],
                                    "no": f["d"]["station_no"],
                                    "reason": f["operator"]["reason"], "at": f["operator"]["at"]}
                                   for f in forms if f["ex"]["operator"]],
            "held": [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
                      "verdict": "escalate"} for f in forms if f["held"]],
            "annulled": [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
                           **f["annulment"]} for f in forms if f["annulment"] is not None],
            "reconciliation": [{"id": f["d"]["id"], "m": f["muni"],
                                "no": f["d"]["station_no"], **f["rec"]} for f in forms],
            "excluded": [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
                          "why": f["why"]} for f in forms if f["why"]],
            "excluded_filed": [{"id": f["d"]["id"], "m": f["muni"], "no": f["d"]["station_no"],
                                "why": f["why_f"]} for f in forms if f["why_f"]]}


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.1%}"


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    w = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    line = lambda r: "  ".join(c.ljust(w[i]) if i == 0 else c.rjust(w[i]) for i, c in enumerate(r))
    return [line(head), *map(line, rows)]


def _delta(t: dict, f: dict) -> dict:
    """corrected - as filed, for one totals block (turnout in percentage points)."""
    out = {k: t[k] - f[k] for k in COUNTED}
    out.update({k: t[k] - f[k] for k in ("vote_forms", "withheld_valid")})
    out["turnout"] = None if t["turnout"] is None or f["turnout"] is None else t["turnout"] - f["turnout"]
    if "lists" in t:
        out["lists"] = [a - b for a, b in zip(t["lists"], f["lists"])]
        out["lists_valid"] = t["lists_valid"] - f["lists_valid"]
        out["lists_ballots_in_box"] = (None if t["lists_ballots_in_box"] is None
                                         or f["lists_ballots_in_box"] is None else
                                         t["lists_ballots_in_box"] - f["lists_ballots_in_box"])
    return out


def _sgn(v: int) -> str:
    return "." if v == 0 else f"{v:+,}"


def _gap(t: dict) -> int | None:
    return None if t["turnout"] is None else t["voted"] - t["valid"] - t["invalid"]


def _fgap(t: dict, diff: bool = False) -> str:
    gap = _gap(t)
    if gap is None:
        return "-"
    return _sgn(gap) if diff else ("0" if gap == 0 else f"{gap:+,}")


def _list_table(names: list[str], rows: list[dict], key: str, diff: bool = False) -> list[str]:
    if diff:
        body = [[r["m"], *(_sgn(v) for v in r[key]["lists"])] for r in rows]
    else:
        body = [[r["m"], *(f"{v:,} ({_pct(v / r[key]['lists_ballots_in_box'] if r[key]['lists_ballots_in_box'] else None)})"
                           for v in r[key]["lists"])] for r in rows]
    return _table(["municipality", *names], body)


def format_tally(t: dict, all_forms: bool = True, view: str = "corrected") -> str:
    """Plain-text report of build_tally(): totals, list votes, flagged forms. `view`: "corrected"
    (RG-7 and operator corrections counted), "filed" (the RG-2s as filed) or "diff"
    (corrected - as filed)."""
    key = "all" if all_forms else "cl"
    fkey = "f" + key
    diff = view == "diff"
    rows = [*t["munis"], t["overall"]]
    if view == "filed":
        key = fkey
        rows = [{**r, "clean": r["fclean"], "excluded": r["fexcluded"]} for r in rows]
    elif diff:
        rows = [{**r, key: _delta(r["pair"][key]["c"], r["pair"][key]["f"])} for r in rows]
    label = {"corrected": "with RG-7 and operator-entered corrections", "filed": "as filed on the RG-2 forms",
             "diff": "difference (corrected - as filed, over term-specific comparable cohorts; . = no change; turnout in percentage points)"}[view]
    out = [f"UNOFFICIAL - numbers read by {t['model']} from the scanned forms, not RIK results",
           f"{t['title']} ({t['parent']}): "
           + ("all counted forms" if all_forms else "clean forms only (leave out flagged)") + "; numbers " + label, ""]
    head = ["municipality", "stations", "counted", "clean", "flagged", "held", "annulled", "cover",
            "registered", "voted", "turnout", "in box", "invalid*", "reconciled valid", "outside"]
    if not diff:
        head += ["vote forms", "withheld valid", "gap"]
    if diff:
        head.append("terms corrected-only/filed-only")
    pp = (lambda v: "-" if v is None else f"{v * 100:+.1f}") if diff else _pct
    num = (lambda v: _sgn(v)) if diff else (lambda v: f"{v:,}")
    body = [[r["m"], str(r["stations"]), str(r["counted"]) + ("*" if (r["corrected"] or r["operator_corrected"]) and view != "filed" else ""), str(r["clean"]), str(r["excluded"]), str(r["held"]), str(r["annulled"]),
             _pct(r["counted"] / r["stations"] if r["stations"] else None),
             *(num(r[key][k]) for k in ("registered", "voted")), pp(r[key]["turnout"]),
              *(num(r[key][k]) for k in ("ballots_in_box", "invalid", "valid", "outside_voted")),
              *([] if diff else [num(r[key]["vote_forms"]), num(r[key]["withheld_valid"]),
                                 _fgap(r[key])]),
             *([f"+{r['pair'][key]['in']} / -{r['pair'][key]['out']}"] if diff else [])]
            for r in rows]
    out += _table(head, body)
    if t["scope"] == "national":
        if t["nlists"]:
            out += ["", "votes per list (share of ballots in the box for reconciled vote forms)"]
            out += _list_table(t["names"], rows, key, diff)
        if t["names_note"]:
            out += ["", "warning: " + t["names_note"]]
    else:
        if t["scope_note"]:
            out += ["", "warning: " + t["scope_note"]]
        for r in t["munis"]:
            if r["nlists"]:
                out += ["", f"{r['m']}: votes per list (share of ballots in the box for reconciled vote forms)"]
                out += _list_table(r["names"], [{**r, key: _delta(r["pair"][key]["c"], r["pair"][key]["f"])} if diff else r],
                                   key, diff)
                if r["names_note"]:
                    out += ["warning: " + r["names_note"]]
            elif r["names_note"]:
                out += ["", f"{r['m']}: warning: {r['names_note']}"]
    pending = t["overall"]["unanalysed"]
    o = rows[-1] if view == "filed" else t["overall"]
    out += ["", f"{o['counted']} form(s) counted, {o['clean']} clean, "
            f"{o['excluded']} flagged, {o['held']} held by escalation, {o['annulled']} annulled by RG-9, "
            f"{pending} not analysed by {t['model']}"]
    if diff:
        o = t["overall"]["pair"][key]
        neither = o["pair_not_contributing"] - o["corrected_only_vote_forms"] - o["filed_only_vote_forms"]
        d = _delta(o["c"], o["f"])
        out += ["", f"{o['pair_vote_forms']} of {o['pair_selected']} forms selected in both views contributed to "
                 "compared vote totals; "
                 f"{o['pair_not_contributing']} not paired ({o['corrected_only_vote_forms']} corrected-only eligible, "
                 f"{o['filed_only_vote_forms']} filed-only eligible, {neither} neither)",
                 f"Like-for-like withheld readable valid difference: {_sgn(d['withheld_valid'])}; "
                  f"unreconciled turnout gap difference: {_fgap(d, diff=True)}",
                 f"forms with terms present only in corrected/filed view: +{o['in']} / -{o['out']}; unmatched terms "
                 "are left out (readability, clean-selection, or vote-reconciliation eligibility may have changed)"]
    else:
        totals = o[key]
        selected = o["counted"] if all_forms else o["clean"]
        out += ["", f"{totals['vote_forms']} of {selected} selected forms contributed reconciled vote totals; "
                 f"{totals['withheld_valid']:,} readable valid votes withheld (known partial amount); "
                 f"reported invalid is limited to those contributing forms. Unreconciled turnout gap: "
                 f"{_fgap(totals)}"]
    if t["corrected"] and view != "filed":
        out += ["", "* corrected by RG-7 decisions (corrected numbers counted):"]
        out += [f"  {c['m']} BM {c['no']}: " + "; ".join(f"RG-7 {d['d']}: {', '.join(d['ch'])}" for d in c["rg7"])
                for c in t["corrected"]]
    if t["operator_corrected"] and view != "filed":
        out += ["", "* operator-entered corrections (corrected numbers counted):"]
        out += [f"  {c['m']} BM {c['no']}: operator correction: {c['reason']} ({c['at']})"
                for c in t["operator_corrected"]]
    if t["annulled"]:
        out += ["", "поништена бирачка места (RG-9):"]
        out += [f"  {a['m']} БМ {a['no']}: Поништено" for a in t["annulled"]]
    if t["notes"]:
        out += ["", "notes (not flagged):"] + [f"  {n['m']} BM {n['no']}: {n['note']}" for n in t["notes"]]
    flagged = t["excluded_filed"] if view == "filed" else t["excluded"]
    if flagged:
        out += ["", "flagged forms (reason):"]
        out += [f"  {e['m']} BM {e['no']}: " + "; ".join(e["why"]) for e in flagged]
    return "\n".join(out)
