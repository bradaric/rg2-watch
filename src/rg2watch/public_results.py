"""Sanitized, self-contained public export of unofficial aggregate and station results."""

import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .analyze import stored_model
from .browse import STATIC, _atomic, _json
from .config import Config
from .store import Store
from .tally import build_tally


TOTAL_KEYS = ("registered", "voted", "turnout", "ballots_in_box", "invalid", "valid",
              "outside_voted", "vote_forms", "withheld_valid", "lists", "lists_valid",
              "lists_ballots_in_box")
REVIEW_KEYS = ("ok", "escalate", "unsure", "undecided")
RECORDS_TITLE_PREFIX = re.compile(r"^(?:записници|zapisnici)(?:\s*[-–—:]\s*|\s+|$)", re.I)
SERBIAN_CYRILLIC = "абвгдђежзијклљмнњопрстћуфхцчџш"
_SERBIAN_ORDER = {char: index for index, char in enumerate(SERBIAN_CYRILLIC)}


def _display_title(title: str) -> str:
    return RECORDS_TITLE_PREFIX.sub("", title).strip()


def _serbian_sort_key(text: str) -> tuple[int, ...]:
    """Deterministic Serbian Cyrillic collation without relying on an installed OS locale."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return tuple(-1 if char.isspace() else _SERBIAN_ORDER.get(char, 1000 + ord(char))
                 for char in normalized)


def _station_sort_key(station: dict) -> tuple:
    number = tuple((0, int(part)) if part.isdigit() else (1, _serbian_sort_key(part))
                   for part in re.split(r"(\d+)", str(station["no"])) if part)
    return _serbian_sort_key(station["m"]), number


def _review_counts(items: list[dict], verdicts: dict[int, str]) -> dict[str, int]:
    counts = {key: 0 for key in REVIEW_KEYS}
    seen: set[int] = set()
    for item in items:
        document_id = item["id"]
        if document_id in seen:
            continue
        seen.add(document_id)
        verdict = verdicts.get(document_id)
        counts[verdict if verdict in REVIEW_KEYS[:-1] else "undecided"] += 1
    return counts


def _public_row(row: dict, flagged: list[dict], verdicts: dict[int, str]) -> dict:
    totals = {key: row["all"][key] for key in TOTAL_KEYS if key in row["all"]}
    out = {
        "m": row["m"],
        "generated_unknown_muni": bool(row.get("generated_unknown_muni")),
        "stations": row["stations"],
        "counted": row["counted"],
        "held": row.get("held", 0),
        "annulled": row.get("annulled", 0),
        "held_annulled": row.get("held_annulled", 0),
        "clean": row["clean"],
        "flagged": len({item["id"] for item in flagged}),
        "review": _review_counts(flagged, verdicts),
        "totals": totals,
    }
    for key in ("names", "fallback_name_indices", "names_note"):
        if key in row:
            out[key] = row[key]
    return out


def _public_station(station: dict) -> dict:
    corrected = station["corrected"]
    return {
        "m": station["m"],
        "no": station["no"],
        "name": station["name"],
        "included": corrected["included"],
        "annulled": station["annulled"] is not None,
        "registered": corrected["registered"],
        "voted": corrected["voted"],
        "ballots_in_box": corrected["ballots_in_box"],
        "invalid": corrected["invalid"],
        "valid": corrected["valid"],
        "lists": corrected["lists"],
    }


def build_public_results(store: Store, cfg: Config, parent: str) -> dict:
    """Return sanitized aggregate and corrected station data for public distribution."""
    tally = build_tally(store, cfg, parent, stored_model(cfg.model), include_station_results=True)
    verdicts = {row["document_id"]: row["verdict"] for row in store.latest_reviews(parent)
                if row["active"] and not row["superseded"]}
    excluded = tally["excluded"]

    def flagged_for(row: dict) -> list[dict]:
        return excluded if row is tally["overall"] else [x for x in excluded if x["m"] == row["m"]]

    return {
        "version": 2,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": _display_title(tally["title"]),
        "scope": tally["scope"],
        "scope_note": tally["scope_note"],
        "names": tally["names"],
        "fallback_name_indices": tally["fallback_name_indices"],
        "names_note": tally["names_note"],
        "overall": _public_row(tally["overall"], flagged_for(tally["overall"]), verdicts),
        "municipalities": [_public_row(row, flagged_for(row), verdicts)
                           for row in sorted(tally["munis"], key=lambda x: _serbian_sort_key(x["m"]))],
        "stations": [_public_station(station)
                     for station in sorted(tally["station_results"], key=_station_sort_key)],
    }


def render_public_results(data: dict) -> str:
    template = (STATIC / "public_results.html").read_text(encoding="utf-8")
    marker = "__RG2_PUBLIC_DATA__"
    if template.count(marker) != 1:
        raise ValueError("public results template must contain exactly one data marker")
    return template.replace(marker, _json(data, ensure_ascii=True))


def write_public_results(store: Store, cfg: Config, parent: str, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = list(out_dir.iterdir())
    if any(path.name != "index.html" or not path.is_file() for path in entries):
        raise ValueError("public results output directory must contain only index.html")
    out = out_dir / "index.html"
    _atomic(out, render_public_results(build_public_results(store, cfg, parent)))
    return out
