import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from rg2watch.models import Doc, Election, Event, Finding, Scope, doc_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS elections(
    parent TEXT PRIMARY KEY, round TEXT, election_type TEXT, title TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY, parent TEXT NOT NULL, municipality TEXT, station_no TEXT,
    station_name TEXT, station_key TEXT, doc_type INTEGER, label TEXT,
    url TEXT NOT NULL UNIQUE, uploaded_at TEXT, first_seen_at TEXT,
    sha256 TEXT, status TEXT DEFAULT 'new', active INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS documents_parent ON documents(parent, active);
CREATE TABLE IF NOT EXISTS analyses(
    id INTEGER PRIMARY KEY, document_id INTEGER, model TEXT, prompt_version TEXT,
    result_json TEXT, created_at TEXT, cost_usd REAL);
CREATE TABLE IF NOT EXISTS findings(
    id INTEGER PRIMARY KEY, document_id INTEGER, parent TEXT, municipality TEXT,
    station_no TEXT, rule TEXT, severity TEXT, summary TEXT, details_json TEXT,
    created_at TEXT, alerted_at TEXT, UNIQUE(document_id, rule));
CREATE TABLE IF NOT EXISTS sweeps(
    id INTEGER PRIMARY KEY, parent TEXT, lane TEXT, started_at TEXT, finished_at TEXT,
    pages INTEGER, records INTEGER, added INTEGER, replaced INTEGER, removed INTEGER);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, sweep_id INTEGER, parent TEXT, kind TEXT, document_id INTEGER,
    station_key TEXT, doc_type INTEGER, url TEXT, uploaded_at TEXT, detected_at TEXT,
    previous_url TEXT);
CREATE TABLE IF NOT EXISTS municipalities(
    parent TEXT, id TEXT, name TEXT, PRIMARY KEY(parent, id));
CREATE TABLE IF NOT EXISTS stations(
    parent TEXT, municipality_id TEXT, station_id TEXT, station_no TEXT,
    PRIMARY KEY(parent, station_id));
CREATE TABLE IF NOT EXISTS counts(
    parent TEXT, scope TEXT, value INTEGER, updated_at TEXT, PRIMARY KEY(parent, scope));
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _previous_url(details_json: str | None) -> str | None:
    try:
        return json.loads(details_json).get("previous_url") if details_json else None
    except (ValueError, AttributeError):
        return None


def _doc(row) -> Doc:
    return Doc(row["municipality"], row["station_no"], row["station_name"], row["label"],
               row["doc_type"], row["url"], row["uploaded_at"])


class Store:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(events)")}
        if "previous_url" not in cols:
            with self._db:
                self._db.execute("ALTER TABLE events ADD COLUMN previous_url TEXT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, sql: str, args=()):
        with self._lock, self._db:
            return self._db.execute(sql, args)

    def upsert_election(self, e: Election) -> None:
        self._write(
            "INSERT INTO elections(parent, round, election_type, title, url) VALUES(?,?,?,?,?) "
            "ON CONFLICT(parent) DO UPDATE SET round=excluded.round, "
            "election_type=excluded.election_type, title=excluded.title, url=excluded.url",
            (e.parent, e.round, e.election_type, e.title, e.base_url))

    def get_election(self, parent: str) -> Election | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM elections WHERE parent=?", (parent,)).fetchone()
        return Election(r["parent"], r["round"], r["election_type"], r["title"], r["url"]) if r else None

    def known_docs(self, parent: str, scope: Scope) -> dict[tuple[str, int | str], Doc]:
        sql, args = "SELECT * FROM documents WHERE parent=? AND active=1", [parent]
        if scope.doc_type is not None:
            sql += " AND doc_type=?"
            args.append(scope.doc_type)
        if scope.municipality is not None:
            sql += " AND municipality=?"
            args.append(scope.municipality)
        if scope.station_key is not None:
            sql += " AND station_key=?"
            args.append(scope.station_key)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id", args).fetchall()
        return {doc_key(d): d for d in map(_doc, rows)}

    def apply_events(self, parent: str, sweep_id: int, events: list[Event]) -> None:
        now = _now()
        with self._lock, self._db:
            for ev in events:
                d = ev.doc
                if ev.kind == "removed":
                    self._db.execute("UPDATE documents SET active=0 WHERE url=?", (d.url,))
                else:
                    if ev.kind == "replaced" and ev.previous_url:
                        self._db.execute("UPDATE documents SET active=0 WHERE url=?",
                                         (ev.previous_url,))
                    if d.doc_type is not None:
                        self._db.execute(
                            "UPDATE documents SET active=0 WHERE parent=? AND station_key=? "
                            "AND doc_type=? AND url<>? AND active=1",
                            (parent, d.station_key, d.doc_type, d.url))
                    else:
                        lbl = " ".join(d.label.split())
                        rows = self._db.execute(
                            "SELECT id, label FROM documents WHERE parent=? AND station_key=? "
                            "AND doc_type IS NULL AND url<>? AND active=1",
                            (parent, d.station_key, d.url)).fetchall()
                        for r in rows:
                            if " ".join((r["label"] or "").split()) == lbl:
                                self._db.execute("UPDATE documents SET active=0 WHERE id=?",
                                                 (r["id"],))
                    self._db.execute(
                        "INSERT INTO documents(parent, municipality, station_no, station_name, "
                        "station_key, doc_type, label, url, uploaded_at, first_seen_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET active=1",
                        (parent, d.municipality, d.station_no, d.station_name, d.station_key,
                         d.doc_type, d.label, d.url, d.uploaded_at, now))
                row = self._db.execute("SELECT id FROM documents WHERE url=?", (d.url,)).fetchone()
                self._db.execute(
                    "INSERT INTO events(sweep_id, parent, kind, document_id, station_key, "
                    "doc_type, url, uploaded_at, detected_at, previous_url) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sweep_id, parent, ev.kind, row["id"] if row else None, d.station_key,
                     d.doc_type, d.url, d.uploaded_at, now, ev.previous_url))

    def start_sweep(self, parent: str, lane: str) -> int:
        return self._write("INSERT INTO sweeps(parent, lane, started_at) VALUES(?,?,?)",
                           (parent, lane, _now())).lastrowid

    def finish_sweep(self, sweep_id: int, pages: int, records: int, added: int,
                     replaced: int, removed: int) -> None:
        self._write(
            "UPDATE sweeps SET finished_at=?, pages=?, records=?, added=?, replaced=?, removed=? "
            "WHERE id=?", (_now(), pages, records, added, replaced, removed, sweep_id))

    def get_count(self, parent: str, scope_key: str) -> int | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM counts WHERE parent=? AND scope=?",
                                   (parent, scope_key)).fetchone()
        return row["value"] if row else None

    def set_count(self, parent: str, scope_key: str, value: int) -> None:
        self._write(
            "INSERT INTO counts(parent, scope, value, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(parent, scope) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at", (parent, scope_key, value, _now()))

    def save_municipalities(self, parent: str, items: dict[str, str]) -> None:
        """items: municipality id -> name."""
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO municipalities(parent, id, name) VALUES(?,?,?)",
                [(parent, i, n) for i, n in items.items()])

    def save_stations(self, parent: str, municipality_id: str, items: dict[str, str]) -> None:
        """items: station_no -> station id."""
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO stations(parent, municipality_id, station_id, station_no) "
                "VALUES(?,?,?,?)", [(parent, municipality_id, sid, no) for no, sid in items.items()])

    def municipality_ids(self, parent: str) -> dict[str, str]:
        """name -> id."""
        with self._lock:
            rows = self._db.execute("SELECT id, name FROM municipalities WHERE parent=?",
                                    (parent,)).fetchall()
        return {r["name"]: r["id"] for r in rows}

    def station_ids(self, parent: str, municipality_id: str) -> dict[str, str]:
        """station_no -> station id."""
        with self._lock:
            rows = self._db.execute(
                "SELECT station_no, station_id FROM stations WHERE parent=? AND municipality_id=?",
                (parent, municipality_id)).fetchall()
        return {r["station_no"]: r["station_id"] for r in rows}

    def add_finding(self, parent: str, f: Finding, document_url: str) -> bool:
        with self._lock, self._db:
            row = self._db.execute("SELECT id FROM documents WHERE url=?",
                                   (document_url,)).fetchone()
            if row is None:
                raise KeyError(f"unknown document url: {document_url}")
            cur = self._db.execute(
                "INSERT OR IGNORE INTO findings(document_id, parent, municipality, station_no, "
                "rule, severity, summary, created_at, details_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (row["id"], parent, f.doc.municipality, f.doc.station_no, f.rule, f.severity,
                 f.summary, _now(),
                 json.dumps({"previous_url": f.previous_url}) if f.previous_url else None))
            return cur.rowcount == 1

    def unalerted(self, parent: str, severities: set[str]) -> list[tuple[int, Finding]]:
        if not severities:
            return []
        marks = ",".join("?" * len(severities))
        with self._lock:
            rows = self._db.execute(
                "SELECT f.id AS fid, f.rule, f.severity, f.summary, f.details_json, d.* FROM findings f "
                "JOIN documents d ON d.id=f.document_id "
                f"WHERE f.parent=? AND f.alerted_at IS NULL AND f.severity IN ({marks}) "
                "ORDER BY f.id", [parent, *sorted(severities)]).fetchall()
        return [(r["fid"], Finding(r["rule"], r["severity"], _doc(r), r["summary"],
                                   _previous_url(r["details_json"]))) for r in rows]

    def mark_alerted(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock, self._db:
            self._db.executemany("UPDATE findings SET alerted_at=? WHERE id=?",
                                 [(_now(), i) for i in ids])
