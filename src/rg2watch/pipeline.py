"""Background analysis of newly published RG-2 forms: fetch -> render -> analyze -> rules."""
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import anthropic

from .analyze import AnalysisError, AnalysisRefused, analyze_document
from .config import Config
from .fetch import fetch_pdf
from .models import Doc, Event, Finding
from .notify import Notifier
from .render import RenderError, render_tiles
from .rik_client import RikBlocked, RikClient, RikError, RikUnavailable
from .rules import evaluate_report
from .store import ANALYSED_TYPES, MAX_ANALYSIS_ATTEMPTS, Store

log = logging.getLogger("rg2watch.pipeline")

PROMPT_VERSION = "v4"
RETRY_STEP_S = 60
RIK_RETRY_S = 120
IDLE_POLL_S = 5.0
SPEND_CAP_ALERT = "[rg2-watch] spend cap reached"
RESUMED_ALERT = "[rg2-watch] analysis resumed"
REALERT_S = 3600  # while a global pause keeps being extended
# Errors that fail every form alike (bad key, no access, unknown model): pause all analysis
# instead of burning each document's attempts.
GLOBAL_ERRORS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                 anthropic.NotFoundError)
# BadRequest is per document, unless this many different documents in a row hit it
# (e.g. no credit, an invalid parameter): then it is treated as global.
BAD_REQUEST_GLOBAL = 3
DOWNLOAD_ALERT_AFTER = 10  # consecutive download deferrals before a status alert
DOWNLOAD_BLOCKED_PAUSE_AFTER = 3  # consecutive 401/403 on PDFs before workers pause downloads
# Anthropic busy after the SDK's own retries: the form is deferred without using an attempt.
OVERLOAD_ERRORS = (anthropic.RateLimitError, anthropic.InternalServerError,
                   anthropic.OverloadedError, anthropic.APIConnectionError)
# The same errors arriving mid-stream reach us as a plain APIStatusError carrying the error type.
_OVERLOAD_TYPES = {"rate_limit_error", "overloaded_error", "api_error"}
OVERLOAD_RETRY_S = 120
OVERLOAD_ALERT_AFTER = 5  # consecutive overload failures before a status alert
_MAX_ERROR = 200
_RENDER_LOCK = threading.Lock()  # PDFium is not thread-safe


def _short(exc: BaseException, limit: int = _MAX_ERROR) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _is_overload(exc: BaseException) -> bool:
    if isinstance(exc, OVERLOAD_ERRORS):
        return True
    body = getattr(exc, "body", None) if isinstance(exc, anthropic.APIStatusError) else None
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get("type") in _OVERLOAD_TYPES


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


class AnalysisPipeline:
    def __init__(self, client: RikClient, store: Store, notifier: Notifier, anthropic_client,
                 cfg: Config, on_findings: Callable[[list[Finding]], None], cache_dir: Path,
                 clock: Callable[[], float] = time.time, backlog: bool = False):
        self.client, self.store, self.notifier = client, store, notifier
        self.anthropic, self.cfg = anthropic_client, cfg
        self.on_findings, self.cache_dir, self.clock = on_findings, cache_dir, clock
        self.parent = client.election.parent
        self.backlog = backlog  # also analyse forms seen before analysis was enabled
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._state = threading.Lock()
        self._cap_alerted = False
        self._paused = False  # global failure episode (bad key etc.)
        self._pause_until = float("-inf")
        self._probing = False
        self._paused_since = 0.0
        self._pause_alerted_at = 0.0
        self._bad_requests: set[str] = set()  # distinct docs hitting 400 since the last success
        self._deferrals = 0  # consecutive download deferrals (RIK busy/blocking)
        self._deferred_since = 0.0
        self._download_alerted = False
        self._download_paused_until = float("-inf")  # pipeline clock; PDFs blocked (401/403)
        self._overloads = 0  # consecutive Anthropic overload failures
        self._overloaded_since = 0.0
        self._overload_alerted = False

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
        self.store.enable_analysis(self.parent)
        if self.backlog:
            n = self.store.requeue_skipped(self.parent)
            log.info("--analyze-backlog: %d form(s) seen before analysis was enabled queued", n)
        for doc in self.store.requeue_running(self.parent):
            self._emit([Finding("X2", "LOW", doc, f"analysis failed after {MAX_ANALYSIS_ATTEMPTS} "
                                "attempts: interrupted every time (crash or shutdown)")])
        log.info("analysis on for election %s: spend cap $%.2f, $%.2f spent so far on this election, "
                 "%d form(s) queued, %d form(s) seen before analysis was enabled not analysed "
                 "(--analyze-backlog to include)", self.parent, self.cfg.max_spend_usd,
                 self.store.total_spend(self.parent), self.store.queued_for_analysis(self.parent),
                 self.store.analysis_backlog(self.parent))
        for i in range(max(1, self.cfg.analysis_workers)):
            t = threading.Thread(target=self._loop, name=f"analysis-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        """Stop taking work and wait up to `stop_wait_s` for in-flight analyses to be recorded."""
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + self.cfg.stop_wait_s
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        busy = sum(t.is_alive() for t in self._threads)
        if busy:
            log.warning("%d analysis worker(s) still busy after %d s; their documents are "
                        "requeued on the next start", busy, self.cfg.stop_wait_s)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.process_next()
            except Exception:
                log.exception("analysis worker error; continuing")
                busy = False
            if not busy and not self._stop.is_set():
                self._wake.wait(IDLE_POLL_S)
                self._wake.clear()

    # work -------------------------------------------------------------------
    def process_next(self) -> bool:
        """Analyse one due document; False when there is nothing to do or analysis is paused
        (spend cap, RIK blocked pause, global API failure)."""
        if self._stop.is_set() or self._over_cap() or self._rik_paused():
            return False
        with self._state:
            probe = self._paused
            if probe:
                if self.clock() < self._pause_until or self._probing:
                    return False
                self._probing = True  # one probe form decides whether to resume
        outcome = None
        try:
            docs = self.store.next_for_analysis(1, parent=self.parent, now=self.clock())
            if not docs:
                return False
            outcome = self._process(docs[0], probe)
            return True
        finally:
            if probe:
                self._end_probe(outcome)

    def _rik_paused(self) -> bool:
        return (self.client.limiter.clock() < self.client.paused_until
                or self.clock() < self._download_paused_until)

    def _over_cap(self) -> bool:
        spent = self.store.total_spend(self.parent)
        if spent < self.cfg.max_spend_usd:
            return False
        with self._state:
            first, self._cap_alerted = not self._cap_alerted, True
        if first:
            log.warning("spend cap reached ($%.2f of $%.2f on election %s); analysis paused, "
                        "watcher continues", spent, self.cfg.max_spend_usd, self.parent)
            self._status(SPEND_CAP_ALERT)
        return True

    def _status(self, text: str) -> None:
        try:
            self.notifier.status(text)
        except Exception:
            log.exception("status alert failed: %s", text)

    def _process(self, doc: Doc, probe: bool = False) -> str | None:
        """Returns "api" when the Anthropic API answered usably, "global" on a global failure,
        None when no API call was made."""
        try:
            return self._run(doc, probe)
        except Exception as exc:  # a store write failed (e.g. closed at shutdown) or a bug
            if not self._stop.is_set():
                log.exception("analysis of %s failed unexpectedly", doc.url)
            try:
                self._transient(doc, exc)
            except Exception:
                log.error("could not record the failure of %s; it is requeued on the next start",
                          doc.url)
            return None

    def _run(self, doc: Doc, probe: bool) -> str | None:
        if self._rik_paused():  # watcher's blocked pause began after the claim
            self.store.mark_analysis(doc.url, "new", refund=True)
            return None
        try:
            before = self.client.download_last_ok
            path = fetch_pdf(self.client, doc.url, self.cache_dir)
            if self.client.download_last_ok != before:
                self._download_ok()
            with _RENDER_LOCK:
                tiles = render_tiles(path)
        except RikUnavailable as exc:  # RIK busy/blocking is not the document's fault
            log.warning("download of %s deferred %d s: %s", doc.url, RIK_RETRY_S, _short(exc))
            self.store.mark_analysis(doc.url, "failed", _short(exc),
                                     retry_at=self.clock() + RIK_RETRY_S, refund=True)
            self._download_deferred(isinstance(exc, RikBlocked))
            return None
        except (RenderError, RikError) as exc:
            self._permanent(doc, "D3", f"document could not be processed: {_short(exc)}", exc)
            return None
        except Exception as exc:
            self._transient(doc, exc)
            return None

        if self._stop.is_set():  # shutting down: do not start a billed call
            self.store.mark_analysis(doc.url, "new", refund=True)
            return None
        try:
            result = analyze_document(tiles, self.anthropic, self.cfg)
        except anthropic.BadRequestError as exc:
            self._record_cost(doc, exc)
            with self._state:
                self._bad_requests.add(doc.url)
                # during a global pause the probe's 400 counts as still account-wide
                account_wide = probe or len(self._bad_requests) >= BAD_REQUEST_GLOBAL
                if account_wide:
                    self._bad_requests.clear()
            if account_wide:
                return self._global_failure(doc, exc, probe)
            self._transient(doc, exc)
            return None
        except GLOBAL_ERRORS as exc:
            self._record_cost(doc, exc)
            return self._global_failure(doc, exc, probe)
        except AnalysisRefused as exc:
            self._record_cost(doc, exc)
            self._api_ok()
            self._permanent(doc, "X1", f"analysis failed: {_short(exc)}", exc)
            return "api"
        except Exception as exc:  # 429/5xx/overloaded/connection after SDK retries, bad output
            self._record_cost(doc, exc)
            if _is_overload(exc):
                self._overloaded(doc, exc)
                return "api" if isinstance(exc, anthropic.APIStatusError) else None
            self._transient(doc, exc)
            if isinstance(exc, AnalysisError):
                self._api_ok()
            return "api" if isinstance(exc, (AnalysisError, anthropic.APIStatusError)) else None
        self._api_ok()
        try:  # first write after a billed call: never lose its cost
            self.store.save_analysis(doc.url, result, PROMPT_VERSION)
        except Exception:
            log.error("analysis of %s cost $%.3f but could not be recorded", doc.url, result.cost_usd)
            raise

        try:
            found = evaluate_report(result.report, doc, self.cfg)
        except Exception as exc:  # malformed report: retrying would bill again
            log.exception("rules failed for %s", doc.url)
            self._permanent(doc, "X1", f"analysis output could not be evaluated: {_short(exc)}", exc)
            return "api"
        self._emit(found)
        self.store.mark_analysis(doc.url, "done")
        log.info("analysed %s BM %s: %d finding(s), $%.3f", doc.municipality, doc.station_no,
                 len(found), result.cost_usd)
        return "api"

    # global pause -----------------------------------------------------------
    def _api_ok(self) -> None:
        with self._state:
            self._bad_requests.clear()
            recovered, self._overload_alerted = self._overload_alerted, False
            self._overloads = 0
        if recovered:
            log.info("Anthropic answering again")
            self._status("[rg2-watch] analysis no longer delayed: Anthropic answering again")

    def _overloaded(self, doc: Doc, exc: BaseException) -> None:
        log.warning("analysis of %s deferred %d s, Anthropic busy: %s", doc.url, OVERLOAD_RETRY_S,
                    _short(exc))
        self.store.mark_analysis(doc.url, "failed", _short(exc),
                                 retry_at=self.clock() + OVERLOAD_RETRY_S, refund=True)
        with self._state:
            if self._overloads == 0:
                self._overloaded_since = self.clock()
            self._overloads += 1
            alert = self._overloads >= OVERLOAD_ALERT_AFTER and not self._overload_alerted
            if alert:
                self._overload_alerted = True
        if alert:
            self._status("[rg2-watch] analysis delayed: Anthropic overloaded since "
                         f"{_hhmm(self._overloaded_since)}")

    def _global_failure(self, doc: Doc, exc: BaseException, probe: bool) -> str:
        if probe:  # the probe pays its attempt, so a poison document ends in X2
            self._transient(doc, exc)
        else:
            self.store.mark_analysis(doc.url, "new", _short(exc), refund=True)
        self._global_pause(exc)
        return "global"

    def _global_pause(self, exc: BaseException) -> None:
        now = self.clock()
        with self._state:
            first, self._paused = not self._paused, True
            self._pause_until = now + self.cfg.analysis_pause_s
            if first:
                self._paused_since = self._pause_alerted_at = now
            realert = not first and now - self._pause_alerted_at >= REALERT_S
            if realert:
                self._pause_alerted_at = now
        if first:
            log.error("analysis paused for %d s: %s", self.cfg.analysis_pause_s, _short(exc))
            self._status(f"[rg2-watch] analysis paused: {_short(exc, 120)}")
            return
        log.warning("analysis still failing, paused another %d s: %s",
                    self.cfg.analysis_pause_s, _short(exc))
        if realert:
            self._status(f"[rg2-watch] analysis still paused since {_hhmm(self._paused_since)}: "
                         f"{type(exc).__name__}")

    # download health --------------------------------------------------------
    def _download_deferred(self, blocked: bool) -> None:
        with self._state:
            if self._deferrals == 0:
                self._deferred_since = self.clock()
            self._deferrals += 1
            pause = blocked and self.client.download_blocked_streak >= DOWNLOAD_BLOCKED_PAUSE_AFTER
            if pause:  # RIK refuses PDFs: stop downloading for a while, like the watcher does
                self._download_paused_until = self.clock() + self.cfg.blocked_pause_s
            alert = (pause or self._deferrals >= DOWNLOAD_ALERT_AFTER) and not self._download_alerted
            if alert:
                self._download_alerted = True
        if pause:
            log.warning("PDF downloads refused %d times in a row; workers pause downloads for %d s",
                        self.client.download_blocked_streak, self.cfg.blocked_pause_s)
        if alert:
            self._status("[rg2-watch] PDF downloads blocked/unavailable since "
                         f"{_hhmm(self._deferred_since)}")

    def _download_ok(self) -> None:
        with self._state:
            recovered, self._download_alerted = self._download_alerted, False
            self._deferrals = 0
        if recovered:
            self._status("[rg2-watch] PDF downloads available again")

    def _end_probe(self, outcome: str | None) -> None:
        with self._state:
            self._probing = False
            resumed = outcome == "api" and self._paused
            if resumed:
                self._paused = False
        if resumed:
            log.info("analysis resumed")
            self._status(RESUMED_ALERT)
            self._wake.set()

    # outcomes ---------------------------------------------------------------
    def _record_cost(self, doc: Doc, exc: BaseException) -> None:
        cost = getattr(exc, "cost_usd", 0.0) or 0.0
        if not cost:
            return
        try:
            self.store.save_failed_analysis(doc.url, self.cfg.model, PROMPT_VERSION, cost,
                                            _short(exc))
        except Exception:
            log.error("billed failure of %s ($%.3f) could not be recorded", doc.url, cost)

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
