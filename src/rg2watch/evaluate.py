"""Golden-set evaluation: analyse labelled local PDFs and score each rule against hand labels."""
import json
import logging
from pathlib import Path

import anthropic

from .analyze import AnalysisError, analyze_document
from .config import Config
from .models import Doc, Event
from .pipeline import GLOBAL_ERRORS, PROMPT_VERSION
from .render import RenderError, page_count, render_tiles
from .rules import evaluate_report
from .store import Store

log = logging.getLogger("rg2watch.evaluate")

PARENT = "eval"
TOKENS_PER_TILE = 4800
OUTPUT_TOKENS = 3000
MAX_TILES = 16


def load_labels(path: Path) -> list[dict]:
    labels = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            entry["file"], entry.setdefault("expect", {})
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path}:{n}: bad label line ({exc})") from exc
        labels.append(entry)
    return labels


def score(labels: list[dict], findings_by_file: dict[str, set[str]]) -> dict[str, dict]:
    """Per rule: tp/fp/fn/tn from each label's `expect`, for files that were analysed.

    precision = tp / (tp + fp), recall = tp / (tp + fn); None when the denominator is 0.
    """
    out: dict[str, dict] = {}
    for entry in labels:
        found = findings_by_file.get(entry["file"])
        if found is None:
            continue
        for rule, expected in (entry.get("expect") or {}).items():
            row = out.setdefault(rule, {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
            fired = rule in found
            key = ("tp" if fired else "fn") if expected else ("fp" if fired else "tn")
            row[key] += 1
    for row in out.values():
        row["precision"] = row["tp"] / (row["tp"] + row["fp"]) if row["tp"] + row["fp"] else None
        row["recall"] = row["tp"] / (row["tp"] + row["fn"]) if row["tp"] + row["fn"] else None
    return dict(sorted(out.items()))


def format_table(table: dict[str, dict]) -> str:
    def pct(v):
        return "-" if v is None else f"{v:.0%}"

    lines = [f"{'rule':<6}{'tp':>4}{'fp':>4}{'fn':>4}{'tn':>4}{'precision':>11}{'recall':>8}"]
    for rule, r in table.items():
        lines.append(f"{rule:<6}{r['tp']:>4}{r['fp']:>4}{r['fn']:>4}{r['tn']:>4}"
                     f"{pct(r['precision']):>11}{pct(r['recall']):>8}")
    return "\n".join(lines)


def estimate_cost(labels: list[dict], pdf_dir: Path, cfg: Config) -> tuple[int, int, float]:
    """(forms, tiles, usd) upper bound: up to two tiles per page, capped at MAX_TILES."""
    forms = tiles = 0
    for entry in labels:
        path = Path(pdf_dir) / entry["file"]
        try:
            n = min(2 * page_count(path), MAX_TILES)
        except (RenderError, OSError):
            continue
        forms += 1
        tiles += n
    usd = (tiles * TOKENS_PER_TILE * cfg.price_in_per_mtok
           + forms * OUTPUT_TOKENS * cfg.price_out_per_mtok) / 1_000_000
    return forms, tiles, usd


def _register(store: Store, entry: dict, path: Path) -> Doc:
    doc = Doc(entry.get("municipality", ""), str(entry.get("station_no", "")), "", "", 6,
              path.resolve().as_uri(), "")
    store.apply_events(PARENT, store.start_sweep(PARENT, "eval"), [Event("added", doc)])
    store.mark_analysis(doc.url, "done")  # never picked up by a watch worker
    return doc


def run_eval(labels: list[dict], pdf_dir: Path, store: Store, client, cfg: Config, *,
             cached: bool, out) -> dict:
    """Analyse (or reuse stored analyses of) each label's PDF; returns files, errors, score, cost."""
    files: dict[str, dict] = {}
    errors: dict[str, str] = {}
    cost = 0.0
    for entry in labels:
        name = entry["file"]
        path = Path(pdf_dir) / name
        if not path.is_file():
            errors[name] = "pdf not found"
            continue
        try:
            doc = _register(store, entry, path)
            report = store.latest_report(doc.url) if cached else None
            if report is None and cached:
                errors[name] = "no stored analysis (run without --cached)"
                continue
            if report is None:
                if store.total_spend(PARENT) >= cfg.max_spend_usd:
                    errors[name] = "spend cap reached"
                    print(f"spend cap ${cfg.max_spend_usd:.2f} reached: stopping", file=out)
                    break
                tiles = render_tiles(path)
                result = analyze_document(tiles, client, cfg)
                store.save_analysis(doc.url, result, PROMPT_VERSION)
                cost += result.cost_usd
                report = result.report
            found = evaluate_report(report, doc, cfg)
        except RenderError as exc:
            errors[name] = f"unreadable PDF: {exc}"
            continue
        except GLOBAL_ERRORS as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
            print(f"API error affecting every form, stopping: {exc}", file=out)
            break
        except Exception as exc:  # one bad form must not end the run; keep billed cost visible
            billed = getattr(exc, "cost_usd", 0.0)
            if billed:
                cost += billed
                try:
                    store.save_failed_analysis(path.resolve().as_uri(), cfg.model, PROMPT_VERSION,
                                               billed, str(exc)[:200])
                except Exception:
                    log.error("billed cost $%.3f for %s not recorded", billed, name)
            errors[name] = f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"
            if not isinstance(exc, (AnalysisError, anthropic.APIError)):
                log.exception("evaluation of %s failed", name)
            continue
        files[name] = {"findings": [{"rule": f.rule, "severity": f.severity, "summary": f.summary}
                                    for f in found]}
    table = score(labels, {n: {f["rule"] for f in v["findings"]} for n, v in files.items()})
    return {"files": files, "errors": errors, "score": table, "cost_usd": cost}
