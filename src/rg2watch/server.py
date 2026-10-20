"""Read-only local web server for the browse page: 127.0.0.1, GET/HEAD, strict allow-list."""
import logging
import os
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger("rg2watch.server")
FILES = {  # the only names served from the page directory
    "index.html": "text/html; charset=utf-8", "status.js": "text/javascript; charset=utf-8",
    "status.json": "application/json", "data.js": "text/javascript; charset=utf-8",
    "data.json": "application/json", "jquery.min.js": "text/javascript; charset=utf-8",
    "dataTables.min.js": "text/javascript; charset=utf-8",
    "dataTables.dataTables.min.css": "text/css; charset=utf-8"}
PDF = re.compile(r"/pdfs/([0-9a-f]{64})\.pdf")
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; object-src 'none'"


class _Handler(BaseHTTPRequestHandler):
    server_version = "rg2watch"

    def _target(self) -> tuple[Path, str] | None:
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"):
            return None  # DNS rebinding
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

    def do_GET(self) -> None:
        self._send(True)

    def do_HEAD(self) -> None:
        self._send(False)

    def _refuse(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    def log_message(self, fmt, *args) -> None:
        log.debug("%s " + fmt, self.address_string(), *args)


def start_server(out_dir: Path, cache_dir: Path, port: int) -> ThreadingHTTPServer | None:
    """Serve `out_dir` (+ cached PDFs) in a daemon thread; None when the port is unavailable."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        log.warning("web server not started, port %d unavailable: %s", port, exc)
        return None
    server.daemon_threads = True
    server.out_dir, server.cache_dir = Path(out_dir).resolve(), Path(cache_dir).resolve()
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("browse page at http://127.0.0.1:%d", server.server_port)
    return server
