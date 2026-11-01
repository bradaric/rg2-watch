"""Bounded, read-only operator form feed independent of HTTP."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import fields
from functools import lru_cache
from pathlib import Path

from .analyze import ARITHMETIC_PROMPT_VERSION, stored_model
from .browse import STATIC, build_data
from .config import Config
from .pipeline import PROMPT_VERSION, RG7_PROMPT_VERSION
from .rules import RULE_INFO
from .store import ANALYSED_TYPES, RG7_TYPES, Store

PROTOCOL_VERSION = 1
MAX_GROUPS = 200
BOOTSTRAP_ROWS = 100
BOOTSTRAP_BYTES = 1_000_000

# Explicit review list: presentation/tally inputs are hashed; operational and secret-bearing
# settings are intentionally excluded. The assertion makes every future Config field a decision.
GENERATION_CONFIG_FIELDS = {
    "model", "list_names", "tally_scope", "turnout_high", "leader_share_high",
    "assisted_share", "min_registered_for_ratio", "unsigned_rows_threshold",
    "outside_share_high", "outside_share_medium", "large_correction_abs",
    "large_correction_share",
}
GENERATION_IGNORED_CONFIG_FIELDS = {
    "base_url", "db_path", "max_rps", "probe_interval_s", "rare_interval_s",
    "safety_sweep_interval_s", "tick_request_budget", "rare_resweep_interval_s",
    "reserved_share", "blocked_pause_s", "blind_alert_s", "missing_station_threshold",
    "missing_max_per_tick", "probe_cost_guard", "watchlist", "only_municipalities",
    "ntfy_url", "ntfy_topic", "ntfy_findings", "ntfy_medium", "medium_digest_s",
    "high_push_max", "high_push_window_s", "mass_replace_threshold", "request_timeout_s",
    "max_pdf_bytes", "anthropic_effort", "fallbacks", "analysis_enabled", "max_spend_usd",
    "ollama_url", "ollama_num_ctx", "ollama_timeout_s", "price_in_per_mtok",
    "price_out_per_mtok", "analysis_workers", "arithmetic_recheck_enabled",
    "arithmetic_local_model", "arithmetic_sonnet_model", "arithmetic_local_queue_limit",
    "arithmetic_sonnet_workers", "browse_interval_s", "serve_port", "drive_client_secrets",
    "drive_token_path", "download_max_rps", "analysis_pause_s", "stop_wait_s",
}
assert GENERATION_CONFIG_FIELDS | GENERATION_IGNORED_CONFIG_FIELDS == {
    item.name for item in fields(Config)}
assert not GENERATION_CONFIG_FIELDS & GENERATION_IGNORED_CONFIG_FIELDS


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str).encode()


@lru_cache(maxsize=1)
def resource_digest() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted([*root.glob("*.py"), *(root / "static").glob("*")]):
        if path.is_file():
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def generation_token(store: Store, cfg: Config, parent: str, resources: str | None = None) -> str:
    election = store.get_election(parent)
    db_uuid, _, _ = store.operator_feed_state(parent)
    config = {name: getattr(cfg, name) for name in sorted(GENERATION_CONFIG_FIELDS)}
    value = {"db": db_uuid, "parent": parent, "protocol": PROTOCOL_VERSION,
             "config": config, "election": election.__dict__ if election is not None else None,
             "resources": resources or resource_digest()}
    return hashlib.sha256(_canonical(value)).hexdigest()


class _GroupStore:
    """A Store-shaped bounded view used by the shared row serializer."""
    def __init__(self, store: Store, parent: str, kind: str, key: str):
        self._store, self.parent, self.kind, self.key = store, parent, kind, key
        self.name, self.path = store.name, store.path
        where = "parent=? AND station_key=?" if kind == "station" else "parent=? AND id=?"
        arg = key if kind == "station" else int(key)
        with store._lock:
            self.docs = store._db.execute(
                f"SELECT * FROM documents WHERE {where} ORDER BY id", (parent, arg)).fetchall()
        self.ids = [row["id"] for row in self.docs]

    def __getattr__(self, name):
        return getattr(self._store, name)

    def _marks(self) -> str:
        return ",".join("?" * len(self.ids)) or "NULL"

    def browse_rows(self, parent=None):
        docs = [row for row in self.docs if row["doc_type"] in ANALYSED_TYPES]
        ids = [row["id"] for row in docs]
        if not ids:
            return [], []
        with self._store._lock:
            found = self._store._db.execute(
                f"SELECT id,document_id,rule,severity,summary FROM findings WHERE document_id IN "
                f"({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()
        return docs, found

    def browse_analyses(self, parent=None, prompt_version=None):
        if not self.ids:
            return []
        with self._store._lock:
            return self._store._db.execute(
                "SELECT a.id,a.document_id,a.model,a.prompt_version,a.created_at FROM analyses a "
                f"WHERE a.document_id IN ({self._marks()}) AND a.status='done' "
                "AND a.result_json IS NOT NULL AND a.id=(SELECT b.id FROM analyses b WHERE "
                "b.document_id=a.document_id AND b.model IS a.model AND b.status='done' "
                "AND b.result_json IS NOT NULL ORDER BY b.prompt_version IS ? DESC,b.id DESC LIMIT 1) "
                "ORDER BY a.id", (*self.ids, prompt_version)).fetchall()

    def arithmetic_analyses(self, parent, prompt_version):
        if not self.ids:
            return []
        with self._store._lock:
            return self._store._db.execute(
                "SELECT a.id,a.document_id,a.model,a.created_at FROM analyses a "
                f"WHERE a.document_id IN ({self._marks()}) AND a.prompt_version=? AND "
                "a.status='done' AND a.result_json IS NOT NULL AND a.id=(SELECT MAX(b.id) FROM "
                "analyses b WHERE b.document_id=a.document_id AND b.model=a.model AND "
                "b.prompt_version=? AND b.status='done' AND b.result_json IS NOT NULL) ORDER BY a.id",
                (*self.ids, prompt_version, prompt_version)).fetchall()

    def latest_classification_overrides(self, parent=None):
        if not self.ids:
            return {}
        with self._store._lock:
            rows = self._store._db.execute(
                "SELECT o.url,o.action FROM classification_overrides o "
                f"WHERE o.document_id IN ({self._marks()}) AND o.id=(SELECT MAX(x.id) FROM "
                "classification_overrides x WHERE x.url=o.url)", self.ids).fetchall()
        return {row["url"]: row["action"] for row in rows}

    def latest_reviews(self, parent=None):
        if not self.ids:
            return []
        with self._store._lock:
            return self._store._db.execute(
                "SELECT r.*,d.parent,d.municipality,d.station_no,d.active,0 superseded FROM reviews r "
                "JOIN documents d ON d.id=r.document_id "
                f"WHERE r.document_id IN ({self._marks()}) AND r.id=(SELECT MAX(x.id) FROM reviews x "
                "WHERE x.url=r.url) ORDER BY r.id DESC", self.ids).fetchall()

    def latest_operator_corrections(self, parent=None):
        if not self.ids:
            return []
        with self._store._lock:
            return self._store._db.execute(
                "SELECT c.* FROM operator_corrections c "
                f"WHERE c.document_id IN ({self._marks()}) AND c.id=(SELECT MAX(x.id) FROM "
                "operator_corrections x WHERE x.url=c.url) ORDER BY c.id", self.ids).fetchall()

    def rg7_reports(self, parent, model, prompt_version, analysed_only=True,
                    station_key=None, fallback=False):
        ids = [row["id"] for row in self.docs if row["doc_type"] in RG7_TYPES]
        if not ids:
            return []
        model_clause = "" if fallback else "a.model=? AND "
        order = "ORDER BY a.model=? DESC,a.id DESC" if fallback else "ORDER BY a.id DESC"
        args = ((prompt_version, model, *ids) if fallback else (model, prompt_version, *ids))
        with self._store._lock:
            rows = self._store._db.execute(
                "SELECT d.*,(SELECT a.result_json FROM analyses a WHERE a.document_id=d.id AND "
                f"{model_clause}a.prompt_version=? AND a.status='done' AND a.result_json IS NOT NULL "
                f"{order} LIMIT 1) rj FROM documents d WHERE d.id IN "
                f"({','.join('?' * len(ids))}) AND d.active=1 ORDER BY d.uploaded_at,d.id", args).fetchall()
        return [(row, json.loads(row["rj"]) if row["rj"] else None) for row in rows
                if row["rj"] or not analysed_only]

    def rg7_findings(self, parent):
        ids = [row["id"] for row in self.docs if row["doc_type"] in RG7_TYPES]
        if not ids:
            return []
        with self._store._lock:
            return self._store._db.execute(
                "SELECT id,document_id,rule,severity,summary FROM findings WHERE document_id IN "
                f"({','.join('?' * len(ids))}) AND rule IN ('R1','R2','R3') ORDER BY id", ids).fetchall()

    def rg7_readers(self, parent, prompt_version):
        ids = [row["id"] for row in self.docs if row["doc_type"] in RG7_TYPES]
        if not ids:
            return {}
        with self._store._lock:
            rows = self._store._db.execute(
                "SELECT document_id,model FROM analyses WHERE document_id IN "
                f"({','.join('?' * len(ids))}) AND prompt_version=? AND status='done' "
                "AND result_json IS NOT NULL ORDER BY model", (*ids, prompt_version)).fetchall()
        out = {}
        for row in rows:
            if row["model"] not in out.setdefault(row["document_id"], []):
                out[row["document_id"]].append(row["model"])
        return out


class OperatorFeed:
    def __init__(self, store: Store, cfg: Config, cache_dir: Path):
        self.store, self.cfg, self.cache_dir = store, cfg, Path(cache_dir)
        self._resource_digest = resource_digest()
        self._cursor_secret = secrets.token_bytes(32)

    def _generation(self, store: Store, parent: str) -> str:
        if store.get_election(parent) is None:
            raise KeyError(parent)
        return generation_token(store, self.cfg, parent, self._resource_digest)

    def generation(self, parent: str) -> str:
        with self.store.read_snapshot() as snapshot:
            return self._generation(snapshot, parent)

    def _rows(self, store: Store, parent: str, kind: str, key: str, revision: int) -> list[dict]:
        view = _GroupStore(store, parent, kind, key)
        data = build_data(view, self.cache_dir, parent, self.cfg, selective=True)
        for row in data["rows"]:
            row.update(group_kind=kind, group_key=key, changed_revision=revision)
        return data["rows"]

    def feed(self, parent: str, since: int) -> dict:
        if not isinstance(parent, str) or not parent or not isinstance(since, int):
            raise ValueError("invalid feed parameters")
        with self.store.read_snapshot() as snapshot:
            generation = self._generation(snapshot, parent)
            db_uuid, revision, floor = snapshot.operator_feed_state(parent)
            base = {"db": db_uuid, "parent": parent, "generation": generation,
                    "revision": revision, "through": since, "more": False,
                    "reset_required": False, "groups": [], "tombstones": []}
            if since < floor or since > revision:
                return {**base, "reset_required": True, "through": revision}
            changes = snapshot.operator_feed_changes(parent, since, MAX_GROUPS + 1)
            if any(row["group_kind"] == "global" for row in changes):
                return {**base, "reset_required": True, "through": revision}
            selected = changes[:MAX_GROUPS]
            if len(changes) > MAX_GROUPS:
                last_revision = selected[-1]["revision"]
                selected = [row for row in selected if row["revision"] < last_revision]
                if not selected:  # one revision should not split; safely request bootstrap
                    return {**base, "reset_required": True, "through": revision}
            identities = {}
            for row in selected:
                identities[(row["group_kind"], row["group_key"])] = row["revision"]
            for (kind, key), changed in identities.items():
                rows = self._rows(snapshot, parent, kind, key, changed)
                target = base["groups"] if rows else base["tombstones"]
                target.append({"kind": kind, "key": key, **({"rows": rows} if rows else {})})
            through = max((row["revision"] for row in selected), default=revision)
            more = bool(snapshot.operator_feed_changes(parent, through, 1))
            return {**base, "through": through, "more": more}

    def _encode_cursor(self, value: dict) -> str:
        body = base64.urlsafe_b64encode(_canonical(value)).rstrip(b"=")
        signature = hmac.new(self._cursor_secret, body, hashlib.sha256).digest()
        return (body + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()

    def _decode_cursor(self, token: str) -> dict:
        try:
            body, encoded_signature = token.encode().split(b".", 1)
            signature = base64.urlsafe_b64decode(encoded_signature + b"=" * (-len(encoded_signature) % 4))
            if not hmac.compare_digest(signature, hmac.new(
                    self._cursor_secret, body, hashlib.sha256).digest()):
                raise ValueError
            return json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
        except Exception as exc:
            raise ValueError("invalid bootstrap cursor") from exc

    def bootstrap(self, parent: str, cursor: str | None = None) -> dict:
        if not isinstance(parent, str) or not parent:
            raise ValueError("invalid bootstrap parent")
        with self.store.read_snapshot() as snapshot:
            generation = self._generation(snapshot, parent)
            _, revision, floor = snapshot.operator_feed_state(parent)
            state = ({"parent": parent, "generation": generation, "revision": revision,
                      "after": None} if cursor is None else self._decode_cursor(cursor))
            if (state.get("parent") != parent or state.get("generation") != generation
                    or not isinstance(state.get("revision"), int)):
                raise ValueError("bootstrap cursor does not match this election")
            if state["revision"] < floor:
                return {"parent": parent, "generation": generation, "revision": revision,
                        "reset_required": True, "rows": [], "cursor": None, "done": False}
            identities = snapshot.operator_group_identities(parent, state.get("after"), BOOTSTRAP_ROWS + 1)
            page, size = [], 0
            for kind, key in identities[:BOOTSTRAP_ROWS]:
                rows = self._rows(snapshot, parent, kind, key, state["revision"])
                blob_size = len(_canonical(rows))
                if page and size + blob_size > BOOTSTRAP_BYTES:
                    break
                page.append({"kind": kind, "key": key, "rows": rows})
                size += blob_size
            done = len(page) == len(identities) and len(identities) <= BOOTSTRAP_ROWS
            after = [page[-1]["kind"], page[-1]["key"]] if page else state.get("after")
            next_cursor = None if done else self._encode_cursor({**state, "after": after})
            election = snapshot.get_election(parent)
            return {"db": snapshot.operator_feed_state(parent)[0], "parent": parent,
                    "generation": generation, "revision": state["revision"],
                    "reset_required": False, "groups": page, "cursor": next_cursor,
                    "done": done, "election": {"title": election.title},
                    "rules_version": PROTOCOL_VERSION, "rules": RULE_INFO,
                    "interval": self.cfg.browse_interval_s}
