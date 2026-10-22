import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .analyze import stored_model
from .config import Config
from .models import Doc
from .operator_corrections import OperatorCorrections
from .pipeline import PROMPT_VERSION, RG2_OF_RG7, RG7_PROMPT_VERSION
from .rules import RULE_INFO, change_text, decision_label, decision_sort_key, evaluate_report
from .status import _rik_link, build_status, to_belgrade
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
              cache: dict) -> dict[int, list[dict]]:
    """document id -> [{m, at, f}]: findings of the newest stored analysis of each model (current
    prompt version preferred), from the current rules (pure, no API). `cache` keeps the evaluated
    findings by analysis id across builds (None = could not be evaluated, logged once)."""
    rows = [a for a in arows if a["document_id"] in docs]
    live = {a["id"] for a in rows}
    for gone in set(cache) - live:
        del cache[gone]
    todo = [a["id"] for a in rows if a["id"] not in cache]
    reports = store.analysis_reports(todo)
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
             reviews: dict | None = None, corrections: dict | None = None) -> dict:
    pdf = hashlib.sha256(d["url"].encode()).hexdigest()
    f = [[x["rule"], x["severity"], x["summary"]] for x in found.get(d["id"], [])]
    f.sort(key=lambda x: -SEV_RANK.get(x[1], 0))
    return {"up": d["uploaded_at"], "seen": to_belgrade(d["first_seen_at"]), "f": f, "ast": _ast(d),
            "pdf": pdf if (cache_dir / (pdf + ".pdf")).is_file() else None,
            "rik": _rik_link(d["url"], base), "ops": opinions.get(d["id"], []),
            "u": d["url"], "rv": (reviews or {}).get(d["url"]),
            "oc": (corrections or {}).get(d["url"])}


def build_data(store: Store, cache_dir: Path, parent: str | None = None,
               cfg: Config | None = None, interval: int = 0, cache: dict | None = None) -> dict:
    cache_dir = Path(cache_dir).resolve()
    docs, findings = store.browse_rows(parent)
    found: dict[int, list] = {}
    for f in findings:
        found.setdefault(f["document_id"], []).append(f)
    arows = store.browse_analyses(parent, PROMPT_VERSION)
    opinions = _opinions(store, {d["id"]: _doc(d) for d in docs}, arows, cfg or Config(),
                         {} if cache is None else cache.setdefault("ops", {}))
    elections = {e.parent: e for e in store.elections()}
    reviews = {r["url"]: review_record(r) for r in store.latest_reviews(parent)}
    cfg = cfg or Config()
    model = stored_model(cfg.model)
    corrections = {}
    if latest := store.latest_operator_corrections(parent):
        service = OperatorCorrections(store, cfg, model)
        for item in latest:
            try:
                state = service.state(item["url"])
                corrections[item["url"]] = {k: state[k] for k in ("status", "reason", "at")}
            except Exception:
                log.warning("operator correction state of %s could not be read", item["url"], exc_info=True)
    r7: dict[tuple, list[dict]] = {}  # (election, station_key, RG-2 type) -> its RG-7 decisions
    r7_docs = store.rg7_reports(parent, model, RG7_PROMPT_VERSION, analysed_only=False) if parent else [
        x for p in elections for x in store.rg7_reports(p, model, RG7_PROMPT_VERSION, analysed_only=False)]
    r7_found: dict[int, list] = {}
    for f in store.rg7_findings(parent):
        r7_found.setdefault(f["document_id"], []).append([f["rule"], f["severity"], f["summary"]])
    readers = store.rg7_readers(parent, RG7_PROMPT_VERSION)
    for d, rep in sorted(r7_docs, key=lambda x: decision_sort_key(x[0]["uploaded_at"], x[0]["id"], x[1])):
        base = elections[d["parent"]].base_url if d["parent"] in elections else None
        x = _version(d, {}, {}, cache_dir, base, corrections=corrections)
        x.update(d=decision_label(d["uploaded_at"], rep),
                 ch=[change_text(c) for c in rep["changes"]] if rep else None,
                 by=[m for m in readers.get(d["id"], []) if m != model],  # read by other models only
                 f=sorted(r7_found.get(d["id"], []), key=lambda f: -SEV_RANK.get(f[1], 0)))
        for t in RG2_OF_RG7.get(d["doc_type"], ()):
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
        row = _version(main, found, opinions, cache_dir, base, reviews, corrections)
        row.update(
            id=main["id"], el=par, muni=main["municipality"] or UNKNOWN, no=main["station_no"],
            name=main["station_name"], label=main["label"],
            state="removed" if not main["active"] else "replaced" if len(ds) > 1 else "current",
            sev=max((SEV_RANK.get(x[1], 0) for x in row["f"]), default=0),
            v=[_version(d, found, opinions, cache_dir, base, reviews, corrections)
               for d in ds if d is not main])
        row["dis"] = _disagreement(row["ops"])
        row["r7"] = r7.get((par, main["station_key"], main["doc_type"]), []) if main["active"] else []
        # "to review": the active current version has stored findings and no verdict on its URL
        row["rf"] = bool(main["active"]) and bool(found.get(main["id"]))
        row["todo"] = row["rf"] and row["rv"] is None
        rows.append(row)
    try:
        tally = build_tally(store, cfg, default_parent(store, parent), stored_model(cfg.model),
                            cache=None if cache is None else cache.setdefault("tally", {}),
                            docs=docs, arows=arows)
    except Exception:
        log.exception("tally could not be built")
        tally = None
    rec = {x["id"]: {k: x[k] for k in ("status", "rules", "details")}
           for x in (tally or {}).pop("reconciliation", [])}
    for row in rows:
        row["rec"] = rec.get(row["id"], {"status": "unavailable", "rules": [],
                                         "details": ["not available for the currently counted tally"]})
    return {"db": store.name, "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "interval": interval,
            "elections": {p: e.title for p, e in elections.items()}, "rows": rows, "tally": tally,
            "rules": RULE_INFO}


def _json(data) -> str:
    """ASCII-only JSON (a lone surrogate cannot break the write); `<` escaped for script safety."""
    return json.dumps(data, ensure_ascii=True).replace("<", "\\u003c")


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
    if with_status:
        write_status(store, out_path, parent, cache_dir=cache_dir)
    log.info("browse page built: %d form(s), %d second opinion(s); data %.2f s, total %.2f s",
             len(data["rows"]), sum(len(r["ops"]) for r in data["rows"]), t1 - t0,
             time.monotonic() - t0)
    return len(data["rows"])
