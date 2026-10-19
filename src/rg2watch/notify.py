import re
import sys
import time
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
        self._status: list[str] = []
        self._backoff = 0.0
        self._backoff_until = 0.0

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
            return False
        self._backoff = 0.0
        return True

    def _print(self, fid: int, f: Finding) -> None:
        if fid in self._printed:
            return
        self._printed.add(fid)
        title, body = format_alert(f)
        print(f"{title}\n{body}\n", file=self.out, flush=True)

    def status(self, text: str) -> None:
        """Self-alert about the watcher itself (HIGH, not stored): printed now, pushed until delivered."""
        print(f"{text}\n", file=self.out, flush=True)
        if self.cfg.ntfy_topic:
            self._status.append(text)
            self._flush_status()

    def _flush_status(self) -> None:
        while self._status and self._post(self._status[0], self._status[0], "high", "warning"):
            self._status.pop(0)

    def send(self, findings_with_ids: list[tuple[int, Finding]]) -> list[int]:
        delivered: list[int] = []
        push = bool(self.cfg.ntfy_topic)
        self._flush_status()
        for fid, f in findings_with_ids:
            self._print(fid, f)
            if f.severity == "LOW" or not push:
                delivered.append(fid)
            elif f.severity == "HIGH":
                title, body = format_alert(f)
                if self._post(title, body, "high", "rotating_light", f.doc.url):
                    delivered.append(fid)
            elif fid not in self._pending:
                self._pending[fid] = f
                if self._pending_since is None:
                    self._pending_since = self.clock()
        if (
            self._pending
            and self._pending_since is not None
            and self.clock() - self._pending_since >= self.cfg.medium_digest_s
        ):
            lines = []
            for f in self._pending.values():
                lines.append(
                    f"{f.rule} {f.doc.municipality} BM {f.doc.station_no} "
                    f"{f.doc.station_name}: {f.summary} {f.doc.url}"
                    + (f" was: {f.previous_url}" if f.previous_url else "")
                )
            n = len(lines)
            if self._post(f"[MEDIUM] {n} finding{'s' if n != 1 else ''}", "\n".join(lines), "default", "warning"):
                delivered.extend(self._pending)
                self._pending.clear()
                self._pending_since = None
        return delivered
