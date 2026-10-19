import io

import httpx

from rg2watch.config import Config
from rg2watch.models import Doc, Finding
from rg2watch.notify import Notifier, format_alert


def fin(sev="HIGH", rule="D2", no="3") -> Finding:
    d = Doc("Град Зајечар", no, "ОШ Тест", "x", 6, "https://x/a.pdf", "2025-11-02T21:05")
    return Finding(rule, sev, d, "RG-2 file replaced")


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def make(handler, topic="t", clock=None):
    cfg = Config(ntfy_topic=topic, ntfy_url="http://ntfy.test", medium_digest_s=600)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    out = io.StringIO()
    return Notifier(cfg, http, out, clock or Clock()), out


def test_alert_format_uk_date_and_url():
    title, body = format_alert(fin())
    assert title == "[HIGH] D2 Град Зајечар BM 3"
    assert body.split("\n") == ["RG-2 file replaced", "ОШ Тест", "uploaded 02/11/2025 21:05", "https://x/a.pdf"]


def test_high_sent_individually():
    reqs = []
    n, out = make(lambda r: reqs.append(r) or httpx.Response(200))
    assert n.send([(1, fin()), (2, fin(no="4"))]) == [1, 2]
    assert len(reqs) == 2
    r = reqs[0]
    assert str(r.url) == "http://ntfy.test/t"
    assert r.headers["Priority"] == "high" and r.headers["Click"] == "https://x/a.pdf"
    r.headers["Title"].encode("ascii")
    assert r.headers["Title"] == "[HIGH] D2 Grad Zajecar BM 3"
    assert "ОШ Тест" in r.content.decode("utf-8")
    assert "Град Зајечар" in out.getvalue()


def test_medium_digest_waits_for_interval():
    reqs = []
    clock = Clock()
    n, out = make(lambda r: reqs.append(r) or httpx.Response(200), clock=clock)
    m = [(5, fin("MEDIUM", "D1")), (6, fin("MEDIUM", "D4", "4"))]
    assert n.send(m) == []
    clock.t = 599
    assert n.send(m) == []
    assert reqs == []
    clock.t = 600
    assert sorted(n.send(m)) == [5, 6]
    assert len(reqs) == 1 and len(reqs[0].content.decode().splitlines()) == 2
    assert out.getvalue().count("[MEDIUM] D1") == 1  # printed once despite retries
    assert n.send([]) == []


def test_low_printed_not_pushed():
    reqs = []
    n, out = make(lambda r: reqs.append(r) or httpx.Response(200))
    assert n.send([(9, fin("LOW", "V10"))]) == [9]
    assert reqs == [] and "[LOW]" in out.getvalue()


def test_ntfy_down_returns_undelivered():
    def boom(request):
        raise httpx.ConnectError("down")

    n, out = make(boom)
    assert n.send([(1, fin())]) == []
    n2, _ = make(lambda r: httpx.Response(503))
    assert n2.send([(1, fin())]) == []


def test_no_topic_stdout_only():
    reqs = []
    n, out = make(lambda r: reqs.append(r) or httpx.Response(200), topic=None)
    assert n.send([(1, fin()), (2, fin("MEDIUM", "D1"))]) == [1, 2]
    assert reqs == [] and "[HIGH]" in out.getvalue()


def test_digest_failure_then_retry():
    ok = [False]
    reqs = []

    def h(r):
        reqs.append(r)
        return httpx.Response(200 if ok[0] else 503)

    clock = Clock()
    n, _ = make(h, clock=clock)
    m = [(5, fin("MEDIUM", "D1"))]
    n.send(m)
    clock.t = 600
    assert n.send(m) == []
    ok[0] = True
    assert n.send(m) == [5]
    assert len(reqs) == 2


def test_click_cyrillic_url_percent_encoded():
    reqs = []
    n, _ = make(lambda r: reqs.append(r) or httpx.Response(200))
    f = fin()
    f = Finding(f.rule, f.severity, Doc(f.doc.municipality, "3", "x", "x", 6, "https://x/записник.pdf", "2025-11-02T21:05"), "s")
    assert n.send([(1, f)]) == [1]
    c = reqs[0].headers["Click"]
    c.encode("ascii")
    assert c.startswith("https://x/%D0%B7")


def test_title_with_newline_delivers_clean_header():
    reqs = []
    n, _ = make(lambda r: reqs.append(r) or httpx.Response(200))
    f = fin()
    f = Finding(f.rule, f.severity, Doc("Град\nЗајечар\r", "3", "x", "x", 6, "https://x/a.pdf", "2025-11-02T21:05"), "s")
    assert n.send([(1, f)]) == [1]
    t = reqs[0].headers["Title"]
    assert "\n" not in t and "\r" not in t and t == "[HIGH] D2 Grad Zajecar BM 3"
