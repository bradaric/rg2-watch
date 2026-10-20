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
    assert len(reqs) == 1 and len(reqs[0].content.decode().splitlines()) == 3
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
    assert n.send(m) == []  # backing off after the failure
    clock.t = 605
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


def test_to_ascii_strips_control_chars():
    from rg2watch.notify import to_ascii
    assert to_ascii("Град\x00Зајечар\x1b[31m\x7fX\t") == "Grad Zajecar [31m X"


def test_d2_alert_shows_previous_url_digest_does_not():
    f = fin()
    f = Finding(f.rule, f.severity, f.doc, f.summary, previous_url="https://x/old.pdf")
    assert format_alert(f)[1].split("\n")[-1] == "was: https://x/old.pdf"
    reqs = []
    clock = Clock()
    n, _ = make(lambda r: reqs.append(r) or httpx.Response(200), clock=clock)
    m = Finding(f.rule, "MEDIUM", f.doc, f.summary, previous_url="https://x/old.pdf")
    n.send([(1, m)])
    clock.t = 600
    assert n.send([(1, m)]) == [1]
    body = reqs[0].content.decode()
    assert body.endswith("https://x/a.pdf") and "was:" not in body


def test_unreachable_ntfy_backs_off_exponentially():
    attempts = []

    def boom(request):
        attempts.append(1)
        raise httpx.ConnectError("down")

    clock = Clock()
    n, out = make(boom, clock=clock)
    pending = [(1, fin(no="1")), (2, fin(no="2")), (3, fin(no="3"))]
    per_tick = []
    for t in range(0, 40):
        clock.t = float(t)
        before = len(attempts)
        assert n.send(pending) == []
        per_tick.append(len(attempts) - before)
    assert max(per_tick) == 1
    assert [t for t, k in enumerate(per_tick) if k] == [0, 5, 15, 35]  # 5, 10, 20 s windows
    assert out.getvalue().count("[HIGH]") == 3  # printing is unaffected


def test_backoff_caps_at_60s_and_resets_on_success():
    ok = [False]
    attempts = []

    def h(r):
        attempts.append(1)
        return httpx.Response(200 if ok[0] else 503)

    clock = Clock()
    n, _ = make(h, clock=clock)
    times = []
    for t in range(0, 400):
        clock.t = float(t)
        before = len(attempts)
        n.send([(1, fin())])
        if len(attempts) > before:
            times.append(t)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps[:4] == [5, 10, 20, 40] and set(gaps[4:]) == {60}
    ok[0] = True
    clock.t = 1000.0
    assert n.send([(1, fin())]) == [1]
    ok[0] = False
    clock.t = 1001.0
    n.send([(2, fin())])
    before = len(attempts)
    clock.t = 1006.0
    n.send([(2, fin())])
    assert len(attempts) == before + 1  # backoff restarted at 5 s


def test_status_alert_printed_and_pushed_without_store():
    reqs = []
    n, out = make(lambda r: reqs.append(r) or httpx.Response(200))
    n.status("[rg2-watch] RIK unreachable/blocking since 21:05")
    assert "[rg2-watch] RIK unreachable/blocking since 21:05" in out.getvalue()
    assert len(reqs) == 1 and reqs[0].headers["Priority"] == "high"
    assert reqs[0].headers["Title"] == "[rg2-watch] RIK unreachable/blocking since 21:05"


def test_status_alert_retried_after_backoff():
    ok = [False]
    reqs = []

    def h(r):
        reqs.append(r)
        return httpx.Response(200 if ok[0] else 503)

    clock = Clock()
    n, out = make(h, clock=clock)
    n.status("[rg2-watch] RIK reachable again")
    ok[0] = True
    n.send([])
    assert len(reqs) == 1
    clock.t = 5
    n.send([])
    n.send([])
    assert len(reqs) == 2
    assert out.getvalue().count("RIK reachable again") == 1


def test_printed_alerts_are_flushed():
    class Out(io.StringIO):
        flushes = 0

        def flush(self):
            Out.flushes += 1

    cfg = Config(ntfy_topic=None)
    out = Out()
    n = Notifier(cfg, None, out, Clock())
    n.send([(1, fin())])
    assert Out.flushes >= 1


def _digest(findings, page_url=None):
    reqs = []
    clock = Clock()
    n, _ = make(lambda r: reqs.append(r) or httpx.Response(200), clock=clock)
    n.page_url = page_url
    items = list(enumerate(findings))
    n.send(items)
    clock.t = 600
    assert n.send(items) == [i for i, _ in items]
    assert len(reqs) == 1
    return reqs[0]


def _mf(rule, no, summary="remark"):
    f = fin("MEDIUM", rule, str(no))
    return Finding(rule, "MEDIUM", f.doc, summary)


def test_big_digest_is_capped_and_lists_counts():
    fs = [_mf("P3", i, "x" * 200) for i in range(41)] + [_mf("V4", i) for i in range(28)]
    fs += [_mf(r, i) for i, r in enumerate(["A9"] * 12 + ["V8"] * 7 + ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7", "Z8"])]
    r = _digest(fs)
    body = r.content.decode("utf-8")
    assert len(r.content) <= 3500
    assert r.headers["Title"] == f"[MEDIUM] {len(fs)} findings"
    assert body.split("\n")[0] == "P3 41, V4 28, A9 12, V8 7, Z1 1, Z2 1, other 6"
    assert "http" not in body and "x" * 81 not in body
    assert body.split("\n")[-1] == f"+{len(fs) - 10} more - open the page, filter MEDIUM"


def test_digest_orders_priority_rules_first_and_limits_to_ten():
    fs = [_mf("P3", i) for i in range(5)] + [_mf("D4", 1), _mf("A9", 2), _mf("V2", 3), _mf("A5", 4)]
    fs += [_mf("P3", 9 + i) for i in range(5)]
    lines = _digest(fs).content.decode("utf-8").split("\n")
    assert [ln.split()[0] for ln in lines[1:5]] == ["A9", "A5", "V2", "D4"]
    assert len(lines) == 1 + 10 + 1 and lines[-1].startswith("+4 more")
    assert lines[1] == "A9 Град Зајечар BM 2: remark"


def test_single_finding_digest_keeps_url():
    body = _digest([_mf("D1", 7)]).content.decode("utf-8")
    assert body.split("\n") == ["D1 1", "D1 Град Зајечар BM 7: remark https://x/a.pdf"]


def test_digest_appends_page_url():
    fs = [_mf("P3", i) for i in range(12)]
    body = _digest(fs, "http://127.0.0.1:8000").content.decode("utf-8")
    assert body.endswith("+2 more - open the page, filter MEDIUM http://127.0.0.1:8000")
    assert "http" not in body.replace("http://127.0.0.1:8000", "")


def test_digest_cap_never_splits_characters():
    f = _mf("P3", 1, "ш" * 5000)
    f = Finding("P3", "MEDIUM", f.doc, f.summary)
    from rg2watch.notify import digest_body
    big = Finding("P3", "MEDIUM", Doc("Ш" * 3000, "1", "s", "x", 6, "https://x/a.pdf", "2025-11-02T21:05"), "s")
    out = digest_body([big, f])
    assert len(out.encode("utf-8")) <= 3500


def _high(i: int, rule="V1") -> Finding:
    d = Doc("Град Зајечар", str(i), "ОШ", "x", 6, f"https://x/{i}.pdf", "2025-11-02T21:05")
    return Finding(rule, "HIGH", d, "crossed out")


def test_high_pushes_are_rate_limited_and_roll_into_a_digest():
    reqs = []
    clock = Clock()
    n, _ = make(lambda r: reqs.append(r) or httpx.Response(200), clock=clock)
    n.cfg.high_push_max, n.cfg.high_push_window_s = 3, 300
    fs = [(i, _high(i)) for i in range(1, 8)]
    assert n.send(fs) == [1, 2, 3]
    assert len(reqs) == 3 and n.status_snapshot()["queued"] == 4
    assert n.send(fs[3:]) == []  # window full: nothing more goes out, nothing is lost
    assert len(reqs) == 3
    clock.t = 301
    assert sorted(n.send(fs[3:])) == [4, 5, 6, 7]
    assert len(reqs) == 4
    r = reqs[-1]
    assert r.headers["Title"] == "[HIGH] 4 findings" and r.headers["Priority"] == "high"
    assert r.content.decode().split("\n")[0] == "V1 4"
    assert n.status_snapshot()["queued"] == 0


def test_status_alert_goes_out_while_high_backlog_is_held():
    reqs = []
    n, _ = make(lambda r: reqs.append(r) or httpx.Response(200))
    n.cfg.high_push_max = 1
    n.send([(i, _high(i)) for i in range(1, 5)])
    n.status("[rg2-watch] RIK unreachable")
    assert [r.headers["Title"] for r in reqs][-1] == "[rg2-watch] RIK unreachable"
    assert len(reqs) == 2


def test_status_retries_before_findings_after_ntfy_failure():
    reqs, ok = [], [False]
    clock = Clock()

    def handler(r):
        reqs.append(r.headers["Title"])
        return httpx.Response(200 if ok[0] else 503)

    n, _ = make(handler, clock=clock)
    n.status("[rg2-watch] spend cap reached")
    ok[0], clock.t = True, 10
    assert n.send([(1, _high(1))]) == [1]
    assert reqs[-2:] == ["[rg2-watch] spend cap reached", "[HIGH] V1 Grad Zajecar BM 1"]


def test_mass_replacement_findings_go_to_the_digest_only():
    reqs = []
    n, _ = make(lambda r: reqs.append(r) or httpx.Response(200))
    fs = [(i, _high(i, "D2")) for i in range(1, 6)]
    n.digest_only([f.doc.url for _, f in fs])
    assert sorted(n.send(fs)) == [1, 2, 3, 4, 5]
    assert len(reqs) == 1 and reqs[0].headers["Title"] == "[HIGH] 5 findings"
