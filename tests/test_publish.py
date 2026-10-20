import hashlib
import http.client
import json
import multiprocessing
import os
import signal
import subprocess
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
    # Recovery uses the frozen staging copy, never a newer cache file.
    assert service.claim(URL, "Опрезан јавни опис.")["pdf_sha256"] == first["pdf_sha256"]
    with pytest.raises(CaseError, match="frozen"):
        service.claim(URL, "Други опис")
    store.close()


def test_restart_after_preclaim_crash_discards_unclaimed_orphan_and_accepts_new_summary(
        tmp_path, monkeypatch):
    from test_drive_publish import FakeDrive
    from rg2watch.drive_publish import DrivePublisher

    store, service, _ = handoff(tmp_path)

    def crash_before_claim(*args, **kwargs):
        raise RuntimeError("simulated claim crash")

    monkeypatch.setattr(store, "claim_publication", crash_before_claim)
    with pytest.raises(RuntimeError, match="claim crash"):
        service.claim(URL, "Замрзнут опис")
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

    result = DrivePublisher(restarted, FakeDrive()).publish(URL, "Нови одобрени опис")
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
                                str(tmp_path / "missing.pdf"), "0" * 64, case)
    assert store.get_publication(URL) is None


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


def test_ai_draft_is_bounded_by_publish_utf8_byte_limit(tmp_path):
    store, service, _ = handoff(tmp_path)
    client = type("Client", (), {"messages": FakeMessages("Ж" * 4096)})()
    draft = CaseDrafter(store, service.cfg, client).draft(service._case(URL))
    assert len(draft.encode("utf-8")) <= 4096
    service.claim(URL, draft)


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
    def submit_publish(self, url, summary, confirm):
        assert confirm is True
        return "publish-job"
    def job(self, job_id):
        return {"id": job_id, "state": "done", "result": {"summary": "Ћирилица"}}
    def publication(self, url):
        return {"status": "uploaded", "summary": "Замрзнути опис", "retryable": True}


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
                            {"url": URL, "summary": "Јавно", "confirm": True}) == (
                                202, {"job_id": "publish-job"})
        assert case_request(srv, "GET", "/case/jobs/prepare-job")[0] == 200
        assert case_request(srv, "GET", "/case/publication?url=" + URL) == (
            200, {"status": "uploaded", "summary": "Замрзнути опис", "retryable": True})
        assert case_request(srv, "POST", "/case/prepare", {"url": URL},
                            {"Origin": "http://evil.example"})[0] == 403
        assert case_request(srv, "GET", "/case/jobs/prepare-job",
                            headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert case_request(srv, "POST", "/case/publish", {"url": URL, "summary": "x",
                                                             "confirm": False})[0] == 400
        assert case_request(srv, "GET", "/case/prepare")[0] == 404
    finally:
        srv.shutdown(); srv.server_close()


@pytest.mark.parametrize("method,path,body,headers,status", [
    ("POST", "/case/prepare", {"url": URL, "extra": 1}, None, 400),
    ("POST", "/case/prepare", {"url": 7}, None, 400),
    ("POST", "/case/publish", {"url": URL, "summary": 7, "confirm": True}, None, 400),
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
    assert "свако са везом" in html
    assert "area.value = pending.summary" in html
    assert "Понови објаву замрзнутог предмета" in html


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
  let prep = findText(box, "Припреми нацрт"); prep.onclick(); await tick(); await tick();
  if (prep.disabled || !box.querySelector(".rvmsg").textContent.includes("prepare boom")) process.exit(2);

  jobs.prepare = {state: "done", result: {summary: "Опис"}};
  jobs.publish = {state: "error", error: "publish boom"};
  box = caseBox({u: "https://example.test/a.pdf", pdf: "hash"}); await tick();
  prep = findText(box, "Припреми нацрт"); prep.onclick(); await tick(); await tick();
  const publish = findText(box, "Објави јавни предмет");
  const confirm = find(box, x => x.tag === "input"); confirm.checked = true;
  publish.onclick(); await tick(); await tick();
  if (publish.disabled || !box.querySelector(".rvmsg").textContent.includes("publish boom")) process.exit(3);
})().catch(e => { console.error(e); process.exit(4); });
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
            jobs.submit_publish(URL, "Јавни опис", True)
        assert jobs.publication(URL) is None
    finally:
        jobs.close()


def test_pending_publication_status_exposes_frozen_summary_for_explicit_retry(tmp_path):
    store, cases, cache = handoff(tmp_path)
    pending = cases.claim(URL, "Замрзнути јавни опис")
    store.save_drive_ids(URL, "folder", "pdf", "text")
    cache.unlink()
    store.set_review(URL, "ok")
    jobs = PublicationJobs(cases, object(), workers=1)
    try:
        state = jobs.publication(URL)
        assert state == {
            "status": pending["phase"],
            "summary": "Замрзнути јавни опис",
            "retryable": True,
        }
    finally:
        jobs.close()


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
        self.state["generate_calls"] += 1
        return ["process-folder", "process-pdf", "process-text"]

    def get_file(self, file_id):
        return self.state["files"].get(file_id)

    def create_folder(self, file_id, name):
        files = dict(self.state["files"])
        files[file_id] = {"id": file_id, "name": name,
                          "mimeType": "application/vnd.google-apps.folder", "parents": []}
        self.state["files"] = files
        self.state["folder_creates"] += 1
        if self.crash_after_folder:
            os._exit(73)

    def upload(self, file_id, name, mime, parent, data):
        files = dict(self.state["files"])
        files[file_id] = {"id": file_id, "name": name, "mimeType": mime, "parents": [parent],
                          "md5Checksum": hashlib.md5(data).hexdigest()}
        self.state["files"] = files

    def folder_is_public(self, folder_id):
        return self.state["public"]

    def share_folder(self, folder_id):
        self.state["share_calls"] += 1
        self.state["public"] = True

    def children_inherit_public(self, folder_id, child_ids):
        return self.state["public"] and all(i in self.state["files"] for i in child_ids)


def _publish_with_process_store(db, state, crash, out=None):
    from rg2watch.drive_publish import DrivePublisher
    store = Store(db)
    try:
        cases = CaseHandoff(store, Config(db_path=Path(db), max_pdf_bytes=1000))
        result = DrivePublisher(cases, _ProcessDrive(state, crash)).publish(URL, "Процесни опис")
        if out is not None:
            out.put(result)
    finally:
        store.close()


def test_two_store_publish_owner_blocks_claim_and_upload_then_recovers_after_crash(tmp_path):
    from test_drive_publish import FakeDrive
    from rg2watch.drive_publish import DrivePublisher, DriveError

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
            DrivePublisher(cases, blocked_drive).publish(URL, "Јавни опис")
        assert blocked_drive.steps == []
        assert second.get_publication(URL) is None
    finally:
        if owner.is_alive():
            owner.kill()
        owner.join(5)
    result = DrivePublisher(cases, blocked_drive).publish(URL, "Јавни опис")
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
        assert set(state["files"]) == set(frozen_ids)
        assert state["files"]["process-pdf"]["md5Checksum"] == hashlib.md5(frozen_bytes).hexdigest()
        assert state["share_calls"] == 1
