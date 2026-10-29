import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .analyze import ARITHMETIC_PROMPT_VERSION, stored_model
from .config import Config
from .models import DOC_TYPES, Doc
from .operator_corrections import OperatorCorrections, Type13Corrections
from .pipeline import PROMPT_VERSION, RG2_OF_RG7, RG7_PROMPT_VERSION
from .rules import (INFORMATIONAL_RULES, RULE_INFO, change_text, decision_label, decision_sort_key,
                    evaluate_report)
from .rounds import bind_repeat_round
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
               cfg: Config | None = None, interval: int = 0, cache: dict | None = None) -> dict:
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
    selected_model_analyses = [a for a in arows if a["model"] == model]
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
    analysed_at = {a["document_id"]: to_belgrade(a["created_at"]) for a in arows
                   if a["model"] == model and a["prompt_version"] == PROMPT_VERSION}
    elections = {e.parent: e for e in store.elections()}
    reviews = {r["url"]: review_record(r) for r in store.latest_reviews(parent)}
    corrections = {}
    expected_rows_cache = {}
    if latest := store.latest_operator_corrections(parent):
        service = OperatorCorrections(store, cfg, model,
                                      expected_rows_cache=expected_rows_cache)
        current = {a["document_id"] for a in arows if a["model"] == model}
        for item in latest:
            if item["document_id"] not in current:
                continue
            try:
                state = service.state(item["url"])
                corrections[item["url"]] = {
                    k: state[k] for k in ("status", "reason", "at", "revision")}
                if state["resolved_rules"]:
                    corrections[item["url"]]["resolved_rules"] = state["resolved_rules"]
            except Exception:
                log.warning("operator correction state of %s could not be read", item["url"], exc_info=True)
    r7: dict[tuple, list[dict]] = {}  # (election, station_key, RG-2 type) -> its RG-7 decisions
    r7_docs = store.rg7_reports(parent, model, RG7_PROMPT_VERSION, analysed_only=False) if parent else [
        x for p in elections for x in store.rg7_reports(p, model, RG7_PROMPT_VERSION, analysed_only=False)]
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
                 f=sorted(r7_found.get(d["id"], []), key=lambda f: -SEV_RANK.get(f[1], 0)))
        types = RG2_OF_RG7.get(d["doc_type"], ())
        if d["doc_type"] in (23, 24, 25, 26):
            at = uploaded(d["uploaded_at"])
            starts = round_starts.get((d["parent"], d["station_key"]), {})
            bound = bind_repeat_round(at, starts)
            types = (bound,) if bound is not None else ()
        for t in types:
            r7.setdefault((d["parent"], d["station_key"], t), []).append(x)
    groups: dict[tuple, list] = {}
    for d in docs:  # a NULL station_key cannot be grouped: the document stands alone
        key = d["station_key"] if d["station_key"] is not None else ("id", d["id"])
        groups.setdefault((d["parent"], key, d["doc_type"]), []).append(d)
    rows = []
    for (par, _, _), ds in groups.items():
        ds.sort(key=lambda d: (d["uploaded_at"] or "", d["id"]), reverse=True)
        main = next((d for d in ds if d["active"]), ds[0])
        base = elections[par].base_url if par in elections else None
        row = _version(main, found, opinions, cache_dir, base, reviews, corrections, analysed_at,
                       arithmetic)
        location = station_location(main["municipality"] or "", main["station_no"] or "")
        row.update(
            id=main["id"], el=par, muni=main["municipality"] or UNKNOWN, no=main["station_no"],
            name=main["station_name"], label=main["label"], doc_type=main["doc_type"],
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
    try:
        tally = build_tally(store, cfg, default_parent(store, parent), stored_model(cfg.model),
                            cache=None if cache is None else cache.setdefault("tally", {}),
                            docs=docs, arows=arows, expected_rows_cache=expected_rows_cache,
                            include_station_results=True, report_json=report_json)
    except Exception:
        log.exception("tally could not be built")
        tally = None
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


def _write_pair(out_dir: Path, name: str, glob: str, data) -> None:
    blob = _json(data)
    _atomic(out_dir / f"{name}.json", blob)
    _atomic(out_dir / f"{name}.js", f"window.{glob} = {blob};\n")


def _data_meta(data: dict) -> dict:
    corrections = {}
    for row in data["rows"]:
        for version in [row, *row["v"]]:
            correction = version.get("oc")
            if correction and correction.get("revision") is not None:
                corrections[version["u"]] = correction["revision"]
    return {"built": data["built"], "corrections": corrections}


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
    _write_pair(out_dir, "status", "RG2_STATUS", build_status(store, parent, live, cache_dir))


def build_page(store: Store, out_path: Path, cache_dir: Path, parent: str | None = None,
               cfg: Config | None = None, interval: int = 0, with_status: bool = True,
               cache: dict | None = None) -> int:
    """Write index.html (+ vendored libraries) and data.js/json next to `out_path`'s directory;
    returns the number of rows. `with_status` also writes status.js/json saying the watcher is not
    running (`watch` has its own status thread and passes False). `cache` keeps evaluated second
    opinions between builds."""
    t0 = time.monotonic()
    out_dir = Path(out_path).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    _clean(out_dir)
    data = build_data(store, cache_dir, parent, cfg, interval, cache)
    t1 = time.monotonic()
    copy_static(out_dir)
    if Path(out_path).name != "index.html":
        _atomic(Path(out_path), (STATIC / "index.html").read_bytes())
    _write_pair(out_dir, "data", "RG2_DATA", data)
    _atomic(out_dir / "data-meta.json", _json(_data_meta(data)))
    if with_status:
        write_status(store, out_path, parent, cache_dir=cache_dir)
    log.info("browse page built: %d form(s), %d second opinion(s); data %.2f s, total %.2f s",
             len(data["rows"]), sum(len(r["ops"]) for r in data["rows"]), t1 - t0,
             time.monotonic() - t0)
    return len(data["rows"])
