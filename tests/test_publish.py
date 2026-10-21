import hashlib
import http.client
import json
import logging
import multiprocessing
import os
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from rg2watch.case_handoff import CaseError, CaseHandoff, manual_template
from rg2watch.case_locks import CaseLocks
from rg2watch.case_draft import CaseDrafter, DraftUnavailable
from rg2watch.config import Config
from rg2watch.store import Store
from rg2watch.server import start_server
from rg2watch.publication_service import PublicationJobs
from rg2watch import cli
from test_browse import BASE, P, add, doc


URL = BASE + "/extfile/publish.pdf"
VIBER_REASON = ("Потребна је провера броја гласача ван бирачког места (18 од 58) "
                "и броја гласова за једну листу (53 од 58).")


def _wait_job_result(jobs, job_id):
    for _ in range(200):
        result = jobs.job(job_id)
        if result["state"] != "running":
            return result
        time.sleep(.01)
    raise AssertionError("job did not finish")


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


def test_existing_publications_migrate_with_nullable_viber_reason(tmp_path):
    store, service, _ = handoff(tmp_path)
    publication = service.claim(URL, "Замрзнути опис", VIBER_REASON)
    store.finalize_publication(URL, "https://drive.google.com/drive/folders/folder", "Стара порука")
    store.close()
    import sqlite3
    con = sqlite3.connect(tmp_path / "watch.sqlite")
    con.execute("ALTER TABLE publications RENAME TO publications_new_schema")
    con.execute("CREATE TABLE publications AS SELECT id,url,document_id,parent,summary,summary_sha256,"
                "staged_pdf_path,pdf_sha256,case_json,folder_id,pdf_file_id,text_file_id,"
                "drive_account_id,legacy_parent_recovery,final_url,viber_text,phase,created_at,"
                "updated_at,published_at FROM publications_new_schema")
    legacy_pending_url = URL + "?legacy-pending"
    con.execute("INSERT INTO documents(parent,municipality,station_no,station_name,station_key,"
                "doc_type,label,url,active) SELECT parent,municipality,station_no,station_name,"
                "station_key,doc_type,label,?,active FROM documents WHERE url=?",
                (legacy_pending_url, URL))
    pending_case = json.loads(publication["case_json"])
    pending_case["url"] = legacy_pending_url
    con.execute("INSERT INTO publications(url,document_id,parent,summary,summary_sha256,"
                "staged_pdf_path,pdf_sha256,case_json,folder_id,pdf_file_id,text_file_id,"
                "drive_account_id,legacy_parent_recovery,final_url,viber_text,phase,created_at,"
                "updated_at,published_at) SELECT ?, (SELECT id FROM documents WHERE url=?),parent,"
                "summary,summary_sha256,staged_pdf_path,pdf_sha256,?,NULL,NULL,NULL,NULL,0,NULL,NULL,"
                "'allocated',created_at,updated_at,NULL FROM publications WHERE url=?",
                (legacy_pending_url, legacy_pending_url, json.dumps(pending_case), URL))
    con.execute("DROP TABLE publications_new_schema")
    con.commit(); con.close()

    migrated = Store(tmp_path / "watch.sqlite")
    row = migrated.get_publication(URL)
    assert "viber_reason" in row.keys()
    assert row["viber_reason"] is None
    assert row["phase"] == "published" and row["viber_text"] == "Стара порука"
    pending = migrated.get_publication(legacy_pending_url)
    assert pending["viber_reason"] is None
    assert pending["phase"] == "allocated" and pending["final_url"] is None


def test_short_reason_is_normalized_frozen_and_validated_before_staging(tmp_path):
    store, service, _ = handoff(tmp_path)
    first = service.claim(URL, "Опрезан јавни опис.", "  Кратак\n  разлог.  ")
    assert first["viber_reason"] == "Кратак разлог."
    assert service.claim(URL, "Опрезан јавни опис.", "Кратак разлог.")["id"] == first["id"]
    with pytest.raises(CaseError, match="frozen"):
        service.claim(URL, "Опрезан јавни опис.", "Други разлог.")

    other = tmp_path / "other"
    other.mkdir()
    _, fresh, _ = handoff(other)
    for reason in ("", "x" * 501, "линк https://example.test", "ftp://example.test/file",
                   "пишите mailto:test@example.test", "посетите example.rs/path",
                   "адреса 192.0.2.1/path", "адреса 2001:db8::1",
                   "посетите пример.срб/страна", "контрола\x00"):
        with pytest.raises(CaseError):
            fresh.claim(URL, "Опис", reason)
        assert fresh.store.get_publication(URL) is None
        assert not (other / ".rg2watch-publications").exists()

    accepted = fresh.claim(URL, "Опис", "Однос 18.58 захтева проверу.")
    assert accepted["viber_reason"] == "Однос 18.58 захтева проверу."


def test_concise_and_legacy_viber_layouts_are_distinct(tmp_path):
    _, service, _ = handoff(tmp_path)
    case = service._case(URL)
    case["municipality"], case["station"] = "Књажевац", "61"
    folder = "https://drive.google.com/drive/folders/existing"
    concise = service.viber_text(case, VIBER_REASON, folder)
    assert concise == ("Могућа неправилност за проверу.\n"
                       f"Књажевац, БМ 61: {VIBER_REASON}\n"
                       f"Записник и опис: {folder}\n"
                       f"РИК: {case['rik_url']}")
    assert concise.count("61") == 1
    assert all(text not in concise for text in
               ("људску", "није утврђена чињеница", "Разлог: Избори:"))
    legacy = service.legacy_viber_text(case, "Избори: Замрзнути опис", folder)
    assert legacy == ("Могућа неправилност за људску проверу — није утврђена чињеница.\n"
                      "Бирачко место: Књажевац, 61\n"
                      "Разлог: Избори: Замрзнути опис\n"
                      f"РИК: {case['rik_url']}\n"
                      f"Јавни предмет (свако са везом може да отвори): {folder}")


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
    first = service.claim(URL, "Опрезан јавни опис.", VIBER_REASON)
    assert Path(first["staged_pdf_path"]).read_bytes() == cache.read_bytes()
    assert Path(first["staged_pdf_path"]).stat().st_mode & 0o777 == 0o600
    cache.write_bytes(b"%PDF-1.4\nchanged")
    # Recovery uses the frozen staging copy, never a newer cache file.
    assert service.claim(URL, "Опрезан јавни опис.", VIBER_REASON)["pdf_sha256"] == first["pdf_sha256"]
    with pytest.raises(CaseError, match="frozen"):
        service.claim(URL, "Други опис", VIBER_REASON)
    store.close()


def test_publication_account_binding_is_nullable_and_immutable(tmp_path):
    store, service, _ = handoff(tmp_path)
    service.claim(URL, "Опрезан јавни опис.", VIBER_REASON)
    assert store.get_publication(URL)["drive_account_id"] is None
    store.bind_publication_account(URL, "account-1")
    assert store.get_publication(URL)["drive_account_id"] == "account-1"
    store.bind_publication_account(URL, "account-1")
    with pytest.raises(ValueError, match="different Google Drive account"):
        store.bind_publication_account(URL, "account-2")


def test_restart_after_preclaim_crash_discards_unclaimed_orphan_and_accepts_new_summary(
        tmp_path, monkeypatch):
    from test_drive_publish import FakeDrive, make_publisher

    store, service, _ = handoff(tmp_path)

    def crash_before_claim(*args, **kwargs):
        raise RuntimeError("simulated claim crash")

    monkeypatch.setattr(store, "claim_publication", crash_before_claim)
    with pytest.raises(RuntimeError, match="claim crash"):
        service.claim(URL, "Замрзнут опис", VIBER_REASON)
    assert store.get_publication(URL) is None
    staged = list((tmp_path / ".rg2watch-publications").glob("*.pdf"))
    assert len(staged) == 1 and staged[0].read_bytes().startswith(b"%PDF")

    # Restart loses the caller's approved summary. With no committed claim and no remote side
    # effects, the orphan must not become an undiscoverable authoritative frozen package.
    store.close()
    restarted_store = Store(tmp_path / "watch.sqlite")
    restarted = CaseHandoff(restarted_store, Config(db_path=tmp_path / "watch.sqlite",
                                                    max_pdf_bytes=1000))
    jobs = PublicationJobs(restarted, object(), workers=1)
    try:
        assert jobs.publication(URL) is None
    finally:
        jobs.close()

    result = make_publisher(restarted, FakeDrive(), tmp_path).publish(URL, "Нови одобрени опис", VIBER_REASON)
    assert result["status"] == "published"
    frozen = restarted_store.get_publication(URL)
    assert frozen["summary"] == "Нови одобрени опис"
    assert Path(frozen["staged_pdf_path"]) != staged[0]
    assert not staged[0].exists()


def test_store_will_not_commit_publication_referencing_missing_staging(tmp_path):
    store, service, _ = handoff(tmp_path)
    case = service._case(URL)
    with pytest.raises(ValueError, match="durable staged PDF"):
        store.claim_publication(URL, "Опис", hashlib.sha256("Опис".encode()).hexdigest(),
                                str(tmp_path / "missing.pdf"), "0" * 64, case, VIBER_REASON)
    assert store.get_publication(URL) is None


def test_completed_publication_is_historical_after_ok(tmp_path):
    store, service, _ = handoff(tmp_path)
    p = service.claim(URL, "Јавни опис", VIBER_REASON)
    store.save_drive_ids(URL, "folder", "pdf", "text")
    store.finalize_publication(URL, "https://drive.google.com/drive/folders/folder",
                               "Вибер порука")
    store.set_review(URL, "ok", "исправљено")
    got = service.claim(URL, "a different ignored summary", VIBER_REASON)
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


def test_sdk_compatible_draft_request(tmp_path):
    store, service, _ = handoff(tmp_path)
    captured = {}

    class StrictMessages:
        def create(self, *, model, max_tokens, messages, timeout):
            captured.update(model=model, max_tokens=max_tokens,
                            messages=messages, timeout=timeout)
            usage = type("Usage", (), {"input_tokens": 100, "output_tokens": 20,
                                        "cache_read_input_tokens": 0,
                                        "cache_creation_input_tokens": 0})()
            block = type("Block", (), {
                "type": "text", "text": "Опрезно треба проверити неслагање."})()
            return type("Response", (), {"usage": usage, "model": model,
                                         "content": [block]})()

    client = type("Client", (), {"messages": StrictMessages()})()
    service.drafter = CaseDrafter(store, service.cfg, client)

    out = service.prepare(URL, draft=True)

    assert out["summary"] == "Опрезно треба проверити неслагање."
    assert out["summary"] != manual_template(out["case"])
    assert out["warning"] is None
    assert captured["model"] == service.cfg.model
    assert captured["max_tokens"] == 600
    assert captured["timeout"] == 30.0
    assert len(captured["messages"]) == 1
    assert captured["messages"][0]["role"] == "user"
    assert "Интерна напомена оператера: проверити записник" in captured["messages"][0]["content"]
    assert store.total_spend(P) > 0


class SensitiveRequestError(Exception):
    status_code = 404


class InvalidStatusRequestError(Exception):
    status_code = "secret-status-object"


class RaisingStatusRequestError(Exception):
    @property
    def status_code(self):
        raise RuntimeError("status-access-secret")


class FailingMessages:
    def __init__(self, error):
        self.error = error

    def create(self, **kw):
        raise self.error


class ChainedFailingMessages:
    def create(self, **kw):
        try:
            raise RuntimeError("underlying client detail")
        except RuntimeError as exc:
            raise TypeError("draft request type mismatch") from exc


def _assert_draft_failure(caplog, out, expected_exception, expected_status,
                          expected_error):
    records = [record for record in caplog.records
               if record.name == "rg2watch.case_draft"
               and record.levelno == logging.WARNING]
    assert len(records) == 1
    record = records[0]
    rendered = record.getMessage()
    assert rendered == ("AI draft failure stage=anthropic-request "
                        f"exception={expected_exception} status={expected_status} "
                        f"error={expected_error}")
    assert record.exc_info is not None
    assert expected_error in caplog.text
    assert "Traceback (most recent call last):" in caplog.text
    assert out["warning"] == "AI draft failed; use the manual template"
    assert out["summary"] == manual_template(out["case"])
    response = json.dumps(out, ensure_ascii=False)
    assert expected_error not in response
    assert "Traceback" not in response


@pytest.mark.parametrize(("error", "expected_exception", "expected_status"), [
    (SensitiveRequestError("secret-token internal-note prompt-body"),
     "SensitiveRequestError", "404"),
    (InvalidStatusRequestError("secret-token internal-note prompt-body"),
     "InvalidStatusRequestError", "unknown"),
    (RaisingStatusRequestError("secret-token internal-note prompt-body"),
     "RaisingStatusRequestError", "unknown"),
])
def test_ai_draft_request_failure_logs_raw_error_and_traceback(
        tmp_path, caplog, error, expected_exception, expected_status):
    store, service, _ = handoff(tmp_path)
    client = type("Client", (), {"messages": FailingMessages(error)})()
    service.drafter = CaseDrafter(store, service.cfg, client)

    with caplog.at_level(logging.WARNING, logger="rg2watch.case_draft"):
        out = service.prepare(URL, draft=True)

    _assert_draft_failure(caplog, out, expected_exception, expected_status, str(error))


def test_ai_draft_request_failure_without_status_logs_chained_traceback(tmp_path, caplog):
    store, service, _ = handoff(tmp_path)
    client = type("Client", (), {"messages": ChainedFailingMessages()})()
    service.drafter = CaseDrafter(store, service.cfg, client)

    with caplog.at_level(logging.WARNING, logger="rg2watch.case_draft"):
        out = service.prepare(URL, draft=True)

    _assert_draft_failure(caplog, out, "TypeError", "unknown", "draft request type mismatch")
    assert "underlying client detail" in caplog.text
    assert "The above exception was the direct cause of the following exception:" in caplog.text
    assert "underlying client detail" not in json.dumps(out, ensure_ascii=False)


def test_ai_draft_is_explicit_bounded_cyrillic_billed_and_has_no_failure_log(tmp_path, caplog):
    store, service, _ = handoff(tmp_path)
    messages = FakeMessages()
    client = type("Client", (), {"messages": messages})()
    service.drafter = CaseDrafter(store, service.cfg, client)
    with caplog.at_level(logging.WARNING, logger="rg2watch.case_draft"):
        out = service.prepare(URL, draft=True)
    assert out["summary"].startswith("Опрезно")
    call = messages.calls[0]
    assert call["max_tokens"] <= 700 and len(call["messages"][0]["content"]) < 7000
    assert "проверити записник" in call["messages"][0]["content"]
    assert store.total_spend(P) > 0
    assert not [record for record in caplog.records
                if record.name == "rg2watch.case_draft"
                and "stage=anthropic-request" in record.getMessage()]


def test_ai_draft_is_bounded_by_publish_utf8_byte_limit(tmp_path):
    store, service, _ = handoff(tmp_path)
    client = type("Client", (), {"messages": FakeMessages("Ж" * 4096)})()
    draft = CaseDrafter(store, service.cfg, client).draft(service._case(URL))
    assert len(draft.encode("utf-8")) <= 4096
    service.claim(URL, draft, VIBER_REASON)


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


class FakeCaseAPI:
    def submit_prepare(self, url):
        return "prepare-job"
    def submit_publish(self, url, summary, viber_reason, confirm):
        assert confirm is True
        assert viber_reason == VIBER_REASON
        return "publish-job"
    def job(self, job_id):
        return {"id": job_id, "state": "done", "result": {"summary": "Ћирилица"}}
    def publication(self, url):
        return {"status": "uploaded", "summary": "Замрзнути опис",
                "viber_reason": VIBER_REASON, "retryable": True}


def case_request(server, method, path, body=None, headers=None):
    host = f"127.0.0.1:{server.server_port}"
    h = {"Host": host, "Origin": "http://" + host, "Sec-Fetch-Site": "same-origin",
         "X-RG2W": "1"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", server.server_port)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse(); raw = r.read()
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        payload = raw.decode()
    out = r.status, payload
    c.close()
    return out


def test_case_http_routes_are_explicit_strict_and_same_origin(tmp_path):
    (tmp_path / "index.html").write_text("ok")
    srv = start_server(tmp_path, tmp_path, 0, case_api=FakeCaseAPI())
    try:
        assert case_request(srv, "POST", "/case/prepare", {"url": URL}) == (202, {"job_id": "prepare-job"})
        assert case_request(srv, "POST", "/case/publish",
                            {"url": URL, "summary": "Јавно", "viber_reason": VIBER_REASON,
                             "confirm": True}) == (
                                202, {"job_id": "publish-job"})
        assert case_request(srv, "GET", "/case/jobs/prepare-job")[0] == 200
        assert case_request(srv, "GET", "/case/publication?url=" + URL) == (
            200, {"status": "uploaded", "summary": "Замрзнути опис",
                  "viber_reason": VIBER_REASON, "retryable": True})
        assert case_request(srv, "POST", "/case/prepare", {"url": URL},
                            {"Origin": "http://evil.example"})[0] == 403
        assert case_request(srv, "GET", "/case/jobs/prepare-job",
                            headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert case_request(srv, "POST", "/case/publish", {"url": URL, "summary": "x",
                                                             "confirm": False})[0] == 400
        assert case_request(srv, "GET", "/case/prepare")[0] == 404
    finally:
        srv.shutdown(); srv.server_close()


def test_case_http_transports_legacy_and_new_frozen_pending_retries(tmp_path):
    class PendingAPI:
        def __init__(self):
            self.calls = []
        def publication(self, url):
            out = {"status": "uploaded", "summary": "Замрзнути опис", "retryable": True}
            if url.endswith("new"):
                out["viber_reason"] = VIBER_REASON
            return out
        def submit_publish(self, url, summary, viber_reason, confirm):
            self.calls.append((url, summary, viber_reason, confirm))
            return "retry-job"

    api = PendingAPI()
    (tmp_path / "index.html").write_text("ok")
    srv = start_server(tmp_path, tmp_path, 0, case_api=api)
    try:
        legacy = URL + "/legacy"
        new = URL + "/new"
        assert case_request(srv, "POST", "/case/publish",
                            {"url": legacy, "summary": "Замрзнути опис", "confirm": True}) == (
                                202, {"job_id": "retry-job"})
        assert case_request(srv, "POST", "/case/publish",
                            {"url": new, "summary": "Замрзнути опис",
                             "viber_reason": VIBER_REASON, "confirm": True}) == (
                                202, {"job_id": "retry-job"})
        assert api.calls == [(legacy, "Замрзнути опис", None, True),
                             (new, "Замрзнути опис", VIBER_REASON, True)]
    finally:
        srv.shutdown(); srv.server_close()


@pytest.mark.parametrize("method,path,body,headers,status", [
    ("POST", "/case/prepare", {"url": URL, "extra": 1}, None, 400),
    ("POST", "/case/prepare", {"url": 7}, None, 400),
    ("POST", "/case/publish", {"url": URL, "summary": 7, "confirm": True}, None, 400),
    ("POST", "/case/publish", {"url": URL, "summary": "x", "viber_reason": "",
                                  "confirm": True}, None, 400),
    ("POST", "/case/publish", {"url": URL, "summary": "x", "confirm": True, "x": 1}, None, 400),
    ("POST", "/case/prepare", {"url": URL}, {"Content-Type": "application/json; charset=utf-8"}, 400),
    ("POST", "/case/prepare", {"url": URL}, {"X-RG2W": ""}, 403),
])
def test_case_http_rejects_invalid_fields_types_and_headers(
        tmp_path, method, path, body, headers, status):
    (tmp_path / "index.html").write_text("ok")
    srv = start_server(tmp_path, tmp_path, 0, case_api=FakeCaseAPI())
    try:
        assert case_request(srv, method, path, body, headers)[0] == status
    finally:
        srv.shutdown(); srv.server_close()


def test_server_without_case_configuration_refuses_case_routes(tmp_path):
    (tmp_path / "index.html").write_text("ok")
    srv = start_server(tmp_path, tmp_path, 0)
    try:
        assert case_request(srv, "POST", "/case/prepare", {"url": URL})[0] == 403
        assert case_request(srv, "GET", "/case/jobs/x")[0] == 404
    finally:
        srv.shutdown(); srv.server_close()


def test_page_has_explicit_safe_case_controls():
    html = (Path("src/rg2watch/static/index.html")).read_text(encoding="utf-8")
    assert '"case/prepare"' in html and '"case/publish"' in html
    assert 'document.createElement("textarea")' in html
    assert ".textContent" in html and "innerHTML = draft" not in html
    for text in ("Prepare draft", "Publish public case", "Anyone with the link can open",
                  "Public case summary", "Short Viber reason", "Copy", "Copied",
                 "Retry frozen publication", "Publication error:"):
        assert text in html
    for old in ("Припреми нацрт", "Објави јавни предмет", "Понови објаву замрзнутог предмета",
                "Грешка:", "Копирано"):
        assert old not in html
    # Prepared/public content is still Serbian Cyrillic and is never translated here.
    assert "area.value = result.summary" in html
    assert "area.value = pending.summary" in html
    assert "reason.value = pending.viber_reason" in html


def test_case_ui_reenables_prepare_and_publish_after_async_job_errors():
    html = Path("src/rg2watch/static/index.html").read_text(encoding="utf-8")
    source = html[html.index("function copyButton"):html.index("function rowOf")]
    script = r'''
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.hidden = false; this.disabled = false;
    this.textContent = ""; this.value = ""; this.checked = false; this.className = ""; }
  append(...xs) { this.children.push(...xs); }
  prepend(...xs) { this.children.unshift(...xs); }
  replaceChildren(...xs) { this.children = xs; }
  setAttribute() {}
  querySelector(sel) { if (sel === ".rvmsg") return find(this, x => x.className === "rvmsg"); }
}
function find(root, pred) { if (root instanceof El && pred(root)) return root;
  for (const child of root.children || []) { const got = child instanceof El && find(child, pred); if (got) return got; }
  return null; }
function findText(root, text) { return find(root, x => x.textContent === text); }
const document = {createElement: tag => new El(tag)};
const navigator = {clipboard: {writeText: () => Promise.resolve()}};
const HTTP = true;
function mk(tag, text, cls) { const e = new El(tag); if (text != null) e.textContent = text; e.className = cls || ""; return e; }
function link(text, href) { return mk("a", text); }
function rvOf() { return {v: "escalate"}; }
let jobs = {};
function caseFetch(path) {
  if (path.startsWith("case/publication")) return Promise.resolve({status: "none"});
  if (path === "case/prepare") return Promise.resolve({job_id: "prepare"});
  if (path === "case/publish") return Promise.resolve({job_id: "publish"});
  return Promise.resolve(jobs[path.split("/").pop()]);
}
''' + source + r'''
async function tick() { await new Promise(resolve => setImmediate(resolve)); }
(async () => {
  jobs.prepare = {state: "error", error: "prepare boom"};
  let box = caseBox({u: "https://example.test/a.pdf", pdf: "hash"}); await tick();
  let prep = findText(box, "Prepare draft"); prep.onclick(); await tick(); await tick();
  if (prep.disabled || !box.querySelector(".rvmsg").textContent.includes("prepare boom")) process.exit(2);

  jobs.prepare = {state: "done", result: {summary: "Опис"}};
  jobs.publish = {state: "error", error: "publish boom"};
  box = caseBox({u: "https://example.test/a.pdf", pdf: "hash"}); await tick();
  prep = findText(box, "Prepare draft"); prep.onclick(); await tick(); await tick();
  const publish = findText(box, "Publish public case");
  const areas = []; (function collect(x) { if (x instanceof El && x.tag === "textarea") areas.push(x); for (const c of x.children || []) if (c instanceof El) collect(c); })(box);
  areas[1].value = "Кратак разлог";
  const confirm = find(box, x => x.tag === "input"); confirm.checked = true;
  publish.onclick(); await tick(); await tick();
  if (publish.disabled || !box.querySelector(".rvmsg").textContent.includes("publish boom")) process.exit(3);
})().catch(e => { console.error(e); process.exit(4); });
'''
    subprocess.run(["node", "-e", script], check=True)


def test_case_ui_executes_all_operator_states_and_preserves_serbian_content():
    html = Path("src/rg2watch/static/index.html").read_text(encoding="utf-8")
    source = html[html.index("function copyButton"):html.index("function rowOf")]
    script = r'''
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.hidden = false; this.disabled = false;
    this.textContent = ""; this.value = ""; this.checked = false; this.className = "";
    this.attrs = {}; this.readOnly = false; }
  append(...xs) { this.children.push(...xs); }
  prepend(...xs) { this.children.unshift(...xs); }
  replaceChildren(...xs) { this.children = xs; }
  setAttribute(k, v) { this.attrs[k] = v; }
  querySelector(sel) { if (sel === ".rvmsg") return find(this, x => x.className === "rvmsg"); }
}
function find(root, pred) { if (root instanceof El && pred(root)) return root;
  for (const child of root.children || []) { const got = child instanceof El && find(child, pred); if (got) return got; }
  return null; }
function findText(root, text) { return find(root, x => x.textContent === text); }
function allText(root) { if (!(root instanceof El)) return String(root || "");
  return root.textContent + root.children.map(allText).join(""); }
function check(ok, message) { if (!ok) throw new Error(message); }
const document = {createElement: tag => new El(tag)};
const clipboard = [];
const navigator = {clipboard: {writeText: text => { clipboard.push(text); return Promise.resolve(); }}};
let HTTP = true;
function mk(tag, text, cls) { const e = new El(tag); if (text != null) e.textContent = text; e.className = cls || ""; return e; }
function link(text, href) { const e = mk("a", text); e.href = href; return e; }
function rvOf(v) { return v.rv || {v: "escalate"}; }
const publications = new Map(), jobs = new Map(), publishRequests = [];
let prepareId = "prepare", publishId = "publish";
function caseFetch(path, options) {
  if (path.startsWith("case/publication")) {
    const url = decodeURIComponent(path.split("url=")[1]);
    return Promise.resolve(publications.get(url) || {status: "none"});
  }
  if (path === "case/prepare") return Promise.resolve({job_id: prepareId});
  if (path === "case/publish") {
    publishRequests.push(JSON.parse(options.body));
    return Promise.resolve({job_id: publishId});
  }
  const id = path.split("/").pop(), values = jobs.get(id), value = Array.isArray(values) ? values.shift() : values;
  return Promise.resolve(value);
}
function setTimeout(fn) { fn(); }
''' + source + r'''
async function flush() { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); }
(async () => {
  HTTP = false;
  let box = caseBox({u: "offline", pdf: "hash"});
  check(allText(box).includes("only through the local rg2watch server"), "local-server guidance");
  HTTP = true;

  publications.set("missing", {status: "none"});
  box = caseBox({u: "missing", pdf: ""}); await flush();
  check(allText(box).includes("No locally cached PDF is available"), "missing-PDF guidance");

  publications.set("ai", {status: "none"});
  jobs.set("prepare-ai", {state: "done", result: {summary: "Српски јавни опис", warning: true}});
  prepareId = "prepare-ai";
  box = caseBox({u: "ai", pdf: "hash"}); await flush();
  findText(box, "Prepare draft").onclick(); await flush();
  check(allText(box).includes("AI draft failed; use the manual template."), "AI fallback");
  const aiArea = find(box, x => x.tag === "textarea");
  check(aiArea.value === "Српски јавни опис", "Serbian prepared summary unchanged");
  check(aiArea.attrs["aria-label"] === "public case summary in Serbian Cyrillic", "textarea label");

  publications.set("running", {status: "none"});
  jobs.set("prepare-running", [{state: "running"},
    {state: "done", result: {summary: "Текст после чекања"}}]);
  prepareId = "prepare-running";
  box = caseBox({u: "running", pdf: "hash"}); await flush();
  const runningPrep = findText(box, "Prepare draft"); runningPrep.onclick();
  check(runningPrep.disabled && allText(box).includes("Preparing..."), "running job status");
  await flush();
  check(!runningPrep.disabled && find(box, x => x.tag === "textarea").value === "Текст после чекања",
    "running job completion");

  publications.set("pending", {status: "uploaded", summary: "Замрзнути опис",
    viber_reason: "Замрзнути кратак разлог", retryable: true});
  box = caseBox({u: "pending", pdf: "hash"}); await flush();
  const pendingArea = find(box, x => x.tag === "textarea");
  check(pendingArea.value === "Замрзнути опис" && pendingArea.readOnly, "frozen summary unchanged");
  const pendingReason = find(box, x => x.tag === "textarea" && x !== pendingArea);
  check(pendingReason.value === "Замрзнути кратак разлог" && pendingReason.readOnly,
    "frozen reason unchanged");
  check(findText(box, "Retry frozen publication") && allText(box).includes("frozen publication is pending"),
    "pending retry UI");
  jobs.set("retry-new", {state: "done", result: {status: "published",
    url: "https://drive.test/new-pending", viber_text: "Нова замрзнута порука"}});
  publishId = "retry-new";
  find(box, x => x.tag === "input").checked = true;
  findText(box, "Retry frozen publication").onclick(); await flush();
  check(publishRequests.at(-1).summary === "Замрзнути опис" &&
    publishRequests.at(-1).viber_reason === "Замрзнути кратак разлог", "new retry sends frozen inputs");

  publications.set("legacy-pending", {status: "uploaded", summary: "Стари замрзнути опис",
    retryable: true});
  jobs.set("retry-legacy", {state: "done", result: {status: "published",
    url: "https://drive.test/legacy", viber_text: "Стара замрзнута порука"}});
  publishId = "retry-legacy";
  box = caseBox({u: "legacy-pending", pdf: "hash"}); await flush();
  check(!find(box, x => x.tag === "textarea" && x !== find(box, y => y.tag === "textarea") && !x.hidden),
    "legacy retry has no visible reason");
  find(box, x => x.tag === "input").checked = true;
  findText(box, "Retry frozen publication").onclick(); await flush();
  check(publishRequests.at(-1).summary === "Стари замрзнути опис" &&
    !("viber_reason" in publishRequests.at(-1)), "legacy retry omits reason");

  publications.set("published", {status: "published", url: "https://drive.test/case",
    viber_text: "Могућа неправилност - Вибер порука"});
  box = caseBox({u: "published", pdf: "hash"}); await flush();
  check(allText(box).includes("Case published"), "published success");
  check(allText(box).includes("Могућа неправилност - Вибер порука"), "Serbian Viber unchanged");
  const copy = findText(box, "Copy"); copy.onclick(); await flush();
  check(copy.textContent === "Copied" && clipboard[0] === "https://drive.test/case", "copy feedback");

  publications.set("success", {status: "none"});
  jobs.set("prepare-success", {state: "done", result: {summary: "Одобрени опис"}});
  jobs.set("publish-success", {state: "done", result: {status: "published",
    url: "https://drive.test/new", viber_text: "Вибер успех"}});
  prepareId = "prepare-success"; publishId = "publish-success";
  box = caseBox({u: "success", pdf: "hash"}); await flush();
  findText(box, "Prepare draft").onclick(); await flush();
  const successAreas = []; (function collect(x) { if (x instanceof El && x.tag === "textarea") successAreas.push(x); for (const c of x.children || []) if (c instanceof El) collect(c); })(box);
  successAreas[1].value = "Одобрени кратак разлог";
  const confirm = find(box, x => x.tag === "input");
  const publish = findText(box, "Publish public case"); confirm.checked = true; publish.onclick(); await flush();
  check(allText(box).includes("Case published") && allText(box).includes("Вибер успех"), "publish success");

  publications.set("errors", {status: "none"});
  jobs.set("prepare-error", {state: "error", error: "preparation unavailable"});
  prepareId = "prepare-error";
  box = caseBox({u: "errors", pdf: "hash"}); await flush();
  const prep = findText(box, "Prepare draft"); prep.onclick(); await flush();
  check(!prep.disabled && allText(box).includes("Preparation error: preparation unavailable"),
    "prepare error and re-enable");

  jobs.set("prepare-for-publish-error", {state: "done", result: {summary: "Опис"}});
  jobs.set("publish-error", {state: "error",
    error: "Drive file ID preallocation did not complete; retry safely"});
  prepareId = "prepare-for-publish-error"; publishId = "publish-error";
  box = caseBox({u: "errors", pdf: "hash"}); await flush();
  findText(box, "Prepare draft").onclick(); await flush();
  const errorAreas = []; (function collect(x) { if (x instanceof El && x.tag === "textarea") errorAreas.push(x); for (const c of x.children || []) if (c instanceof El) collect(c); })(box);
  errorAreas[1].value = "Кратак разлог";
  const failedPublish = findText(box, "Publish public case");
  find(box, x => x.tag === "input").checked = true; failedPublish.onclick(); await flush();
  check(!failedPublish.disabled && allText(box).includes(
    "Publication error: Drive file ID preallocation did not complete; retry safely"),
    "publish error and re-enable");
  check(!allText(box).includes("secret-token-value") && !allText(box).includes("provider debug"),
    "raw provider details absent from UI");
})().catch(e => { console.error(e); process.exit(2); });
'''
    subprocess.run(["node", "-e", script], check=True)


def test_bounded_job_service_returns_manual_preparation_and_persisted_state(tmp_path):
    _, cases, _ = handoff(tmp_path)
    jobs = PublicationJobs(cases, None, workers=1)
    try:
        job_id = jobs.submit_prepare(URL)
        for _ in range(100):
            result = jobs.job(job_id)
            if result["state"] != "running":
                break
            time.sleep(.01)
        assert result["state"] == "done" and result["result"]["summary"].startswith("Избори:")
        assert "note" not in result["result"]["case"]
        with pytest.raises(ValueError, match="not configured"):
            jobs.submit_publish(URL, "Јавни опис", VIBER_REASON, True)
        assert jobs.publication(URL) is None
    finally:
        jobs.close()


def test_publication_jobs_retry_legacy_null_and_new_frozen_reason(tmp_path):
    store, cases, _ = handoff(tmp_path)
    cases.claim(URL, "Замрзнути опис", VIBER_REASON)

    class Publisher:
        def __init__(self):
            self.calls = []
        def publish(self, url, summary, reason):
            self.calls.append((url, summary, reason))
            return {"status": "published"}

    publisher = Publisher()
    jobs = PublicationJobs(cases, publisher, workers=1)
    try:
        new_job = jobs.submit_publish(URL, "Замрзнути опис", VIBER_REASON, True)
        assert _wait_job_result(jobs, new_job)["state"] == "done"
        with pytest.raises(ValueError, match="frozen"):
            jobs.submit_publish(URL, "Замрзнути опис", "Промењени разлог", True)
        assert publisher.calls == [(URL, "Замрзнути опис", VIBER_REASON)]

        store._write("UPDATE publications SET viber_reason=NULL WHERE url=?", (URL,))
        legacy_job = jobs.submit_publish(URL, "Замрзнути опис", None, True)
        assert _wait_job_result(jobs, legacy_job)["state"] == "done"
        with pytest.raises(ValueError, match="legacy"):
            jobs.submit_publish(URL, "Замрзнути опис", VIBER_REASON, True)
        assert publisher.calls[-1] == (URL, "Замрзнути опис", None)
    finally:
        jobs.close()


def test_publication_job_exposes_only_sanitized_generate_ids_error(tmp_path):
    from rg2watch.drive_publish import DriveError, GoogleDriveAdapter

    class FailingRequest:
        def execute(self):
            raise RuntimeError("provider debug: bearer secret-token-value")

    class FilesService:
        def files(self):
            return self

        def generateIds(self, **kwargs):
            return FailingRequest()

    class Publisher:
        def publish(self, url, summary, viber_reason):
            GoogleDriveAdapter(FilesService()).generate_ids(3)

    _, cases, _ = handoff(tmp_path)
    jobs = PublicationJobs(cases, Publisher(), workers=1)
    try:
        job_id = jobs.submit_publish(URL, "Јавни опис", VIBER_REASON, True)
        for _ in range(100):
            result = jobs.job(job_id)
            if result["state"] != "running":
                break
            time.sleep(.01)
        assert result == {"id": job_id, "state": "error", "kind": "publication",
                          "error": "Drive file ID preallocation did not complete; retry safely"}
        assert "provider debug" not in json.dumps(result)
        assert "secret-token-value" not in json.dumps(result)
    finally:
        jobs.close()


def test_publication_job_contexts_are_isolated_from_each_other_and_drafts(tmp_path, caplog):
    from rg2watch.drive_publish import _publication_diagnostic_id

    _, cases, _ = handoff(tmp_path)
    barrier = threading.Barrier(2)
    observed = {}

    class Publisher:
        def publish(self, url, summary, viber_reason):
            if "retry-secret" not in url:
                barrier.wait(timeout=5)
            observed[url] = _publication_diagnostic_id.get()
            return {"status": "published"}

    jobs = PublicationJobs(cases, Publisher(), workers=2)
    try:
        with caplog.at_level(logging.INFO):
            first = jobs.submit_publish("https://fixture.invalid/first-secret", "first", VIBER_REASON, True)
            second = jobs.submit_publish("https://fixture.invalid/second-secret", "second", VIBER_REASON, True)
            first_result = _wait_job_result(jobs, first)
            second_result = _wait_job_result(jobs, second)
            draft_context = []
            draft = jobs._submit("preparation", "https://fixture.invalid/draft-secret",
                                 lambda: draft_context.append(
                                     _publication_diagnostic_id.get()) or {"summary": "draft"})
            draft_result = _wait_job_result(jobs, draft)
            retry = jobs.submit_publish("https://fixture.invalid/retry-secret", "retry", VIBER_REASON, True)
            retry_result = _wait_job_result(jobs, retry)
    finally:
        jobs.close()

    assert first_result["state"] == second_result["state"] == "done"
    assert draft_result["state"] == retry_result["state"] == "done"
    assert observed == {
        "https://fixture.invalid/first-secret": first,
        "https://fixture.invalid/second-secret": second,
        "https://fixture.invalid/retry-secret": retry,
    }
    assert len({first, second, retry}) == 3
    assert draft_context == [None]
    assert _publication_diagnostic_id.get() is None
    for job_id in (first, second, retry):
        assert f"publication start diagnostic_id={job_id}" in caplog.text
        assert f"publication done diagnostic_id={job_id}" in caplog.text
    assert f"diagnostic_id={draft}" not in caplog.text
    assert "fixture.invalid" not in caplog.text


def test_pending_publication_status_exposes_frozen_summary_for_explicit_retry(tmp_path):
    store, cases, cache = handoff(tmp_path)
    pending = cases.claim(URL, "Замрзнути јавни опис", VIBER_REASON)
    store.save_drive_ids(URL, "folder", "pdf", "text")
    cache.unlink()
    store.set_review(URL, "ok")
    jobs = PublicationJobs(cases, object(), workers=1)
    try:
        state = jobs.publication(URL)
        assert state == {
            "status": pending["phase"],
            "summary": "Замрзнути јавни опис",
            "viber_reason": VIBER_REASON,
            "retryable": True,
        }
    finally:
        jobs.close()


def published_knjazevac_case(tmp_path):
    store, cases, _ = handoff(tmp_path)
    store._write("UPDATE documents SET municipality='Књажевац',station_no='61' WHERE url=?",
                 (URL,))
    row = cases.claim(URL, "Дужи замрзнути јавни опис", VIBER_REASON)
    store.save_drive_ids(URL, "existing-folder", "existing-pdf", "existing-text")
    case = json.loads(row["case_json"])
    final = "https://drive.google.com/drive/folders/existing-folder"
    store.finalize_publication(URL, final, cases.legacy_viber_text(case, row["summary"], final))
    store.close()
    return tmp_path / "watch.sqlite"


def test_revise_viber_cli_previews_read_only_then_updates_only_saved_copy(tmp_path, capsys):
    db = published_knjazevac_case(tmp_path)
    before_bytes = db.read_bytes()
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    before = dict(con.execute("SELECT * FROM publications WHERE url=?", (URL,)).fetchone())
    con.close()

    args = ["revise-viber", "--db", str(db), "--url", URL, "--reason", VIBER_REASON]
    assert cli.main(args) == 0
    preview = capsys.readouterr().out
    assert "Књажевац, БМ 61" in preview
    assert before["viber_text"] in preview
    assert "would replace only the saved Viber text" in preview
    assert db.read_bytes() == before_bytes

    assert cli.main([*args, "--confirm"]) == 0
    confirmed = capsys.readouterr().out
    assert "Updated only the saved Viber text" in confirmed
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    after = dict(con.execute("SELECT * FROM publications WHERE url=?", (URL,)).fetchone())
    con.close()
    expected = CaseHandoff.viber_text(json.loads(before["case_json"]), VIBER_REASON,
                                      before["final_url"])
    assert after["viber_text"] == expected
    for key, value in before.items():
        if key not in {"viber_text", "updated_at"}:
            assert after[key] == value

    updated_at = after["updated_at"]
    assert cli.main([*args, "--confirm"]) == 0
    assert "already has the approved saved Viber text" in capsys.readouterr().out
    con = sqlite3.connect(db)
    assert con.execute("SELECT updated_at FROM publications WHERE url=?", (URL,)).fetchone()[0] == updated_at
    con.close()


@pytest.mark.parametrize("mutation", [
    "UPDATE publications SET phase='uploaded' WHERE url=?",
    "UPDATE publications SET folder_id='wrong' WHERE url=?",
    "UPDATE publications SET viber_text='other edited text' WHERE url=?",
    "UPDATE publications SET case_json=replace(case_json, 'Књажевац', 'Друга') WHERE url=?",
])
def test_revise_viber_confirmation_fails_closed_on_changed_target(tmp_path, capsys, mutation):
    db = published_knjazevac_case(tmp_path)
    con = sqlite3.connect(db); con.execute(mutation, (URL,)); con.commit(); con.close()
    assert cli.main(["revise-viber", "--db", str(db), "--url", URL,
                     "--reason", VIBER_REASON, "--confirm"]) == 1
    assert "not changed" in capsys.readouterr().err


@pytest.mark.parametrize("field,bad_url", [
    ("url", "relative/document.pdf"),
    ("rik_url", "//rik.example/extfile/publish.pdf"),
])
def test_revise_viber_rejects_corrupted_stored_source_urls(tmp_path, field, bad_url):
    db = published_knjazevac_case(tmp_path)
    con = sqlite3.connect(db)
    row = con.execute("SELECT case_json FROM publications WHERE url=?", (URL,)).fetchone()
    case = json.loads(row[0])
    case[field] = bad_url
    if field == "url":
        con.execute("UPDATE publications SET url=?,case_json=? WHERE url=?",
                    (bad_url, json.dumps(case), URL))
        selected_url = bad_url
    else:
        con.execute("UPDATE publications SET case_json=? WHERE url=?", (json.dumps(case), URL))
        selected_url = URL
    con.commit()
    con.close()

    with pytest.raises(CaseError, match="absolute HTTP"):
        Store.revise_viber_text(db, selected_url, VIBER_REASON)


def test_revise_viber_holds_write_lock_across_guard_and_update(tmp_path, monkeypatch):
    import rg2watch.case_handoff as handoff_module

    db = published_knjazevac_case(tmp_path)
    checked = threading.Event(); release = threading.Event()
    original = handoff_module.viber_revision

    def blocked(row, url, reason):
        result = original(row, url, reason)
        checked.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(handoff_module, "viber_revision", blocked)
    result = []
    worker = threading.Thread(target=lambda: result.append(
        Store.revise_viber_text(db, URL, VIBER_REASON)))
    worker.start(); assert checked.wait(5)
    second = sqlite3.connect(db, timeout=.05)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        second.execute("UPDATE publications SET summary='race' WHERE url=?", (URL,))
    second.close(); release.set(); worker.join(5)
    assert result[0]["changed"] is True


def test_revise_viber_rejects_missing_db_wrong_url_and_wrong_reason_without_creating_files(
        tmp_path, capsys):
    missing = tmp_path / "missing.sqlite"
    base = ["revise-viber", "--db", str(missing), "--url", URL,
            "--reason", VIBER_REASON, "--confirm"]
    assert cli.main(base) == 1
    assert not missing.exists()
    capsys.readouterr()

    db = published_knjazevac_case(tmp_path)
    assert cli.main(["revise-viber", "--db", str(db), "--url", URL + "?wrong",
                     "--reason", VIBER_REASON, "--confirm"]) == 1
    assert cli.main(["revise-viber", "--db", str(db), "--url", URL,
                     "--reason", "Други разлог.", "--confirm"]) == 1


def _hold_publish_lock(db, url, ready):
    store = Store(db)
    lock = store.case_locks.publish(url, blocking=False)
    assert lock is not None
    ready.set()
    signal.pause()


class _ProcessDrive:
    def __init__(self, state, crash_after_folder=False):
        self.state = state
        self.crash_after_folder = crash_after_folder

    def generate_ids(self, count):
        if count == 1:
            return ["process-root"]
        self.state["generate_calls"] += 1
        return ["process-folder", "process-pdf", "process-text"]

    def account_permission_id(self):
        return "process-account"

    def get_file(self, file_id):
        return self.state["files"].get(file_id)

    def create_root_folder(self, file_id, name):
        files = dict(self.state["files"])
        files[file_id] = {"id": file_id, "name": name,
                          "mimeType": "application/vnd.google-apps.folder",
                          "parents": ["my-drive-root"],
                          "owners": [{"permissionId": "process-account"}], "trashed": False}
        self.state["files"] = files
        return files[file_id]

    def create_folder(self, file_id, name, parent):
        files = dict(self.state["files"])
        files[file_id] = {"id": file_id, "name": name,
                          "mimeType": "application/vnd.google-apps.folder", "parents": [parent],
                          "owners": [{"permissionId": "process-account"}], "trashed": False}
        self.state["files"] = files
        if file_id != "process-root":
            self.state["folder_creates"] += 1
        if file_id != "process-root" and self.crash_after_folder:
            os._exit(73)

    def upload(self, file_id, name, mime, parent, data):
        files = dict(self.state["files"])
        files[file_id] = {"id": file_id, "name": name, "mimeType": mime, "parents": [parent],
                          "md5Checksum": hashlib.md5(data).hexdigest()}
        self.state["files"] = files

    def folder_is_public(self, folder_id):
        return False if folder_id == "process-root" else self.state["public"]

    def list_children(self, folder_id):
        return [f for f in self.state["files"].values() if f.get("parents") == [folder_id]]

    def share_folder(self, folder_id):
        self.state["share_calls"] += 1
        self.state["public"] = True

    def children_have_public_reader(self, child_ids):
        return self.state["public"] and all(i in self.state["files"] for i in child_ids)


def _publish_with_process_store(db, state, crash, out=None):
    from rg2watch.drive_publish import DrivePublisher
    from rg2watch.drive_root import DriveRoot
    store = Store(db)
    try:
        cases = CaseHandoff(store, Config(db_path=Path(db), max_pdf_bytes=1000))
        drive = _ProcessDrive(state, crash)
        root = DriveRoot(Path(db).parent / "process-token.json", drive)
        root.setup()
        result = DrivePublisher(cases, drive, root).publish(URL, "Процесни опис", VIBER_REASON)
        if out is not None:
            out.put(result)
    finally:
        store.close()


def test_two_store_publish_owner_blocks_claim_and_upload_then_recovers_after_crash(tmp_path):
    from test_drive_publish import FakeDrive, make_publisher
    from rg2watch.drive_publish import DriveError

    first, _, _ = handoff(tmp_path)
    second = Store(first.path)
    cases = CaseHandoff(second, Config(db_path=Path(first.path), max_pdf_bytes=1000))
    ready = multiprocessing.Event()
    owner = multiprocessing.Process(target=_hold_publish_lock, args=(first.path, URL, ready))
    owner.start()
    try:
        assert ready.wait(5)
        blocked_drive = FakeDrive()
        with pytest.raises(DriveError, match="in progress"):
            make_publisher(cases, blocked_drive, tmp_path).publish(URL, "Јавни опис", VIBER_REASON)
        assert blocked_drive.steps == []
        assert second.get_publication(URL) is None
    finally:
        if owner.is_alive():
            owner.kill()
        owner.join(5)
    result = make_publisher(cases, blocked_drive, tmp_path).publish(URL, "Јавни опис", VIBER_REASON)
    assert result["status"] == "published"


def test_second_process_recovers_durable_pending_and_remote_create_without_duplicates(tmp_path):
    first, _, cache = handoff(tmp_path)
    db = first.path
    first.close()
    with multiprocessing.Manager() as manager:
        state = manager.dict(files={}, generate_calls=0, folder_creates=0,
                             share_calls=0, public=False)
        crashed = multiprocessing.Process(
            target=_publish_with_process_store, args=(db, state, True))
        crashed.start(); crashed.join(10)
        assert crashed.exitcode == 73

        pending_store = Store(db)
        pending = pending_store.get_publication(URL)
        frozen_path = Path(pending["staged_pdf_path"])
        frozen_bytes = frozen_path.read_bytes()
        frozen_ids = [pending["folder_id"], pending["pdf_file_id"], pending["text_file_id"]]
        pending_store.close()
        cache.write_bytes(b"%PDF-1.4\nnewer cache must not be used")

        out = multiprocessing.Queue()
        recovery = multiprocessing.Process(
            target=_publish_with_process_store, args=(db, state, False, out))
        recovery.start(); recovery.join(10)
        assert recovery.exitcode == 0
        assert out.get(timeout=1)["status"] == "published"
        assert frozen_ids == ["process-folder", "process-pdf", "process-text"]
        assert state["generate_calls"] == 1
        assert state["folder_creates"] == 1
        assert set(state["files"]) == set(frozen_ids) | {"process-root"}
        assert state["files"]["process-pdf"]["md5Checksum"] == hashlib.md5(frozen_bytes).hexdigest()
        assert state["share_calls"] == 1
