"""Snapshot of what the watcher is doing, for the status panel of the browse page."""
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .store import Store

RS = ZoneInfo("Europe/Belgrade")


def to_belgrade(utc_iso: str | None) -> str | None:
    """UTC "YYYY-MM-DDTHH:MM:SSZ" (first_seen_at, created_at) as Serbian local "YYYY-MM-DDTHH:MM",
    the same clock as uploaded_at."""
    try:
        t = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return utc_iso
    return t.astimezone(RS).strftime("%Y-%m-%dT%H:%M")


def build_status(store: Store, parent: str, live: dict | None = None) -> dict:
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
    return {
        "ts": round(time.time(), 1), "watching": "watcher" in live, "parent": parent,
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
        "alerts": [[to_belgrade(r["created_at"]), r["severity"], r["rule"], r["municipality"] or "",
                    r["station_no"] or "", r["summary"], r["alerted_at"] is not None]
                   for r in store.recent_findings(parent, 20)],
        "ntfy": live.get("ntfy"),
    }
