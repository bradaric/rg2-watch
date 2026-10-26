"""Validated, operator-entered numeric snapshots for an exact RG-2 document version."""
import copy
import hashlib
import json
import logging
from collections import Counter

from .pipeline import PROMPT_VERSION, RG7_PROMPT_VERSION
from .rules import _list_zero, decision_sort_key
from .store import CorrectionRevisionConflict


FIELDS = ("registered", "voted", "ballots_in_box", "invalid", "valid",
          "outside_voted", "outside_confirmations")
SNAPSHOT_KEYS = {*FIELDS, "list_votes"}
log = logging.getLogger("rg2watch.operator_corrections")


class CorrectionConflict(ValueError):
    """The editor was opened against an older source or correction revision."""


def validate_snapshot(snapshot: dict, expected_rows: list[int]) -> dict:
    """Return a valid complete snapshot, or raise ValueError with an operator-facing reason."""
    if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_KEYS:
        raise ValueError("correction must contain every numeric field and only those fields")
    for key in FIELDS:
        value = snapshot[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    votes = snapshot["list_votes"]
    if not isinstance(votes, list) or any(not isinstance(x, dict) or set(x) != {"row", "value"}
                                          for x in votes):
        raise ValueError("list_votes must contain row and value entries")
    if any(not isinstance(x["row"], int) or isinstance(x["row"], bool)
           or not isinstance(x["value"], int) or isinstance(x["value"], bool) or x["value"] < 0
           for x in votes):
        raise ValueError("list vote rows and values must be non-negative integers")
    rows = [x["row"] for x in votes]
    if rows != expected_rows or len(set(rows)) != len(rows):
        raise ValueError("list vote rows must exactly match the expected rows")
    if snapshot["ballots_in_box"] > snapshot["voted"]:
        raise ValueError("ballots in box cannot exceed voted")
    if snapshot["invalid"] + snapshot["valid"] != snapshot["ballots_in_box"]:
        raise ValueError("invalid plus valid must equal ballots in box")
    if sum(x["value"] for x in votes) != snapshot["valid"]:
        raise ValueError("list votes must sum to valid")
    if snapshot["voted"] > snapshot["registered"]:
        raise ValueError("voted cannot exceed registered")
    return copy.deepcopy(snapshot)


def apply_operator_snapshot(report: dict, snapshot: dict) -> dict:
    """Copy a report and replace its tally/arithmetic numbers with audited operator values."""
    out = copy.deepcopy(report)
    field = lambda value: {"value": value, "original_value": None, "legible": True,
                           "mark": "clean", "note": "operator-entered correction"}
    for key in FIELDS:
        out["numbers"][key] = field(snapshot[key])
    out["numbers"]["list_votes"] = [
        {"row": x["row"], "field": field(x["value"])} for x in snapshot["list_votes"]]
    return out


def snapshot_of(report: dict, expected_rows: list[int] | None = None) -> dict:
    nums = report["numbers"]
    by_row = {x["row"]: _list_zero(x["field"])["value"] for x in nums["list_votes"]}
    rows = sorted(by_row) if expected_rows is None else expected_rows
    return {**{key: nums[key]["value"] for key in FIELDS},
            "list_votes": [{"row": row, "value": by_row.get(row)} for row in rows]}


class OperatorCorrections:
    """Validated correction history and source/revision conflict detection."""

    def __init__(self, store, cfg, model: str, on_change=None):
        self.store, self.cfg, self.model, self.on_change = store, cfg, model, on_change

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.exception("browse data could not be refreshed after an operator correction")

    @staticmethod
    def _hash(value) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _expected_rows(self, source) -> list[int]:
        docs = [d for d in self.store.browse_rows(source["parent"])[0]
                if d["doc_type"] == 6 and d["active"]]
        from .tally import scope_of
        scope = scope_of(self.store, self.cfg, source["parent"])[0]
        if scope != "national":
            docs = [d for d in docs if d["municipality"] == source["municipality"]]
        ids = {d["id"] for d in docs}
        analyses = [a for a in self.store.browse_analyses(source["parent"], PROMPT_VERSION)
                    if a["document_id"] in ids and a["model"] == self.model]
        reports = self.store.analysis_reports([a["id"] for a in analyses])
        sets = Counter(tuple(sorted(x["row"] for x in json.loads(reports[a["id"]])["numbers"]["list_votes"]))
                       for a in analyses)
        if not sets:
            return []
        most = max(sets.values())
        return list(max((rows for rows, count in sets.items() if count == most), key=len))

    def _source(self, url: str) -> tuple[object, dict, str, list[int]]:
        row = self.store.operator_correction_source(url, self.model, PROMPT_VERSION)
        if row is None or row["doc_type"] != 6:
            raise ValueError("an analysed first-round RG-2 document is required")
        report = json.loads(row["result_json"])
        rg7 = self.store.rg7_reports(row["parent"], self.model, RG7_PROMPT_VERSION,
                                     analysed_only=False, station_key=row["station_key"])
        decisions = []
        source_rg7 = []
        for d, rep in rg7:
            if d["doc_type"] not in (19, 20, 21, 22):
                continue
            source_rg7.append([d["id"], d["url"], rep])
            if rep is not None:
                item = {"d": str(d["id"]), "rep": rep, "u": d["url"]}
                decisions.append((item, decision_sort_key(d["uploaded_at"], d["id"], rep)))
        if decisions:
            from .tally import apply_rg7  # local import: tally also applies operator snapshots
            report = apply_rg7(report, [x for x, _ in sorted(decisions, key=lambda x: x[1])])[0]
        expected = self._expected_rows(row)
        raw = self._hash({"analysis": row["analysis_id"], "report": report, "rg7": source_rg7,
                          "expected_rows": expected})
        return row, report, raw, expected

    @staticmethod
    def _history(rows, source_revision: str, source_snapshot: dict) -> list[dict]:
        out = []
        for r in rows:
            values = json.loads(r["snapshot_json"]) if r["snapshot_json"] else None
            if r["action"] == "revoke":
                kind = "revoke"
            elif r["source_token"] != source_revision:
                kind = "saved"
            else:
                kind = "verification" if values == source_snapshot else "correction"
            out.append({"id": r["id"], "action": r["action"], "kind": kind,
                        "reason": r["reason"], "at": r["created_at"], "snapshot": values})
        return out

    def state(self, url: str) -> dict:
        row, report, raw, expected = self._source(url)
        history_rows = self.store.operator_correction_history(url)
        source_snapshot = snapshot_of(report, expected)
        latest = history_rows[-1] if history_rows else None
        token = self._hash({"source": raw, "revision": latest["id"] if latest else None})
        status, active = "none", None
        if latest is not None and latest["action"] == "revoke":
            status = "revoked"
        elif latest is not None and latest["source_token"] == raw:
            status, active = "active", json.loads(latest["snapshot_json"])
        elif latest is not None:
            status = "suspended"
        return {"url": url, "status": status, "source_token": token,
                "source_revision": raw,
                "snapshot": source_snapshot, "expected_rows": expected,
                "active_snapshot": active, "reason": latest["reason"] if latest else None,
                "at": latest["created_at"] if latest else None, "active": bool(row["active"]),
                "revision": latest["id"] if latest else None,
                "history": self._history(history_rows, raw, source_snapshot)}

    def save(self, url: str, snapshot: dict, reason: str, source_token: str) -> dict:
        state = self.state(url)
        if source_token != state["source_token"]:
            raise CorrectionConflict("correction source changed; review the new values before saving")
        if not state["active"]:
            raise ValueError("cannot correct a superseded or removed document")
        values = validate_snapshot(snapshot, state["expected_rows"])
        _, _, raw, _ = self._source(url)
        if raw != state["source_revision"]:
            raise CorrectionConflict("correction source changed during save; review the new values")
        try:
            added = self.store.add_operator_correction(
                url, "correction", values, reason, raw, expected_revision=state["revision"])
        except CorrectionRevisionConflict as exc:
            raise CorrectionConflict(str(exc)) from exc
        if added is None:
            raise ValueError("unknown document URL")
        self._changed()
        return self.state(url)

    def revoke(self, url: str, reason: str, source_token: str) -> dict:
        state = self.state(url)
        if source_token != state["source_token"]:
            raise CorrectionConflict("correction source changed; review it before revoking")
        if state["status"] not in ("active", "suspended"):
            raise ValueError("there is no active or suspended correction to revoke")
        _, _, raw, _ = self._source(url)
        if raw != state["source_revision"]:
            raise CorrectionConflict("correction source changed during save; review it before revoking")
        try:
            self.store.add_operator_correction(
                url, "revoke", None, reason, raw, expected_revision=state["revision"])
        except CorrectionRevisionConflict as exc:
            raise CorrectionConflict(str(exc)) from exc
        self._changed()
        return self.state(url)
