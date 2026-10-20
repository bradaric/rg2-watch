import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from .case_handoff import CaseHandoff
from .drive_publish import DrivePublisher


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
            try:
                result = fn()
                with self._lock:
                    self._jobs[job_id] = {"id": job_id, "state": "done", "kind": kind,
                                          "result": result}
            except Exception as exc:
                with self._lock:
                    self._jobs[job_id] = {"id": job_id, "state": "error", "kind": kind,
                                          "error": str(exc)[:500] or type(exc).__name__}
            finally:
                with self._lock:
                    self._active.discard(key)

        self._executor.submit(run)
        return job_id

    def submit_prepare(self, url: str) -> str:
        return self._submit("preparation", url, lambda: self.cases.prepare(url, draft=True))

    def submit_publish(self, url: str, summary: str, confirm: bool) -> str:
        if confirm is not True:
            raise ValueError("explicit public-sharing confirmation is required")
        if self.publisher is None:
            raise ValueError("Google Drive is not configured; run `rg2watch drive-auth`")
        return self._submit("publication", url, lambda: self.publisher.publish(url, summary))

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
            out["retryable"] = True
        return out

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
