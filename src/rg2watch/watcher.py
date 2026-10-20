import logging
import math
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config
from .diff import diff
from .models import DOC_TYPES, RARE_TYPES, Doc, Event, Filters, Scope, doc_key
from .rik_client import PAGE_SIZE, RikClient, RikUnavailable
from .store import Store

log = logging.getLogger("rg2watch.watcher")
REFRESH_MIN_S = 600
MISSING_BACKOFF_CAP_S = 600
BLOCKED_STREAK = 3  # consecutive 401/403 answers before all lanes pause
MIN_BUDGET = 5  # per-tick request budget floor
URGENT_RARE = (8, 9, 27, 28)  # RG-8/RG-9 family: swept first
BASELINE_LANES = ("rare", "watchlist", "probe", "missing", "safety")


@dataclass
class _Job:
    """A listing sweep spread over ticks; every page is charged to the per-tick request budget.
    Complete jobs diff once at the end (removals queued for direct re-check); incomplete jobs
    emit additions/replacements per batch."""
    filters: Filters
    scope: Scope
    complete: bool
    doc_type: int | None
    on_done: Callable[["_Job"], None] | None = None
    total: int | None = None  # count to store once the job succeeds
    key: object = None
    prio: tuple = ()
    stale_total: int | None = None  # count moved after the job started: re-run instead of storing
    next_page: int = 1
    max_page: int | None = None
    ok: bool = True
    docs: list[Doc] = field(default_factory=list)
    extra: tuple = (None, None)  # rare jobs: (municipality name, id) under a municipality filter
    touched: set = field(default_factory=set)  # keys changed by other lanes since the job began


@dataclass
class _Round:
    """Per-municipality probe round: counts every municipality, sweeps the changed ones."""
    total: int
    pending: list[tuple[str, str]]  # (name, id) still to count
    counts: int = 0
    ok: bool = True


def _count_key(key: int | tuple[int, str]) -> str:
    return f"type:{key}" if isinstance(key, int) else f"type:{key[0]}:m:{key[1]}"


def _type_name(d: Doc) -> str:
    return DOC_TYPES.get(d.doc_type, d.label) if d.doc_type is not None else d.label


class Watcher:
    def __init__(self, client: RikClient, store: Store, cfg: Config,
                 on_events: Callable[[list[Event]], None], clock=time.monotonic, sleep=time.sleep,
                 initial_safety: bool = False, on_status: Callable[[str], None] | None = None,
                 baseline: bool = False, only: dict[str, str] | None = None):
        self.client, self.store, self.cfg = client, store, cfg
        self.on_events, self.clock, self.sleep = on_events, clock, sleep
        self.on_status = on_status
        self.parent = client.election.parent
        self._only_ids = set(only.values()) if only is not None else None
        self._only = dict(only) if only is not None else None  # name -> id; None = every municipality
        self._bootstrapped = False
        self._abort = False
        self._stats: dict[str, int] = {}
        self._refresh_wanted = False
        self._last_bootstrap: float | None = None
        self._warned: set[str] = set()
        self._unknown_names: set[str] = set()
        self._shortfall_streak = 0
        self._missing_state: dict[str, tuple[float, float]] = {}  # station key -> (next_at, delay)
        self._started = clock()
        self._paused_until = float("-inf")
        self._blind = False
        self._blind_ref: float | None = None
        self._lane = ""
        self._budget = 0
        self._missing_work = cfg.missing_station_threshold > 0  # had stations to query last run
        self._last: dict[str, float] = {}
        self._jobs: dict[str, list[_Job]] = {"rare": [], "watchlist": [], "probe": []}
        self._safety_job: _Job | None = None
        self._safety_rest: list[_Job] = []  # filtered mode: municipalities still to sweep
        self._round: _Round | None = None
        self._recheck: dict[str, dict] = {}  # station key -> {doc key: removal candidate}
        self._rare_resweep_at = self._started
        # The first safety pass starts one interval after start, or at once when it is the baseline.
        early = initial_safety or baseline
        self._safety_started = self._started - (cfg.safety_sweep_interval_s if early else 0)
        self._baseline = set(BASELINE_LANES) if baseline else set()
        self._baseline_counts: Counter[str] = Counter()
        self._progress = {"rare": False, "probe": False, "missing": False, "safety": False}
    def bootstrap(self, refresh: bool = False) -> None:
        self.store.upsert_election(self.client.election)
        if refresh or not self.store.municipality_ids(self.parent):
            self.store.save_municipalities(self.parent, self.client.municipalities())
        names = self.store.municipality_ids(self.parent)
        if self._only_ids is not None:
            self._only = {n: i for n, i in names.items() if i in self._only_ids}
        for mid in (self._only or names).values():
            if not self.store.station_ids(self.parent, mid):
                options = self.client.stations(mid)  # station id -> station no
                self.store.save_stations(self.parent, mid, {no: sid for sid, no in options.items()})
        self._bootstrapped = True
        self._refresh_wanted = False
        self._last_bootstrap = self.clock()

    def run_once(self) -> None:
        self._abort = False
        if self.clock() >= self._paused_until:
            self._tick()
        self._check_health()

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_once()
            self.sleep(1)

    def _tick(self) -> None:
        if not self._bootstrapped:
            try:
                self.bootstrap()
            except Exception as exc:
                log.warning("bootstrap failed: %r", exc)
                self._abort = True
        elif self._refresh_wanted and self.clock() - (self._last_bootstrap or 0) >= REFRESH_MIN_S:
            try:
                self.bootstrap(refresh=True)
            except Exception as exc:
                log.warning("municipality refresh failed: %r", exc)
                self._abort = isinstance(exc, RikUnavailable)
                self._last_bootstrap = self.clock()  # throttle retries
        self._budget = budget = max(MIN_BUDGET, self.cfg.tick_request_budget)
        # Priority: rare > removal re-checks > watchlist > probe > missing > safety, except that a
        # reserved share is held back from watchlist/probe for safety (first) and missing when they
        # have work, so a busy probe cannot starve them. Unused reserve goes back to the probe,
        # then to safety.
        self._lane_if("rare", self._rare_wants, self._rare)
        self._lane_if("recheck", self._recheck_wants, self._rechecks)
        now = self.clock()
        held = 0
        if self._safety_wants(now) or (self._missing_work and self._missing_wants(now)):
            held = min(self._budget, int(budget * self.cfg.reserved_share))
        self._budget -= held
        self._lane_if("watchlist", self._watchlist_wants, self._watchlist)
        self._lane_if("probe", self._probe_wants, self._probe)
        self._budget += held
        before = self._budget
        self._lane_if("safety", self._safety_wants, self._capped(self._safety, held))
        held -= before - self._budget
        self._lane_if("missing", self._missing_wants, self._capped(self._missing, held))
        self._lane_if("probe", self._probe_wants, self._probe)
        self._lane_if("safety", self._safety_wants, self._safety)
        self._lane_if("recheck", self._recheck_wants, self._rechecks)

    def _lane_if(self, name: str, wants, fn) -> None:
        if self._abort or not wants(self.clock()):
            return
        self._run_lane(name, fn)
        if self._baseline:
            self._baseline_update()

    def _capped(self, fn, cap: int):
        """Run fn on at most `cap` requests of the remaining budget."""
        def run(sweep_id: int) -> None:
            rest = max(0, self._budget - cap)
            self._budget -= rest
            try:
                fn(sweep_id)
            finally:
                self._budget += rest
        return run

    def _recheck_wants(self, now: float) -> bool:
        return bool(self._recheck)

    def _missing_wants(self, now: float) -> bool:
        return self._due("missing", self.cfg.probe_interval_s, now)

    def _safety_wants(self, now: float) -> bool:
        return self._safety_job is not None or self._safety_due(now)

    def _due(self, name: str, interval: float, now: float) -> bool:
        last = self._last.get(name)
        return last is None or now - last >= interval

    def _safety_due(self, now: float) -> bool:
        # Measured from the previous pass's start, so long passes run back to back.
        return now - self._safety_started >= self.cfg.safety_sweep_interval_s

    def _run_lane(self, name: str, fn) -> None:
        self._lane = name
        self._stats = {"pages": 0, "records": 0, "added": 0, "replaced": 0, "removed": 0}
        sweep_id = self.store.start_sweep(self.parent, name)
        start = self.clock()
        try:
            fn(sweep_id)
        except RikUnavailable as exc:
            log.warning("lane %s: RIK unavailable, skipping rest of tick: %s", name, exc)
            self._abort = True
        except Exception:
            log.exception("lane %s failed", name)
        s = self._stats
        self.store.finish_sweep(sweep_id, **s)
        log.info("lane=%s requests=%d duration_s=%.1f events=%d budget_left=%d", name, s["pages"],
                 self.clock() - start, s["added"] + s["replaced"] + s["removed"], self._budget)

    # health -----------------------------------------------------------------
    def _check_health(self) -> None:
        now, c = self.clock(), self.client
        if c.blocked_streak >= BLOCKED_STREAK:
            c.blocked_streak = 0
            self._paused_until = now + self.cfg.blocked_pause_s
            c.paused_until = self._paused_until  # analysis workers honour the pause too
            log.warning("RIK refused %d requests in a row (401/403); pausing all lanes for %d s",
                        BLOCKED_STREAK, self.cfg.blocked_pause_s)
        last = c.last_ok if c.last_ok is not None else self._started
        if not self._blind:
            if now - last >= self.cfg.blind_alert_s:
                self._blind, self._blind_ref = True, c.last_ok
                since = (datetime.now() - timedelta(seconds=now - last)).strftime("%H:%M")
                self._status(f"[rg2-watch] RIK unreachable/blocking since {since}")
        elif c.last_ok is not None and (self._blind_ref is None or c.last_ok > self._blind_ref):
            self._blind = False
            self._status("[rg2-watch] RIK reachable again")

    def _status(self, text: str) -> None:
        log.warning("%s", text)
        if self.on_status is not None:
            try:
                self.on_status(text)
            except Exception:
                log.exception("status alert failed")

    # baseline ---------------------------------------------------------------
    def _baseline_update(self) -> None:
        done = {
            "rare": self._progress["rare"] and not self._jobs["rare"],
            "watchlist": not self.cfg.watchlist or ("watchlist" in self._last
                                                    and not self._jobs["watchlist"]),
            "probe": self._progress["probe"] and self._round is None and not self._jobs["probe"],
            # stations only count as missing once the probe has recorded the RG-2s
            "missing": self._progress["missing"],
            "safety": self._progress["safety"],
        }
        for lane in list(self._baseline):
            if done[lane]:
                self._baseline.discard(lane)
        if self._baseline:
            return
        for code, n in sorted(self._baseline_counts.items()):
            print(f"[baseline] {code}: {n} document(s) recorded", flush=True)
        if not self._baseline_counts:
            print("[baseline] no documents recorded", flush=True)

    # requests -------------------------------------------------------------
    def _listing(self, filters: Filters, page: int, charge: bool = True):
        self._stats["pages"] += 1
        if charge:
            self._budget -= 1
        result = self.client.listing(filters, page)
        self._stats["records"] += result.records
        return result

    def _count(self, filters: Filters, charge: bool = True) -> tuple[int, int]:
        """Returns (record count, max_page)."""
        first = self._listing(filters, 1, charge)
        if first.max_page <= 1:
            return first.records, first.max_page
        last = self._listing(filters, first.max_page, charge)
        return (first.max_page - 1) * PAGE_SIZE + last.records, first.max_page

    def _process(self, sweep_id: int, docs: list[Doc], scope: Scope, complete: bool,
                 doc_type: int | None, exclude: set = frozenset()) -> list[Doc]:
        """Diff observed docs against the store and emit; removal candidates are queued for a
        direct per-station re-check. Returns the docs kept for this scope."""
        if doc_type is not None:
            docs = [d for d in docs if d.doc_type == doc_type]
        if self._only is not None:
            docs = [d for d in docs if d.municipality in self._only]
        known_names = self.store.municipality_ids(self.parent)
        for name in {d.municipality for d in docs} - set(known_names):
            self._unknown_names.add(name)
            self._refresh_wanted = True
            if name not in self._warned:
                self._warned.add(name)
                log.warning("listing municipality %r is not among stored municipalities", name)
        if scope.municipality is not None:
            # Docs under another name cannot be diffed in this scope; the probe falls back to a full sweep.
            docs = [d for d in docs if d.municipality == scope.municipality]
        events = diff(self.store.known_docs(self.parent, scope), docs, complete)
        events = [e for e in events if doc_key(e.doc) not in exclude]
        self._emit(sweep_id, [e for e in events if e.kind != "removed"])
        for e in events:
            if e.kind == "removed":
                self._recheck.setdefault(e.doc.station_key, {})[doc_key(e.doc)] = e
        return docs

    def _advance(self, sweep_id: int, job: _Job) -> bool:
        """Fetch pages of a job while budget lasts. Returns True once the job is finished.
        RikUnavailable aborts the tick and retries the same page later."""
        while self._budget > 0 and (job.max_page is None or job.next_page <= job.max_page):
            try:
                res = self._listing(job.filters, job.next_page)
            except RikUnavailable:
                self._abort = True
                break
            except Exception:
                log.exception("page %d of %s failed", job.next_page, job.filters)
                job.ok = False
                if job.max_page is None:
                    job.max_page = 0  # no page count known: give the job up
                job.next_page += 1
                continue
            docs = res.docs if job.doc_type is None else [d for d in res.docs if d.doc_type == job.doc_type]
            if docs and not job.complete:
                # If this raises the cursor stays put and the page is fetched again next tick.
                self._process(sweep_id, docs, job.scope, False, job.doc_type)
            job.docs += docs
            job.max_page = res.max_page if job.max_page is None else max(job.max_page, res.max_page)
            job.next_page += 1
        return job.max_page is not None and job.next_page > job.max_page

    def _run_jobs(self, sweep_id: int, jobs: list[_Job]) -> None:
        while jobs and not self._abort and self._budget > 0:
            if not self._advance(sweep_id, jobs[0]):
                return
            self._finish(sweep_id, jobs[0])  # on error the job stays queued and is finished later
            jobs.pop(0)

    def _finish(self, sweep_id: int, job: _Job) -> None:
        if job.complete:
            # Keys another lane changed after this job saw them hold stale observations: skip them.
            self._process(sweep_id, job.docs, job.scope, job.ok, job.doc_type, exclude=job.touched)
        if job.on_done is not None:
            job.on_done(job)

    def _emit(self, sweep_id: int, events: list[Event]) -> None:
        if not events:
            return
        self.store.apply_events(self.parent, sweep_id, events)
        for ev in events:
            self._stats[ev.kind] = self._stats.get(ev.kind, 0) + 1
        keys = {doc_key(ev.doc) for ev in events}
        started = [j for jobs in self._jobs.values() for j in jobs] + [self._safety_job]
        for job in started:
            if job is not None and job.complete and job.next_page > 1:
                job.touched |= keys
        if self._lane in self._baseline or (self._lane == "recheck" and self._baseline):
            self._baseline_counts.update(_type_name(ev.doc) for ev in events if ev.kind != "removed")
            return
        self.on_events(events)

    # lanes ----------------------------------------------------------------
    def _rare_wants(self, now: float) -> bool:
        return bool(self._jobs["rare"]) or self._due("rare", self.cfg.rare_interval_s, now)

    def _rare(self, sweep_id: int) -> None:
        now = self.clock()
        if self._due("rare", self.cfg.rare_interval_s, now):
            self._last["rare"] = now
            self._rare_counts(now)
        self._run_jobs(sweep_id, self._jobs["rare"])

    def _rare_counts(self, now: float) -> None:
        """Count checks are not charged to the budget: they always run every rare_interval_s."""
        resweep = now - self._rare_resweep_at >= self.cfg.rare_resweep_interval_s
        jobs = self._jobs["rare"]
        targets = [(t, None, None) for t in RARE_TYPES] if self._only is None else [
            (t, name, mid) for t in RARE_TYPES for name, mid in self._only.items()]
        for t, name, mid in targets:
            if self._abort:
                return
            try:
                n, _ = self._count(Filters(doc_type=t, municipality_id=mid), charge=False)
            except RikUnavailable:
                raise
            except Exception:
                log.exception("rare lane: type %d count failed", t)
                continue
            key = t if mid is None else (t, mid)
            stored = self.store.get_count(self.parent, _count_key(key))
            queued = next((j for j in jobs if j.key == key), None)
            if queued is not None:
                if queued.next_page == 1:
                    queued.total = n
                elif n != queued.total:
                    queued.stale_total = n  # may be on a page already passed: re-run when done
            elif (n != stored or (resweep and n > 0)) and not (n == 0 and stored is None):
                jobs.append(self._rare_job(t, n, n != stored, name, mid))
            else:
                self.store.set_count(self.parent, _count_key(key), n)
        self._sort_rare()
        if resweep:  # every non-empty rare type re-swept: catches count-neutral replacements
            self._rare_resweep_at = now
        self._progress["rare"] = True

    def _rare_job(self, t: int, n: int, count_changed: bool, name: str | None = None,
                  mid: str | None = None) -> _Job:
        key = t if mid is None else (t, mid)
        return _Job(Filters(doc_type=t, municipality_id=mid), Scope(doc_type=t, municipality=name),
                    True, t, on_done=self._rare_done, total=n, key=key,
                    prio=(not count_changed, t not in URGENT_RARE), extra=(name, mid))

    def _sort_rare(self) -> None:
        # Started job first, then count changes before re-sweeps, RG-8/RG-9 family first.
        self._jobs["rare"].sort(key=lambda j: (j.next_page == 1, j.prio))

    def _rare_done(self, job: _Job) -> None:
        if job.stale_total is not None:
            self._jobs["rare"].append(self._rare_job(job.doc_type, job.stale_total, True, *job.extra))
            self._sort_rare()
        elif job.ok:
            self.store.set_count(self.parent, _count_key(job.key), job.total)

    def _rechecks(self, sweep_id: int) -> None:
        """Confirm removal candidates with a direct per-station query (charged to the budget)."""
        names = self.store.municipality_ids(self.parent)
        while self._recheck and self._budget > 0 and not self._abort:
            key = next(iter(self._recheck))
            candidates = self._recheck[key]
            sample = next(iter(candidates.values())).doc
            mid = names.get(sample.municipality)
            sid = self.store.station_ids(self.parent, mid).get(sample.station_no) if mid else None
            if sid is None:
                log.warning("cannot confirm removal at %s: station ids unknown", key)
                del self._recheck[key]
                continue
            try:
                page = self._listing(Filters(municipality_id=mid, station_no_id=sid), 1)
            except RikUnavailable:
                raise
            except Exception:
                log.exception("removal re-check failed for %s", key)
                del self._recheck[key]
                continue
            del self._recheck[key]
            present = {doc_key(x) for x in page.docs}
            known = self.store.known_docs(self.parent, Scope(station_key=key))
            self._emit(sweep_id, [e for k, e in candidates.items()
                                  if k not in present and known.get(k) == e.doc])

    def _watchlist_wants(self, now: float) -> bool:
        return bool(self._jobs["watchlist"]) or (
            bool(self.cfg.watchlist) and self._due("watchlist", self.cfg.probe_interval_s, now))

    def _watchlist(self, sweep_id: int) -> None:
        jobs = self._jobs["watchlist"]
        now = self.clock()
        if not jobs and self._due("watchlist", self.cfg.probe_interval_s, now):
            self._last["watchlist"] = now
            names = self.store.municipality_ids(self.parent)
            by_id = {i: n for n, i in names.items()}
            for entry in self.cfg.watchlist:
                name, mid = (entry, names[entry]) if entry in names else (by_id.get(entry), entry)
                if name is None:
                    log.warning("watchlist entry %r matches no municipality", entry)
                    continue
                if self._only is not None and mid not in self._only.values():
                    continue  # outside the --only set
                jobs.append(_Job(Filters(municipality_id=mid), Scope(municipality=name), True, None))
        self._run_jobs(sweep_id, jobs)

    def _probe_wants(self, now: float) -> bool:
        return (self._round is not None or bool(self._jobs["probe"])
                or self._due("probe", self.cfg.probe_interval_s, now))

    def _probe(self, sweep_id: int) -> None:
        """Global RG-2 count; then either a full pass (cost guard) or a per-municipality round
        whose counts and changed-municipality sweeps continue across ticks."""
        jobs, full = self._jobs["probe"], Filters(doc_type=6)
        while not self._abort:
            if jobs:
                if not self._advance(sweep_id, jobs[0]):
                    return
                try:
                    self._finish(sweep_id, jobs[0])
                except Exception:
                    if self._round is not None:
                        self._round.ok = False  # do not store "rg2" for this round
                    raise
                jobs.pop(0)
                continue
            r = self._round
            if r is None:
                now = self.clock()
                if not self._due("probe", self.cfg.probe_interval_s, now) or self._budget < 2:
                    return
                self._last["probe"] = now
                if self._only is not None:
                    # No global listing: count each selected municipality every round.
                    self._progress["probe"] = True
                    self._round = _Round(0, list(self._only.items()))
                    continue
                total, max_page = self._count(full)
                self._progress["probe"] = True
                if total == self.store.get_count(self.parent, "rg2"):
                    return
                names = self.store.municipality_ids(self.parent)
                if self.cfg.probe_cost_guard and self._round_cost(total, names) >= max_page:
                    jobs.append(_Job(full, Scope(doc_type=6), False, 6, self._guard_done, total))
                else:
                    self._round = _Round(total, list(names.items()))
                continue
            if self._budget < 2:
                return
            if r.pending:
                name, mid = r.pending[0]
                try:
                    n, _ = self._count(Filters(doc_type=6, municipality_id=mid))
                except RikUnavailable:
                    raise
                except Exception:
                    log.exception("probe: municipality %s failed", name)
                    r.ok = False
                    r.pending.pop(0)
                    continue
                r.pending.pop(0)
                r.counts += n
                key, stored = f"rg2:m:{mid}", self.store.get_count(self.parent, f"rg2:m:{mid}")
                if n != stored and not (n == 0 and stored is None):
                    jobs.append(_Job(Filters(doc_type=6, municipality_id=mid),
                                     Scope(doc_type=6, municipality=name), True, 6,
                                     lambda j, r=r, key=key: self._muni_done(r, key, j), n))
                else:
                    self.store.set_count(self.parent, key, n)
                continue
            if self._only is not None:  # per-municipality counts are stored as they are checked
                self._round = None
                self._last["probe"] = self.clock()
                return
            total_after, _ = self._count(full)
            self._round = None
            self._last["probe"] = self.clock()  # next round no sooner than probe_interval_s from now
            self._finish_round(r, total_after)  # may queue a fallback pass: loop advances it

    def _round_cost(self, total: int, names: dict[str, str]) -> float:
        """Estimated requests of a per-municipality round: 2 counts per municipality plus a full
        sweep of each changed one. Changed municipalities ~ min(|change|, n); pages per sweep ~ the
        average of ceil(count / 10), using total / n for a municipality with no stored count (so a
        first run on an empty store picks the full pass), at least 1 page."""
        n = len(names)
        if not n:
            return 0
        change = abs(total - (self.store.get_count(self.parent, "rg2") or 0))
        counts = [self.store.get_count(self.parent, f"rg2:m:{mid}") for mid in names.values()]
        pages = [max(1, math.ceil((total / n if c is None else c) / PAGE_SIZE)) for c in counts]
        return 2 * n + min(change, n) * sum(pages) / n

    def _muni_done(self, r: _Round, key: str, job: _Job) -> None:
        if job.ok:
            self.store.set_count(self.parent, key, job.total)
        else:
            r.ok = False

    def _finish_round(self, r: _Round, total_after: int) -> None:
        if not r.ok:
            return
        names = self.store.municipality_ids(self.parent)
        # A sum between the two totals is a race with new uploads, not a shortfall.
        if r.counts < min(r.total, total_after) or self._unknown_names - set(names):
            # Stored municipalities cannot account for the docs (incomplete list or name mismatch).
            self._refresh_wanted = True
            self._shortfall_streak += 1
            if self._shortfall_streak >= 2:
                self._shortfall_streak = 0
                self._jobs["probe"].append(_Job(Filters(doc_type=6), Scope(doc_type=6), False, 6,
                                                self._full_done, total_after))
        else:
            # Count-neutral changes (one add + one removal) are caught by the safety sweep.
            self._shortfall_streak = 0
            self.store.set_count(self.parent, "rg2", r.total)

    def _guard_done(self, job: _Job) -> None:
        self._last["probe"] = self.clock()
        if not job.ok:
            return
        per = Counter(d.municipality for d in {d.station_key: d for d in job.docs}.values())
        for name, mid in self.store.municipality_ids(self.parent).items():
            self.store.set_count(self.parent, f"rg2:m:{mid}", per.get(name, 0))
        self.store.set_count(self.parent, "rg2", job.total)

    def _full_done(self, job: _Job) -> None:
        self._last["probe"] = self.clock()
        if job.ok:
            self.store.set_count(self.parent, "rg2", job.total)

    def _missing(self, sweep_id: int) -> None:
        self._last["missing"] = self.clock()
        have = {k[0] for k in self.store.known_docs(self.parent, Scope(doc_type=6))}
        missing = []
        for name, mid in (self._only or self.store.municipality_ids(self.parent)).items():
            for no, sid in self.store.station_ids(self.parent, mid).items():
                if f"{name}|{no}" not in have:
                    missing.append((f"{name}|{no}", mid, sid))
        self._missing_state = {k: v for k, v in self._missing_state.items()
                               if k in {m[0] for m in missing}}
        self._missing_work = bool(missing) and len(missing) <= self.cfg.missing_station_threshold
        if self._missing_work:
            now = self.clock()
            due = sorted((self._missing_state.get(m[0], (0.0, 0.0))[0], i)
                         for i, m in enumerate(missing)
                         if self._missing_state.get(m[0], (0.0, 0.0))[0] <= now)
            for _, i in due[:self.cfg.missing_max_per_tick]:
                key, mid, sid = missing[i]
                if self._abort or self._budget <= 0:
                    return
                delay = self._missing_state.get(key, (0.0, self.cfg.probe_interval_s / 2))[1]
                delay = min(MISSING_BACKOFF_CAP_S, delay * 2)
                self._missing_state[key] = (now + delay, delay)
                try:
                    page = self._listing(Filters(municipality_id=mid, station_no_id=sid), 1)
                    docs = [d for d in page.docs if d.station_key == key]
                    # Additions only: an empty answer is not evidence of removal.
                    self._emit(sweep_id, diff(
                        self.store.known_docs(self.parent, Scope(station_key=key)), docs, False))
                except RikUnavailable:
                    raise
                except Exception:
                    log.exception("missing lane: station %s failed", key)
        if "probe" not in self._baseline and not self._abort:
            self._progress["missing"] = True

    def _safety(self, sweep_id: int) -> None:
        if self._safety_job is None:
            self._safety_started = self.clock()
            if self._only is None:
                self._safety_job = _Job(Filters(doc_type=6), Scope(doc_type=6), True, 6)
            else:  # one sweep per selected municipality, diffed within that municipality only
                jobs = [_Job(Filters(doc_type=6, municipality_id=mid), Scope(doc_type=6, municipality=name),
                             True, 6) for name, mid in self._only.items()]
                self._safety_job, self._safety_rest = jobs[0], jobs[1:]
        while True:
            if not self._advance(sweep_id, self._safety_job):
                return
            self._finish(sweep_id, self._safety_job)
            if not self._safety_rest:
                break
            self._safety_job = self._safety_rest.pop(0)
        self._safety_job = None
        self._progress["safety"] = True
