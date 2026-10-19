import json
import random
import re
import threading
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

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
DOWNLOAD_MAX_RPS = 0.5
USER_AGENT = "rg2-watch/0.1 (+election monitoring)"
MAX_ATTEMPTS = 4
PAGE_SIZE = 10


class RikError(Exception):
    pass


class RikUnavailable(RikError):
    pass


class RikBlocked(RikUnavailable):
    """HTTP 401/403: treated as overload, never retried."""


class AdaptiveLimiter:
    """Shared request pacer with AIMD: halve on overload, +0.1 rps on fast success."""

    def __init__(self, max_rps: float, min_rps: float = 0.1, clock=time.monotonic, sleep=time.sleep,
                 slow_is_overload: bool = True):
        self.max_rps = max_rps
        self.slow_is_overload = slow_is_overload  # False for PDF downloads: big files are slow
        self.min_rps = min_rps
        self.clock = clock
        self.sleep = sleep
        self._rps = max_rps
        self._next_slot = 0.0
        self._lock = threading.Lock()

    @property
    def current_rps(self) -> float:
        return self._rps

    def acquire(self) -> None:
        with self._lock:
            now = self.clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + 1.0 / self._rps
        if slot > now:
            self.sleep(slot - now)

    def on_success(self, latency_s: float) -> None:
        if not self.slow_is_overload:
            with self._lock:
                self._rps = min(self.max_rps, self._rps + 0.1)
        elif latency_s > 5.0:
            self.on_overload()
        elif latency_s < 2.0:
            with self._lock:
                self._rps = min(self.max_rps, self._rps + 0.1)

    def on_overload(self) -> None:
        with self._lock:
            self._rps = max(self.min_rps, self._rps / 2)


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, min(60.0, float(resp.headers.get("Retry-After", ""))))
    except ValueError:
        return 0.0


def _send(http: httpx.Client, limiter: AdaptiveLimiter, timeout_s: float,
          method: str, url: str, parse=None, opener=None, **kwargs):
    """Send with retries. `parse(resp)` may raise ValueError for a 200 that is
    really an error/maintenance page; that counts as overload."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        limiter.acquire()
        start = limiter.clock()
        retry_after = 0.0
        try:
            resp = opener() if opener else http.request(method, url, timeout=timeout_s, **kwargs)
        except httpx.TransportError as exc:  # timeouts, connection errors
            limiter.on_overload()
            reason = repr(exc)
        except ValueError as exc:  # opener saw a maintenance-looking body
            limiter.on_overload()
            reason = f"bad body: {exc}"
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                limiter.on_overload()
                reason = f"HTTP {resp.status_code}"
                retry_after = _retry_after(resp)
            elif resp.status_code in (401, 403):
                limiter.on_overload()
                raise RikBlocked(f"{method} {url} -> HTTP {resp.status_code}")
            elif resp.status_code >= 400:
                raise RikError(f"{method} {url} -> HTTP {resp.status_code}")
            else:
                try:
                    result = parse(resp) if parse else resp
                except ValueError as exc:
                    limiter.on_overload()
                    reason = f"bad body: {exc}"
                else:
                    limiter.on_success(limiter.clock() - start)
                    return result
        if attempt == MAX_ATTEMPTS:
            raise RikUnavailable(f"{method} {url} failed after {MAX_ATTEMPTS} attempts: {reason}")
        limiter.sleep(max(retry_after, min(60, 2**attempt) + random.uniform(0, 1)))
    raise AssertionError("unreachable")


MAINTENANCE_STEMS = ("одржавањ", "недоступ", "održavanj", "odrzavanj", "nedostup")
MAINTENANCE_WORDS = re.compile(r"\b(maintenance|unavailable|temporarily|overload|busy)\b")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S)
CLASSIFY_BYTES = 4096


def _read_capped(http: httpx.Client, url: str, timeout_s: float, max_bytes: int,
                 deadline: float, clock) -> httpx.Response:
    """Stream a PDF body with a size cap, an early %PDF check and a deadline.
    Any 3xx/non-PDF is a permanent RikError (redirects are never followed);
    maintenance-looking HTML raises ValueError (retried as overload)."""
    with http.stream("GET", url, timeout=timeout_s, follow_redirects=False) as r:
        if 300 <= r.status_code < 400:
            raise RikError(f"GET {url} -> HTTP {r.status_code} (redirect not followed)")
        if r.status_code >= 400:
            return httpx.Response(r.status_code,
                                  headers={"Retry-After": r.headers.get("Retry-After", "")})
        length = r.headers.get("content-length", "")
        if length.isdigit() and int(length) > max_bytes:
            raise RikError(f"GET {url}: Content-Length {length} exceeds {max_bytes}")
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > max_bytes:
                raise RikError(f"GET {url}: body exceeds {max_bytes} bytes")
            if clock() > deadline:
                raise RikError(f"GET {url}: download exceeded time limit")
            if len(buf) >= 4 and not buf.startswith(b"%PDF") and len(buf) >= CLASSIFY_BYTES:
                break  # not a PDF: stop reading, classify the head
        if not buf.startswith(b"%PDF"):
            _reject_non_pdf(url, bytes(buf[:CLASSIFY_BYTES]))
        return httpx.Response(r.status_code, content=bytes(buf))


def _reject_non_pdf(url: str, head: bytes) -> None:
    text = head.decode("utf-8", errors="ignore").casefold()
    if "<html" in text or "<!doctype html" in text:
        visible = _SCRIPT_STYLE.sub(" ", text)
        if MAINTENANCE_WORDS.search(visible) or any(w in visible for w in MAINTENANCE_STEMS):
            raise ValueError("maintenance page")
    raise RikError(f"GET {url}: not a PDF")


def download_limiter_for(cfg: Config) -> AdaptiveLimiter:
    return AdaptiveLimiter(cfg.download_max_rps, slow_is_overload=False)


class RikClient:
    def __init__(self, election: Election, limiter: AdaptiveLimiter, http: httpx.Client,
                 timeout_s: float = 20.0, max_pdf_bytes: int = 50_000_000,
                 download_limiter: AdaptiveLimiter | None = None):
        self.max_pdf_bytes = max_pdf_bytes
        self.election = election
        self.limiter = limiter
        # PDF downloads are paced separately so analysis can never throttle detection
        self.download_limiter = download_limiter or AdaptiveLimiter(
            DOWNLOAD_MAX_RPS, clock=limiter.clock, sleep=limiter.sleep, slow_is_overload=False)
        self.http = http
        self.timeout_s = timeout_s
        self.last_ok: float | None = None  # limiter clock of the last successful listing request
        self.blocked_streak = 0  # consecutive 401/403 listing responses
        self.paused_until = float("-inf")  # limiter clock; set by the watcher's blocked pause
        self.download_last_ok: float | None = None  # downloads keep their own health state
        self.download_blocked_streak = 0
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
        return cls(election, limiter, http, cfg.request_timeout_s, cfg.max_pdf_bytes,
                   download_limiter_for(cfg))

    def _request(self, method: str, path: str, shape: str, **kwargs):
        """Returns decoded JSON; body must be a dict containing key `shape`
        (or, for shape '', a list)."""
        def parse(resp: httpx.Response):
            try:
                body = json.loads(resp.text)
            except ValueError as exc:
                raise ValueError("not JSON") from exc
            if shape:
                if not isinstance(body, dict) or shape not in body:
                    raise ValueError(f"missing {shape!r}")
            elif not isinstance(body, list):
                raise ValueError("expected list")
            return body

        try:
            body = _send(self.http, self.limiter, self.timeout_s, method,
                         self.election.base_url + path, parse=parse, headers=self._headers, **kwargs)
        except RikBlocked:
            self.blocked_streak += 1
            raise
        self.last_ok = self.limiter.clock()
        self.blocked_streak = 0
        return body

    def listing(self, filters: Filters, page: int) -> ListingPage:
        return parse_listing(self.listing_raw(filters, page), self.election.base_url)

    def listing_raw(self, filters: Filters, page: int) -> dict:
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
        return self._request("POST", "/get-election", "records", data=data)

    def count(self, filters: Filters) -> int:
        first = self.listing(filters, 1)
        if first.max_page <= 1:
            return first.records
        last = self.listing(filters, first.max_page)
        return (first.max_page - 1) * PAGE_SIZE + last.records

    def municipalities(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for q in "aeiou":
            body = self._request("GET", "/templates/election/getElectionStations.php", "",
                                 params={"q": q, "electionId": self.election.round})
            merged.update(parse_municipalities(body))
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
        body = self._request("POST", "/templates/election/ajaxSelectMunicipality.php", "number",
                             data=data)
        return parse_station_options(body["number"])

    def download(self, url: str, max_total_s: float = 120.0) -> bytes:
        """GET a PDF through the limiter/retry path (streamed, size-capped, base host only)."""
        base = urlsplit(self.election.base_url)
        parts = urlsplit(urljoin(self.election.base_url + "/", url))
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
            raise RikError(f"refusing download from foreign host: {parts.netloc}")
        path = quote(parts.path, safe="/%:@~-._!$&'()*+,;=")
        full = urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
        lim = self.download_limiter
        deadline = lim.clock() + max_total_s

        def opener() -> httpx.Response:
            return _read_capped(self.http, full, self.timeout_s, self.max_pdf_bytes, deadline,
                                lim.clock)

        def parse(resp: httpx.Response) -> bytes:
            if not resp.content.startswith(b"%PDF"):
                raise RikError(f"GET {full}: not a PDF")
            return resp.content

        try:
            body = _send(self.http, lim, self.timeout_s, "GET", full, parse=parse, opener=opener)
        except RikBlocked:
            self.download_blocked_streak += 1
            raise
        self.download_last_ok = lim.clock()
        self.download_blocked_streak = 0
        return body
