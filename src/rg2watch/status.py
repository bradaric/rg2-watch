"""Snapshot of what the watcher is doing, for the status panel of the browse page."""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from .store import RG7_TYPES, Store

RS = ZoneInfo("Europe/Belgrade")


def _rik_link(url: str, base: str | None) -> str | None:
    full = urljoin(base + "/", url) if base else url
    return full if urlsplit(full).scheme in ("http", "https") else None


def to_belgrade(utc_iso: str | None) -> str | None:
    """UTC "YYYY-MM-DDTHH:MM:SSZ" (first_seen_at, created_at) as Serbian local "YYYY-MM-DDTHH:MM",
    the same clock as uploaded_at."""
    try:
        t = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return utc_iso
    return t.astimezone(RS).strftime("%Y-%m-%dT%H:%M")


def build_status(store: Store, parent: str, live: dict | None = None,
                 cache_dir: Path | None = None) -> dict:
    """`live` (watch process only) holds the in-memory snapshots: watcher, analysis, ntfy, url, only.
    Without it the DB-derived parts are filled and the watcher is reported as not running."""
    live = live or {}
    c = store.status_counts(parent)
    e = store.get_election(parent)
    ast = c.pop("ast")
    retrying = c.pop("retrying")
    spend = c.pop("spend")
    a = live.get("analysis")
    queued = ast.get("new", 0) + retrying
    avg = a["avg_s"] if a else None
    def alert(r) -> list:
        links = [None, None]
        if r["doc_type"] in RG7_TYPES:
            digest = hashlib.sha256(r["url"].encode()).hexdigest()
            pdf = digest if cache_dir is not None and (Path(cache_dir) / (digest + ".pdf")).is_file() else None
            links = [pdf, _rik_link(r["url"], e.base_url if e else None)]
        return [to_belgrade(r["created_at"]), r["severity"], r["rule"], r["municipality"] or "",
                r["station_no"] or "", r["summary"], r["alerted_at"] is not None, r["url"], *links]

    return {
        "ts": round(time.time(), 1), "db": store.name, "watching": "watcher" in live, "parent": parent,
        "title": e.title if e else parent, "url": live.get("url"),
        "watcher": live.get("watcher"),
        "docs": c,
        "analysis": {
            "on": a is not None, "model": a["model"] if a else None, "queued": queued,
            "running": ast.get("running", 0), "done": ast.get("done", 0),
            "failed": ast.get("failed", 0) - retrying, "retrying": retrying,
            "skipped": ast.get("skipped", 0), "paused": a["paused"] if a else None,
            "spend": spend, "cap": a["cap"] if a else None, "avg_s": avg,
            "eta_s": round(queued * avg / a["workers"]) if a and avg and queued else None},
        "alerts": [alert(r) for r in store.recent_findings(parent, 20)],
        "ntfy": live.get("ntfy"),
    }


WATCHER_FRESH_S = 60


def format_status(store: Store, parent: str, status_file, cfg_cap: float, now: float | None = None) -> str:
    """Compact plain-text status from the DB; the watcher is "running" when `status_file` (written
    every 10 s by `watch`) is for this election, says watching and is younger than 60 s."""
    st = build_status(store, parent)
    sev = store.run_summary(parent)["findings"]
    verdicts: dict[str, int] = {}
    for r in store.latest_reviews(parent):
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    a, d = st["analysis"], st["docs"]
    try:
        live = json.loads(status_file.read_text(encoding="utf-8"))
        running = bool(live.get("watching") and live.get("parent") == parent
                       and (now or time.time()) - live["ts"] < WATCHER_FRESH_S)
    except (OSError, ValueError, KeyError, TypeError):
        running, live = False, {}
    page = f"page {live['url']}" if running and live.get("url") else None
    last = to_belgrade(store.last_alert_at(parent))
    expected = f" of {d['rg2_rik']} on RIK" if d.get("rg2_rik") else ""
    return "\n".join([
        f"election {parent}: {st['title']}",
        f"RG-2 seen {d['rg2_seen']}{expected}, stations {d['stations']}",
        *([f"RG-7 decisions: read {d['rg7']['read']} / pending {d['rg7']['pending']}"]
          if d["rg7"]["read"] or d["rg7"]["pending"] else []),
        f"analysis done {a['done']}, failed {a['failed']}, queued {a['queued']}",
        f"spend ${a['spend']:.2f} of ${cfg_cap:.2f} cap",
        f"to review {len(store.review_todo(parent))}, escalated {verdicts.get('escalate', 0)}, "
        f"unsure {verdicts.get('unsure', 0)}, ok {verdicts.get('ok', 0)}",
        "findings " + ", ".join(f"{k} {sev.get(k, 0)}" for k in ("HIGH", "MEDIUM", "LOW")),
        f"last alert {last.replace('T', ' ') if last else 'none'}",
        "watcher running" if running else "watcher not running", *([page] if page else [])])
