import re
from datetime import date

from rg2watch.config import Config
from rg2watch.models import Doc, Finding

_BAD_MARKS = {"overwritten", "crossed_out", "correction_fluid", "erased_scratched", "digit_altered"}
_SECTIONS = ("I", "II", "III", "IV", "V")
_MAX_SUMMARY = 300
_RESULT_FIELDS = ("voted", "ballots_in_box", "invalid", "valid")
_OTHER_FIELDS = ("registered", "assisted", "outside_voted", "outside_confirmations")
_LABELS = {
    "registered": "3", "voted": "10.1", "ballots_in_box": "10.2", "invalid": "10.3",
    "valid": "10.4", "assisted": "11", "outside_voted": "12", "outside_confirmations": "12.1",
}
# Plain-language description of every finding code the watcher can emit (page tooltips and legend).
# Thresholds are the config defaults.
RULE_INFO = {
    "V1": "A result number (10.1-10.4 or a list's votes) was crossed out, overwritten, erased or altered",
    "V2": "Another number (registered, assisted, outside votes or confirmations) was crossed out or altered",
    "V3": "The committee president did not sign the form",
    "V4": "Named committee members did not sign (2 or more rows by default)",
    "V5": "Several signatures look like the same handwriting",
    "V6": "The results block is in a different pen or handwriting than the rest of the form",
    "V7": "Part of the form looks pasted over or patched",
    "V8": "A required number is empty or illegible (registered, 10.1-10.4, list votes, items 12/12.1)",
    "V9": "Pages are missing or do not belong together, or the form does not match the listed station",
    "V10": "The scan is of poor quality or unreadable, so other checks are less reliable",
    "V11": "A yes/no item has both or neither answer circled (item 14 is not checked)",
    "V12": "Large correction: a number was changed by 5 or more and by 30% or more of the original",
    "P1": "The control sheet was not found or not signed (item 9 or 9.1 = NE)",
    "P2": "Voting was interrupted (item 7 = DA)",
    "P3": "The committee recorded remarks (item 14 = DA) or wrote text in item 15",
    "P4": "Times do not add up: polls closed before 20:00, or the minutes were completed before closing",
    "A1": "More ballots in the box than people who voted (10.2 > 10.1)",
    "A2": "Invalid plus valid ballots do not equal the ballots in the box (10.3 + 10.4 != 10.2)",
    "A3": "The list votes do not add up to the valid ballots (sum of lists != 10.4)",
    "A4": "More people voted than are registered (10.1 > 3)",
    "A5": "More people voted outside the station than there are confirmations (12 > 12.1)",
    "A6": "Very high turnout (95% or more, stations with over 50 registered)",
    "A7": "One list got 90% or more of the valid votes (stations with over 50 valid votes)",
    "A8": "Many voters were assisted (more than 5% of those who voted)",
    "A9": "High share of votes cast outside the polling station (10%+ medium, 15%+ high)",
    "R1": "An RG-7 decision corrected numbers (HIGH when results 10.1-10.4 or list votes changed)",
    "R2": "An RG-7 decision's old numbers differ from what we read on that station's RG-2",
    "R3": "An RG-7 decision could not be parsed into changes",
    "D1": "A follow-up document (RG-7, RG-8, RG-9 or a complaint) was published for the station",
    "D2": "A published document was replaced by a new file",
    "D3": "The PDF could not be downloaded or read",
    "D4": "A document disappeared from RIK's listing",
    "X1": "The model analysis failed or its output could not be used",
    "X2": "Analysis gave up after repeated failures",
}

_YESNO_LABELS = {
    "interruption": "7", "control_sheet_found": "9", "control_sheet_signed": "9.1",
    "observers_present": "13", "observer_form_filled": "13.1", "committee_remarks": "14",
}


def _dash_zero(f: dict) -> dict:
    """A dash/slash in a numeric field means 0: a legible zero."""
    return {**f, "value": 0, "legible": True} if f["mark"] == "dash" else f


def _ok(f: dict) -> bool:
    return bool(f["legible"]) and f["value"] is not None


def _parse_time(text) -> int | None:
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:.hчč ]?\s*(\d{2})(?::\d{2})?\s*[hчč]?\s*", text.lower())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if h < 24 and mi < 60 else None


def _station(s) -> str | None:
    m = re.match(r"\s*(\d+)", str(s))
    return str(int(m.group(1))) if m else None


_CORE = ("registered", "voted", "ballots_in_box", "invalid", "valid")
_AFTER_MIDNIGHT = 8 * 60


def evaluate_report(report: dict, doc: Doc, cfg: Config) -> list[Finding]:
    hits: list[tuple[str, str, str]] = []

    def add(rule: str, sev: str, parts: list[str] | str) -> None:
        text = parts if isinstance(parts, str) else "; ".join(parts)
        hits.append((rule, sev, text))

    hdr, yn = report["header"], report["yesno"]
    times, sig, vis = report["times"], report["signatures"], report["visual"]
    lists = [{**r, "field": _dash_zero(r["field"])} for r in report["numbers"]["list_votes"]]
    nums = {k: _dash_zero(f) for k, f in report["numbers"].items() if k != "list_votes"}

    # unreliable reads must not produce false HIGH
    illegible_core = sum(1 for k in _CORE if not nums[k]["legible"])
    unreliable = vis["page_quality"] == "unreadable" or illegible_core * 2 > len(_CORE)
    poor = vis["page_quality"] == "poor"
    phys = "MEDIUM" if poor else "HIGH"

    # V1 / V2: a visible correction counts even when the corrected value cannot be read
    def mark_text(f: dict) -> str:
        val = f["value"] if _ok(f) else "value unreadable"
        orig = f.get("original_value")
        return f"{f['mark']} ({orig} -> {val})" if orig is not None else f"{f['mark']} ({val})"

    v1 = [f"{_LABELS[k]} {k} {mark_text(nums[k])}" for k in _RESULT_FIELDS
          if nums[k]["mark"] in _BAD_MARKS]
    v1 += [f"list row {r['row']} {mark_text(r['field'])}" for r in lists
           if r["field"]["mark"] in _BAD_MARKS]
    if v1:
        add("V1", "MEDIUM" if unreliable else "HIGH", v1)
    v2 = [f"{_LABELS[k]} {k} {mark_text(nums[k])}" for k in _OTHER_FIELDS
          if nums[k]["mark"] in _BAD_MARKS]
    if v2:
        add("V2", "MEDIUM", v2)

    # V12: large correction, one finding aggregating all fields
    labelled = [(f"item {_LABELS[k]} {k}", nums[k]) for k in _LABELS if k in nums]
    labelled += [(f"list row {r['row']}", r["field"]) for r in lists]
    v12 = []
    for label, f in labelled:
        o, v = f.get("original_value"), f["value"]
        if o is None or v is None:
            continue
        d = abs(o - v)
        if d >= cfg.large_correction_abs and d / max(o, 1) >= cfg.large_correction_share:
            v12.append(f"{label} {o} -> {v} ({(v - o) / max(o, 1):+.0%})")
    if v12:
        add("V12", "MEDIUM" if unreliable else "HIGH", v12)

    # V3-V7

    def add_phys(rule: str, text: str) -> None:
        if not unreliable:
            add(rule, phys, text)

    if not sig["president_signed"]:
        add_phys("V3", "president did not sign")
    if sig["rows_name_without_signature"] >= cfg.unsigned_rows_threshold:
        add("V4", "MEDIUM", f"{sig['rows_name_without_signature']} named committee rows without signature")
    if sig["signatures_look_same_hand"]:
        add_phys("V5", "signatures look like the same hand")
    v6 = [n for n, flag in (("pen", vis["results_block_different_pen"]),
                            ("hand", vis["results_block_different_hand"])) if flag]
    if v6:
        add_phys("V6", "results block in different " + " and ".join(v6))
    if vis["pasted_or_patched_area"]:
        add_phys("V7", "pasted or patched area")

    # V8
    v8 = [f"{_LABELS[k]} {k} {'illegible' if not nums[k]['legible'] else 'empty'}"
          for k in ("registered",) + _RESULT_FIELDS if not _ok(nums[k])]
    v8 += [f"list row {r['row']} {'illegible' if not r['field']['legible'] else 'empty'}"
           for r in lists if not _ok(r["field"])]
    v8 += [f"{_LABELS[k]} {k} illegible" for k in ("outside_voted", "outside_confirmations")
           if not nums[k]["legible"] and nums[k]["mark"] != "empty"]
    if v8:
        add("V8", "MEDIUM", v8)

    # V9
    v9 = []
    form_st, list_st = _station(hdr["station_no"]) if hdr["station_no"] is not None else None, _station(doc.station_no)
    station_bad = form_st is not None and list_st is not None and form_st != list_st
    if station_bad:
        v9.append(f"station_no on form {hdr['station_no']} differs from listing {doc.station_no}")
    if not unreliable:
        missing = [s for s in _SECTIONS if s not in hdr["sections_present"]]
        if missing:
            extra = " (no committee/signatures)" if "V" in missing else ""
            v9.append("missing sections " + ",".join(missing) + extra)
        if not hdr["pages_match_same_form"]:
            v9.append("pages do not match the same form")
    if v9:
        add("V9", "HIGH" if station_bad else phys, v9)

    if vis["page_quality"] in ("poor", "unreadable"):
        add("V10", "LOW", f"page quality {vis['page_quality']}")
    # "none" on a follow-up question is expected when its parent question is NE
    not_asked = {follow for parent, follow in (("control_sheet_found", "control_sheet_signed"),
                                               ("observers_present", "observer_form_filled"))
                 if yn[parent]["answer"] == "NE"}
    v11 = [f"{_YESNO_LABELS[k]} {k} {yn[k]['answer']}" for k in _YESNO_LABELS if k != "committee_remarks"  # item 14: operator decided it is noise
           if yn[k]["answer"] == "both" or (yn[k]["answer"] == "none" and k not in not_asked)]
    if v11:
        add("V11", "MEDIUM", v11)

    # P1-P4
    p1 = [f"{_YESNO_LABELS[k]} {k} = NE" for k in ("control_sheet_found", "control_sheet_signed")
          if yn[k]["answer"] == "NE"]
    if p1:
        add("P1", "HIGH", p1)
    if yn["interruption"]["answer"] == "DA":
        add("P2", "MEDIUM", "7 voting interrupted")
    p3 = []
    if yn["committee_remarks"]["answer"] == "DA":
        p3.append("14 committee remarks = DA")
    if report["remarks_text_present"]:
        p3.append("15 remarks text present")
    if p3:
        add("P3", "MEDIUM", p3)
    close, done = _parse_time(times["close_time"]), _parse_time(times["completed_at"])
    p4 = []
    if close is not None and close < 20 * 60:
        p4.append(f"8 close_time {times['close_time']} before 20:00")
    if close is not None and done is not None and _AFTER_MIDNIGHT <= done < close:
        p4.append(f"completed_at {times['completed_at']} before close_time {times['close_time']}")
    if p4:
        add("P4", "MEDIUM", p4)

    # A1-A8 (legible, non-null values only)
    n = {k: nums[k]["value"] for k in nums if _ok(nums[k])}
    if "ballots_in_box" in n and "voted" in n and n["ballots_in_box"] > n["voted"]:
        add("A1", "HIGH", f"ballots in box {n['ballots_in_box']} > voted {n['voted']}")
    if all(k in n for k in ("invalid", "valid", "ballots_in_box")) \
            and n["invalid"] + n["valid"] != n["ballots_in_box"]:
        add("A2", "MEDIUM", f"invalid {n['invalid']} + valid {n['valid']} != ballots in box {n['ballots_in_box']}")
    list_vals = [r["field"]["value"] for r in lists] if lists and all(_ok(r["field"]) for r in lists) else None
    if list_vals is not None and "valid" in n and sum(list_vals) != n["valid"]:
        add("A3", "MEDIUM", f"list votes sum {sum(list_vals)} != valid {n['valid']}")
    if "voted" in n and "registered" in n and n["voted"] > n["registered"]:
        add("A4", "HIGH", f"voted {n['voted']} > registered {n['registered']}")
    if "outside_voted" in n and "outside_confirmations" in n and n["outside_voted"] > n["outside_confirmations"]:
        add("A5", "MEDIUM", f"outside voted {n['outside_voted']} > confirmations {n['outside_confirmations']}")
    if "voted" in n and "registered" in n and n["registered"] > cfg.min_registered_for_ratio \
            and n["voted"] / n["registered"] >= cfg.turnout_high:
        add("A6", "MEDIUM", f"turnout {n['voted'] / n['registered']:.1%} (voted {n['voted']} of {n['registered']})")
    if list_vals and "valid" in n and n["valid"] > cfg.min_registered_for_ratio \
            and max(list_vals) >= cfg.leader_share_high * n["valid"]:
        add("A7", "MEDIUM", f"one list {max(list_vals)} of valid {n['valid']} ({max(list_vals) / n['valid']:.1%})")
    if "assisted" in n and "voted" in n and n["assisted"] > cfg.assisted_share * n["voted"]:
        add("A8", "LOW", f"assisted {n['assisted']} > {cfg.assisted_share:.0%} of voted {n['voted']}")

    if "outside_voted" in n and "voted" in n and n["voted"] > 0:
        cnt = n["outside_voted"]
        orig = nums["outside_voted"].get("original_value")
        if orig is not None and orig > cnt:
            cnt_used = orig
        else:
            cnt_used, orig = cnt, None
        share = cnt_used / n["voted"]
        text = f"outside-station voting {cnt} of {n['voted']} ({cnt / n['voted']:.1%})"
        if orig is not None:
            text += f", originally {orig} ({share:.1%})"
        if share >= cfg.outside_share_high:
            add("A9", "HIGH", text)
        elif share >= cfg.outside_share_medium and cnt_used >= 10:
            add("A9", "MEDIUM", text)

    low = report["confidence"] == "low"
    out = []
    for rule, sev, text in hits:
        suffix = " (low-confidence read)" if low else ""
        if low and sev == "HIGH":
            sev = "MEDIUM"
        if len(text) + len(suffix) > _MAX_SUMMARY:
            text = text[:_MAX_SUMMARY - len(suffix) - 3] + "..."
        text += suffix
        out.append(Finding(rule, sev, doc, text))
    return out


# Canonical RG-7 item names are field meanings: registered, voted, ballots_in_box, invalid, valid,
# outside_voted, outside_confirmations, list:N, other: <label>. Reports read with the r1 prompt carry
# the 2022 item numbers instead; they map as below (the numbering differs between form versions).
RG7_FIELDS = ("registered", "voted", "ballots_in_box", "invalid", "valid", "outside_voted",
              "outside_confirmations")
_LEGACY_FIELD = {"3": "registered", "10.1": "voted", "10.2": "ballots_in_box", "10.3": "invalid",
                 "10.4": "valid"}
_RG7_RESULT = ("voted", "ballots_in_box", "invalid", "valid")


def rg7_field(item: str) -> str:
    """Canonical field of a stored RG-7 item name (old numbered names included)."""
    return _LEGACY_FIELD.get(item, item)


def _is_result(item: str) -> bool:
    f = rg7_field(item)
    return f in _RG7_RESULT or f.startswith("list:")


def change_text(c: dict) -> str:
    old = "?" if c["old"] is None else c["old"]
    new = "?" if c["new"] is None else c["new"]
    printed = c.get("printed")
    return f"{c['item']}{f' (item {printed})' if printed and printed not in c['item'] else ''} {old}->{new}"


def rg2_value(rg2: dict, item: str) -> int | None:
    """The number the watch model read for an RG-7 item on an RG-2 report (None = not readable)."""
    item = rg7_field(item)
    if item in RG7_FIELDS:
        f = _dash_zero(rg2["numbers"][item])
    elif item.startswith("list:") and item[5:].isdigit():
        row = int(item[5:])
        f = next((_dash_zero(r["field"]) for r in rg2["numbers"]["list_votes"] if r["row"] == row), None)
    else:
        return None
    return f["value"] if f is not None and _ok(f) else None


def _prior_new(item: str, prior: list[dict]) -> int | None:
    """The value earlier decisions left for an item: their latest non-null new (a table is final)."""
    out = None
    f = rg7_field(item)
    for rep in prior:
        for c in rep["changes"]:
            if rg7_field(c["item"]) == f and c["new"] is not None:
                out = c["new"]
        for e in rep["table"]:
            if rg7_field(e["item"]) == f and e["value"] is not None:
                out = e["value"]
    return out


def rg7_mismatch(report: dict, rg2: dict | None, prior: list[dict] | None = None) -> list[str]:
    """Changes whose `old` differs from what is already established: the previous decisions' value
    for that item when there is one, else the RG-2 reading (when there is an RG-2 report)."""
    out = []
    for c in report["changes"]:
        before = _prior_new(c["item"], prior or [])
        if before is not None:
            src, read = "previous decision new", before
        else:
            src, read = "RG-2 read", rg2_value(rg2, c["item"]) if rg2 is not None else None
        if c["old"] is not None and read is not None and read != c["old"]:
            out.append(f"{c['item']}: decision old {c['old']}, {src} {read}")
    return out


_MONTHS = {m: i for i, names in enumerate((
    "јануара januara", "фебруара februara", "марта marta", "априла aprila", "маја maja", "јуна juna",
    "јула jula", "августа avgusta", "септембра septembra", "октобра oktobra", "новембра novembra",
    "децембра decembra"), 1) for m in names.split()}


def parse_decision_date(text) -> date | None:
    """"14.10.2026", "14.10.2026. године" or "9. јуна 2025." (Cyrillic or Latin month) -> date."""
    t = str(text or "").lower()
    m = re.search(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})", t)
    if m:
        d, mo, y = (int(x) for x in m.groups())
    elif m := re.search(r"(\d{1,2})\s*\.?\s*([^\W\d_]+)\s*(\d{4})", t):
        d, mo, y = int(m.group(1)), _MONTHS.get(m.group(2)), int(m.group(3))
    else:
        return None
    try:
        return date(y, mo, d)
    except (TypeError, ValueError):
        return None


def decision_sort_key(uploaded_at: str | None, doc_id: int, report: dict | None) -> tuple:
    """Decision order: parsed decision date, else upload date; then upload time, then id (an unknown
    upload time sorts last)."""
    parsed = parse_decision_date(report.get("decision_date") if report else None)
    up = uploaded_at or ""
    return ((parsed.isoformat() if parsed else up[:10]) or "9999", up or "9999", doc_id)


def decision_label(uploaded_at: str | None, report: dict | None) -> str:
    """dd/mm/yyyy of the decision (parsed) or, failing that, of its upload ("?" when neither)."""
    d = parse_decision_date(report.get("decision_date") if report else None)
    if d is None:
        try:
            d = date.fromisoformat((uploaded_at or "")[:10])
        except ValueError:
            return "?"
    return d.strftime("%d/%m/%Y")


def evaluate_rg7(report: dict, doc: Doc, cfg: Config, rg2: dict | None = None,
                 prior: list[dict] | None = None) -> list[Finding]:
    """R1-R3 of an RG-7 report; `rg2` (the station's RG-2 report) and `prior` (earlier decisions of
    the station, oldest first) enable the R2 cross-check."""
    changes = report["changes"]
    if not changes:
        return [Finding("R3", "LOW", doc, "RG-7 decision: no changes could be read")]
    low = report["confidence"] == "low"
    sev = "HIGH" if any(_is_result(c["item"]) for c in changes) and not low else "MEDIUM"
    text = "RG-7 corrected " + "; ".join(change_text(c) for c in changes)
    suffix = " (low-confidence read)" if low else ""
    if len(text) + len(suffix) > _MAX_SUMMARY:
        text = text[:_MAX_SUMMARY - len(suffix) - 3] + "..."
    out = [Finding("R1", sev, doc, text + suffix)]
    if bad := rg7_mismatch(report, rg2, prior):
        out.append(Finding("R2", "MEDIUM", doc, ("RG-7 vs RG-2 - " + "; ".join(bad))[:_MAX_SUMMARY]))
    return out
