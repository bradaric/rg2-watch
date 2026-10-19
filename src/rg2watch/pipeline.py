"""Background analysis of newly published RG-2 forms: fetch -> render -> analyze -> rules."""
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .analyze import AnalysisError, AnalysisRefused, analyze_document
from .config import Config
from .fetch import fetch_pdf
from .models import Doc, Event, Finding
from .notify import Notifier
from .render import RenderError, render_tiles
from .rik_client import RikClient, RikError, RikUnavailable
from .rules import evaluate_report
from .store import ANALYSED_TYPES, MAX_ANALYSIS_ATTEMPTS, Store

log = logging.getLogger("rg2watch.pipeline")

PROMPT_VERSION = "v1"
RETRY_STEP_S = 60
IDLE_POLL_S = 5.0
STOP_JOIN_S = 5.0
SPEND_CAP_ALERT = "[rg2-watch] spend cap reached"
_MAX_ERROR = 200
_RENDER_LOCK = threading.Lock()  # PDFium is not thread-safe


def _short(exc: BaseException) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text if len(text) <= _MAX_ERROR else text[:_MAX_ERROR - 3] + "..."


class AnalysisPipeline:
    def __init__(self, client: RikClient, store: Store, notifier: Notifier, anthropic_client,
                 cfg: Config, on_findings: Callable[[list[Finding]], None], cache_dir: Path,
                 clock: Callable[[], float] = time.time):
        self.client, self.store, self.notifier = client, store, notifier
        self.anthropic, self.cfg = anthropic_client, cfg
        self.on_findings, self.cache_dir, self.clock = on_findings, cache_dir, clock
        self.parent = client.election.parent
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._cap_lock = threading.Lock()
        self._cap_alerted = False

    # control ----------------------------------------------------------------
    def submit(self, events: list[Event]) -> None:
        """Queue added/replaced RG-2 documents (the store dedupes by URL) and wake the workers."""
        queued = False
        for ev in events:
            if ev.kind in ("added", "replaced") and ev.doc.doc_type in ANALYSED_TYPES:
                queued |= self.store.queue_analysis(ev.doc.url)
        if queued:
            self._wake.set()

    def start(self) -> None:
        n = self.store.requeue_running()
        if n:
            log.info("requeued %d document(s) left running by a previous run", n)
        for i in range(max(1, self.cfg.analysis_workers)):
            t = threading.Thread(target=self._loop, name=f"analysis-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + STOP_JOIN_S
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.process_next()
            except Exception:
                if self._stop.is_set():
                    return
                log.exception("analysis worker error; continuing")
                busy = False
            if not busy:
                self._wake.wait(IDLE_POLL_S)
                self._wake.clear()

    # work -------------------------------------------------------------------
    def process_next(self) -> bool:
        """Analyse one due document; False when there is nothing to do (or spend cap reached)."""
        if self._over_cap():
            return False
        docs = self.store.next_for_analysis(1, parent=self.parent, now=self.clock())
        if not docs:
            return False
        self._process(docs[0])
        return True

    def _over_cap(self) -> bool:
        spent = self.store.total_spend()
        if spent < self.cfg.max_spend_usd:
            return False
        with self._cap_lock:
            first, self._cap_alerted = not self._cap_alerted, True
        if first:
            log.warning("spend cap reached ($%.2f of $%.2f); analysis paused, watcher continues",
                        spent, self.cfg.max_spend_usd)
            try:
                self.notifier.status(SPEND_CAP_ALERT)
            except Exception:
                log.exception("spend cap alert failed")
        return True

    def _process(self, doc: Doc) -> None:
        try:
            path = fetch_pdf(self.client, doc.url, self.cache_dir)
            with _RENDER_LOCK:
                tiles = render_tiles(path)
        except (RenderError, RikError) as exc:
            if isinstance(exc, RikUnavailable):
                self._transient(doc, exc)
            else:
                self._permanent(doc, "D3", f"document could not be processed: {_short(exc)}", exc)
            return
        except Exception as exc:
            self._transient(doc, exc)
            return

        try:
            result = analyze_document(tiles, self.anthropic, self.cfg)
        except AnalysisRefused as exc:
            self._record_failed(doc, exc)
            self._permanent(doc, "X1", f"analysis failed: {_short(exc)}", exc)
            return
        except Exception as exc:  # API errors (after SDK retries), truncated/invalid output
            if isinstance(exc, AnalysisError):
                self._record_failed(doc, exc)
            self._transient(doc, exc)
            return
        self.store.save_analysis(doc.url, result, PROMPT_VERSION)

        try:
            found = evaluate_report(result.report, doc, self.cfg)
        except Exception as exc:  # malformed report: retrying would bill again
            log.exception("rules failed for %s", doc.url)
            self._permanent(doc, "X1", f"analysis output could not be evaluated: {_short(exc)}", exc)
            return
        self._emit(found)
        self.store.mark_analysis(doc.url, "done")
        log.info("analysed %s BM %s: %d finding(s), $%.3f", doc.municipality, doc.station_no,
                 len(found), result.cost_usd)

    # outcomes ---------------------------------------------------------------
    def _record_failed(self, doc: Doc, exc: AnalysisError) -> None:
        try:
            self.store.save_failed_analysis(doc.url, self.cfg.model, PROMPT_VERSION, exc.cost_usd,
                                            _short(exc))
        except Exception:
            log.exception("could not record billed failure for %s ($%.3f)", doc.url, exc.cost_usd)

    def _emit(self, found: list[Finding]) -> None:
        if not found:
            return
        try:
            self.on_findings(found)
        except Exception:
            log.exception("findings handler failed for %s", found[0].doc.url)

    def _permanent(self, doc: Doc, rule: str, summary: str, exc: BaseException) -> None:
        log.warning("%s %s: %s", rule, doc.url, _short(exc))
        self.store.mark_analysis(doc.url, "failed", _short(exc))
        self._emit([Finding(rule, "LOW", doc, summary)])

    def _transient(self, doc: Doc, exc: BaseException) -> None:
        attempts = self.store.analysis_attempts(doc.url)
        if attempts >= MAX_ANALYSIS_ATTEMPTS:
            log.error("analysis of %s failed after %d attempts: %s", doc.url, attempts, _short(exc))
            self.store.mark_analysis(doc.url, "failed", _short(exc))
            self._emit([Finding("X2", "LOW", doc,
                                f"analysis failed after {attempts} attempts: {_short(exc)}")])
            return
        retry_at = self.clock() + RETRY_STEP_S * attempts
        log.warning("analysis of %s failed (attempt %d), retrying in %d s: %s", doc.url, attempts,
                    RETRY_STEP_S * attempts, _short(exc))
        self.store.mark_analysis(doc.url, "failed", _short(exc), retry_at=retry_at)
