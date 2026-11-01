import hashlib
import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .analyze import ARITHMETIC_PROMPT_VERSION, stored_model
from .analysis_selection import preferred_analyses
from .config import Config
from .models import DOC_TYPES, Doc
from .operator_corrections import OperatorCorrections, Type13Corrections
from .pipeline import PROMPT_VERSION, RG7_PROMPT_VERSION
from .rules import (INFORMATIONAL_RULES, RULE_INFO, change_text, decision_label, decision_sort_key,
                    evaluate_report)
from .rounds import rg7_applies
from .status import _rik_link, build_status, to_belgrade
from .station_locations import station_coordinates, station_location
from .store import Store, _doc
from .tally import build_tally

log = logging.getLogger("rg2watch.browse")
SEV_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
UNKNOWN = "(unknown)"
STATIC = Path(__file__).parent / "static"
STATIC_FILES = ("index.html", "jquery.min.js", "dataTables.min.js", "dataTables.dataTables.min.css")
STALE_TMP_S = 300
_MEMORY_BUILD_LOCKS: dict[str, threading.Lock] = {}
_MEMORY_BUILD_LOCKS_GUARD = threading.Lock()


class SnapshotBusy(RuntimeError):
    pass


@contextmanager
def snapshot_build_lock(store: Store, out_path: Path, blocking: bool = False):
    """One snapshot publisher per database/page pair, across threads and processes."""
    page = str(Path(out_path).resolve().parent)
    if store.path == ":memory:":
        key = store.path + "\0" + page
        with _MEMORY_BUILD_LOCKS_GUARD:
            lock = _MEMORY_BUILD_LOCKS.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=blocking):
            raise SnapshotBusy("operator snapshot build already running")
        try:
            yield
        finally:
            lock.release()
        return
    db = Path(store.path).resolve()
    digest = hashlib.sha256((str(db) + "\0" + page).encode()).hexdigest()[:16]
    lock_path = db.parent / f".{db.name}.{digest}.operator-snapshot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise SnapshotBusy("operator snapshot build already running") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _ast(d) -> str:
    s = d["analysis_status"]
    if s == "failed" and d["analysis_next_at"] is not None:
        return "retrying"
    return {"new": "pending", "running": "pending", "failed": "failed", "done": "done"}.get(s, "off")


def _opinions(store: Store, docs: dict[int, Doc], arows: list, cfg: Config,
              cache: dict, report_json: dict[int, str] | None = None) -> dict[int, list[dict]]:
    """document id -> [{m, at, f}]: findings of the newest stored analysis of each model (current
    prompt version preferred), from the current rules (pure, no API). `cache` keeps the evaluated
    findings by analysis id across builds (None = could not be evaluated, logged once)."""
    rows = [a for a in arows if a["document_id"] in docs]
    live = {a["id"] for a in rows}
    for gone in set(cache) - live:
        del cache[gone]
    todo = [a["id"] for a in rows if a["id"] not in cache]
    reports = store.analysis_reports(todo) if report_json is None else report_json
    by_id = {a["id"]: a for a in rows}
    for aid in todo:
        a = by_id[aid]
        try:
            found = evaluate_report(json.loads(reports[aid]), docs[a["document_id"]], cfg)
            cache[aid] = sorted(([x.rule, x.severity, x.summary] for x in found),
                                key=lambda x: -SEV_RANK.get(x[1], 0))
        except Exception:
            log.warning("stored analysis %s of document %s could not be re-evaluated", aid,
                        a["document_id"], exc_info=True)
            cache[aid] = None
    out: dict[int, list[dict]] = {}
    for a in rows:
        if cache[a["id"]] is not None:
            out.setdefault(a["document_id"], []).append(
                {"m": a["model"] or "?", "at": to_belgrade(a["created_at"]), "f": cache[a["id"]]})
    for ops in out.values():
        ops.sort(key=lambda o: o["m"])
    return out


def _disagreement(ops: list[dict]) -> list[str]:
    """Rules that are HIGH for one model and not HIGH for another (needs two models)."""
    if len(ops) < 2:
        return []
    high = [{f[0] for f in o["f"] if f[1] == "HIGH"} for o in ops]
    return sorted(set.union(*high) - set.intersection(*high))


def review_record(r) -> dict:
    """A stored review as the page shows it: verdict, note, Serbian local time."""
    return {"v": r["verdict"], "n": r["note"], "at": to_belgrade(r["created_at"])}


def reviews_payload(store: Store) -> dict:
    """URL -> newest review, for the page's live reviews.json."""
    return {r["url"]: review_record(r) for r in store.latest_reviews()}


def record_review(store: Store, url: str, verdict: str, note: str | None) -> dict | None:
    """Store a verdict; the page's record of it, or None when `url` is not a stored document."""
    r = store.set_review(url, verdict, note)
    return None if r is None else {"url": url, **review_record(r)}


def _version(d, found: dict, opinions: dict, cache_dir: Path, base: str | None,
              reviews: dict | None = None, corrections: dict | None = None,
              analysed_at: dict[int, str | None] | None = None,
              arithmetic: dict[int, list[dict]] | None = None) -> dict:
    pdf = hashlib.sha256(d["url"].encode()).hexdigest()
    path = cache_dir / (pdf + ".pdf")
    cached = None
    try:
        if path.is_file():
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            cached = to_belgrade(modified.strftime("%Y-%m-%dT%H:%M:%SZ"))
    except OSError:
        pass
    f = [[x["rule"], x["severity"], x["summary"]] for x in found.get(d["id"], [])]
    f.sort(key=lambda x: -SEV_RANK.get(x[1], 0))
    analysis_status = _ast(d)
    done = (analysed_at or {}).get(d["id"]) if analysis_status == "done" else None
    return {"up": d["uploaded_at"], "seen": to_belgrade(d["first_seen_at"]),
            "cache": cached, "done": done, "f": f, "ast": analysis_status,
            "pdf": pdf if cached is not None else None,
            "rik": _rik_link(d["url"], base), "ops": opinions.get(d["id"], []),
            "ars": (arithmetic or {}).get(d["id"], []),
            "u": d["url"], "rv": (reviews or {}).get(d["url"]),
            "oc": (corrections or {}).get(d["url"]), "correctable": False}


def build_data(store: Store, cache_dir: Path, parent: str | None = None,
               cfg: Config | None = None, interval: int = 0, cache: dict | None = None,
               timings: dict[str, float] | None = None, selective: bool = False) -> dict:
    timings = {} if timings is None else timings
    phase = time.monotonic()

    def mark(name: str) -> None:
        nonlocal phase
        now = time.monotonic()
        timings[name] = timings.get(name, 0.0) + now - phase
        phase = now

    cache_dir = Path(cache_dir).resolve()
    cfg = cfg or Config()
    model = stored_model(cfg.model)
    docs, findings = store.browse_rows(parent)
    found: dict[int, list] = {}
    for f in findings:
        found.setdefault(f["document_id"], []).append(f)
    arows = [a for a in store.browse_analyses(parent, PROMPT_VERSION)
             if a["prompt_version"].startswith("v")]
    report_json = store.analysis_reports([a["id"] for a in arows])
    mark("db_reads")
    selected_by_doc = preferred_analyses(arows, model, "v")
    selected_model_analyses = list(selected_by_doc.values())
    classification_reports = report_json
    classifications = {}
    for a in selected_model_analyses:
        try:
            legacy = a["prompt_version"] != PROMPT_VERSION
            value = json.loads(classification_reports[a["id"]]).get("document_classification", {})
            kind = "rg2" if legacy else value.get("kind")
            if kind in ("rg2", "rg7", "other", "uncertain"):
                classifications[a["document_id"]] = {
                    "kind": kind,
                    "evidence": ("successful legacy analysis" if legacy else
                                 str(value.get("evidence") or "")[:500]),
                    "legacy": legacy}
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    overrides = store.latest_classification_overrides(parent)
    opinions = _opinions(store, {d["id"]: _doc(d) for d in docs}, arows, cfg,
                          {} if cache is None else cache.setdefault("ops", {}), report_json)
    arithmetic: dict[int, list[dict]] = {}
    for a in store.arithmetic_analyses(parent, ARITHMETIC_PROMPT_VERSION):
        arithmetic.setdefault(a["document_id"], []).append(
            {"m": a["model"] or "?", "at": to_belgrade(a["created_at"])})
    analysed_at = {a["document_id"]: to_belgrade(a["created_at"])
                   for a in selected_model_analyses if a["prompt_version"] == PROMPT_VERSION}
    elections = {e.parent: e for e in store.elections()}
    reviews = {r["url"]: review_record(r) for r in store.latest_reviews(parent)}
    mark("selection")
    corrections = {}
    expected_rows_cache = {}
    if latest := store.latest_operator_corrections(parent):
        services = {}
        current = set(selected_by_doc)
        for item in latest:
            if item["document_id"] not in current:
                continue
            if selective:
                corrections[item["url"]] = {
                    "status": "active" if item["action"] == "correction" else "revoked",
                    "reason": item["reason"], "at": to_belgrade(item["created_at"]),
                    "revision": item["id"], "verification": "Updating"}
                continue
            try:
                source_model = selected_by_doc[item["document_id"]]["model"]
                service = services.setdefault(source_model, OperatorCorrections(
                    store, cfg, source_model, expected_rows_cache=expected_rows_cache))
                state = service.state(item["url"])
                corrections[item["url"]] = {
                    k: state[k] for k in ("status", "reason", "at", "revision")}
                if state["resolved_rules"]:
                    corrections[item["url"]]["resolved_rules"] = state["resolved_rules"]
            except Exception:
                log.warning("operator correction state of %s could not be read", item["url"], exc_info=True)
    mark("corrections")
    r7: dict[tuple, list[dict]] = {}  # (election, station_key, RG-2 type) -> its RG-7 decisions
    r7_docs = store.rg7_reports(
        parent, model, RG7_PROMPT_VERSION, analysed_only=False, fallback=True) if parent else [
        x for p in elections for x in store.rg7_reports(
            p, model, RG7_PROMPT_VERSION, analysed_only=False, fallback=True)]
    r7_found: dict[int, list] = {}
    for f in store.rg7_findings(parent):
        r7_found.setdefault(f["document_id"], []).append([f["rule"], f["severity"], f["summary"]])
    readers = store.rg7_readers(parent, RG7_PROMPT_VERSION)

    def uploaded(value) -> datetime | None:
        try:
            out = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
            return out.replace(tzinfo=out.tzinfo or timezone.utc)
        except (TypeError, ValueError):
            return None

    round_starts: dict[tuple[str, str], dict[int, datetime]] = {}
    for source in docs:
        if source["doc_type"] not in (10, 15, 16) or source["station_key"] is None:
            continue
        value = classifications.get(source["id"], {})
        override = overrides.get(source["url"])
        legitimate = (override == "confirm_rg2" or
                      (override != "quarantine_rg7" and value.get("kind") == "rg2"))
        if legitimate and (at := uploaded(source["uploaded_at"])):
            starts = round_starts.setdefault((source["parent"], source["station_key"]), {})
            starts[source["doc_type"]] = min(starts.get(source["doc_type"], at), at)

    for d, rep in sorted(r7_docs, key=lambda x: decision_sort_key(x[0]["uploaded_at"], x[0]["id"], x[1])):
        base = elections[d["parent"]].base_url if d["parent"] in elections else None
        x = _version(d, {}, {}, cache_dir, base, corrections=corrections, analysed_at=analysed_at)
        x.update(d=decision_label(d["uploaded_at"], rep),
                  ch=[change_text(c) for c in rep["changes"]] if rep else None,
                  by=[m for m in readers.get(d["id"], []) if m != model],  # read by other models only
                  f=sorted(r7_found.get(d["id"], []), key=lambda f: -SEV_RANK.get(f[1], 0)),
                  target_round=rep.get("target_round") if rep else None,
                  target_evidence=rep.get("target_evidence", "") if rep else "")
        at = uploaded(d["uploaded_at"])
        starts = round_starts.get((d["parent"], d["station_key"]), {})
        types = tuple(t for t in (6, 10, 15, 16)
                      if rg7_applies(d["doc_type"], rep, t, at, starts))
        for t in types:
            r7.setdefault((d["parent"], d["station_key"], t), []).append(x)
    groups: dict[tuple, list] = {}
    for d in docs:  # a NULL station_key cannot be grouped: the document stands alone
        key = d["station_key"] if d["station_key"] is not None else ("id", d["id"])
        groups.setdefault((d["parent"], key, d["doc_type"]), []).append(d)
    rows = []
    for (par, group_key, _), ds in groups.items():
        ds.sort(key=lambda d: (d["uploaded_at"] or "", d["id"]), reverse=True)
        main = next((d for d in ds if d["active"]), ds[0])
        base = elections[par].base_url if par in elections else None
        row = _version(main, found, opinions, cache_dir, base, reviews, corrections, analysed_at,
                       arithmetic)
        location = station_location(main["municipality"] or "", main["station_no"] or "")
        row.update(
            id=main["id"], el=par, muni=main["municipality"] or UNKNOWN, no=main["station_no"],
            group_kind="document" if isinstance(group_key, tuple) else "station",
            group_key=str(group_key[1]) if isinstance(group_key, tuple) else group_key,
            council=main["minority"],
            name=main["station_name"], label=main["label"], doc_type=main["doc_type"],
            analysis_model=(selected_by_doc[main["id"]]["model"]
                            if main["id"] in selected_by_doc else None),
            doc_type_label=DOC_TYPES.get(main["doc_type"], main["label"]).replace("RG-", "РГ-", 1),
            state="removed" if not main["active"] else "replaced" if len(ds) > 1 else "current",
            sev=max((SEV_RANK.get(x[1], 0) for x in row["f"]), default=0),
            v=[_version(d, found, opinions, cache_dir, base, reviews, corrections, analysed_at,
                         arithmetic)
               for d in ds if d is not main],
            **({"settlement": location["settlement"]} if location and location["settlement"] else {}))
        row["dis"] = _disagreement(row["ops"])
        row["r7"] = r7.get((par, main["station_key"], main["doc_type"]), []) if main["active"] else []
        # "to review": the active current version has a non-informational finding and no verdict
        row["rf"] = bool(main["active"]) and any(
            f["rule"] not in INFORMATIONAL_RULES for f in found.get(main["id"], ()))
        row["todo"] = row["rf"] and row["rv"] is None
        rows.append(row)
    docs_by_url = {d["url"]: d for d in docs}
    for row in rows:
        for version in (row, *row["v"]):
            source = docs_by_url.get(version["u"])
            value = classifications.get(source["id"] if source is not None else None,
                                        {"kind": "uncertain", "evidence": "classification pending",
                                         "legacy": False})
            override_action = overrides.get(version["u"])
            kind = ("rg2" if override_action == "confirm_rg2" else "rg7"
                    if override_action == "quarantine_rg7" else value["kind"])
            status = {"rg2": "confirmed_rg2", "rg7": "quarantined_rg7",
                      "other": "quarantined_other", "uncertain": "pending"}[kind]
            evidence = ("operator marked mislabeled RG-7" if override_action == "quarantine_rg7"
                        else value["evidence"])
            version["classification"] = {**value, "kind": kind, "evidence": evidence,
                                           "override": override_action in ("confirm_rg2", "quarantine_rg7"),
                                           "override_action": override_action, "status": status}
    mark("rows")
    try:
        if selective:
            raise LookupError("selective rows do not build a tally")
        tally = build_tally(store, cfg, default_parent(store, parent), stored_model(cfg.model),
                            cache=None if cache is None else cache.setdefault("tally", {}),
                            docs=docs, arows=arows, expected_rows_cache=expected_rows_cache,
                            include_station_results=True, report_json=report_json)
    except LookupError:
        tally = None
    except Exception:
        log.exception("tally could not be built")
        tally = None
    mark("tally")
    try:
        for station in (tally or {}).get("station_results", []):
            if coordinates := station_coordinates(station["m"], station["no"]):
                station["coordinates"] = coordinates
    except (OSError, ValueError, KeyError):
        log.warning("operator station coordinates unavailable", exc_info=True)
    annulled = {x["id"]: {"rik": x["rik"]} for x in (tally or {}).get("annulled", [])}
    undetermined = {x["id"]: {"rik": x["rik"]} for x in (tally or {}).get("undetermined", [])}
    selected_ids = set((tally or {}).get("selected_ids", []))
    decision_warnings: dict[int, list[dict]] = {}
    for warning in (tally or {}).get("decision_warnings", []):
        decision_warnings.setdefault(warning["id"], []).append(
            {k: warning[k] for k in ("label", "rik")})
    decision_notices: dict[int, list[dict]] = {}
    for notice in (tally or {}).get("decision_notices", []):
        decision_notices.setdefault(notice["id"], []).append(
            {k: notice[k] for k in ("label", "rik")})
    rec = {x["id"]: {k: x[k] for k in ("status", "rules", "details", "gap", "result")}
           for x in (tally or {}).pop("reconciliation", [])}
    for row in rows:
        if selective:
            reason = "Updating"
            row["tally_state"] = "updating"
            row["correctable"] = False
            row["rec"] = {"status": "updating", "rules": [], "details": [reason], "gap": 0,
                          "result": {"corrected": {"included": None, "reason": reason},
                                     "filed": {"included": None, "reason": reason}},
                          "decision_warnings": [], "decision_notices": [],
                          "annulled": None, "undetermined": None}
            row["resolved"] = []
            row["t13"] = []
            continue
        cstatus = row["classification"]["status"]
        row["tally_state"] = ("selected" if row["id"] in selected_ids else
                              "quarantined" if cstatus.startswith("quarantined_") else
                              "pending" if cstatus == "pending" else "superseded")
        row["correctable"] = row["id"] in rec and row["classification"]["status"] == "confirmed_rg2"
        if row["correctable"]:
            reason, included = "tally data unavailable", False
        else:
            reason = {"quarantined_rg7": "quarantined: likely mislabeled RG-7",
                      "quarantined_other": "quarantined: not an RG-2",
                      "pending": "pending document classification"}.get(
                          row["classification"]["status"], "superseded by repeat vote")
            included = None
        row["rec"] = rec.get(row["id"], {"status": "unavailable", "rules": [],
                                          "details": [reason], "gap": 0,
                                          "result": {
                                              "corrected": {"included": included, "reason": reason},
                                              "filed": {"included": included, "reason": reason},
                                          }})
        row["rec"]["decision_warnings"] = decision_warnings.get(row["id"], [])
        row["rec"]["decision_notices"] = decision_notices.get(row["id"], [])
        row["rec"]["annulled"] = annulled.get(row["id"])
        row["rec"]["undetermined"] = undetermined.get(row["id"])
        resolved_rules = set((row.get("oc") or {}).get("resolved_rules", []))
        row["resolved"] = [finding for finding in row["f"] if finding[0] in resolved_rules]
        row["f"] = [finding for finding in row["f"] if finding[0] not in resolved_rules]
        row["sev"] = max((SEV_RANK.get(x[1], 0) for x in row["f"]), default=0)
        row["rf"] = row["state"] != "removed" and any(
            finding[0] not in INFORMATIONAL_RULES for finding in row["f"])
        row["todo"] = row["rf"] and row["rv"] is None
        row["t13"] = []
    if selective:
        return {"db": store.name,
                "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "interval": interval, "elections": {p: e.title for p, e in elections.items()},
                "rows": rows, "tally": None, "rules": RULE_INFO}
    type13 = Type13Corrections(store, cfg, model, expected_rows_cache=expected_rows_cache)
    rows_by_url = {row["u"]: row for row in rows}
    rows_by_station: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        source_doc = docs_by_url.get(row["u"])
        if source_doc is not None and source_doc["station_key"] is not None:
            rows_by_station.setdefault((row["el"], source_doc["station_key"]), []).append(row)
    for source in store.active_type13(parent):
        try:
            state = type13.state(source["url"])
        except Exception:
            log.warning("type-13 state of %s could not be read", source["url"], exc_info=True)
            continue
        target = rows_by_url.get(state["target_url"])
        if target is None:
            candidates = rows_by_station.get((source["parent"], source["station_key"]), [])
            target = max(candidates, key=lambda row: (row["up"] or "", row["id"]), default=None)
        if target is None:
            continue
        base = elections[source["parent"]].base_url if source["parent"] in elections else None
        source_version = _version(source, {}, {}, cache_dir, base)
        target["t13"].append({
            "u": source["url"], "rik": _rik_link(source["url"], base),
            "up": source["uploaded_at"], "pdf": source_version["pdf"],
            "classification": state["classification"],
            "evidence": state["evidence"], "confidence": state["confidence"],
            "resolution": state["resolution"], "can_approve": state["can_approve"],
            "can_edit": state["can_edit"], "source_token": state["source_token"],
        })
    mark("rows")
    return {"db": store.name, "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "interval": interval,
            "elections": {p: e.title for p, e in elections.items()}, "rows": rows, "tally": tally,
            "rules": RULE_INFO}


def _json(data, ensure_ascii: bool = False) -> str:
    """UTF-8 JSON; escape lone surrogates and `<` so writes and script embedding stay safe."""
    text = json.dumps(data, ensure_ascii=ensure_ascii).replace("<", "\\u003c")
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _atomic(path: Path, text: bytes | str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text if isinstance(text, bytes) else text.encode("utf-8"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _remove_obsolete_script(out_dir: Path, name: str, glob: str) -> None:
    """Remove only a script twin produced by an older rg2watch operator build."""
    path = out_dir / f"{name}.js"
    try:
        if path.is_file() and path.read_bytes().startswith(f"window.{glob} = ".encode()):
            path.unlink()
    except OSError:
        log.warning("obsolete operator script could not be removed: %s", path, exc_info=True)


def _write_json(out_dir: Path, name: str, glob: str, data) -> int:
    blob = _json(data).encode("utf-8")
    _atomic(out_dir / f"{name}.json", blob)
    _remove_obsolete_script(out_dir, name, glob)
    return len(blob)


def _data_meta(data: dict) -> dict:
    corrections = {}
    for row in data["rows"]:
        for version in [row, *row["v"]]:
            correction = version.get("oc")
            if correction and correction.get("revision") is not None:
                corrections[version["u"]] = correction["revision"]
    return {"built": data["built"], "generation": data.get("generation"),
            "snapshot_revision": data.get("snapshot_revision"),
            "snapshot_id": data.get("snapshot_id"), "corrections": corrections}


def _clean(out_dir: Path) -> None:
    """Remove temp files left by a crashed write (old enough not to be another writer's)."""
    for t in out_dir.glob("*.tmp"):
        try:
            if time.time() - t.stat().st_mtime > STALE_TMP_S:
                t.unlink()
        except OSError:
            pass


def copy_static(out_dir: Path) -> None:
    """Copy the page and its vendored libraries next to the data, only when they differ."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in STATIC_FILES:
        src = (STATIC / name).read_bytes()
        dst = out_dir / name
        if not dst.is_file() or dst.read_bytes() != src:
            _atomic(dst, src)


def default_parent(store: Store, parent: str | None) -> str:
    """The election the status panel describes: `parent`, else the newest registered one (or the
    newest parent that has documents, e.g. an evaluation DB)."""
    if parent is not None:
        return parent
    els = store.elections()
    return els[-1].parent if els else store.latest_document_parent() or ""


def write_status(store: Store, out_path: Path, parent: str | None, live: dict | None = None,
                 cache_dir: Path | None = None) -> None:
    out_dir = Path(out_path).resolve().parent
    parent = default_parent(store, parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir, "status", "RG2_STATUS", build_status(store, parent, live, cache_dir))


def _build_page_unlocked(store: Store, out_path: Path, cache_dir: Path, parent: str | None = None,
                         cfg: Config | None = None, interval: int = 0,
                         with_status: bool = True, cache: dict | None = None) -> int:
    """Write index.html (+ vendored libraries) and data.js/json next to `out_path`'s directory;
    returns the number of rows. `with_status` also writes status.js/json saying the watcher is not
    running (`watch` has its own status thread and passes False). `cache` keeps evaluated second
    opinions between builds."""
    t0 = time.monotonic()
    out_dir = Path(out_path).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    _clean(out_dir)
    timings: dict[str, float] = {}
    with store.read_snapshot() as snapshot:
        snapshot_parent = default_parent(snapshot, parent)
        _, snapshot_revision, _ = snapshot.operator_feed_state(snapshot_parent)
        data = build_data(snapshot, cache_dir, parent, cfg, interval, cache, timings)
        from .operator_feed import generation_token
        data["generation"] = generation_token(snapshot, cfg or Config(), snapshot_parent)
        data["snapshot_revision"] = snapshot_revision
        group_revisions = snapshot.operator_group_revisions(snapshot_parent)
        for row in data["rows"]:
            row["changed_revision"] = group_revisions.get(
                (row["group_kind"], row["group_key"]), 0)
        data["snapshot_id"] = hashlib.sha256(_json({
            "generation": data["generation"], "revision": snapshot_revision,
            "built": data["built"], "rows": [row["id"] for row in data["rows"]],
        }).encode()).hexdigest()
    t1 = time.monotonic()
    copy_static(out_dir)
    if Path(out_path).name != "index.html":
        _atomic(Path(out_path), (STATIC / "index.html").read_bytes())
    serial = time.monotonic()
    data_blob = _json(data).encode("utf-8")
    meta_blob = _json(_data_meta(data)).encode("utf-8")
    timings["serialization"] = time.monotonic() - serial
    writes = time.monotonic()
    _atomic(out_dir / "data.json", data_blob)
    _remove_obsolete_script(out_dir, "data", "RG2_DATA")
    _atomic(out_dir / "data-meta.json", meta_blob)
    if with_status:
        write_status(store, out_path, parent, cache_dir=cache_dir)
    timings["writes"] = time.monotonic() - writes
    log.info("browse page built: rows=%d opinions=%d bytes=%d "
             "db_reads=%.3fs selection=%.3fs corrections=%.3fs tally=%.3fs rows=%.3fs "
             "serialization=%.3fs writes=%.3fs data=%.2fs total=%.2fs",
             len(data["rows"]), sum(len(r["ops"]) for r in data["rows"]), len(data_blob),
             *(timings.get(name, 0.0) for name in
               ("db_reads", "selection", "corrections", "tally", "rows", "serialization", "writes")),
             t1 - t0, time.monotonic() - t0)
    return len(data["rows"])


def build_page(store: Store, out_path: Path, cache_dir: Path, parent: str | None = None,
               cfg: Config | None = None, interval: int = 0, with_status: bool = True,
               cache: dict | None = None, lock_blocking: bool = False) -> int:
    with snapshot_build_lock(store, out_path, blocking=lock_blocking):
        return _build_page_unlocked(
            store, out_path, cache_dir, parent, cfg, interval, with_status, cache)
