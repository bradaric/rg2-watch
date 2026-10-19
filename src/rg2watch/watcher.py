import logging
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
BASELINE_LANES = ("rare", "watchlist", "probe", "missing", "safety")


@dataclass
class _Pass:
    """A full listing sweep spread over ticks, at most the per-tick page budget at a time."""
    kind: str  # "guard" / "fallback" (probe, complete=False) or "safety" (complete=True)
    filters: Filters
    scope: Scope
    complete: bool
    doc_type: int
    total: int | None = None  # probe: RG-2 count to store once the pass succeeds
    next_page: int = 1
    max_page: int | None = None
    ok: bool = True
    docs: list[Doc] = field(default_factory=list)
    touched: set = field(default_factory=set)  # keys changed by other lanes since the pass began


def _rg2_pass(kind: str, total: int | None = None) -> _Pass:
    return _Pass(kind, Filters(doc_type=6), Scope(doc_type=6), kind == "safety", 6, total)


def _type_name(d: Doc) -> str:
    return DOC_TYPES.get(d.doc_type, d.label) if d.doc_type is not None else d.label


class Watcher:
    def __init__(self, client: RikClient, store: Store, cfg: Config,
                 on_events: Callable[[list[Event]], None], clock=time.monotonic, sleep=time.sleep,
                 initial_safety: bool = False, on_status: Callable[[str], None] | None = None,
                 baseline: bool = False):
        self.client, self.store, self.cfg = client, store, cfg
        self.on_events, self.clock, self.sleep = on_events, clock, sleep
        self.on_status = on_status
        self.parent = client.election.parent
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
        self._pages_left = 0
        self._probe_pass: _Pass | None = None
        self._safety_pass: _Pass | None = None
        # The first safety pass starts one interval after start, or at once when it is the baseline.
        early = initial_safety or baseline
        self._safety_started = self._started - (cfg.safety_sweep_interval_s if early else 0)
        self._baseline = set(BASELINE_LANES) if baseline else set()
        self._baseline_counts: Counter[str] = Counter()
        self._last: dict[str, float] = {}
        self._lanes = [
            ("rare", cfg.rare_interval_s, self._rare),
            ("watchlist", cfg.probe_interval_s, self._watchlist),
            ("probe", cfg.probe_interval_s, self._probe),
            ("probe_sweep", 0, self._probe_sweep),
            ("missing", cfg.probe_interval_s, self._missing),
            ("safety", cfg.safety_sweep_interval_s, self._safety),
        ]

    def bootstrap(self, refresh: bool = False) -> None:
        self.store.upsert_election(self.client.election)
        if refresh or not self.store.municipality_ids(self.parent):
            self.store.save_municipalities(self.parent, self.client.municipalities())
        for mid in self.store.municipality_ids(self.parent).values():
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
        self._pages_left = max(1, self.cfg.sweep_pages_per_tick)
        for name, interval, fn in self._lanes:
            if self._abort:
                break
            now = self.clock()
            if name == "probe_sweep":
                due = self._probe_pass is not None
            elif name == "safety":
                due = self._safety_pass is not None or self._safety_due(now)
            else:
                last = self._last.get(name)
                due = last is None or now - last >= interval
                if due:
                    self._last[name] = now
            if due:
                self._run_lane(name, fn)

    def _safety_due(self, now: float) -> bool:
        # Measured from the previous pass's start, so long passes run back to back.
        return now - self._safety_started >= self.cfg.safety_sweep_interval_s

    def _run_lane(self, name: str, fn) -> None:
        self._lane = name
        self._stats = {"pages": 0, "records": 0, "added": 0, "replaced": 0, "removed": 0}
        sweep_id = self.store.start_sweep(self.parent, name)
        start = self.clock()
        failed = False
        try:
            fn(sweep_id)
        except RikUnavailable as exc:
            log.warning("lane %s: RIK unavailable, skipping rest of tick: %s", name, exc)
            self._abort = True
        except Exception:
            log.exception("lane %s failed", name)
            failed = True
        s = self._stats
        self.store.finish_sweep(sweep_id, **s)
        log.info("lane=%s requests=%d duration_s=%.1f events=%d", name, s["pages"],
                 self.clock() - start, s["added"] + s["replaced"] + s["removed"])
        if self._baseline and not self._abort and not failed:
            self._baseline_lane_done(name)

    # health -----------------------------------------------------------------
    def _check_health(self) -> None:
        now, c = self.clock(), self.client
        if c.blocked_streak >= BLOCKED_STREAK:
            c.blocked_streak = 0
            self._paused_until = now + self.cfg.blocked_pause_s
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
    def _baseline_lane_done(self, name: str) -> None:
        lane = "probe" if name == "probe_sweep" else name
        if lane == "probe" and self._probe_pass is not None:
            return
        if lane == "safety" and self._safety_pass is not None:
            return
        if lane == "missing" and "probe" in self._baseline:
            return  # stations only count as missing once the probe has recorded the RG-2s
        if lane not in self._baseline:
            return
        self._baseline.discard(lane)
        if self._baseline:
            return
        for code, n in sorted(self._baseline_counts.items()):
            print(f"[baseline] {code}: {n} document(s) recorded", flush=True)
        if not self._baseline_counts:
            print("[baseline] no documents recorded", flush=True)

    # requests -------------------------------------------------------------
    def _listing(self, filters: Filters, page: int):
        self._stats["pages"] += 1
        result = self.client.listing(filters, page)
        self._stats["records"] += result.records
        return result

    def _count(self, filters: Filters) -> tuple[int, int]:
        """Returns (record count, max_page)."""
        first = self._listing(filters, 1)
        if first.max_page <= 1:
            return first.records, first.max_page
        last = self._listing(filters, first.max_page)
        return (first.max_page - 1) * PAGE_SIZE + last.records, first.max_page

    def _sweep(self, sweep_id: int, filters: Filters, scope: Scope, complete: bool = True,
               doc_type: int | None = None) -> tuple[bool, list[Doc]]:
        """Page through a (small, filtered) listing in one go, diff against the store and emit.
        Returns (ok, observed docs). A page that fails leaves the sweep incomplete: additions
        still apply, removals do not."""
        first = self._listing(filters, 1)
        docs, ok = list(first.docs), True
        for page in range(2, first.max_page + 1):
            try:
                docs += self._listing(filters, page).docs
            except RikUnavailable:
                self._abort, ok = True, False
                break
            except Exception:
                log.exception("page %d of %s failed", page, filters)
                ok = False
        confirmed, docs = self._process(sweep_id, docs, scope, complete and ok, doc_type)
        return ok and confirmed, docs

    def _process(self, sweep_id: int, docs: list[Doc], scope: Scope, complete: bool,
                 doc_type: int | None, exclude: set = frozenset()) -> tuple[bool, list[Doc]]:
        """Diff observed docs against the store, confirm removals, emit. Returns (confirmed, docs)."""
        if doc_type is not None:
            docs = [d for d in docs if d.doc_type == doc_type]
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
        events, confirmed = self._confirm_removed(events)
        self._emit(sweep_id, events)
        return confirmed, docs

    def _advance(self, sweep_id: int, p: _Pass, limit: int) -> bool:
        """Fetch up to `limit` more pages of a pass. Returns True once the pass is finished.
        Incomplete passes emit additions/replacements per batch; RikUnavailable retries the page later."""
        batch: list[Doc] = []
        for _ in range(max(0, limit)):
            if p.max_page is not None and p.next_page > p.max_page:
                break
            self._pages_left -= 1
            try:
                res = self._listing(p.filters, p.next_page)
            except RikUnavailable:
                self._abort = True
                break
            except Exception:
                log.exception("page %d of %s failed", p.next_page, p.filters)
                p.ok = False
                if p.max_page is None:
                    p.max_page = 0  # no page count known: give the pass up
                p.next_page += 1
                continue
            p.max_page = res.max_page if p.max_page is None else max(p.max_page, res.max_page)
            p.next_page += 1
            batch += res.docs
        batch = [d for d in batch if d.doc_type == p.doc_type]
        if batch and not p.complete:
            self._process(sweep_id, batch, p.scope, False, p.doc_type)
        p.docs += batch
        return p.max_page is not None and p.next_page > p.max_page

    def _confirm_removed(self, events: list[Event]) -> tuple[list[Event], bool]:
        """Drop removal candidates unless a direct per-station query also lacks the document."""
        names = self.store.municipality_ids(self.parent)
        direct: dict[str, set] = {}
        out, ok = [], True
        for ev in events:
            if ev.kind != "removed":
                out.append(ev)
                continue
            d = ev.doc
            mid = names.get(d.municipality)
            sid = self.store.station_ids(self.parent, mid).get(d.station_no) if mid else None
            if sid is None:
                log.warning("cannot confirm removal at %s: station ids unknown", d.station_key)
                continue
            if d.station_key not in direct:
                try:
                    page = self._listing(Filters(municipality_id=mid, station_no_id=sid), 1)
                except RikUnavailable:
                    self._abort, ok = True, False
                    break
                except Exception:
                    log.exception("removal re-check failed for %s", d.station_key)
                    ok = False
                    continue
                direct[d.station_key] = {doc_key(x) for x in page.docs}
            if doc_key(d) not in direct[d.station_key]:
                out.append(ev)
        return out, ok

    def _emit(self, sweep_id: int, events: list[Event]) -> None:
        if not events:
            return
        self.store.apply_events(self.parent, sweep_id, events)
        for ev in events:
            self._stats[ev.kind] = self._stats.get(ev.kind, 0) + 1
        if self._safety_pass is not None:
            self._safety_pass.touched.update(doc_key(ev.doc) for ev in events)
        lane = "probe" if self._lane == "probe_sweep" else self._lane
        if lane in self._baseline:
            self._baseline_counts.update(_type_name(ev.doc) for ev in events if ev.kind != "removed")
            return
        self.on_events(events)

    # lanes ----------------------------------------------------------------
    def _rare(self, sweep_id: int) -> None:
        safety_due = self._safety_pass is None and self._safety_due(self.clock())
        for t in RARE_TYPES:
            if self._abort:
                break
            try:
                n, _ = self._count(Filters(doc_type=t))
                key, stored = f"type:{t}", self.store.get_count(self.parent, f"type:{t}")
                if (n != stored or (safety_due and n > 0)) and not (n == 0 and stored is None):
                    ok, _ = self._sweep(sweep_id, Filters(doc_type=t), Scope(doc_type=t), doc_type=t)
                    if not ok:
                        continue
                self.store.set_count(self.parent, key, n)
            except RikUnavailable:
                raise
            except Exception:
                log.exception("rare lane: type %d failed", t)

    def _watchlist(self, sweep_id: int) -> None:
        names = self.store.municipality_ids(self.parent)
        for entry in self.cfg.watchlist:
            if self._abort:
                break
            by_id = {i: n for n, i in names.items()}
            name, mid = (entry, names[entry]) if entry in names else (by_id.get(entry), entry)
            if name is None:
                log.warning("watchlist entry %r matches no municipality", entry)
                continue
            self._sweep(sweep_id, Filters(municipality_id=mid), Scope(municipality=name))

    def _probe(self, sweep_id: int) -> None:
        if self._probe_pass is not None and self._probe_pass.kind == "guard":
            return  # the full pass in progress already covers every RG-2 page
        full = Filters(doc_type=6)
        total, max_page = self._count(full)
        if total == self.store.get_count(self.parent, "rg2"):
            return
        names = self.store.municipality_ids(self.parent)
        if self.cfg.probe_cost_guard and 2 * len(names) >= max_page:
            if self._probe_pass is None:
                self._probe_pass = _rg2_pass("guard", total)
            return
        # Count-neutral changes (one add + one removal) are caught by the safety sweep.
        all_ok, counts = True, 0
        for name, mid in names.items():
            if self._abort:
                return
            try:
                n, _ = self._count(Filters(doc_type=6, municipality_id=mid))
                key, stored = f"rg2:m:{mid}", self.store.get_count(self.parent, f"rg2:m:{mid}")
                if n != stored and not (n == 0 and stored is None):
                    ok, _ = self._sweep(sweep_id, Filters(doc_type=6, municipality_id=mid),
                                        Scope(doc_type=6, municipality=name), doc_type=6)
                    if not ok:
                        all_ok = False
                        continue
                self.store.set_count(self.parent, key, n)
                counts += n
            except RikUnavailable:
                raise
            except Exception:
                log.exception("probe: municipality %s failed", name)
                all_ok = False
        if not all_ok:
            return
        total_after, _ = self._count(full)
        # A sum between the two totals is a race with new uploads, not a shortfall.
        if counts < min(total, total_after) or self._unknown_names - set(names):
            # Stored municipalities cannot account for the docs (incomplete list or name mismatch).
            self._refresh_wanted = True
            if self._probe_pass is None:
                self._shortfall_streak += 1
                if self._shortfall_streak >= 2:
                    self._shortfall_streak = 0
                    self._probe_pass = _rg2_pass("fallback", total_after)
        else:
            self._shortfall_streak = 0
            self.store.set_count(self.parent, "rg2", total)

    def _probe_sweep(self, sweep_id: int) -> None:
        p = self._probe_pass
        limit = self._pages_left
        if self._safety_pass is not None or self._safety_due(self.clock()):
            limit = (limit + 1) // 2  # leave the rest of the budget to the safety pass
        if not self._advance(sweep_id, p, limit):
            return
        self._probe_pass = None
        if not p.ok:
            return
        if p.kind == "guard":
            per = Counter(d.municipality for d in {d.station_key: d for d in p.docs}.values())
            for name, mid in self.store.municipality_ids(self.parent).items():
                self.store.set_count(self.parent, f"rg2:m:{mid}", per.get(name, 0))
        self.store.set_count(self.parent, "rg2", p.total)

    def _missing(self, sweep_id: int) -> None:
        have = {k[0] for k in self.store.known_docs(self.parent, Scope(doc_type=6))}
        missing = []
        for name, mid in self.store.municipality_ids(self.parent).items():
            for no, sid in self.store.station_ids(self.parent, mid).items():
                if f"{name}|{no}" not in have:
                    missing.append((f"{name}|{no}", mid, sid))
        self._missing_state = {k: v for k, v in self._missing_state.items()
                               if k in {m[0] for m in missing}}
        if not missing or len(missing) > self.cfg.missing_station_threshold:
            return
        now = self.clock()
        due = sorted((self._missing_state.get(m[0], (0.0, 0.0))[0], i) for i, m in enumerate(missing)
                     if self._missing_state.get(m[0], (0.0, 0.0))[0] <= now)
        for _, i in due[:self.cfg.missing_max_per_tick]:
            key, mid, sid = missing[i]
            if self._abort:
                break
            delay = self._missing_state.get(key, (0.0, self.cfg.probe_interval_s / 2))[1]
            delay = min(MISSING_BACKOFF_CAP_S, delay * 2)
            self._missing_state[key] = (now + delay, delay)
            try:
                page = self._listing(Filters(municipality_id=mid, station_no_id=sid), 1)
                docs = [d for d in page.docs if d.station_key == key]
                # Additions only: an empty answer is not evidence of removal.
                self._emit(sweep_id, diff(self.store.known_docs(self.parent, Scope(station_key=key)),
                                          docs, False))
            except RikUnavailable:
                raise
            except Exception:
                log.exception("missing lane: station %s failed", key)

    def _safety(self, sweep_id: int) -> None:
        if self._safety_pass is None:
            self._safety_started = self.clock()
            self._safety_pass = _rg2_pass("safety")
        p = self._safety_pass
        if not self._advance(sweep_id, p, self._pages_left):
            return
        self._safety_pass = None
        # Keys another lane changed after this pass saw them hold stale observations: skip them.
        self._process(sweep_id, p.docs, p.scope, p.ok, 6, exclude=p.touched)
