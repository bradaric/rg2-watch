"""Validated, operator-entered numeric snapshots for an exact RG-2 document version."""
import copy
import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone

from .pipeline import PROMPT_VERSION, RG7_PROMPT_VERSION, TYPE13_PROMPT_VERSION
from .rounds import bind_repeat_round
from .rules import _list_zero, decision_sort_key, evaluate_report
from .store import CorrectionRevisionConflict, _doc


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

    def __init__(self, store, cfg, model: str, on_change=None,
                 expected_rows_cache: dict | None = None):
        self.store, self.cfg, self.model, self.on_change = store, cfg, model, on_change
        self.expected_rows_cache = expected_rows_cache

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
        from .tally import scope_of
        scope = scope_of(self.store, self.cfg, source["parent"])[0]
        key = (source["parent"], None if scope == "national" else source["municipality"])
        if self.expected_rows_cache is not None and key in self.expected_rows_cache:
            return list(self.expected_rows_cache[key])
        selected = self.store.selected_rg2_urls(source["parent"], self.model, PROMPT_VERSION)
        docs = [d for d in self.store.browse_rows(source["parent"])[0] if d["url"] in selected]
        if scope != "national":
            docs = [d for d in docs if d["municipality"] == source["municipality"]]
        ids = {d["id"] for d in docs}
        analyses = [a for a in self.store.browse_analyses(source["parent"], PROMPT_VERSION)
                    if a["document_id"] in ids and a["model"] == self.model]
        reports = self.store.analysis_reports([a["id"] for a in analyses])
        sets = Counter(tuple(sorted(x["row"] for x in json.loads(reports[a["id"]])["numbers"]["list_votes"]))
                       for a in analyses)
        if not sets:
            rows = []
        else:
            most = max(sets.values())
            rows = list(max((rows for rows, count in sets.items() if count == most), key=len))
        if self.expected_rows_cache is not None:
            self.expected_rows_cache[key] = tuple(rows)
        return rows

    def _source(self, url: str) -> tuple[object, dict, str, list[int]]:
        row = self.store.operator_correction_source(url, self.model, PROMPT_VERSION)
        if row is None or row["doc_type"] not in (6, 10, 15, 16):
            raise ValueError("an analysed RG-2 document is required")
        report = json.loads(row["result_json"])
        rg7 = self.store.rg7_reports(row["parent"], self.model, RG7_PROMPT_VERSION,
                                     analysed_only=False, station_key=row["station_key"])
        decisions = []
        source_rg7 = []

        def uploaded(value):
            try:
                out = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
                return out.replace(tzinfo=out.tzinfo or timezone.utc)
            except (TypeError, ValueError):
                return None

        round_starts = {}
        if row["doc_type"] != 6:
            round_docs = [d for d in self.store.browse_rows(row["parent"])[0]
                          if d["station_key"] == row["station_key"] and d["doc_type"] in (10, 15, 16)]
            round_analyses = [a for a in self.store.browse_analyses(row["parent"], PROMPT_VERSION)
                              if a["model"] == self.model]
            round_by_doc = {a["document_id"]: a for a in round_analyses}
            round_reports = self.store.analysis_reports([a["id"] for a in round_analyses])
            overrides = self.store.latest_classification_overrides(row["parent"])
            for d in round_docs:
                a = round_by_doc.get(d["id"])
                confirmed = overrides.get(d["url"]) == "confirm_rg2"
                if overrides.get(d["url"]) == "quarantine_rg7":
                    continue
                if not confirmed:
                    try:
                        confirmed = (a is not None and (a["prompt_version"] != PROMPT_VERSION or
                                     json.loads(round_reports[a["id"]])["document_classification"]["kind"] == "rg2"))
                    except (KeyError, TypeError, ValueError):
                        confirmed = False
                if not confirmed:
                    continue
                if at := uploaded(d["uploaded_at"]):
                    round_starts[d["doc_type"]] = min(round_starts.get(d["doc_type"], at), at)

        def applies(d) -> bool:
            if row["doc_type"] == 6:
                return d["doc_type"] in (19, 20, 21, 22)
            if d["doc_type"] not in (23, 24, 25, 26):
                return False
            at = uploaded(d["uploaded_at"])
            return bind_repeat_round(at, round_starts) == row["doc_type"]

        for d, rep in rg7:
            if not applies(d):
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
        linked = (self.store.type13_correction_source_state(latest["id"])
                  if latest is not None and latest["action"] == "correction" else None)
        linked_current = (linked is None or
                          (bool(linked["active"]) and linked["analysis_id"] == linked["current_analysis_id"]))
        token = self._hash({"source": raw, "revision": latest["id"] if latest else None})
        status, active = "none", None
        if latest is not None and latest["action"] == "revoke":
            status = "revoked"
        elif latest is not None and latest["source_token"] == raw and linked_current:
            status, active = "active", json.loads(latest["snapshot_json"])
        elif latest is not None:
            status = "suspended"
        resolved_rules = []
        if active is not None:
            before = {f.rule for f in evaluate_report(report, _doc(row), self.cfg)}
            after = {f.rule for f in evaluate_report(apply_operator_snapshot(report, active),
                                                     _doc(row), self.cfg)}
            resolved_rules = sorted(before - after)
        selected = self.store.selected_rg2_url(row["parent"], row["station_key"], self.model,
                                               PROMPT_VERSION) == url
        return {"url": url, "status": status, "source_token": token,
                "source_revision": raw,
                "snapshot": source_snapshot, "expected_rows": expected,
                "active_snapshot": active, "reason": latest["reason"] if latest else None,
                "at": latest["created_at"] if latest else None,
                "active": bool(row["active"]) and selected,
                "revision": latest["id"] if latest else None,
                "resolved_rules": resolved_rules,
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


class Type13Corrections:
    """Advisory type-13 reads attached to the existing complete-snapshot correction path."""

    def __init__(self, store, cfg, model: str, on_change=None,
                 expected_rows_cache: dict | None = None):
        self.store, self.cfg, self.model, self.on_change = store, cfg, model, on_change
        self.operator = OperatorCorrections(store, cfg, model, on_change,
                                            expected_rows_cache=expected_rows_cache)

    @staticmethod
    def _proposal(snapshot: dict, report: dict) -> tuple[dict, list[str]]:
        out = copy.deepcopy(snapshot)
        mismatches = []

        def current(item: str):
            if item.startswith("list:") and item[5:].isdigit():
                row = int(item[5:])
                return next((x["value"] for x in out["list_votes"] if x["row"] == row), None)
            return out.get(item)

        def put(item: str, value: int | None) -> None:
            if value is None:
                return
            if item.startswith("list:") and item[5:].isdigit():
                row = int(item[5:])
                target = next((x for x in out["list_votes"] if x["row"] == row), None)
                if target is not None:
                    target["value"] = value
            elif item in FIELDS:
                out[item] = value

        for change in report.get("changes", []):
            old = current(change["item"])
            if change.get("old") is not None and old != change["old"]:
                mismatches.append(f"{change['item']}: decision old {change['old']}, source {old}")
            put(change["item"], change.get("new"))
        for entry in report.get("table", []):
            put(entry["item"], entry.get("value"))
        return out, mismatches

    def state(self, source_url: str) -> dict:
        source = self.store.type13_source(source_url, self.model, TYPE13_PROMPT_VERSION)
        if source is None:
            raise ValueError("a RIK-listed type-13 document is required")
        report = json.loads(source["result_json"]) if source["result_json"] else None
        rounds = self.store.selected_rg2_round_urls(
            source["parent"], source["station_key"], self.model, PROMPT_VERSION)
        repeats = {kind: self._uploaded(value[1]) for kind, value in rounds.items() if kind != 6}
        source_at = self._uploaded(source["uploaded_at"])
        if not repeats:
            target_url = rounds.get(6, (None, ""))[0]
        elif source_at is None or any(value is None for value in repeats.values()):
            target_url = None
        elif source_at < min(repeats.values()):
            target_url = rounds.get(6, (None, ""))[0]
        else:
            bound = bind_repeat_round(source_at, repeats)
            target_url = rounds.get(bound, (None, ""))[0] if bound is not None else None
        target = self.operator.state(target_url) if target_url is not None else None
        proposal, mismatches = (self._proposal(target["snapshot"], report)
                                if target is not None and report is not None else (None, []))
        problem = None
        if proposal is not None:
            try:
                validate_snapshot(proposal, target["expected_rows"])
            except ValueError as exc:
                problem = str(exc)
        latest = self.store.latest_type13_resolution(source_url)
        token = self.operator._hash({
            "source_url": source_url,
            "source_active": source["active"],
            "analysis": source["analysis_id"],
            "report": report,
            "target": target_url,
            "target_token": target["source_token"] if target else None,
            "resolution": latest["id"] if latest else None,
        })
        classification = report.get("target", "uncertain") if report else "uncertain"
        can_edit = bool(source["active"] and target and target["active"])
        return {
            "source_url": source_url,
            "target_url": target_url,
            "classification": classification,
            "evidence": report.get("evidence", "") if report else "",
            "confidence": report.get("confidence") if report else None,
            "proposal": proposal,
            "mismatches": mismatches,
            "problem": problem,
            "can_edit": can_edit,
            "can_approve": bool(can_edit and classification == "rg2" and proposal is not None
                                and problem is None),
            "source_token": token,
            "resolution": latest["action"] if latest else None,
            "revision": latest["id"] if latest else None,
            "target_state": target,
        }

    @staticmethod
    def _uploaded(value):
        try:
            out = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
            return out.replace(tzinfo=out.tzinfo or timezone.utc)
        except (TypeError, ValueError):
            return None

    def approve(self, source_url: str, snapshot: dict, reason: str, source_token: str) -> dict:
        state = self.state(source_url)
        if source_token != state["source_token"]:
            raise CorrectionConflict("correction source changed; review the new values before saving")
        if not state["can_edit"]:
            raise ValueError("type-13 correction is not ready for approval")
        values = validate_snapshot(snapshot, state["target_state"]["expected_rows"])
        source = self.store.type13_source(source_url, self.model, TYPE13_PROMPT_VERSION)
        _, _, target_raw, _ = self.operator._source(state["target_url"])
        if target_raw != state["target_state"]["source_revision"]:
            raise CorrectionConflict("correction source changed during save; review the new values")
        try:
            self.store.add_type13_correction(
                source_url, state["target_url"], values, reason, target_raw, source_token,
                source["analysis_id"], self.model, TYPE13_PROMPT_VERSION,
                state["target_state"]["revision"], state["revision"])
        except CorrectionRevisionConflict as exc:
            raise CorrectionConflict(str(exc)) from exc
        self.operator._changed()
        return self.state(source_url)

    def resolve(self, source_url: str, action: str, reason: str, source_token: str) -> dict:
        if action not in ("other_decision", "uncertain"):
            raise ValueError("type-13 action must be other_decision or uncertain")
        state = self.state(source_url)
        if source_token != state["source_token"]:
            raise CorrectionConflict("correction source changed; review it before saving")
        if state["resolution"] == "rg2_correction":
            raise ValueError("revoke the approved correction before reclassifying it")
        source = self.store.type13_source(source_url, self.model, TYPE13_PROMPT_VERSION)
        try:
            self.store.add_type13_resolution(
                source_url, state["target_url"], action, reason, source_token,
                source["analysis_id"], state["revision"])
        except CorrectionRevisionConflict as exc:
            raise CorrectionConflict(str(exc)) from exc
        self.operator._changed()
        return self.state(source_url)

    def revoke(self, source_url: str, reason: str, source_token: str) -> dict:
        state = self.state(source_url)
        if source_token != state["source_token"]:
            raise CorrectionConflict("correction source changed; review it before revoking")
        if state["resolution"] != "rg2_correction" or state["revision"] is None:
            raise ValueError("there is no active type-13 correction to revoke")
        _, _, target_raw, _ = self.operator._source(state["target_url"])
        try:
            self.store.revoke_type13_correction(
                source_url, reason, target_raw, source_token, state["revision"],
                state["target_state"]["revision"])
        except CorrectionRevisionConflict as exc:
            raise CorrectionConflict(str(exc)) from exc
        self.operator._changed()
        return self.state(source_url)
