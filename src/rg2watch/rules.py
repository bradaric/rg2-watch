import re

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
        return f"{f['mark']} ({f['value'] if _ok(f) else 'value unreadable'})"

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
    if "outside_voted" in n and "outside_confirmations" in n and n["outside_voted"] != n["outside_confirmations"]:
        add("A5", "MEDIUM", f"outside voted {n['outside_voted']} != confirmations {n['outside_confirmations']}")
    if "voted" in n and "registered" in n and n["registered"] > cfg.min_registered_for_ratio \
            and n["voted"] / n["registered"] >= cfg.turnout_high:
        add("A6", "MEDIUM", f"turnout {n['voted'] / n['registered']:.1%} (voted {n['voted']} of {n['registered']})")
    if list_vals and "valid" in n and n["valid"] > cfg.min_registered_for_ratio \
            and max(list_vals) >= cfg.leader_share_high * n["valid"]:
        add("A7", "MEDIUM", f"one list {max(list_vals)} of valid {n['valid']} ({max(list_vals) / n['valid']:.1%})")
    if "assisted" in n and "voted" in n and n["assisted"] > cfg.assisted_share * n["voted"]:
        add("A8", "LOW", f"assisted {n['assisted']} > {cfg.assisted_share:.0%} of voted {n['voted']}")

    if "outside_voted" in n and "voted" in n and n["voted"] > 0:
        share = n["outside_voted"] / n["voted"]
        text = f"outside-station voting {n['outside_voted']} of {n['voted']} ({share:.1%})"
        if share >= cfg.outside_share_high:
            add("A9", "HIGH", text)
        elif share >= cfg.outside_share_medium and n["outside_voted"] >= 10:
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
