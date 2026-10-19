import logging
import threading
import time
from collections import Counter
from collections.abc import Callable

from .config import Config
from .diff import diff
from .models import RARE_TYPES, Doc, Event, Filters, Scope, doc_key
from .rik_client import PAGE_SIZE, RikClient, RikUnavailable
from .store import Store

log = logging.getLogger("rg2watch.watcher")


class Watcher:
    def __init__(self, client: RikClient, store: Store, cfg: Config,
                 on_events: Callable[[list[Event]], None], clock=time.monotonic, sleep=time.sleep):
        self.client, self.store, self.cfg = client, store, cfg
        self.on_events, self.clock, self.sleep = on_events, clock, sleep
        self.parent = client.election.parent
        self._bootstrapped = False
        self._abort = False
        self._stats: dict[str, int] = {}
        # The safety sweep first fires one interval after start; other lanes fire immediately.
        self._last: dict[str, float] = {"safety": clock()}
        self._lanes = [
            ("rare", cfg.rare_interval_s, self._rare),
            ("watchlist", cfg.probe_interval_s, self._watchlist),
            ("probe", cfg.probe_interval_s, self._probe),
            ("missing", cfg.probe_interval_s, self._missing),
            ("safety", cfg.safety_sweep_interval_s, self._safety),
        ]

    def bootstrap(self) -> None:
        self.store.upsert_election(self.client.election)
        if not self.store.municipality_ids(self.parent):
            self.store.save_municipalities(self.parent, self.client.municipalities())
        for mid in self.store.municipality_ids(self.parent).values():
            if not self.store.station_ids(self.parent, mid):
                options = self.client.stations(mid)  # station id -> station no
                self.store.save_stations(self.parent, mid, {no: sid for sid, no in options.items()})
        self._bootstrapped = True

    def run_once(self) -> None:
        self._abort = False
        if not self._bootstrapped:
            try:
                self.bootstrap()
            except Exception as exc:
                log.warning("bootstrap failed: %r", exc)
                self._abort = True
        for name, interval, fn in self._lanes:
            if self._abort:
                break
            now = self.clock()
            last = self._last.get(name)
            if last is not None and now - last < interval:
                continue
            self._last[name] = now
            self._run_lane(name, fn)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_once()
            self.sleep(1)

    def _run_lane(self, name: str, fn) -> None:
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
        log.info("lane=%s requests=%d duration_s=%.1f events=%d", name, s["pages"],
                 self.clock() - start, s["added"] + s["replaced"] + s["removed"])

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
        """Page through a listing, diff against the store and emit. Returns (ok, observed docs).
        A page that fails leaves the sweep incomplete: additions still apply, removals do not."""
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
        if doc_type is not None:
            docs = [d for d in docs if d.doc_type == doc_type]
        events = diff(self.store.known_docs(self.parent, scope), docs, complete and ok)
        self._emit(sweep_id, self._confirm_removed(events))
        return ok, docs

    def _confirm_removed(self, events: list[Event]) -> list[Event]:
        """Drop removal candidates unless a direct per-station query also lacks the document."""
        names = self.store.municipality_ids(self.parent)
        direct: dict[str, set] = {}
        out = []
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
                    self._abort = True
                    break
                except Exception:
                    log.exception("removal re-check failed for %s", d.station_key)
                    continue
                direct[d.station_key] = {doc_key(x) for x in page.docs}
            if doc_key(d) not in direct[d.station_key]:
                out.append(ev)
        return out

    def _emit(self, sweep_id: int, events: list[Event]) -> None:
        if not events:
            return
        self.store.apply_events(self.parent, sweep_id, events)
        for ev in events:
            self._stats[ev.kind] += 1
        self.on_events(events)

    # lanes ----------------------------------------------------------------
    def _rare(self, sweep_id: int) -> None:
        safety_due = self.clock() - self._last["safety"] >= self.cfg.safety_sweep_interval_s
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
        full = Filters(doc_type=6)
        total, max_page = self._count(full)
        if total == self.store.get_count(self.parent, "rg2"):
            return
        names = self.store.municipality_ids(self.parent)
        if self.cfg.probe_cost_guard and 2 * len(names) >= max_page:
            ok, docs = self._sweep(sweep_id, full, Scope(doc_type=6), complete=False, doc_type=6)
            if ok:
                per = Counter(d.municipality for d in {d.station_key: d for d in docs}.values())
                for name, mid in names.items():
                    self.store.set_count(self.parent, f"rg2:m:{mid}", per.get(name, 0))
                self.store.set_count(self.parent, "rg2", total)
            return
        all_ok = True
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
            except RikUnavailable:
                raise
            except Exception:
                log.exception("probe: municipality %s failed", name)
                all_ok = False
        if all_ok:
            self.store.set_count(self.parent, "rg2", total)

    def _missing(self, sweep_id: int) -> None:
        have = {k[0] for k in self.store.known_docs(self.parent, Scope(doc_type=6))}
        missing = []
        for name, mid in self.store.municipality_ids(self.parent).items():
            for no, sid in self.store.station_ids(self.parent, mid).items():
                if f"{name}|{no}" not in have:
                    missing.append((f"{name}|{no}", mid, sid))
        if not missing or len(missing) > self.cfg.missing_station_threshold:
            return
        for key, mid, sid in missing:
            if self._abort:
                break
            try:
                page = self._listing(Filters(municipality_id=mid, station_no_id=sid), 1)
                docs = [d for d in page.docs if d.station_key == key]
                self._emit(sweep_id, diff(self.store.known_docs(self.parent, Scope(station_key=key)),
                                          docs, True))
            except RikUnavailable:
                raise
            except Exception:
                log.exception("missing lane: station %s failed", key)

    def _safety(self, sweep_id: int) -> None:
        self._sweep(sweep_id, Filters(doc_type=6), Scope(doc_type=6), doc_type=6)
