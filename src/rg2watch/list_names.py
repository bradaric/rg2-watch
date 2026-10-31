import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .analyze import AnalysisResult, analyze_document
from .config import Config
from .models import Doc
from .render import render_tiles
from .store import Store, _doc
from .tally import UNKNOWN_COUNCIL, UNKNOWN_MUNI, configured_names, scope_of


RG2_ROUND_RANK = {6: 0, 10: 1, 15: 2, 16: 3}


@dataclass(frozen=True)
class Candidate:
    group: str | None
    doc: Doc
    path: Path


@dataclass(frozen=True)
class Selection:
    scope: str
    candidates: list[Candidate]
    unavailable: list[str]


@dataclass(frozen=True)
class Extraction:
    group: str | None
    names: list[str]
    confidence: str
    note: str
    source_url: str


def _group(row, scope: str) -> str | None:
    if scope == "national":
        return None
    if scope == "council":
        return row["minority"] or UNKNOWN_COUNCIL
    return row["municipality"] or UNKNOWN_MUNI


def _configured(cfg: Config, parent: str, scope: str, group: str | None) -> bool:
    names, _ = configured_names(cfg, parent, scope, group)
    return bool(names)


def select_candidates(store: Store, cfg: Config, parent: str,
                      cache_dir: Path) -> Selection:
    scope, _ = scope_of(store, cfg, parent)
    rows, _ = store.browse_rows(parent)
    grouped: dict[str | None, list] = {}
    for row in rows:
        if row["active"] and row["doc_type"] in RG2_ROUND_RANK:
            grouped.setdefault(_group(row, scope), []).append(row)

    candidates: list[Candidate] = []
    unavailable: list[str] = []
    for group in sorted(grouped, key=lambda value: value or ""):
        if _configured(cfg, parent, scope, group):
            continue
        cached = []
        for row in grouped[group]:
            path = Path(cache_dir) / (hashlib.sha256(row["url"].encode()).hexdigest() + ".pdf")
            if path.is_file():
                cached.append((row, path))
        if not cached:
            unavailable.append(group or "(national ballot)")
            continue
        row, path = min(cached, key=lambda item: (
            item[0]["analysis_status"] != "done",
            RG2_ROUND_RANK[item[0]["doc_type"]],
            item[0]["id"],
        ))
        candidates.append(Candidate(group, _doc(row), path))
    return Selection(scope, candidates, unavailable)


def extract_candidate(candidate: Candidate, client, cfg: Config) -> tuple[Extraction, AnalysisResult]:
    result = analyze_document(render_tiles(candidate.path), client, cfg, "list_names")
    report = result.report
    extraction = Extraction(
        candidate.group,
        [item["name"] for item in report["lists"]],
        report["confidence"],
        report["note"],
        candidate.doc.url,
    )
    return extraction, result


def extract_all(candidates: Iterable[Candidate], extract: Callable[[Candidate], Extraction]
                ) -> tuple[list[Extraction], list[tuple[str, str]]]:
    results: list[Extraction] = []
    errors: list[tuple[str, str]] = []
    for candidate in candidates:
        try:
            results.append(extract(candidate))
        except Exception as exc:
            errors.append((candidate.group or "(national ballot)", str(exc)))
    return results, errors


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _comment(value: str) -> str:
    return " ".join(value.split()).replace("#", "＃")


def format_toml(parent: str, scope: str, configured: list[str] | dict[str, list[str]] | None,
                extractions: list[Extraction]) -> str:
    by_group = {item.group: item for item in extractions}
    if scope == "national":
        names = configured if isinstance(configured, list) and configured else by_group[None].names
        item = by_group.get(None)
        lines = []
        if item:
            lines += [f"# source: {_comment(item.source_url)}",
                      f"# confidence: {item.confidence}" +
                      (f"; {_comment(item.note)}" if item.note else "")]
        lines.append(f"{_quoted(parent)} = {_quoted(names)}")
        return "\n".join(lines) + "\n"

    merged = dict(configured) if isinstance(configured, dict) else {}
    merged.update({item.group: item.names for item in extractions if item.group is not None})
    lines = []
    if isinstance(configured, dict) and configured:
        lines.append("# Existing configured ballot groups are retained unchanged below.")
    for group in sorted(by_group, key=lambda value: value or ""):
        item = by_group.get(group)
        lines += [f"# {group} — source: {_comment(item.source_url)}",
                  f"# {group} — confidence: {item.confidence}" +
                  (f"; {_comment(item.note)}" if item.note else "")]
    entries = ", ".join(f"{_quoted(group)} = {_quoted(merged[group])}"
                        for group in sorted(merged))
    lines.append(f"{_quoted(parent)} = {{{entries}}}")
    return "\n".join(lines) + "\n"
