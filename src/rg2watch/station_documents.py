"""Live, read-only comparison of one stored station with RIK's current listing."""
from collections.abc import Callable

from .models import DOC_TYPES, Filters
from .store import Store

MAX_PAGES = 20


def _id_for_number(items: dict[str, str], number: str) -> str | None:
    if number in items:
        return items[number]
    if number.isdigit():
        wanted = int(number)
        return next((value for key, value in items.items()
                     if key.isdigit() and int(key) == wanted), None)
    return None


class StationDocuments:
    def __init__(self, store: Store, client_for: Callable[[str], object]):
        self.store = store
        self.client_for = client_for

    def __call__(self, url: str) -> dict:
        found = self.store.find_document(url)
        if found is None:
            raise ValueError("unknown document URL")
        parent, source = found
        election = self.store.get_election(parent)
        if election is None:
            raise ValueError("document election is not registered")
        client = self.client_for(parent)

        municipality_ids = self.store.municipality_ids(parent)
        municipality_id = municipality_ids.get(source.municipality)
        if municipality_id is None:
            municipality_id = client.municipalities().get(source.municipality)
        if municipality_id is None:
            raise ValueError(f"RIK municipality is unavailable: {source.municipality}")

        station_ids = self.store.station_ids(parent, municipality_id)
        station_id = _id_for_number(station_ids, source.station_no)
        if station_id is None:
            station_id = _id_for_number(client.stations(municipality_id), source.station_no)
        if station_id is None:
            raise ValueError(f"RIK station is unavailable: {source.station_no}")

        filters = Filters(municipality_id=municipality_id, station_no_id=station_id)
        first = client.listing(filters, 1)
        if first.max_page > MAX_PAGES:
            raise ValueError(f"RIK returned too many station pages: {first.max_page}")
        remote = list(first.docs)
        for page in range(2, first.max_page + 1):
            remote.extend(client.listing(filters, page).docs)

        stored_urls = self.store.station_document_urls(parent, source.station_key)
        documents, seen = [], set()
        for item in remote:
            if (item.url in seen or item.municipality != source.municipality
                    or item.station_no != source.station_no
                    or item.minority != source.minority):
                continue
            seen.add(item.url)
            documents.append({
                "url": item.url,
                "label": item.label or DOC_TYPES.get(item.doc_type, "Unknown document"),
                "doc_type": item.doc_type,
                "uploaded_at": item.uploaded_at,
                "stored": item.url in stored_urls,
            })
        return {
            "parent": parent,
            "municipality": source.municipality,
            "station_no": source.station_no,
            "station_name": source.station_name,
            "minority": source.minority,
            "listing_url": f"{election.base_url.rstrip('/')}/zapisnici/{parent}",
            "documents": documents,
            "missing": sum(not item["stored"] for item in documents),
        }
