"""Local browse server: strict GET/HEAD allow-list and same-origin review/correction/case writes."""
import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .operator_corrections import CorrectionConflict
from .store import VERDICTS

log = logging.getLogger("rg2watch.server")
# The only names served from the page directory. status.js/data.js are deliberately absent: a
# cross-origin <script src> could read their globals; over http the page fetch()es the .json files.
FILES = {
    "index.html": "text/html; charset=utf-8", "status.json": "application/json",
    "data.json": "application/json", "jquery.min.js": "text/javascript; charset=utf-8",
    "dataTables.min.js": "text/javascript; charset=utf-8",
    "dataTables.dataTables.min.css": "text/css; charset=utf-8"}
PDF = re.compile(r"/pdfs/([0-9a-f]{64})\.pdf")
REVIEW_MAX_BYTES = 4096
CASE_MAX_BYTES = 8192
CORRECTION_MAX_BYTES = 65536
REVIEW_HEADERS = ("Host", "Origin", "Content-Length", "Content-Type", "X-RG2W", "Sec-Fetch-Site")
LENGTH = re.compile(r"[0-9]{1,6}")
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class _Handler(BaseHTTPRequestHandler):
    server_version = "rg2watch"
    timeout = 30  # socket timeout: an idle or stalled client cannot hold a thread forever

    def _host(self) -> str | None:
        """The Host header when it names this server (anything else may be DNS rebinding)."""
        host = (self.headers.get("Host") or "").lower()
        ok = (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")
        return host if host in ok else None

    def _target(self) -> tuple[Path, str] | None:
        if self._host() is None or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return None
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            path = "/index.html"
        if m := PDF.fullmatch(path):
            return self.server.cache_dir / (m[1] + ".pdf"), "application/pdf"
        name = path[1:]
        if name in FILES:
            return self.server.out_dir / name, FILES[name]
        return None

    def _send(self, body: bool) -> None:
        if (self.server.get_reviews is not None and urlsplit(self.path).path == "/reviews.json"
                and self._host() is not None and self.headers.get("Sec-Fetch-Site") != "cross-site"):
            try:
                data = self.server.get_reviews()
            except Exception:
                log.exception("reviews could not be read")
                return self._json(500, {"error": "reviews could not be read"}, body)
            return self._json(200, data, body)
        t = self._target()
        try:
            f = t[0].open("rb") if t and t[0].is_file() else None
        except OSError:
            f = None
        if f is None:
            msg = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            if body:
                self.wfile.write(msg)
            return
        with f:
            self.send_response(200)
            self.send_header("Content-Type", t[1])
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if t[1].startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if body:
                shutil.copyfileobj(f, self.wfile)

    def _json(self, status: int, data, body: bool = True) -> None:
        blob = json.dumps(data, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(blob)

    def _review_refusal(self) -> tuple[int, str] | None:
        """(status, reason) when this POST /review must be refused, before the body is read."""
        if any(len(self.headers.get_all(h) or ()) > 1 for h in REVIEW_HEADERS):
            return 400, "duplicate header"
        host = self._host()
        if host is None or self.server.put_review is None:
            return 403, "forbidden"
        site, origin = self.headers.get("Sec-Fetch-Site"), self.headers.get("Origin")
        if origin is not None and origin.lower() != f"http://{host}":
            return 403, "cross-origin"
        if site != "same-origin" and not (site is None and origin is not None):
            return 403, "cross-origin"
        if self.headers.get("X-RG2W") != "1":
            return 403, "missing X-RG2W"
        if (self.headers.get("Content-Type") or "").strip().lower() != "application/json":
            return 400, "Content-Type must be exactly application/json"
        n = self.headers.get("Content-Length")
        if n is None or not LENGTH.fullmatch(n) or int(n) > REVIEW_MAX_BYTES:
            return 400, f"body must have a Content-Length of at most {REVIEW_MAX_BYTES} bytes"
        return None

    def _same_origin(self) -> bool:
        host = self._host()
        if host is None or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        site, origin = self.headers.get("Sec-Fetch-Site"), self.headers.get("Origin")
        return not ((origin is not None and origin.lower() != f"http://{host}") or
                    (site != "same-origin" and not (site is None and origin is not None)))

    def _case_post_refusal(self) -> tuple[int, str] | None:
        if any(len(self.headers.get_all(h) or ()) > 1 for h in REVIEW_HEADERS):
            return 400, "duplicate header"
        if self.server.case_api is None or not self._same_origin():
            return 403, "forbidden"
        if self.headers.get("X-RG2W") != "1":
            return 403, "missing X-RG2W"
        if (self.headers.get("Content-Type") or "").strip().lower() != "application/json":
            return 400, "Content-Type must be exactly application/json"
        n = self.headers.get("Content-Length")
        if n is None or not LENGTH.fullmatch(n) or int(n) > CASE_MAX_BYTES:
            return 400, f"body must have a Content-Length of at most {CASE_MAX_BYTES} bytes"
        return None

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/correction" and not parsed.query:
            return self._correction_post()
        if parsed.path in ("/case/prepare", "/case/publish") and not parsed.query:
            return self._case_post(parsed.path)
        if parsed.path != "/review" or parsed.query:
            return self._refuse()
        self.close_connection = True  # a refused body is never read: do not reuse the connection
        refusal = self._review_refusal()
        if refusal:
            return self._json(refusal[0], {"error": refusal[1]})
        n = int(self.headers["Content-Length"])
        try:
            raw = self.rfile.read(n)
        except (OSError, TimeoutError):  # client stalled or went away: nothing to answer
            return
        if len(raw) != n:
            return self._json(400, {"error": "body shorter than Content-Length"})
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"error": "body is not JSON"})
        if not (isinstance(req, dict) and set(req) <= {"url", "verdict", "note"}
                and isinstance(req.get("url"), str) and isinstance(req.get("verdict"), str)
                and (req.get("note") is None or isinstance(req["note"], str))):
            return self._json(400, {"error": "expected {url, verdict, note}"})
        if req["verdict"] not in VERDICTS:
            return self._json(400, {"error": f"verdict must be one of {', '.join(VERDICTS)}"})
        try:
            rec = self.server.put_review(req["url"], req["verdict"], req.get("note"))
        except ValueError as exc:  # note too long after cleaning (validated once, in the store)
            return self._json(400, {"error": str(exc)})
        except Exception:
            log.exception("review could not be stored")
            return self._json(500, {"error": "review could not be stored"})
        if rec is None:
            return self._json(400, {"error": "unknown document URL"})
        log.info("review: %s %s", req["verdict"], req["url"])
        self._json(200, {"review": rec})

    def _correction_post(self) -> None:
        self.close_connection = True
        refusal = self._correction_post_refusal()
        if refusal:
            return self._json(refusal[0], {"error": refusal[1]})
        n = int(self.headers["Content-Length"])
        try:
            raw = self.rfile.read(n)
        except (OSError, TimeoutError):
            return
        if len(raw) != n:
            return self._json(400, {"error": "body shorter than Content-Length"})
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"error": "body is not JSON"})
        if not isinstance(req, dict) or req.get("action") not in ("correction", "revoke"):
            return self._json(400, {"error": "expected a correction or revoke action"})
        common = (isinstance(req.get("url"), str) and isinstance(req.get("reason"), str)
                  and isinstance(req.get("source_token"), str))
        if req["action"] == "correction":
            valid = common and set(req) == {"url", "action", "snapshot", "reason", "source_token"}
        else:
            valid = common and set(req) == {"url", "action", "reason", "source_token"}
        if not valid:
            return self._json(400, {"error": "malformed correction request"})
        try:
            if req["action"] == "correction":
                state = self.server.correction_api.save(
                    req["url"], req["snapshot"], req["reason"], req["source_token"])
            else:
                state = self.server.correction_api.revoke(
                    req["url"], req["reason"], req["source_token"])
        except CorrectionConflict as exc:
            return self._json(409, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)[:300]})
        except Exception:
            log.exception("operator correction could not be stored")
            return self._json(500, {"error": "operator correction could not be stored"})
        self._json(200, state)

    def _correction_post_refusal(self) -> tuple[int, str] | None:
        if any(len(self.headers.get_all(h) or ()) > 1 for h in REVIEW_HEADERS):
            return 400, "duplicate header"
        if self.server.correction_api is None or not self._same_origin():
            return 403, "forbidden"
        if self.headers.get("X-RG2W") != "1":
            return 403, "missing X-RG2W"
        if (self.headers.get("Content-Type") or "").strip().lower() != "application/json":
            return 400, "Content-Type must be exactly application/json"
        n = self.headers.get("Content-Length")
        if n is None or not LENGTH.fullmatch(n) or int(n) > CORRECTION_MAX_BYTES:
            return 400, f"body must have a Content-Length of at most {CORRECTION_MAX_BYTES} bytes"
        return None

    def _case_post(self, path: str) -> None:
        self.close_connection = True
        refusal = self._case_post_refusal()
        if refusal:
            return self._json(refusal[0], {"error": refusal[1]})
        n = int(self.headers["Content-Length"])
        try:
            raw = self.rfile.read(n)
        except (OSError, TimeoutError):
            return
        if len(raw) != n:
            return self._json(400, {"error": "body shorter than Content-Length"})
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"error": "body is not JSON"})
        try:
            if path == "/case/prepare":
                if not (isinstance(req, dict) and set(req) == {"url", "draft"}
                        and isinstance(req["url"], str) and req["url"]
                        and isinstance(req["draft"], bool)):
                    return self._json(400, {"error": "expected {url, draft: boolean}"})
                job = self.server.case_api.submit_prepare(req["url"], req["draft"])
            else:
                if not (isinstance(req, dict)
                        and set(req) in ({"url", "summary", "confirm"},
                                        {"url", "summary", "viber_reason", "confirm"})
                        and isinstance(req["url"], str) and isinstance(req["summary"], str)
                        and ("viber_reason" not in req or isinstance(req["viber_reason"], str))
                        and req["confirm"] is True):
                    return self._json(400, {"error":
                                      "expected {url, summary, viber_reason, confirm: true}"})
                reason = req.get("viber_reason")
                if "viber_reason" not in req:
                    pending = self.server.case_api.publication(req["url"])
                    if not (pending and pending.get("status") not in (None, "none", "published")
                            and "viber_reason" not in pending):
                        return self._json(400, {"error": "short Viber reason is required"})
                else:
                    from .case_handoff import normalize_viber_reason
                    reason = normalize_viber_reason(reason)
                job = self.server.case_api.submit_publish(
                    req["url"], req["summary"], reason, True)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)[:300]})
        except Exception:
            log.exception("case job could not be submitted")
            return self._json(500, {"error": "case job could not be submitted"})
        self._json(202, {"job_id": job})

    def _case_get(self) -> bool:
        parsed = urlsplit(self.path)
        if not (parsed.path.startswith("/case/jobs/") or parsed.path == "/case/publication"):
            return False
        if self.server.case_api is None:
            return False
        if not self._same_origin():
            self._json(403, {"error": "forbidden"})
            return True
        try:
            if parsed.path.startswith("/case/jobs/") and not parsed.query:
                job_id = parsed.path.removeprefix("/case/jobs/")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
                    self._json(404, {"error": "not found"})
                else:
                    data = self.server.case_api.job(job_id)
                    self._json(200 if data else 404, data or {"error": "not found"})
            elif parsed.path == "/case/publication":
                values = parse_qs(parsed.query, strict_parsing=True)
                if set(values) != {"url"} or len(values["url"]) != 1:
                    self._json(400, {"error": "expected one url query parameter"})
                else:
                    data = self.server.case_api.publication(values["url"][0])
                    self._json(200, data or {"status": "none"})
            else:
                self._json(404, {"error": "not found"})
        except (ValueError, KeyError):
            self._json(400, {"error": "bad request"})
        except Exception:
            log.exception("case status could not be read")
            self._json(500, {"error": "case status could not be read"})
        return True

    def _correction_get(self) -> bool:
        parsed = urlsplit(self.path)
        if parsed.path != "/correction":
            return False
        if self.server.correction_api is None:
            return False
        if not self._same_origin():
            self._json(403, {"error": "forbidden"})
            return True
        try:
            values = parse_qs(parsed.query, strict_parsing=True)
            if set(values) != {"url"} or len(values["url"]) != 1:
                self._json(400, {"error": "expected one url query parameter"})
            else:
                self._json(200, self.server.correction_api.state(values["url"][0]))
        except ValueError as exc:
            self._json(400, {"error": str(exc)[:300]})
        except Exception:
            log.exception("operator correction could not be read")
            self._json(500, {"error": "operator correction could not be read"})
        return True

    def do_GET(self) -> None:
        if self._correction_get():
            return
        if self._case_get():
            return
        self._send(True)

    def do_HEAD(self) -> None:
        self._send(False)

    def _refuse(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    def log_message(self, fmt, *args) -> None:
        log.debug("%s " + fmt, self.address_string(), *args)


def start_server(out_dir: Path, cache_dir: Path, port: int,
                   get_reviews: Callable[[], dict] | None = None,
                   put_review: Callable[[str, str, str | None], dict | None] | None = None,
                   case_api=None, correction_api=None
                 ) -> ThreadingHTTPServer | None:
    """Serve `out_dir` (+ cached PDFs) in a daemon thread; None when the port is unavailable.
    `get_reviews` serves GET /reviews.json, `put_review(url, verdict, note)` backs POST /review,
    and `correction_api` backs the audited correction endpoint; absent APIs are refused."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        log.warning("web server not started, port %d unavailable: %s", port, exc)
        return None
    server.daemon_threads = True
    server.out_dir, server.cache_dir = Path(out_dir).resolve(), Path(cache_dir).resolve()
    server.get_reviews, server.put_review = get_reviews, put_review
    server.case_api = case_api
    server.correction_api = correction_api
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("browse page at http://127.0.0.1:%d", server.server_port)
    return server
