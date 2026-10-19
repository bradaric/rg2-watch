"""Fixture cache fetcher and replay server (re-enacts a past election from cached data)."""
import json
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

from .fake_rik import FakeRik
from .models import Election, Filters
from .rik_client import RikClient, RikError, _send
from .rik_parse import parse_listing

RG9_LABEL = "Решење о поништавању гласања на бирачком месту (Образац РГ-9)"
_FMT = "%Y-%m-%d %H:%M:%S"


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sweep(client: RikClient, filters: Filters) -> list[dict]:
    first = client.listing_raw(filters, 1)
    records = list(first["records"])
    for page in range(2, parse_listing(first, client.election.base_url).max_page + 1):
        records.extend(client.listing_raw(filters, page)["records"])
    return records


def _station_key(rec: dict) -> tuple[str, str]:
    return rec["municipality"], str(rec["election_station_number"])


def fetch_fixtures(client: RikClient, out_dir: Path, with_pdfs: bool = False,
                   limit_pdfs: int | None = None) -> Path:
    e = client.election
    root = Path(out_dir) / e.parent
    root.mkdir(parents=True, exist_ok=True)

    if not (root / "election.json").exists():
        resp = _send(client.http, client.limiter, client.timeout_s, "GET",
                     f"{e.base_url}/zapisnici/{e.parent}")
        _write_json(root / "election.json", {
            "parent": e.parent, "round": e.round, "election_type": e.election_type,
            "title": e.title, "base_url": e.base_url, "page_html": resp.text})

    munis: dict[str, str] | None = None
    if not (root / "municipalities.json").exists():
        munis = client.municipalities()
        _write_json(root / "municipalities.json", munis)
    if not (root / "stations.json").exists():
        munis = munis or _read_json(root / "municipalities.json")
        _write_json(root / "stations.json", {m: client.stations(m) for m in munis})

    if not (root / "listing.json").exists():
        munis = munis or _read_json(root / "municipalities.json")
        records = _sweep(client, Filters())
        muni_of: dict[tuple[str, str], str] = {}
        for mid in munis:
            for rec in _sweep(client, Filters(municipality_id=mid)):
                muni_of[_station_key(rec)] = mid
        for rec in records:
            mid = muni_of.get(_station_key(rec))
            if mid is not None:
                rec["_municipality_id"] = mid
        _write_json(root / "listing.json", records)

    if with_pdfs:
        _fetch_pdfs(client, root, limit_pdfs)
    return root


def _fetch_pdfs(client: RikClient, root: Path, limit: int | None) -> None:
    pdf_dir = root / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    base = client.election.base_url
    seen = 0
    for rec in _read_json(root / "listing.json"):
        for doc in parse_listing({"records": [rec], "pagination": ""}, base).docs:
            # Hrefs are untrusted: stay on the election's host and never leave pdf_dir.
            if not doc.url.startswith(base + "/"):
                continue
            path = urlsplit(doc.url).path
            name = Path(unquote(path.rsplit("/", 1)[-1])).name
            if name in ("", ".", ".."):
                continue
            if limit is not None and seen >= limit:
                return
            seen += 1
            target = pdf_dir / name
            if target.exists():
                continue
            try:
                resp = _send(client.http, client.limiter, client.timeout_s, "GET",
                             base + quote(path, safe="/%@:+,;=~!$&'()*"))
            except RikError:
                continue  # not written, so a re-run retries it
            target.write_bytes(resp.content)


class ReplayClock:
    """Simulated wall clock: `speed` simulated seconds per real second from `start`."""

    def __init__(self, start: datetime, speed: float, wall: Callable[[], float] = time.monotonic):
        self.start = start
        self.speed = speed
        self.wall = wall
        self._t0 = wall()

    def now(self) -> datetime:
        return self.start + timedelta(seconds=(self.wall() - self._t0) * self.speed)

    def now_str(self) -> str:
        return self.now().strftime(_FMT)

    __call__ = now_str


def _uploads(records: list[dict]) -> list[str]:
    return sorted(d["uploaded"] for r in records for d in r["_docs"])


def load_fake(fixture_dir: Path, clock: Callable[[], str] | None) -> FakeRik:
    root = Path(fixture_dir)
    meta = _read_json(root / "election.json")
    election = Election(meta["parent"], meta["round"], meta["election_type"], meta["title"],
                        meta["base_url"])
    records = _read_json(root / "listing.json")
    prefix = election.base_url + "/"
    for rec in records:
        page = parse_listing({"records": [rec], "pagination": ""}, election.base_url)
        rec["_docs"] = [{
            "type": d.doc_type, "label": d.label,
            "href": "/" + d.url[len(prefix):] if d.url.startswith(prefix) else d.url,
            "uploaded": d.uploaded_at.replace("T", " ") + ":00",
        } for d in page.docs]
    pdf_dir = root / "pdfs"
    return FakeRik(election, records, _read_json(root / "municipalities.json"),
                   _read_json(root / "stations.json"), clock=clock,
                   pdf_dir=pdf_dir if pdf_dir.is_dir() else None)


def fault_plan(records: list[dict], seed: int = 7) -> list[dict]:
    """Deterministic fault schedule at 25/50/75% of [first, last] upload."""
    uploads = _uploads(records)
    if not uploads:
        return []
    first, last = (datetime.strptime(u, _FMT) for u in (uploads[0], uploads[-1]))
    rng = random.Random(seed)
    plan = []
    for frac, kind in ((0.25, "replace"), (0.5, "remove"), (0.75, "add")):
        when = (first + (last - first) * frac).strftime(_FMT)
        visible = [(r, d) for r in records for d in r["_docs"] if d["uploaded"] <= when]
        if kind == "replace":
            visible = [(r, d) for r, d in visible if d["type"] == 6]
        if not visible:
            continue
        rec, doc = rng.choice(visible)
        key = f"{rec.get('municipality')}|{rec['election_station_number']}"
        plan.append({"at": when, "kind": kind, "station_key": key, "doc_type": doc["type"],
                     "href": doc["href"]})
    return plan


def apply_fault(fake: FakeRik, fault: dict) -> None:
    key = fault["station_key"]
    if fault["kind"] == "replace":
        # Same path (so the cached PDF is still served) but a new URL.
        fake.replace_doc(key, fault["doc_type"], fault["href"] + "?v=replay-fault")
    elif fault["kind"] == "remove":
        fake.remove_doc(key, fault["doc_type"])
    else:
        fake.add_doc(key, {"type": 9, "label": RG9_LABEL, "uploaded": fault["at"],
                           "href": "/extfile/replay/synthetic-rg9.pdf"})


def make_replay_server(fixture_dir: Path, host: str = "127.0.0.1", port: int = 8765,
                       speed: float = 60.0, faults: bool = False,
                       clock: ReplayClock | None = None) -> ThreadingHTTPServer:
    root = Path(fixture_dir)
    probe = load_fake(root, None)  # only to learn the upload timeline
    uploads = _uploads(probe.records)
    if clock is None:
        first = datetime.strptime(uploads[0], _FMT) if uploads else datetime.now()
        clock = ReplayClock(first - timedelta(minutes=1), speed)
    fake = load_fake(root, clock)
    pending = fault_plan(fake.records) if faults else []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            request = httpx.Request(self.command, f"http://{host}{self.path}", content=body)
            with lock:
                now = clock.now_str()
                while pending and pending[0]["at"] <= now:
                    apply_fault(fake, pending.pop(0))
                resp = fake.handle(request)
            self.send_response(resp.status_code)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "text/plain"))
            self.send_header("Content-Length", str(len(resp.content)))
            self.end_headers()
            self.wfile.write(resp.content)

        do_GET = do_POST = _serve

        def log_message(self, *args) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve_replay(fixture_dir: Path, host: str = "127.0.0.1", port: int = 8765,
                 speed: float = 60.0, faults: bool = False) -> None:
    make_replay_server(fixture_dir, host, port, speed, faults).serve_forever()
