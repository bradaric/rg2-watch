import json
import random
import time
from urllib.parse import urlsplit

import httpx

from .config import Config
from .models import Election, Filters, ListingPage
from .rik_parse import (
    parse_election_page,
    parse_listing,
    parse_municipalities,
    parse_station_options,
)

DEFAULT_BASE_URL = "https://www.rik.parlament.gov.rs"
USER_AGENT = "rg2-watch/0.1 (+election monitoring)"
MAX_ATTEMPTS = 4
PAGE_SIZE = 10


class RikError(Exception):
    pass


class RikUnavailable(RikError):
    pass


class AdaptiveLimiter:
    """Shared request pacer with AIMD: halve on overload, +0.1 rps on fast success."""

    def __init__(self, max_rps: float, min_rps: float = 0.1, clock=time.monotonic, sleep=time.sleep):
        self.max_rps = max_rps
        self.min_rps = min_rps
        self.clock = clock
        self.sleep = sleep
        self._rps = max_rps
        self._next_slot = 0.0

    @property
    def current_rps(self) -> float:
        return self._rps

    def acquire(self) -> None:
        now = self.clock()
        slot = max(now, self._next_slot)
        self._next_slot = slot + 1.0 / self._rps
        if slot > now:
            self.sleep(slot - now)

    def on_success(self, latency_s: float) -> None:
        if latency_s > 5.0:
            self.on_overload()
        elif latency_s < 2.0:
            self._rps = min(self.max_rps, self._rps + 0.1)

    def on_overload(self) -> None:
        self._rps = max(self.min_rps, self._rps / 2)


def _send(http: httpx.Client, limiter: AdaptiveLimiter, timeout_s: float,
          method: str, url: str, **kwargs) -> httpx.Response:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        limiter.acquire()
        start = limiter.clock()
        try:
            resp = http.request(method, url, timeout=timeout_s, **kwargs)
        except httpx.TransportError as exc:  # timeouts, connection errors
            limiter.on_overload()
            reason = repr(exc)
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                limiter.on_overload()
                reason = f"HTTP {resp.status_code}"
            elif resp.status_code >= 400:
                raise RikError(f"{method} {url} -> HTTP {resp.status_code}")
            else:
                limiter.on_success(limiter.clock() - start)
                return resp
        if attempt == MAX_ATTEMPTS:
            raise RikUnavailable(f"{method} {url} failed after {MAX_ATTEMPTS} attempts: {reason}")
        limiter.sleep(min(60, 2**attempt) + random.uniform(0, 1))
    raise AssertionError("unreachable")


class RikClient:
    def __init__(self, election: Election, limiter: AdaptiveLimiter, http: httpx.Client,
                 timeout_s: float = 20.0):
        self.election = election
        self.limiter = limiter
        self.http = http
        self.timeout_s = timeout_s
        self._headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{election.base_url}/zapisnici/{election.parent}",
        }

    @classmethod
    def from_url(cls, zapisnici_url: str, cfg: Config) -> "RikClient":
        parts = urlsplit(zapisnici_url)
        if cfg.base_url != DEFAULT_BASE_URL:
            base = cfg.base_url.rstrip("/")
        else:
            base = f"{parts.scheme}://{parts.netloc}"
        limiter = AdaptiveLimiter(cfg.max_rps)
        http = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        resp = _send(http, limiter, cfg.request_timeout_s, "GET", base + parts.path)
        election = parse_election_page(resp.text, base)
        return cls(election, limiter, http, cfg.request_timeout_s)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return _send(self.http, self.limiter, self.timeout_s, method,
                     self.election.base_url + path, headers=self._headers, **kwargs)

    def _json(self, resp: httpx.Response):
        try:
            return json.loads(resp.text)
        except ValueError as exc:
            raise RikError(f"invalid JSON from {resp.request.url}") from exc

    def listing(self, filters: Filters, page: int) -> ListingPage:
        e = self.election
        data = {
            "page": str(page),
            "data[type]": "1",
            "data[parent]": e.parent,
            "data[election_round]": e.round,
            "data[property_election_type]": e.election_type,
            "election_type": e.election_type,
        }
        if filters.doc_type is not None:
            data["filters[election_document_type]"] = str(filters.doc_type)
        if filters.municipality_id is not None:
            data["filters[municipality]"] = filters.municipality_id
        if filters.station_no_id is not None:
            data["filters[election_station_no]"] = filters.station_no_id
        payload = self._json(self._request("POST", "/get-election", data=data))
        return parse_listing(payload, e.base_url)

    def count(self, filters: Filters) -> int:
        first = self.listing(filters, 1)
        if first.max_page <= 1:
            return first.records
        last = self.listing(filters, first.max_page)
        return (first.max_page - 1) * PAGE_SIZE + last.records

    def municipalities(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for q in "aeiou":
            resp = self._request("GET", "/templates/election/getElectionStations.php",
                                 params={"q": q, "electionId": self.election.round})
            merged.update(parse_municipalities(self._json(resp)))
        return merged

    def stations(self, municipality_id: str) -> dict[str, str]:
        data = {
            "method": "getelectionstation",
            "html": "1",
            "get_both": "1",
            "municipalityId": municipality_id,
            "site_lang": "sr",
            "election_round": self.election.round,
        }
        resp = self._request("POST", "/templates/election/ajaxSelectMunicipality.php", data=data)
        return parse_station_options(self._json(resp)["number"])
