"""`analyze --parent`: re-analyse stored RG-2 forms (second opinions), spend recorded under adhoc."""
import dataclasses
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic
import httpx

from .analyze import AnalysisError, OllamaUnavailable, analyze_document, is_ollama, stored_model
from .browse import SEV_RANK
from .config import Config
from .evaluate import TOKENS_PER_TILE, count_tiles, usd_for
from .fetch import fetch_pdf
from .models import Doc
from .names import resolve_municipalities
from .pipeline import BAD_REQUEST_GLOBAL, DOWNLOAD_BLOCKED_PAUSE_AFTER, GLOBAL_ERRORS, PROMPT_VERSION, _is_overload, _short
from .render import RenderError, render_tiles
from .rik_client import RikBlocked, RikClient, RikError
from .rules import evaluate_report
from .store import Store, _doc

log = logging.getLogger("rg2watch.batch")
ADHOC = "adhoc"  # parent that `analyze` records its spend under
ARITHMETIC_PROMPT_VERSION = "a1"
BACKOFF_S = (10, 60)  # waits before the retries of one form
ASSUMED_TILES = 4  # per form whose PDF is not cached yet (estimate only)
MAX_WAIT_S = 1800  # cumulative overload backoff per batch before giving up
_sleep = time.sleep


@dataclass
class Selection:
    docs: list[Doc]
    state: dict[str, str]  # url -> current / replaced / removed


def _station(no: str) -> str:
    return (no or "").strip().lstrip("0")


def select_forms(store: Store, parent: str, *, municipalities: list[str] | None = None,
                 stations: list[str] | None = None, rules: list[str] | None = None,
                 severity: str | None = None, state: str | None = None,
                 limit: int | None = None) -> Selection:
    """Stored RG-2 forms of `parent`, filters ANDed. A form is a station + document type; its
    state is browse's: removed (no active version; the newest one is used), replaced (an active
    version with older ones) or current. Without `state`, forms with an active version."""
    docs, found = store.browse_rows(parent)
    by_doc: dict[int, list] = {}
    for f in found:
        by_doc.setdefault(f["document_id"], []).append(f)
    groups: dict[tuple, list] = {}
    for d in docs:
        groups.setdefault((d["station_key"], d["doc_type"]), []).append(d)
    names = None
    if municipalities:
        names = set(resolve_municipalities({d["municipality"]: "" for d in docs}, municipalities))
    stations = [_station(n) for n in stations or []]
    floor = SEV_RANK[severity] if severity else 0
    picked = []
    for ds in groups.values():
        ds.sort(key=lambda d: (d["uploaded_at"] or "", d["id"]), reverse=True)
        main = next((d for d in ds if d["active"]), ds[0])
        form_state = "removed" if not main["active"] else "replaced" if len(ds) > 1 else "current"
        if form_state == "removed" and state != "removed" or state and state != form_state:
            continue
        if names is not None and main["municipality"] not in names:
            continue
        if stations and _station(main["station_no"]) not in stations:
            continue
        if rules or severity:
            if not any((not rules or f["rule"] in rules) and SEV_RANK.get(f["severity"], 0) >= floor
                       for f in by_doc.get(main["id"], [])):
                continue
        picked.append((main, form_state))
    picked.sort(key=lambda p: (p[0]["municipality"] or "", _station(p[0]["station_no"]).zfill(6),
                               p[0]["doc_type"]))
    picked = picked[:limit] if limit else picked
    return Selection([_doc(d) for d, _ in picked], {d["url"]: s for d, s in picked})


def estimate(docs: list[Doc], cache_dir: Path, cfg: Config) -> tuple[int, int, int, float]:
    """(forms, tiles, forms assumed at ASSUMED_TILES because not downloaded yet, usd)."""
    tiles = assumed = 0
    for d in docs:
        try:
            tiles += count_tiles(cache_dir / (hashlib.sha256(d.url.encode()).hexdigest() + ".pdf"))
        except (RenderError, OSError):
            tiles += ASSUMED_TILES
            assumed += 1
    return len(docs), tiles, assumed, usd_for(len(docs), tiles, cfg)


def describe_estimate(docs: list[Doc], cache_dir: Path, cfg: Config) -> tuple[str, float]:
    forms, tiles, assumed, usd = estimate(docs, cache_dir, cfg)
    note = f" ({assumed} not downloaded yet, assumed {ASSUMED_TILES} tiles each)" if assumed else ""
    return (f"{forms} form(s) to analyse, {tiles} tiles{note}: estimated cost ${usd:.2f} (estimate, "
            f"actual may differ; {cfg.model}, ~{TOKENS_PER_TILE // 1000}k tokens in per tile + "
            f"~10k tokens out per form)"), usd


def _print_findings(report: dict, doc: Doc, cfg: Config, out) -> None:
    try:
        found = evaluate_report(report, doc, cfg)
    except Exception as exc:  # a malformed stored report must not end the batch
        print(f"    rules failed: {_short(exc)}", file=out)
        return
    for f in found:
        print(f"    [{f.severity}] {f.rule} {f.summary}", file=out)
    if not found:
        print("    no findings", file=out)


def _print_arithmetic(report: dict, out) -> None:
    def value(key):
        field = report[key]
        return field["value"] if field["legible"] and isinstance(field["value"], int) else None

    voted, box, invalid, valid = (value(k) for k in
                                  ("voted", "ballots_in_box", "invalid", "valid"))
    if voted is None or box is None:
        print("    A1 unavailable: voted or ballots in box unreadable", file=out)
    elif box > voted:
        print(f"    A1 ballots in box {box} > voted {voted}", file=out)
    else:
        print(f"    A1 reconcile: ballots in box {box} <= voted {voted}", file=out)
    if None in (box, invalid, valid):
        print("    A2 unavailable: box, invalid or valid unreadable", file=out)
    elif invalid + valid != box:
        print(f"    A2 invalid {invalid} + valid {valid} != ballots in box {box}", file=out)
    else:
        print(f"    A2 reconcile: invalid {invalid} + valid {valid} = ballots in box {box}", file=out)
    rows = report["list_votes"]
    complete = bool(rows) and [r["row"] for r in rows] == list(range(1, rows[-1]["row"] + 1)) \
        and all(r["legible"] and isinstance(r["value"], int) for r in rows)
    if valid is None or not complete:
        print("    A3 unavailable: valid or complete list rows unreadable", file=out)
    else:
        total = sum(r["value"] for r in rows)
        op = "=" if total == valid else "!="
        status = "reconcile" if total == valid else "list votes differ"
        print(f"    A3 {status}: list sum {total} {op} valid {valid} ({len(rows)} rows)", file=out)


def run_batch(docs: list[Doc], store: Store, rik: RikClient, ai, cfg: Config, cache_dir: Path,
              max_usd: float, out, err, kind: str = "rg2",
              prompt_version: str = PROMPT_VERSION) -> dict:
    """Analyse each form once, one at a time; returns analysed/cached/failed counts, spend, stopped."""
    model = stored_model(cfg.model)
    res = {"analysed": 0, "cached": 0, "failed": 0, "cost_usd": 0.0, "stopped": None,
           "unrecorded": 0}
    blocked = bad = 0  # consecutive download blocks / distinct forms rejected with 400
    waited = 0

    def record(doc: Doc, save) -> None:
        try:
            save()
        except Exception:
            res["unrecorded"] += 1
            log.error("spend of the analysis of %s could not be recorded", doc.url, exc_info=True)

    for doc in docs:
        label = f"{doc.municipality} BM {doc.station_no}"
        report = store.latest_report(doc.url, model, prompt_version)
        if report is not None:
            res["cached"] += 1
            print(f"{label} | {model} | cached, $0.000", file=out)
            (_print_arithmetic(report, out) if kind == "arithmetic"
             else _print_findings(report, doc, cfg, out))
            continue
        if res["cost_usd"] >= max_usd:
            res["stopped"] = f"spend cap ${max_usd:.2f} reached"
            print(f"stopping: {res['stopped']}", file=err)
            break
        result = None
        for attempt in range(len(BACKOFF_S) + 1):
            try:
                path = fetch_pdf(rik, doc.url, cache_dir)
                blocked = 0
                tiles = render_tiles(path)
                result = (analyze_document(tiles, ai, cfg, kind) if kind != "rg2"
                          else analyze_document(tiles, ai, cfg))
                bad = 0
                break
            except Exception as exc:
                cost = getattr(exc, "cost_usd", 0.0)
                if cost:
                    res["cost_usd"] += cost
                    record(doc, lambda: store.save_failed_analysis(
                        doc.url, model, prompt_version, cost, str(exc)[:200], parent=ADHOC))
                if isinstance(exc, GLOBAL_ERRORS):
                    res["stopped"] = f"API refused the request ({type(exc).__name__}): {exc}"
                    break
                if isinstance(exc, RikBlocked):
                    blocked += 1
                    if blocked >= DOWNLOAD_BLOCKED_PAUSE_AFTER:
                        res["stopped"] = f"RIK blocked {blocked} downloads in a row: {_short(exc)}"
                elif isinstance(exc, anthropic.BadRequestError):
                    bad += 1
                    if bad >= BAD_REQUEST_GLOBAL:
                        res["stopped"] = f"{bad} forms in a row rejected as bad requests: {_short(exc)}"
                if res["stopped"]:
                    break
                if _is_overload(exc) and attempt < len(BACKOFF_S):
                    if res["cost_usd"] >= max_usd:
                        res["stopped"] = f"spend cap ${max_usd:.2f} reached"
                        break
                    if waited + BACKOFF_S[attempt] > MAX_WAIT_S:
                        res["stopped"] = "API still busy after 30 min of waiting"
                        break
                    log.warning("%s: %s; retrying in %d s", label, _short(exc), BACKOFF_S[attempt])
                    waited += BACKOFF_S[attempt]
                    _sleep(BACKOFF_S[attempt])
                    continue
                if not isinstance(exc, (RikError, httpx.HTTPError, RenderError, AnalysisError,
                                        OllamaUnavailable)) and not _is_overload(exc):
                    log.exception("analysis of %s failed", doc.url)
                res["failed"] += 1
                print(f"{label} | {model} | skipped: {_short(exc)}", file=err)
                break
        if res["stopped"]:
            print(f"stopping: {res['stopped']}", file=err)
            break
        if result is None:
            continue
        res["cost_usd"] += result.cost_usd
        res["analysed"] += 1
        # keyed by the requested model so the cache lookup hits; the responding one stays in the report
        stored = dataclasses.replace(result, model=model,
                                     report={**result.report, "responding_model": result.model})
        record(doc, lambda: store.save_analysis(doc.url, stored, prompt_version, parent=ADHOC))
        print(f"{label} | {model} | ${result.cost_usd:.3f}", file=out)
        (_print_arithmetic(result.report, out) if kind == "arithmetic"
         else _print_findings(result.report, doc, cfg, out))
    return res
