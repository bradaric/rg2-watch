import hashlib
import multiprocessing
from pathlib import Path

import pytest

from rg2watch.case_handoff import CaseError, CaseHandoff, manual_template
from rg2watch.case_locks import CaseLocks
from rg2watch.case_draft import CaseDrafter, DraftUnavailable
from rg2watch.config import Config
from rg2watch.store import Store
from test_browse import BASE, P, add, doc


URL = BASE + "/extfile/publish.pdf"


def handoff(tmp_path, verdict="escalate", pdf=b"%PDF-1.4\ncase"):
    db = tmp_path / "watch.sqlite"
    store = Store(db)
    d = doc(URL)
    add(store, d)
    store.set_review(URL, verdict, "проверити записник")
    cache = tmp_path / "pdfs"
    cache.mkdir()
    path = cache / (hashlib.sha256(URL.encode()).hexdigest() + ".pdf")
    if pdf is not None:
        path.write_bytes(pdf)
    cfg = Config(db_path=db, max_pdf_bytes=1000)
    return store, CaseHandoff(store, cfg), path


def test_old_database_migrates_publication_and_draft_cost_tables(tmp_path):
    db = tmp_path / "old.sqlite"
    Store(db).close()
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("DROP TABLE publications")
    con.execute("DROP TABLE ai_draft_costs")
    con.commit()
    con.close()
    s = Store(db)
    names = {r[0] for r in s._db.execute("SELECT name FROM sqlite_master")}
    assert {"publications", "ai_draft_costs"} <= names


def test_draft_cost_counts_against_existing_parent_cap_and_status(tmp_path):
    store, _, _ = handoff(tmp_path)
    store.record_draft_cost(P, URL, "claude-haiku-5-5", 12, 8, .125, "done")
    assert store.total_spend(P) == pytest.approx(.125)
    assert store.status_counts(P)["spend"] == pytest.approx(.125)


@pytest.mark.parametrize("verdict", ["ok", "unsure"])
def test_new_publication_requires_latest_escalate(tmp_path, verdict):
    _, service, _ = handoff(tmp_path, verdict)
    with pytest.raises(CaseError, match="escalate"):
        service.prepare(URL, draft=False)


def test_preflight_uses_exact_cached_pdf_without_downloading(tmp_path):
    store, service, path = handoff(tmp_path)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    out = service.prepare(URL, draft=False)
    assert out["pdf_sha256"] == expected
    assert out["summary"] == manual_template(out["case"])
    path.unlink()
    with pytest.raises(CaseError, match="cached PDF"):
        service.prepare(URL, draft=False)
    store.close()


@pytest.mark.parametrize("data", [b"not pdf", b"", b"%PDF" + b"x" * 1001])
def test_preflight_rejects_bad_or_oversize_pdf(tmp_path, data):
    _, service, _ = handoff(tmp_path, pdf=data)
    with pytest.raises(CaseError):
        service.prepare(URL, draft=False)


def test_claim_freezes_summary_and_pdf_and_refuses_changed_retry(tmp_path):
    store, service, cache = handoff(tmp_path)
    first = service.claim(URL, "Опрезан јавни опис.")
    assert Path(first["staged_pdf_path"]).read_bytes() == cache.read_bytes()
    assert Path(first["staged_pdf_path"]).stat().st_mode & 0o777 == 0o600
    cache.write_bytes(b"%PDF-1.4\nchanged")
    with pytest.raises(CaseError, match="frozen"):
        service.claim(URL, "Опрезан јавни опис.")
    with pytest.raises(CaseError, match="frozen"):
        service.claim(URL, "Други опис")
    store.close()


def test_completed_publication_is_historical_after_ok(tmp_path):
    store, service, _ = handoff(tmp_path)
    p = service.claim(URL, "Јавни опис")
    store.save_drive_ids(URL, "folder", "pdf", "text")
    store.finalize_publication(URL, "https://drive.google.com/drive/folders/folder",
                               "Вибер порука")
    store.set_review(URL, "ok", "исправљено")
    got = service.claim(URL, "a different ignored summary")
    assert got["phase"] == "published"
    assert got["final_url"].endswith("/folder")
    assert got["viber_text"] == "Вибер порука"
    assert p["url"] == URL


def _try_publish_lock(db, url, q):
    lock = CaseLocks(db).publish(url, blocking=False)
    q.put(lock is not None)
    if lock:
        lock.close()


def test_publish_flock_is_cross_process_and_released_on_exit(tmp_path):
    db = tmp_path / "watch.sqlite"
    held = CaseLocks(db).publish(URL, blocking=False)
    assert held is not None
    q = multiprocessing.Queue()
    p = multiprocessing.Process(target=_try_publish_lock, args=(db, URL, q))
    p.start(); p.join(5)
    assert q.get(timeout=1) is False
    held.close()
    p = multiprocessing.Process(target=_try_publish_lock, args=(db, URL, q))
    p.start(); p.join(5)
    assert q.get(timeout=1) is True


def test_manual_template_and_viber_treat_urls_and_markup_as_text(tmp_path):
    _, service, _ = handoff(tmp_path)
    out = service.prepare(URL, draft=False)
    assert "Избори:" in out["summary"] and URL in out["summary"]
    text = service.viber_text(out["case"], "<b>Непроверено</b>", "https://drive.example/f")
    assert "Могућа неправилност" in text
    assert "<b>Непроверено</b>" in text
    assert "https://drive.example/f" in text


class FakeMessages:
    def __init__(self, text="Опрезно треба проверити неслагање."):
        self.text = text
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        usage = type("Usage", (), {"input_tokens": 100, "output_tokens": 20,
                                    "cache_read_input_tokens": 0,
                                    "cache_creation_input_tokens": 0})()
        block = type("Block", (), {"type": "text", "text": self.text})()
        return type("Response", (), {"usage": usage, "model": kw["model"],
                                     "content": [block]})()


def test_ai_draft_is_explicit_bounded_cyrillic_and_billed_to_existing_cap(tmp_path):
    store, service, _ = handoff(tmp_path)
    messages = FakeMessages()
    client = type("Client", (), {"messages": messages})()
    service.drafter = CaseDrafter(store, service.cfg, client)
    out = service.prepare(URL, draft=True)
    assert out["summary"].startswith("Опрезно")
    call = messages.calls[0]
    assert call["max_tokens"] <= 700 and len(call["messages"][0]["content"]) < 7000
    assert "проверити записник" in call["messages"][0]["content"]
    assert store.total_spend(P) > 0


def test_draft_refuses_non_anthropic_unknown_price_and_exhausted_cap(tmp_path):
    store, service, _ = handoff(tmp_path)
    client = type("Client", (), {"messages": FakeMessages()})()
    service.cfg.model = "ollama:qwen"
    with pytest.raises(DraftUnavailable):
        CaseDrafter(store, service.cfg, client).draft(service._case(URL))
    service.cfg.model = "unknown-model"
    with pytest.raises(DraftUnavailable):
        CaseDrafter(store, service.cfg, client).draft(service._case(URL))
    service.cfg.model = "claude-haiku-5-5"
    service.cfg.max_spend_usd = 0
    with pytest.raises(DraftUnavailable, match="cap"):
        CaseDrafter(store, service.cfg, client).draft(service._case(URL))


def test_unusable_paid_draft_is_still_accounted(tmp_path):
    store, service, _ = handoff(tmp_path)
    client = type("Client", (), {"messages": FakeMessages("plain latin")})()
    with pytest.raises(DraftUnavailable, match="Cyrillic"):
        CaseDrafter(store, service.cfg, client).draft(service._case(URL))
    assert store.total_spend(P) > 0
