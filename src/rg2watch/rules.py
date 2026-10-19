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


def _ok(f: dict) -> bool:
    return bool(f["legible"]) and f["value"] is not None


def _parse_time(text) -> int | None:
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:. ]?\s*(\d{2})\s*", text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if h < 24 and mi < 60 else None


def _station(s) -> str:
    return str(s).strip().lstrip("0")


def evaluate_report(report: dict, doc: Doc, cfg: Config) -> list[Finding]:
    hits: list[tuple[str, str, str]] = []

    def add(rule: str, sev: str, parts: list[str] | str) -> None:
        text = parts if isinstance(parts, str) else "; ".join(parts)
        hits.append((rule, sev, text))

    hdr, nums, yn = report["header"], report["numbers"], report["yesno"]
    times, sig, vis = report["times"], report["signatures"], report["visual"]
    lists = nums["list_votes"]

    # V1 / V2
    v1 = [f"{_LABELS[k]} {k} {nums[k]['mark']} ({nums[k]['value']})"
          for k in _RESULT_FIELDS if nums[k]["mark"] in _BAD_MARKS]
    v1 += [f"list row {r['row']} {r['field']['mark']} ({r['field']['value']})"
           for r in lists if r["field"]["mark"] in _BAD_MARKS]
    if v1:
        add("V1", "HIGH", v1)
    v2 = [f"{_LABELS[k]} {k} {nums[k]['mark']} ({nums[k]['value']})"
          for k in _OTHER_FIELDS if nums[k]["mark"] in _BAD_MARKS]
    if v2:
        add("V2", "MEDIUM", v2)

    # V3-V7
    if not sig["president_signed"]:
        add("V3", "HIGH", "president did not sign")
    if sig["rows_name_without_signature"] >= cfg.unsigned_rows_threshold:
        add("V4", "MEDIUM", f"{sig['rows_name_without_signature']} named committee rows without signature")
    if sig["signatures_look_same_hand"]:
        add("V5", "HIGH", "signatures look like the same hand")
    v6 = [n for n, flag in (("pen", vis["results_block_different_pen"]),
                            ("hand", vis["results_block_different_hand"])) if flag]
    if v6:
        add("V6", "HIGH", "results block in different " + " and ".join(v6))
    if vis["pasted_or_patched_area"]:
        add("V7", "HIGH", "pasted or patched area")

    # V8
    v8 = [f"{_LABELS[k]} {k} {'illegible' if not nums[k]['legible'] else 'empty'}"
          for k in ("registered",) + _RESULT_FIELDS if not _ok(nums[k])]
    v8 += [f"list row {r['row']} {'illegible' if not r['field']['legible'] else 'empty'}"
           for r in lists if not _ok(r["field"])]
    if v8:
        add("V8", "MEDIUM", v8)

    # V9
    v9 = []
    if hdr["station_no"] is not None and _station(hdr["station_no"]) != _station(doc.station_no):
        v9.append(f"station_no on form {hdr['station_no']} differs from listing {doc.station_no}")
    missing = [s for s in _SECTIONS if s not in hdr["sections_present"]]
    if missing:
        extra = " (no committee/signatures)" if "V" in missing else ""
        v9.append("missing sections " + ",".join(missing) + extra)
    if not hdr["pages_match_same_form"]:
        v9.append("pages do not match the same form")
    if v9:
        add("V9", "HIGH", v9)

    if vis["page_quality"] in ("poor", "unreadable"):
        add("V10", "LOW", f"page quality {vis['page_quality']}")
    v11 = [f"{_YESNO_LABELS[k]} {k} {yn[k]['answer']}" for k in _YESNO_LABELS
           if yn[k]["answer"] in ("both", "none")]
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
    if close is not None and done is not None and done < close:
        p4.append(f"completed_at {times['completed_at']} before close_time {times['close_time']}")
    if p4:
        add("P4", "MEDIUM", p4)

    # A1-A8 (legible, non-null values only)
    n = {k: nums[k]["value"] for k in nums if k != "list_votes" and _ok(nums[k])}
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

    low = report["confidence"] == "low"
    out = []
    for rule, sev, text in hits:
        if low:
            if sev == "HIGH":
                sev = "MEDIUM"
            text += " (low-confidence read)"
        if len(text) > _MAX_SUMMARY:
            text = text[:_MAX_SUMMARY - 3] + "..."
        out.append(Finding(rule, sev, doc, text))
    return out
