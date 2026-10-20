"""In-memory RIK backend for tests and replay (usable as httpx.MockTransport(fake.handle))."""
import html
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from .models import Election

PAGE_SIZE = 10


class FakeRik:
    def __init__(self, election: Election, records: list[dict], municipalities: dict[str, str],
                 stations: dict[str, dict[str, str]], clock: Callable[[], str] | None = None,
                 pdf_dir: Path | None = None,
                 listing_names: dict[str, str] | None = None):
        self.election = election
        self.records = records
        self.municipalities = municipalities  # id -> name
        self.stations = stations  # municipality id -> {station option id: station no}
        self.clock = clock
        self.pdf_dir = pdf_dir
        self.listing_names = listing_names or {}  # id -> name shown in listing records
        self.request_log: list[dict] = []
        self.fail_status: int | None = None
        self.hidden_in_sweeps: set[str] = set()
        self.before_request: Callable[[dict], None] | None = None

    # mutators -------------------------------------------------------------
    def _record(self, station_key: str) -> dict:
        for rec in self.records:
            if f"{rec.get('municipality')}|{rec['election_station_number']}" == station_key:
                return rec
        raise KeyError(station_key)

    def add_doc(self, station_key: str, doc: dict) -> None:
        self._record(station_key)["_docs"].append(doc)

    def replace_doc(self, station_key: str, doc_type: int, new_href: str) -> None:
        for d in self._record(station_key)["_docs"]:
            if d["type"] == doc_type:
                d["href"] = new_href

    def remove_doc(self, station_key: str, doc_type: int) -> None:
        rec = self._record(station_key)
        rec["_docs"] = [d for d in rec["_docs"] if d["type"] != doc_type]

    def hide_from_sweeps(self, station_key: str) -> None:
        """Simulate a record shifting out of paginated sweeps (still found by a station query)."""
        self.hidden_in_sweeps.add(station_key)

    # rendering ------------------------------------------------------------
    def _visible(self, rec: dict) -> list[dict]:
        now = self.clock() if self.clock else None
        return [d for d in rec["_docs"] if now is None or d["uploaded"] <= now]

    @staticmethod
    def _extfile(docs: list[dict]) -> str:
        parts = []
        for d in docs:
            stamp = datetime.strptime(d["uploaded"], "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%Y %H:%Mh")
            name = d["href"].rsplit("/", 1)[-1]
            parts.append(
                f"<div class=''><span>{html.escape(d['label'], quote=False)}</span></div>\n"
                f"<div class='table-file active'><a href='{d['href']}' target='_blank'>{name}"
                f"<span class='election-file active'></span></a><span>{stamp}</span></div>")
        return "\n".join(parts)

    def _listing(self, form: dict[str, str]) -> dict:
        doc_type = form.get("filters[election_document_type]")
        muni = form.get("filters[municipality]")
        station = form.get("filters[election_station_no]")
        matched = []
        for rec in self.records:
            if muni is not None and rec.get("_municipality_id") != muni:
                continue
            docs = self._visible(rec)
            if doc_type is not None and not any(str(d["type"]) == doc_type for d in docs):
                continue
            if not docs:
                continue
            if station is not None:
                if self.stations.get(rec.get("_municipality_id"), {}).get(station) != \
                        rec["election_station_number"]:
                    continue
            elif f"{rec.get('municipality')}|{rec['election_station_number']}" in self.hidden_in_sweeps:
                continue
            matched.append((rec, docs))
        pages = -(-len(matched) // PAGE_SIZE)
        page = int(form.get("page", "1"))
        out = []
        for rec, docs in matched[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]:
            item = {k: v for k, v in rec.items() if not k.startswith("_")}
            if rec.get("_municipality_id") in self.listing_names:
                item["municipality"] = self.listing_names[rec["_municipality_id"]]
            item["extfiles"] = [{"extfile": self._extfile(docs), "uid": "u", "date": "", "file_type": ""}]
            out.append(item)
        pagination = ""
        if pages > 1:
            pagination = "".join(f'<li><a href="javascript:void(0)" data-page="{i}">{i}</a></li>'
                                 for i in range(1, pages + 1))
        return {"records": out, "pagination": pagination}

    # transport ------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.request_log.append({"method": request.method, "path": path, "form": form,
                                 "params": dict(request.url.params)})
        if self.before_request is not None:
            self.before_request(self.request_log[-1])
        if self.fail_status is not None:
            return httpx.Response(self.fail_status)
        e = self.election
        if path == f"/zapisnici/{e.parent}":
            data = {"type": 1, "parent": e.parent, "election_round": e.round,
                    "property_election_type": e.election_type}
            return httpx.Response(200, text=f"<h1>{e.title}</h1><script>var additionalData = "
                                            f"{json.dumps(data)};</script>")
        if path == "/get-election":
            return httpx.Response(200, json=self._listing(form))
        if path == "/templates/election/getElectionStations.php":
            return httpx.Response(200, json=[{"id": i, "name": n}
                                             for i, n in self.municipalities.items()])
        if path == "/templates/election/ajaxSelectMunicipality.php":
            opts = '<option value="-1" selected>x</option>' + "".join(
                f'<option value="{sid}">{no}</option>'
                for sid, no in self.stations.get(form.get("municipalityId", ""), {}).items())
            return httpx.Response(200, text=json.dumps({"number": opts, "name": "<option></option>"}),
                                  headers={"Content-Type": "text/html; charset=UTF-8"})
        if path.startswith("/extfile/") and self.pdf_dir is not None:
            f = Path(self.pdf_dir) / path.rsplit("/", 1)[-1]
            if f.is_file():
                return httpx.Response(200, content=f.read_bytes(),
                                      headers={"Content-Type": "application/pdf"})
        return httpx.Response(404)
