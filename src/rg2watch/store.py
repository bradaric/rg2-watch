import json
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from rg2watch.models import DOC_TYPES, Doc, Election, Event, Finding, Scope, doc_key

if TYPE_CHECKING:
    from rg2watch.analyze import AnalysisResult

ANALYSED_TYPES = (6, 10, 15, 16)  # RG-2 and repeat-vote RG-2
MAX_ANALYSIS_ATTEMPTS = 3
BROWSE_CHUNK = 2000
STATUS_RARE = (7, 8, 9, 14, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31)  # RG-7/8/9 families, Primedba
_ANALYSED = ",".join(map(str, ANALYSED_TYPES))
VERDICTS = ("ok", "escalate", "unsure")  # operator review of one document version (URL)
NOTE_MAX = 500

INDEXES = """
CREATE INDEX IF NOT EXISTS analyses_doc ON analyses(document_id, model);
CREATE INDEX IF NOT EXISTS analyses_parent_cost ON analyses(parent, cost_usd);
CREATE INDEX IF NOT EXISTS events_parent_kind ON events(parent, kind);
CREATE INDEX IF NOT EXISTS reviews_url ON reviews(url, id);
"""  # after the migrations: older DBs lack some of these columns until then

SCHEMA = """
CREATE TABLE IF NOT EXISTS elections(
    parent TEXT PRIMARY KEY, round TEXT, election_type TEXT, title TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY, parent TEXT NOT NULL, municipality TEXT, station_no TEXT,
    station_name TEXT, station_key TEXT, doc_type INTEGER, label TEXT,
    url TEXT NOT NULL UNIQUE, uploaded_at TEXT, first_seen_at TEXT,
    sha256 TEXT, status TEXT DEFAULT 'new', active INTEGER DEFAULT 1,
    analysis_status TEXT, analysis_attempts INTEGER DEFAULT 0, analysis_next_at REAL,
    analysis_error TEXT);
CREATE INDEX IF NOT EXISTS documents_parent ON documents(parent, active);
CREATE TABLE IF NOT EXISTS analyses(
    id INTEGER PRIMARY KEY, document_id INTEGER, model TEXT, prompt_version TEXT,
    result_json TEXT, created_at TEXT, cost_usd REAL, status TEXT, error TEXT, parent TEXT);
CREATE TABLE IF NOT EXISTS analysis_state(parent TEXT PRIMARY KEY, enabled_at TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS reviews(
    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, url TEXT NOT NULL, verdict TEXT NOT NULL,
    note TEXT, created_at TEXT NOT NULL);
"""


def clean_note(note: str | None) -> str | None:
    """A review note without control characters (tabs/newlines become spaces), stripped; None
    when empty. ValueError when longer than NOTE_MAX."""
    note = "".join(" " if c in "\t\n\r" else c for c in (note or "")
                   if c in "\t\n\r" or unicodedata.category(c) != "Cc").strip()
    if len(note) > NOTE_MAX:
        raise ValueError(f"note longer than {NOTE_MAX} characters")
    return note or None


def _in_municipalities(names: Collection[str] | None) -> tuple[str, list[str]]:
    """SQL suffix and args restricting documents to these municipality names (None = no filter)."""
    if names is None:
        return "", []
    return f" AND municipality IN ({','.join('?' * len(names))})", sorted(names)


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
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(analyses)")}
        with self._db:
            for col in ("status", "error", "parent"):
                if col not in cols:
                    self._db.execute(f"ALTER TABLE analyses ADD COLUMN {col} TEXT")
            if "parent" not in cols:  # spend is capped per election
                self._db.execute("UPDATE analyses SET parent=(SELECT parent FROM documents "
                                 "WHERE documents.id=analyses.document_id)")
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(documents)")}
        if "analysis_status" not in cols:
            with self._db:
                self._db.execute("ALTER TABLE documents ADD COLUMN analysis_status TEXT")
                self._db.execute(
                    "ALTER TABLE documents ADD COLUMN analysis_attempts INTEGER DEFAULT 0")
                self._db.execute("ALTER TABLE documents ADD COLUMN analysis_next_at REAL")
                self._db.execute("ALTER TABLE documents ADD COLUMN analysis_error TEXT")
                # documents recorded before analysis existed are never analysed implicitly
                # (`requeue --skipped` or `watch --analyze-backlog` opts them in)
                self._db.execute("UPDATE documents SET analysis_status='skipped'")
        self._db.executescript(INDEXES)

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

    def elections(self) -> list[Election]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM elections ORDER BY parent").fetchall()
        return [Election(r["parent"], r["round"], r["election_type"], r["title"], r["url"]) for r in rows]

    def find_document(self, url: str) -> tuple[str, Doc] | None:
        """(parent, doc) for a stored document URL."""
        with self._lock:
            r = self._db.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
        return (r["parent"], _doc(r)) if r else None

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
                        "station_key, doc_type, label, url, uploaded_at, first_seen_at, "
                        "analysis_status) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(url) DO UPDATE SET active=1",
                        (parent, d.municipality, d.station_no, d.station_name, d.station_key,
                         d.doc_type, d.label, d.url, d.uploaded_at, now,
                         "new" if d.doc_type in ANALYSED_TYPES else "skipped"))
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

    # analysis queue --------------------------------------------------------
    def queue_analysis(self, url: str) -> bool:
        """Queue an RG-2 document that is not queued yet; returns True when it is pending."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE documents SET analysis_status='new' WHERE url=? AND "
                f"(analysis_status IS NULL OR analysis_status='skipped') AND doc_type IN ({_ANALYSED})",
                (url,))
            row = self._db.execute("SELECT analysis_status FROM documents WHERE url=?",
                                   (url,)).fetchone()
        return row is not None and row["analysis_status"] == "new"

    def enable_analysis(self, parent: str) -> str:
        """Record when analysis was first enabled for `parent` (kept on later calls) and return it.
        The first time, RG-2 forms seen before that moment and still pending are marked skipped:
        analysis is opt-in and never bills for a backlog unless asked."""
        now = _now()
        with self._lock, self._db:
            if self._db.execute("INSERT OR IGNORE INTO analysis_state(parent, enabled_at) "
                                "VALUES(?,?)", (parent, now)).rowcount:
                self._db.execute(
                    "UPDATE documents SET analysis_status='skipped', analysis_next_at=NULL "
                    "WHERE parent=? AND first_seen_at < ? AND (analysis_status='new' OR "
                    "(analysis_status='failed' AND analysis_next_at IS NOT NULL))", (parent, now))
            return self._db.execute("SELECT enabled_at FROM analysis_state WHERE parent=?",
                                    (parent,)).fetchone()["enabled_at"]

    def analysis_backlog(self, parent: str, municipalities: Collection[str] | None = None) -> int:
        """Active RG-2 forms of `parent` that are not queued (seen before analysis was enabled)."""
        where, extra = _in_municipalities(municipalities)
        with self._lock:
            return self._db.execute(
                f"SELECT COUNT(*) FROM documents WHERE parent=? AND active=1 AND doc_type IN "
                f"({_ANALYSED}) AND analysis_status='skipped'{where}", (parent, *extra)).fetchone()[0]

    def queued_for_analysis(self, parent: str, municipalities: Collection[str] | None = None) -> int:
        """Active RG-2 forms of `parent` waiting for (or retrying) analysis."""
        where, extra = _in_municipalities(municipalities)
        with self._lock:
            return self._db.execute(
                f"SELECT COUNT(*) FROM documents WHERE parent=? AND active=1 AND doc_type IN "
                f"({_ANALYSED}) AND analysis_attempts < ? AND (analysis_status='new' OR "
                f"(analysis_status='failed' AND analysis_next_at IS NOT NULL)){where}",
                (parent, MAX_ANALYSIS_ATTEMPTS, *extra)).fetchone()[0]

    def unfinished_analysis(self, parent: str, municipalities: Collection[str] | None = None) -> int:
        """Active RG-2 forms of `parent` queued, running or waiting for a retry."""
        where, extra = _in_municipalities(municipalities)
        with self._lock:
            return self._db.execute(
                f"SELECT COUNT(*) FROM documents WHERE parent=? AND active=1 AND doc_type IN "
                f"({_ANALYSED}) AND (analysis_status='running' OR (analysis_attempts < ? AND "
                f"(analysis_status='new' OR (analysis_status='failed' AND analysis_next_at IS NOT "
                f"NULL)))){where}", (parent, MAX_ANALYSIS_ATTEMPTS, *extra)).fetchone()[0]

    def run_summary(self, parent: str, municipalities: Collection[str] | None = None) -> dict:
        """End-of-run counts: documents by type, RG-2 analysis outcomes, findings by severity."""
        where, extra = _in_municipalities(municipalities)
        with self._lock:
            rows = lambda sql: self._db.execute(sql, (parent, *extra)).fetchall()
            docs = {DOC_TYPES.get(t, str(t)): n for t, n in rows(
                f"SELECT doc_type, COUNT(*) FROM documents WHERE parent=? AND active=1{where} "
                "GROUP BY 1")}
            ast = {r[0]: r[1] for r in rows(
                f"SELECT analysis_status, COUNT(*) FROM documents WHERE parent=? AND active=1 AND "
                f"doc_type IN ({_ANALYSED}){where} GROUP BY 1")}
            retrying = rows(f"SELECT COUNT(*) FROM documents WHERE parent=? AND active=1 AND "
                            f"doc_type IN ({_ANALYSED}) AND analysis_status='failed' AND "
                            f"analysis_next_at IS NOT NULL{where}")[0][0]
            sev = {r[0]: r[1] for r in rows(
                f"SELECT severity, COUNT(*) FROM findings WHERE parent=?{where} GROUP BY 1")}
        return {"docs": docs, "done": ast.get("done", 0), "failed": ast.get("failed", 0) - retrying,
                "skipped": ast.get("skipped", 0), "findings": sev}

    def requeue_skipped(self, parent: str | None = None,
                        municipalities: Collection[str] | None = None) -> int:
        """Queue skipped RG-2 forms (the backlog from before analysis was enabled)."""
        sql = (f"UPDATE documents SET analysis_status='new' WHERE analysis_status='skipped' "
               f"AND doc_type IN ({_ANALYSED})")
        args: list = []
        if parent is not None:
            sql += " AND parent=?"
            args.append(parent)
        where, extra = _in_municipalities(municipalities)
        return self._write(sql + where, [*args, *extra]).rowcount

    def next_for_analysis(self, limit: int, parent: str | None = None,
                          now: float | None = None,
                          municipalities: Collection[str] | None = None) -> list[Doc]:
        """Claim up to `limit` due documents (marked running, attempts+1), newest first.

        Due: active RG-2 types, attempts < 3, status new, or failed with a retry time
        that has passed (permanent failures have no retry time).
        """
        now = time.time() if now is None else now
        sql = (f"SELECT * FROM documents WHERE active=1 AND doc_type IN ({_ANALYSED}) "
               "AND analysis_attempts < ? AND (analysis_status='new' OR (analysis_status='failed' "
               "AND analysis_next_at IS NOT NULL AND analysis_next_at <= ?))")
        args: list = [MAX_ANALYSIS_ATTEMPTS, now]
        if parent is not None:
            sql += " AND parent=?"
            args.append(parent)
        where, extra = _in_municipalities(municipalities)
        sql, args = sql + where, [*args, *extra]
        with self._lock, self._db:
            rows = self._db.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()
            self._db.executemany(
                "UPDATE documents SET analysis_status='running', "
                "analysis_attempts=analysis_attempts+1, analysis_next_at=NULL WHERE id=?",
                [(r["id"],) for r in rows])
        return [_doc(r) for r in rows]

    def analysis_attempts(self, url: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT analysis_attempts FROM documents WHERE url=?",
                                   (url,)).fetchone()
        return row["analysis_attempts"] if row else 0

    def mark_analysis(self, url: str, status: str, error: str | None = None,
                      retry_at: float | None = None, refund: bool = False) -> None:
        """status new|done|failed; a failed document is retried at `retry_at` (None = never).
        `refund` gives back the attempt counted when the document was claimed."""
        self._write(
            "UPDATE documents SET analysis_status=?, analysis_next_at=?, analysis_error=?, "
            "analysis_attempts=MAX(0, analysis_attempts - ?) WHERE url=?",
            (status, retry_at if status == "failed" else None, error, int(refund), url))

    def requeue_running(self, parent: str,
                        municipalities: Collection[str] | None = None) -> list[Doc]:
        """Documents of `parent` left running by a previous process go back to new; those that
        already used every attempt (a crash loop) become failed and are returned."""
        where, extra = _in_municipalities(municipalities)
        with self._lock, self._db:
            exhausted = self._db.execute(
                "SELECT * FROM documents WHERE analysis_status='running' AND parent=? "
                f"AND analysis_attempts >= ?{where}", (parent, MAX_ANALYSIS_ATTEMPTS, *extra)).fetchall()
            self._db.execute(
                "UPDATE documents SET analysis_status = CASE WHEN analysis_attempts >= ? "
                "THEN 'failed' ELSE 'new' END, analysis_next_at=NULL "
                f"WHERE analysis_status='running' AND parent=?{where}",
                (MAX_ANALYSIS_ATTEMPTS, parent, *extra))
        return [_doc(r) for r in exhausted]

    def requeue_failed(self, parent: str | None = None, all_failed: bool = False) -> int:
        """Reset failed documents to new with 0 attempts: those that used every attempt (X2),
        or with `all_failed` every failed one (also D3/X1). Returns the number reset."""
        sql = "UPDATE documents SET analysis_status='new', analysis_attempts=0, " \
              "analysis_next_at=NULL, analysis_error=NULL WHERE analysis_status='failed'"
        args: list = []
        if not all_failed:
            sql += " AND analysis_attempts >= ?"
            args.append(MAX_ANALYSIS_ATTEMPTS)
        if parent is not None:
            sql += " AND parent=?"
            args.append(parent)
        return self._write(sql, args).rowcount

    def save_analysis(self, url: str, result: "AnalysisResult", prompt_version: str,
                      parent: str | None = None) -> None:
        """`parent` defaults to the document's election (whose spend cap it counts against)."""
        self._insert_analysis(url, result.model, prompt_version, json.dumps(result.report),
                              result.cost_usd, "done", None, parent)

    def save_failed_analysis(self, url: str, model: str, prompt_version: str, cost_usd: float,
                             error: str, parent: str | None = None) -> None:
        """Record a billed attempt that produced no usable report (keeps total_spend accurate)."""
        self._insert_analysis(url, model, prompt_version, None, cost_usd, "failed", error, parent)

    def _insert_analysis(self, url, model, prompt_version, result_json, cost_usd, status, error,
                         parent):
        with self._lock, self._db:
            row = self._db.execute("SELECT id, parent FROM documents WHERE url=?", (url,)).fetchone()
            self._db.execute(
                "INSERT INTO analyses(document_id, model, prompt_version, result_json, created_at, "
                "cost_usd, status, error, parent) VALUES(?,?,?,?,?,?,?,?,?)",
                (row["id"] if row else None, model, prompt_version, result_json, _now(), cost_usd,
                 status, error, parent if parent is not None else row["parent"] if row else None))

    def latest_report(self, url: str, model: str, prompt_version: str) -> dict | None:
        """Report of the newest successful analysis of a document by this model and prompt version."""
        with self._lock:
            row = self._db.execute(
                "SELECT a.result_json FROM analyses a JOIN documents d ON d.id=a.document_id "
                "WHERE d.url=? AND a.model=? AND a.prompt_version=? AND a.status='done' "
                "AND a.result_json IS NOT NULL ORDER BY a.id DESC LIMIT 1",
                (url, model, prompt_version)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def browse_rows(self, parent: str | None = None) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """(RG-2 documents, their findings), oldest first; optionally of one election. Read in
        chunks so the store lock is never held for one giant fetch."""
        where, args = f"doc_type IN ({_ANALYSED})", ()
        if parent is not None:
            where, args = where + " AND parent=?", (parent,)
        docs: list[sqlite3.Row] = []
        found: list[sqlite3.Row] = []
        for table, out, extra in (
                ("documents", docs, ""),
                ("findings", found, f"document_id IN (SELECT id FROM documents WHERE {where}) AND ")):
            cols = "*" if table == "documents" else "id, document_id, rule, severity, summary"
            sql_where = (where + " AND " if table == "documents" else extra)
            last = 0
            while True:
                with self._lock:
                    rows = self._db.execute(
                        f"SELECT {cols} FROM {table} WHERE {sql_where}id > ? ORDER BY id LIMIT ?",
                        (*args, last, BROWSE_CHUNK)).fetchall()
                out.extend(rows)
                if len(rows) < BROWSE_CHUNK:
                    break
                last = rows[-1]["id"]
        return docs, found

    def browse_analyses(self, parent: str | None = None,
                        prompt_version: str | None = None) -> list[sqlite3.Row]:
        """Newest successful analysis of each (RG-2 document, model), without the report (see
        `analysis_reports`): any parent it was recorded under, incl. adhoc second opinions. The
        current `prompt_version` wins over older ones when a model has both."""
        where, args = f"d.doc_type IN ({_ANALYSED})", []
        if parent is not None:
            where, args = where + " AND d.parent=?", [parent]
        with self._lock:
            return self._db.execute(
                "SELECT a.id, a.document_id, a.model, a.prompt_version, a.created_at "
                f"FROM analyses a JOIN documents d ON d.id=a.document_id WHERE {where} "
                "AND a.status='done' AND a.result_json IS NOT NULL AND a.id=(SELECT b.id FROM analyses b "
                "WHERE b.document_id=a.document_id AND b.model IS a.model AND b.status='done' "
                "AND b.result_json IS NOT NULL ORDER BY b.prompt_version IS ? DESC, b.id DESC LIMIT 1) "
                "ORDER BY a.id", (*args, prompt_version)).fetchall()

    def analysis_reports(self, ids: list[int]) -> dict[int, str]:
        """result_json by analysis id, fetched in small chunks."""
        out: dict[int, str] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT id, result_json FROM analyses WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()
            out.update((r["id"], r["result_json"]) for r in rows)
        return out

    def station_counts(self, parent: str) -> dict[str, int]:
        """Known stations (from the stations table) per municipality name."""
        with self._lock:
            rows = self._db.execute(
                "SELECT m.name, COUNT(*) FROM stations s JOIN municipalities m "
                "ON m.parent=s.parent AND m.id=s.municipality_id WHERE s.parent=? GROUP BY m.name",
                (parent,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def tally_municipalities(self, parent: str) -> list[str]:
        """Municipality names of an election: from its first-round forms and the municipality list."""
        with self._lock:
            rows = self._db.execute(
                "SELECT COALESCE(municipality, '(unknown)') FROM documents WHERE parent=? AND doc_type=6 "
                "UNION SELECT name FROM municipalities WHERE parent=?", (parent, parent)).fetchall()
        return sorted(r[0] for r in rows)

    def latest_document_parent(self) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT parent FROM documents ORDER BY id DESC LIMIT 1").fetchone()
        return row["parent"] if row else None

    def recent_findings(self, parent: str, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT created_at, severity, rule, municipality, station_no, summary, alerted_at "
                "FROM findings WHERE parent=? ORDER BY id DESC LIMIT ?", (parent, limit)).fetchall()

    def last_alert_at(self, parent: str) -> str | None:
        with self._lock:
            return self._db.execute("SELECT MAX(alerted_at) FROM findings WHERE parent=?",
                                    (parent,)).fetchone()[0]

    def status_counts(self, parent: str) -> dict:
        """Cheap counts for the live status panel."""
        def since(minutes: int) -> str:
            return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

        base = f"FROM documents WHERE parent=? AND doc_type IN ({_ANALYSED})"
        ev = f"FROM events WHERE parent=? AND doc_type IN ({_ANALYSED}) AND kind="
        with self._lock:
            one = lambda sql, *a: self._db.execute(sql, (parent, *a)).fetchone()[0]
            ast = {r[0]: r[1] for r in self._db.execute(
                f"SELECT COALESCE(analysis_status, 'none'), COUNT(*) {base} AND active=1 GROUP BY 1",
                (parent,))}
            rik = self._db.execute("SELECT value FROM counts WHERE parent=? AND scope='rg2'",
                                   (parent,)).fetchone()
            return {
                "rg2_seen": one(f"SELECT COUNT(DISTINCT COALESCE(station_key, id)) {base} AND active=1"),
                "rg2_rik": rik["value"] if rik else None,
                "stations": one("SELECT COUNT(*) FROM stations WHERE parent=?"),
                "new10": one(f"SELECT COUNT(*) {base} AND first_seen_at >= ?", since(10)),
                "new60": one(f"SELECT COUNT(*) {base} AND first_seen_at >= ?", since(60)),
                "replaced": one(f"SELECT COUNT(*) {ev}'replaced'"),
                "removed": one(f"SELECT COUNT(*) {ev}'removed'"),
                "rare": {DOC_TYPES[t]: n for t, n in self._db.execute(
                    "SELECT doc_type, COUNT(*) FROM documents WHERE parent=? AND active=1 AND "
                    f"doc_type IN ({','.join(map(str, STATUS_RARE))}) GROUP BY 1", (parent,))},
                "ast": ast,
                "retrying": one(f"SELECT COUNT(*) {base} AND active=1 AND analysis_status='failed' "
                                "AND analysis_next_at IS NOT NULL"),
                "spend": float(one("SELECT COALESCE(SUM(cost_usd), 0) FROM analyses WHERE parent=?")),
            }

    def set_review(self, url: str, verdict: str, note: str | None = None) -> sqlite3.Row | None:
        """Record a verdict on the document version at `url` (history kept, latest wins); the stored
        review row, or None when `url` is not a stored document."""
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}: {verdict!r}")
        note = clean_note(note)
        with self._lock, self._db:
            d = self._db.execute("SELECT id FROM documents WHERE url=?", (url,)).fetchone()
            if d is None:
                return None
            cur = self._db.execute(
                "INSERT INTO reviews(document_id, url, verdict, note, created_at) VALUES(?,?,?,?,?)",
                (d["id"], url, verdict, note, _now()))
            return self._db.execute("SELECT * FROM reviews WHERE id=?", (cur.lastrowid,)).fetchone()

    def latest_reviews(self, parent: str | None = None) -> list[sqlite3.Row]:
        """The newest review of each reviewed URL, with its document's parent, municipality,
        station, active flag and whether a newer version replaced it; newest first."""
        where, args = ("WHERE d.parent=?", (parent,)) if parent is not None else ("", ())
        with self._lock:
            return self._db.execute(
                "SELECT r.*, d.parent, d.municipality, d.station_no, d.active, EXISTS (SELECT 1 FROM "
                "documents n WHERE n.parent=d.parent AND n.station_key=d.station_key AND "
                "n.doc_type=d.doc_type AND n.active=1 AND n.id<>d.id) AS superseded FROM reviews r "
                "JOIN (SELECT url, MAX(id) AS id FROM reviews GROUP BY url) l ON l.id=r.id "
                f"JOIN documents d ON d.id=r.document_id {where} ORDER BY r.id DESC", args).fetchall()

    def review_todo(self, parent: str | None = None) -> list[sqlite3.Row]:
        """"To review": active (current) documents with stored findings and no verdict on their
        URL, oldest first; removed forms and replaced versions are not included."""
        where, args = ("AND d.parent=?", (parent,)) if parent is not None else ("", ())
        with self._lock:
            return self._db.execute(
                "SELECT d.id, d.url, d.parent, d.municipality, d.station_no, d.first_seen_at "
                "FROM documents d WHERE d.active=1 AND EXISTS (SELECT 1 FROM findings f "
                "WHERE f.document_id=d.id) AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.url=d.url) "
                f"{where} ORDER BY d.id", args).fetchall()

    def finding_rules(self, ids: Collection[int]) -> dict[int, list[str]]:
        """document id -> sorted rules that fired on it."""
        out: dict[int, list[str]] = {}
        ids = list(ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT document_id, rule FROM findings WHERE document_id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()
            for r in rows:
                out.setdefault(r["document_id"], []).append(r["rule"])
        return {k: sorted(v) for k, v in out.items()}

    def total_spend(self, parent: str | None = None) -> float:
        """Recorded spend of one election (`parent`), or of everything when None."""
        sql, args = "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM analyses", ()
        if parent is not None:
            sql, args = sql + " WHERE parent=?", (parent,)
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return float(row["s"])
