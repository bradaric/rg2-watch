import re
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime
from urllib.parse import quote

import httpx

from rg2watch.config import Config
from rg2watch.models import Finding

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "dj", "е": "e", "ж": "z",
    "з": "z", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m", "н": "n",
    "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ћ": "c", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "c", "џ": "dz", "ш": "s",
}
_TRANSLIT.update({k.upper(): v.capitalize() for k, v in list(_TRANSLIT.items())})


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def to_ascii(text: str) -> str:
    out = "".join(_TRANSLIT.get(c, c) for c in text).encode("ascii", "replace").decode()
    out = _CONTROL.sub(" ", out)
    return " ".join(out.split())


def _uploaded(f: Finding) -> str:
    try:
        return datetime.fromisoformat(f.doc.uploaded_at).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return f.doc.uploaded_at


def format_alert(f: Finding) -> tuple[str, str]:
    d = f.doc
    title = f"[{f.severity}] {f.rule} {d.municipality} BM {d.station_no}"
    lines = [f.summary, d.station_name, f"uploaded {_uploaded(f)}", d.url]
    if f.previous_url:
        lines.append(f"was: {f.previous_url}")
    body = "\n".join(lines)
    return title, body


DIGEST_MAX_BYTES = 3500  # ntfy.sh body limit is 4096
DIGEST_DETAIL_LINES = 10
DIGEST_CODES = 6
DIGEST_SUMMARY_CHARS = 80
_DIGEST_PRIORITY = ["A9", "A5", "V9", "V2", "V8", "R1", "D1", "D2", "D4"]


def digest_body(findings: list[Finding], page_url: str | None = None, severity: str = "MEDIUM") -> str:
    """Compact digest (MEDIUM, or HIGH when rate-limited): counts by rule, a few detail lines, no URLs (except for a single finding)."""
    counts = Counter(f.rule for f in findings)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    parts = [f"{r} {c}" for r, c in ranked[:DIGEST_CODES]]
    if len(ranked) > DIGEST_CODES:
        parts.append(f"other {sum(c for _, c in ranked[DIGEST_CODES:])}")
    lines = [", ".join(parts)]
    rank = {r: i for i, r in enumerate(_DIGEST_PRIORITY)}
    shown = sorted(findings, key=lambda f: rank.get(f.rule, len(rank)))[:DIGEST_DETAIL_LINES]
    for f in shown:
        summary = f.summary if len(f.summary) <= DIGEST_SUMMARY_CHARS else f.summary[:DIGEST_SUMMARY_CHARS - 3] + "..."
        line = f"{f.rule} {f.doc.municipality} BM {f.doc.station_no}: {summary}"
        if len(findings) == 1:
            line += f" {f.doc.url}"
        lines.append(line)
    if len(findings) > len(shown):
        more = f"+{len(findings) - len(shown)} more - open the page, filter {severity}"
        lines.append(f"{more} {page_url}" if page_url else more)
    body = "\n".join(lines)
    while len(body.encode("utf-8")) > DIGEST_MAX_BYTES and len(lines) > 1:
        lines.pop(-2 if len(lines) > 2 and lines[-1].startswith("+") else -1)
        body = "\n".join(lines)
    return body.encode("utf-8")[:DIGEST_MAX_BYTES].decode("utf-8", "ignore")


BACKOFF_START_S = 5.0
BACKOFF_CAP_S = 60.0


class Notifier:
    def __init__(self, cfg: Config, http: httpx.Client, out=sys.stdout, clock=time.monotonic):
        self.cfg = cfg
        self.http = http
        self.out = out
        self.clock = clock
        self._printed: set[int] = set()
        self._pending: dict[int, Finding] = {}
        self._pending_since: float | None = None
        self._high_times: deque[float] = deque()  # clock of each HIGH push (single or digest) in the window
        self._high_held: dict[int, Finding] = {}  # HIGH findings rolled into a digest, not yet sent
        self._digest_urls: set[str] = set()  # D2 replacements of a mass replacement: digest only
        self._status: list[str] = []
        self.page_url: str | None = None  # set by the CLI once the web page is serving
        self._backoff = 0.0
        self._backoff_until = 0.0
        self.last_ok: float | None = None  # wall time of the last accepted push
        self.last_fail: float | None = None
        self._lock = threading.RLock()  # status() is called from analysis worker threads
        self.unmarked: set[int] = set()  # delivered but not yet marked alerted (mark failed): never re-sent

    def _post(self, title: str, body: str, priority: str, tags: str, click: str | None = None) -> bool:
        """Push one message; after a failure, skip pushes until the backoff window ends."""
        if self.clock() < self._backoff_until:
            return False
        headers = {"Title": to_ascii(title), "Priority": priority, "Tags": to_ascii(tags)}
        if click:
            headers["Click"] = quote(click, safe=":/?&=%#@+,;~-._")
        try:
            r = self.http.post(
                f"{self.cfg.ntfy_url.rstrip('/')}/{self.cfg.ntfy_topic}",
                content=body.encode("utf-8"),
                headers=headers,
            )
            r.raise_for_status()
        except httpx.HTTPError:
            self._backoff = min(BACKOFF_CAP_S, self._backoff * 2 or BACKOFF_START_S)
            self._backoff_until = self.clock() + self._backoff
            self.last_fail = time.time()
            return False
        self._backoff = 0.0
        self.last_ok = time.time()
        return True

    def status_snapshot(self) -> dict:
        """ntfy delivery state for the status panel: in-memory only."""
        failing = self._backoff > 0
        return {"enabled": bool(self.cfg.ntfy_topic), "failing": failing, "last_ok": self.last_ok,
                "last_fail": self.last_fail, "queued": len(self._status) + len(self._pending) + len(self._high_held)}

    def _print(self, fid: int, f: Finding) -> None:
        if fid in self._printed:
            return
        self._printed.add(fid)
        title, body = format_alert(f)
        print(f"{title}\n{body}\n", file=self.out, flush=True)

    def status(self, text: str) -> None:
        """Self-alert about the watcher itself (HIGH, not stored): printed now, pushed until delivered."""
        with self._lock:
            print(f"{text}\n", file=self.out, flush=True)
            if self.cfg.ntfy_topic:
                self._status.append(text)
                self._flush_status()

    def _flush_status(self) -> None:
        while self._status and self._post(self._status[0], self._status[0], "high", "warning"):
            self._status.pop(0)

    def digest_only(self, urls: list[str]) -> None:
        """Route the D2 findings of these documents to the HIGH digest (mass replacement)."""
        with self._lock:
            self._digest_urls.update(urls)

    def _high_slot(self) -> bool:
        now = self.clock()
        while self._high_times and now - self._high_times[0] >= self.cfg.high_push_window_s:
            self._high_times.popleft()
        return len(self._high_times) < self.cfg.high_push_max

    def send(self, findings_with_ids: list[tuple[int, Finding]], flush: bool = False) -> list[int]:
        """`flush` pushes the MEDIUM digest now instead of waiting for medium_digest_s."""
        with self._lock:
            delivered = self._send(findings_with_ids, flush)
            self.unmarked.update(delivered)
            return delivered

    def marked(self, ids) -> None:
        with self._lock:
            self.unmarked.difference_update(ids)

    def _send(self, findings_with_ids: list[tuple[int, Finding]], flush: bool = False) -> list[int]:
        delivered: list[int] = []
        push = bool(self.cfg.ntfy_topic and self.cfg.ntfy_findings)
        self._flush_status()
        for fid, f in findings_with_ids:
            self._print(fid, f)
            if (f.severity == "LOW" or not push
                    or (f.severity == "MEDIUM" and not self.cfg.ntfy_medium)):
                delivered.append(fid)
            elif f.severity == "HIGH":
                if fid in self._high_held:
                    continue
                if (f.rule == "D2" and f.doc.url in self._digest_urls) or self._high_held \
                        or not self._high_slot():
                    self._high_held[fid] = f  # over the rate limit, or a backlog is waiting: digest
                    continue
                title, body = format_alert(f)
                if self._post(title, body, "high", "rotating_light", f.doc.url):
                    self._high_times.append(self.clock())
                    delivered.append(fid)
            elif fid not in self._pending:
                self._pending[fid] = f
                if self._pending_since is None:
                    self._pending_since = self.clock()
        if self._high_held and push and self._high_slot():
            n = len(self._high_held)
            body = digest_body(list(self._high_held.values()), self.page_url, "HIGH")
            if self._post(f"[HIGH] {n} finding{'s' if n != 1 else ''}", body, "high", "rotating_light"):
                self._high_times.append(self.clock())
                delivered.extend(self._high_held)
                self._high_held.clear()
        if (
            self._pending
            and self._pending_since is not None
            and (flush or self.clock() - self._pending_since >= self.cfg.medium_digest_s)
        ):
            n = len(self._pending)
            body = digest_body(list(self._pending.values()), self.page_url)
            if self._post(f"[MEDIUM] {n} finding{'s' if n != 1 else ''}", body, "default", "warning"):
                delivered.extend(self._pending)
                self._pending.clear()
                self._pending_since = None
        return delivered
