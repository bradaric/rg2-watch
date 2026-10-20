import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
import logging

from .case_handoff import CaseHandoff, normalize_viber_reason
from .drive_publish import DrivePublisher, _publication_diagnostic_id


logger = logging.getLogger("rg2watch.publication_service")


class PublicationJobs:
    """Small bounded executor for optional drafting and Drive work.

    Jobs are intentionally in-memory; durable pending/publication state lives in SQLite and is
    recovered by another explicit Publish after restart.
    """
    def __init__(self, cases: CaseHandoff, publisher: DrivePublisher | None, workers: int = 2):
        self.cases, self.publisher = cases, publisher
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="case")
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._active: set[tuple[str, str]] = set()

    def _submit(self, kind: str, url: str, fn) -> str:
        key = kind, url
        with self._lock:
            if key in self._active:
                raise ValueError(f"{kind} is already in progress for this document URL")
            job_id = uuid.uuid4().hex
            self._active.add(key)
            self._jobs[job_id] = {"id": job_id, "state": "running", "kind": kind}

        def run():
            token = None
            if kind == "publication":
                token = _publication_diagnostic_id.set(job_id)
                logger.info("publication start diagnostic_id=%s", job_id)
            try:
                result = fn()
                with self._lock:
                    self._jobs[job_id] = {"id": job_id, "state": "done", "kind": kind,
                                          "result": result}
                if kind == "publication":
                    logger.info("publication done diagnostic_id=%s", job_id)
            except Exception as exc:
                with self._lock:
                    self._jobs[job_id] = {"id": job_id, "state": "error", "kind": kind,
                                          "error": str(exc)[:500] or type(exc).__name__}
                if kind == "publication":
                    logger.info("publication fail diagnostic_id=%s exception=%s",
                                job_id, type(exc).__name__)
            finally:
                if token is not None:
                    _publication_diagnostic_id.reset(token)
                with self._lock:
                    self._active.discard(key)

        self._executor.submit(run)
        return job_id

    def submit_prepare(self, url: str) -> str:
        return self._submit("preparation", url, lambda: self.cases.prepare(url, draft=True))

    def submit_publish(self, url: str, summary: str, viber_reason: str | None,
                       confirm: bool) -> str:
        if confirm is not True:
            raise ValueError("explicit public-sharing confirmation is required")
        if self.publisher is None:
            raise ValueError("Google Drive is not configured; run `rg2watch drive-auth`")
        row = self.cases.store.get_publication(url)
        if row is None:
            viber_reason = normalize_viber_reason(viber_reason)
        elif row["phase"] != "published":
            if row["viber_reason"] is None:
                if viber_reason is not None:
                    raise ValueError("legacy frozen publication does not accept a Viber reason")
            else:
                viber_reason = normalize_viber_reason(viber_reason)
                if viber_reason != row["viber_reason"]:
                    raise ValueError("inputs differ from the frozen pending package")
        return self._submit(
            "publication", url,
            lambda: self.publisher.publish(url, summary, viber_reason))

    def job(self, job_id: str) -> dict | None:
        with self._lock:
            return dict(self._jobs[job_id]) if job_id in self._jobs else None

    def publication(self, url: str) -> dict | None:
        row = self.cases.store.get_publication(url)
        if row is None:
            return None
        out = {"status": row["phase"]}
        if row["phase"] == "published":
            out.update(url=row["final_url"], viber_text=row["viber_text"])
        else:
            out.update(summary=row["summary"], retryable=True)
            if row["viber_reason"] is not None:
                out["viber_reason"] = row["viber_reason"]
        return out

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
