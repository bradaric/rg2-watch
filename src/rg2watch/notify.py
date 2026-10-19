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


def to_ascii(text: str) -> str:
    return "".join(_TRANSLIT.get(c, c) for c in text).encode("ascii", "replace").decode()


def _uploaded(f: Finding) -> str:
    try:
        return datetime.fromisoformat(f.doc.uploaded_at).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return f.doc.uploaded_at


def format_alert(f: Finding) -> tuple[str, str]:
    d = f.doc
    title = f"[{f.severity}] {f.rule} {d.municipality} BM {d.station_no}"
    body = "\n".join([f.summary, d.station_name, f"uploaded {_uploaded(f)}", d.url])
    return title, body


class Notifier:
    def __init__(self, cfg: Config, http: httpx.Client, out=sys.stdout, clock=time.monotonic):
        self.cfg = cfg
        self.http = http
        self.out = out
        self.clock = clock
        self._printed: set[int] = set()
        self._pending: dict[int, Finding] = {}
        self._pending_since: float | None = None

    def _post(self, title: str, body: str, priority: str, tags: str, click: str | None = None) -> bool:
        headers = {"Title": to_ascii(title), "Priority": priority, "Tags": tags}
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
            return False
        return True

    def _print(self, fid: int, f: Finding) -> None:
        if fid in self._printed:
            return
        self._printed.add(fid)
        title, body = format_alert(f)
        print(f"{title}\n{body}\n", file=self.out)

    def send(self, findings_with_ids: list[tuple[int, Finding]]) -> list[int]:
        delivered: list[int] = []
        push = bool(self.cfg.ntfy_topic)
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
                )
            n = len(lines)
            if self._post(f"[MEDIUM] {n} finding{'s' if n != 1 else ''}", "\n".join(lines), "default", "warning"):
                delivered.extend(self._pending)
                self._pending.clear()
                self._pending_since = None
        return delivered
