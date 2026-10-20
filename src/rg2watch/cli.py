import argparse
import dataclasses
import json
import logging
import math
import signal
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .analyze import AnalysisError, OllamaUnavailable, analyze_document, is_ollama, stored_model
from .batch import ADHOC, describe_estimate, run_batch, select_forms
from .browse import (build_page, copy_static, default_parent, record_review, review_record,
                     reviews_payload, write_status)
from .config import Config, load_config
from .evaluate import OUTPUT_TOKENS, TOKENS_PER_TILE, estimate_cost, format_table, load_labels
from .evaluate import PARENT as EVAL_PARENT
from .evaluate import run_eval
from .fetch import fetch_pdf
from .findings import findings_for
from .models import Doc, Event, Finding
from .names import MunicipalityError, resolve_municipalities
from .notify import Notifier
from .pipeline import PROMPT_VERSION, AnalysisPipeline
from .render import RenderError, render_tiles
from .replay import fetch_fixtures, make_replay_server
from .rik_client import USER_AGENT, AdaptiveLimiter, RikClient, RikError, download_limiter_for
from .rules import evaluate_report
from .server import start_server
from .status import to_belgrade
from .store import NOTE_MAX, VERDICTS, Store
from .tally import build_tally, format_tally
from .watcher import Watcher, filter_request_rate

log = logging.getLogger("rg2watch.cli")


def _positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {text}")
    return n


def _port(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text}") from None
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 0-65535: {text}")
    return n


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="path to rg2watch.toml")
    common.add_argument("--db", type=Path, help="SQLite path (overrides config db_path)")
    common.add_argument("--verbose", "-v", action="store_true", help="debug logging")

    p = argparse.ArgumentParser(prog="rg2watch", description="RIK election-night document watcher")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", parents=[common], help="register an election from its zapisnici URL")
    s.add_argument("url")
    s.add_argument("--base-url")

    s = sub.add_parser("watch", parents=[common], help="live diff loop")
    s.add_argument("url", nargs="?", help="zapisnici URL (alternative to --parent after add)")
    s.add_argument("--parent", help="election id registered with `add`")
    s.add_argument("--base-url", help="override the RIK base URL")
    s.add_argument("--once", action="store_true",
                   help="bootstrap, run every lane once (full passes, no page budget), exit")
    s.add_argument("--only", help="comma-separated municipality names (Cyrillic or Latin): watch "
                   "only these (config only_municipalities)")
    s.add_argument("--baseline", action="store_true",
                   help="record what is already published silently on each lane's first full pass")
    s.add_argument("--serve-port", type=_port, help="port of the local web server (config serve_port, "
                   "default 8770; 0 = off)")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--analyze", action="store_true",
                      help="analyse new RG-2 forms with Claude (paid API; also config "
                           "analysis_enabled = true)")
    mode.add_argument("--no-analyze", action="store_true",
                      help="do not analyse, even if the config enables it")
    s.add_argument("--model", help="override config model (ollama:<name> for a local model)")
    s.add_argument("--analyze-backlog", action="store_true",
                   help="with analysis on, also analyse forms seen before analysis was enabled")

    s = sub.add_parser("analyze", parents=[common],
                       help="analyse one RG-2 PDF (local path or URL of a registered election)")
    s.add_argument("source", nargs="?", help="local PDF path or document URL")
    s.add_argument("--parent", help="batch mode: re-analyse stored forms of this election")
    s.add_argument("--municipality", help="batch: comma-separated municipality names")
    s.add_argument("--station", help="batch: comma-separated station numbers")
    s.add_argument("--rule", help="batch: forms with a stored finding of these rules (comma-separated)")
    s.add_argument("--severity", type=str.upper, choices=["LOW", "MEDIUM", "HIGH"],
                   help="batch: forms with a stored finding of at least this severity")
    s.add_argument("--state", choices=["current", "replaced", "removed"], help="batch: form state")
    s.add_argument("--limit", type=_positive_int, help="batch: only the first N selected forms")
    s.add_argument("--yes", action="store_true", help="batch: confirm the estimated API spend")
    s.add_argument("--max-usd", type=float, help="batch: stop at this spend (default max_spend_usd)")
    s.add_argument("--json", action="store_true", help="print report and findings as JSON")
    s.add_argument("--model", help="override config model (ollama:<name> for a local model)")

    s = sub.add_parser("eval", parents=[common],
                       help="score the rules against hand labels on local PDFs")
    s.add_argument("--labels", type=Path, required=True, help="labels JSONL (tests/golden/labels.jsonl)")
    s.add_argument("--pdf-dir", type=Path, required=True, help="directory holding the labelled PDFs")
    s.add_argument("--limit", type=_positive_int, help="only the first N labelled forms (N >= 1)")
    s.add_argument("--model", help="override config model (ollama:<name> for a local model)")
    s.add_argument("--cached", action="store_true",
                   help="use stored analyses only; never calls the API")
    s.add_argument("--yes", action="store_true", help="confirm the estimated API spend")
    s.add_argument("--force-over-cap", action="store_true",
                   help="run even though the estimate exceeds what is left of max_spend_usd "
                        "(the run still stops at the cap)")

    s = sub.add_parser("requeue", parents=[common],
                       help="reset documents whose analysis failed after all attempts to new")
    s.add_argument("--parent", help="only this election")
    s.add_argument("--failed", action="store_true",
                   help="reset every failed document, also unreadable PDFs (D3) and refusals (X1)")
    s.add_argument("--skipped", action="store_true",
                   help="also queue RG-2 forms skipped because they were seen before analysis "
                        "was enabled")

    s = sub.add_parser("browse", parents=[common], help="write the local HTML page of RG-2 forms")
    s.add_argument("--parent", help="only this election")
    s.add_argument("--out", type=Path, help="HTML path (default: index.html next to the DB)")

    s = sub.add_parser("tally", parents=[common],
                       help="unofficial totals read by the model from the stored forms")
    s.add_argument("--parent", help="election (default: the newest registered)")
    s.add_argument("--municipality", help="comma-separated municipality names")
    s.add_argument("--model", help="model whose readings are counted (default: config model)")
    s.add_argument("--clean", action="store_true", help="show totals over clean forms only "
                   "(default: all counted forms)")

    s = sub.add_parser("review", parents=[common], help="record or list operator verdicts on forms")
    s.add_argument("url", nargs="?", help="document URL (one version of a form)")
    s.add_argument("verdict", nargs="?", choices=VERDICTS)
    s.add_argument("--note", help=f"optional note (at most {NOTE_MAX} characters)")
    s.add_argument("--list", choices=("escalated", "ok", "unsure", "todo"),
                   help="list forms by newest verdict, or todo: findings and no verdict")
    s.add_argument("--parent", help="only this election (with --list)")

    s = sub.add_parser("labels", parents=[common], help="eval label suggestions from verdicts")
    s.add_argument("--from-reviews", action="store_true",
                   help="the source (the only one, also the default): ok -> fired rules false, "
                   "escalate -> fired rules true")
    s.add_argument("--out", type=Path, required=True, help="new JSONL file (never overwritten)")
    s.add_argument("--parent", help="only this election")

    s = sub.add_parser("serve", parents=[common], help="serve the browse page on 127.0.0.1 (no watching)")
    s.add_argument("--port", type=_port, help="port (config serve_port, default 8770)")

    s = sub.add_parser("replay", parents=[common], help="serve fixtures as a fake RIK")
    s.add_argument("--fixtures", type=Path, required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--speed", default="60", help="simulated seconds per real second, e.g. 60 or 60x")
    s.add_argument("--faults", action="store_true", help="inject replace/remove/RG-9 faults")

    s = sub.add_parser("fixtures", parents=[common], help="download listing (and PDFs) once")
    s.add_argument("url")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--pdfs", action="store_true")
    s.add_argument("--limit-pdfs", type=int)
    s.add_argument("--max-rps", type=float, default=1.0)
    s.add_argument("--base-url")
    return p


def _config(args) -> Config:
    cfg = load_config(args.config)
    if args.db is not None:
        cfg.db_path = args.db
    if getattr(args, "model", None):
        cfg.model = args.model
    if getattr(args, "base_url", None):
        cfg.base_url = args.base_url
    return cfg


def _open_store(cfg: Config) -> Store:
    if str(cfg.db_path) != ":memory:":
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    return Store(cfg.db_path)


def _cmd_add(args) -> int:
    cfg = _config(args)
    try:
        client = RikClient.from_url(args.url, cfg)
    except (RikError, httpx.HTTPError) as exc:
        print(f"cannot reach {args.url}: {exc}", file=sys.stderr)
        return 1
    store = _open_store(cfg)
    try:
        store.upsert_election(client.election)
    finally:
        store.close()
    e = client.election
    print(f"parent={e.parent} round={e.round} title={e.title}")
    return 0


def _notifier_http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0), headers={"User-Agent": USER_AGENT})


def _analysis_client(cfg: Config):
    """(client, None) for cfg.model's backend: an httpx client for ollama:, else Anthropic."""
    if is_ollama(cfg.model):
        return httpx.Client(), None
    return _anthropic_client()


def _anthropic_client():
    """(client, None) when Anthropic credentials are available, else (None, reason)."""
    try:
        import anthropic

        client = anthropic.Anthropic()
    except Exception as exc:
        return None, f"cannot create Anthropic client: {exc}"
    if client.api_key is None and client.auth_token is None and client.credentials is None:
        return None, "no Anthropic credentials (set ANTHROPIC_API_KEY or configure an ant profile)"
    return client, None


def _cache_dir(cfg: Config) -> Path:
    db = str(cfg.db_path)
    return (Path("data") if db == ":memory:" else Path(db).parent) / "pdfs"


def _browse_path(cfg: Config) -> Path:
    db = str(cfg.db_path)
    return (Path("data") if db == ":memory:" else Path(db).parent) / "index.html"


def _cmd_browse(args) -> int:
    cfg = _config(args)
    store = _open_store(cfg)
    try:
        out = args.out or _browse_path(cfg)
        n = build_page(store, out, _cache_dir(cfg), args.parent, cfg)
    finally:
        store.close()
    print(f"{out} ({n} form(s))")
    return 0


STATUS_EVERY_S = 10


def _live(watcher: Watcher, pipeline, notifier: Notifier, url: str | None):
    def live() -> dict:
        d = {"watcher": watcher.status_snapshot(), "ntfy": notifier.status_snapshot(), "url": url}
        if pipeline is not None:
            d["analysis"] = pipeline.status_snapshot()
        return d
    return live


def _background(name: str, stop: threading.Event, period: float, fn, first=None) -> threading.Thread:
    """Run fn every `period` s in a daemon thread; failures are logged and swallowed, so the watch
    loop never waits for (or is hurt by) page building."""
    def run() -> None:
        if first is not None:
            try:
                first()
            except Exception:
                log.exception("%s setup failed", name)
        while not stop.is_set():
            try:
                fn()
            except Exception:
                log.exception("%s update failed", name)
            stop.wait(period)

    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t


def _cmd_tally(args) -> int:
    cfg = _config(args)
    store = _open_store(cfg)
    try:
        parent = default_parent(store, args.parent)
        only = None
        if args.municipality:
            known = {m: "" for m in store.tally_municipalities(parent)}
            try:
                only = set(resolve_municipalities(known, _csv(args.municipality) or []))
            except MunicipalityError as exc:
                print(exc, file=sys.stderr)
                for name in exc.candidates:
                    print(f"  {name}", file=sys.stderr)
                return 2
        t = build_tally(store, cfg, parent, stored_model(cfg.model), only)
    finally:
        store.close()
    print(format_tally(t, not args.clean))
    return 0


def _cmd_serve(args) -> int:
    cfg = _config(args)
    port = cfg.serve_port if args.port is None else args.port
    if port == 0:
        print("serve needs a port (--port or serve_port > 0)", file=sys.stderr)
        return 2
    out = _browse_path(cfg)
    copy_static(out.parent)
    store = _open_store(cfg)  # stays open: POST /review writes verdicts
    try:
        if not (out.parent / "data.json").is_file():
            build_page(store, out, _cache_dir(cfg), cfg=cfg)
        server = start_server(out.parent, _cache_dir(cfg), port, *_review_api(store))
        if server is None:
            return 1
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            log.info("interrupted, shutting down")
        finally:
            server.shutdown()
            server.server_close()
        return 0
    finally:
        store.close()


def _cmd_review(args) -> int:
    if args.list is None and (args.url is None or args.verdict is None):
        print("review needs <url> ok|escalate|unsure, or --list escalated|ok|unsure|todo", file=sys.stderr)
        return 2
    if args.list is not None and args.url is not None:
        print("review: give either <url> <verdict> or --list, not both", file=sys.stderr)
        return 2
    store = _open_store(_config(args))
    try:
        if args.list is None:
            try:
                rec = record_review(store, args.url, args.verdict, args.note)
            except ValueError as exc:
                print(f"review: {exc}", file=sys.stderr)
                return 2
            if rec is None:
                print(f"not a stored document: {args.url}", file=sys.stderr)
                return 1
            print(f"{rec['v']} recorded {rec['at']} for {args.url}")
            return 0
        if args.list == "todo":
            rows = [(to_belgrade(r["first_seen_at"]), "todo", r) for r in store.review_todo(args.parent)]
        else:
            want = {"escalated": "escalate"}.get(args.list, args.list)
            rows = [(review_record(r)["at"], r["verdict"] + (" (superseded)" if r["superseded"] else
                     " (removed)" if not r["active"] else ""), r)
                    for r in store.latest_reviews(args.parent) if r["verdict"] == want]
        for at, verdict, r in rows:
            note = r["note"] if "note" in r.keys() and r["note"] else ""
            print("  ".join(x for x in ((at or "-").replace("T", " "), verdict,
                                        f"{r['municipality'] or '(unknown)'} BM {r['station_no'] or '?'}",
                                        r["url"], note) if x))
        print(f"{len(rows)} form(s)")
        return 0
    finally:
        store.close()


def _cmd_labels(args) -> int:
    """Label suggestions in tests/golden/labels.jsonl format, for the operator to check and merge."""
    if args.out.exists():
        print(f"{args.out} exists: choose a new file (it is never overwritten)", file=sys.stderr)
        return 2
    store = _open_store(_config(args))
    try:
        reviews = [r for r in store.latest_reviews(args.parent) if r["verdict"] in ("ok", "escalate")]
        rules = store.finding_rules([r["document_id"] for r in reviews])
    finally:
        store.close()
    lines = []
    for r in reversed(reviews):  # oldest first
        fired = rules.get(r["document_id"], [])
        if not fired:
            continue
        note = f"from review: {r['verdict']} {review_record(r)['at']}" + (f"; {r['note']}" if r["note"] else "")
        lines.append(json.dumps({
            "file": unquote(urlsplit(r["url"]).path.rsplit("/", 1)[-1]),
            "municipality": r["municipality"] or "", "station_no": r["station_no"] or "",
            "expect": {rule: r["verdict"] == "escalate" for rule in fired},
            "verified": False, "note": note, "url": r["url"]}, ensure_ascii=False))
    if not lines:
        print("no ok/escalate verdicts on forms with findings: nothing written")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as fh:  # "x": never replaces a file
        fh.write("".join(line + "\n" for line in lines))
    print(f"{len(lines)} label suggestion(s) written to {args.out} (verified: false - check, then merge)")
    return 0


def _review_api(store: Store) -> tuple:
    """(get_reviews, put_review) for the web server: the server module holds no SQL."""
    return (lambda: reviews_payload(store),
            lambda url, verdict, note: record_review(store, url, verdict, note))


def _handlers(store: Store, notifier: Notifier, parent: str, pipeline=None):
    def deliver() -> None:
        """Called once per tick by the watch loop."""
        try:
            store.mark_alerted(notifier.send(store.unalerted(parent, {"HIGH", "MEDIUM", "LOW"})))
        except Exception:
            log.exception("alert delivery failed; retrying next tick")

    def on_events(events: list[Event]) -> None:
        try:
            found = findings_for(events)
        except Exception:
            log.exception("findings_for failed for %d event(s)", len(events))
            found = []
            for ev in events:  # one bad event must not drop the rest
                try:
                    found.extend(findings_for([ev]))
                except Exception:
                    log.error("finding lost: %s %s BM %s %s", ev.kind, ev.doc.municipality,
                              ev.doc.station_no, ev.doc.url, exc_info=True)
        for f in found:
            _store_finding(store, parent, f)
        if pipeline is not None:
            try:
                pipeline.submit(events)
            except Exception:
                log.exception("analysis queueing failed for %d event(s)", len(events))

    return on_events, deliver


def _store_finding(store: Store, parent: str, f: Finding) -> None:
    try:
        store.add_finding(parent, f, f.doc.url)
    except Exception:
        log.error("finding not stored: %s %s %s BM %s %s", f.rule, f.severity, f.doc.municipality,
                  f.doc.station_no, f.doc.url, exc_info=True)


def _cmd_watch(args) -> int:
    cfg = _config(args)
    if args.once:
        cfg.tick_request_budget = 10**9  # a single tick must finish every pass
    store = _open_store(cfg)
    try:
        if args.url:
            client = RikClient.from_url(args.url, cfg)
        elif args.parent:
            stored = store.get_election(args.parent)
            if stored is None:
                print(f"unknown election {args.parent}; run `rg2watch add <zapisnici-url>` first",
                      file=sys.stderr)
                return 2
            if args.base_url:
                stored = dataclasses.replace(stored, base_url=args.base_url.rstrip("/"))
            http = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)
            client = RikClient(stored, AdaptiveLimiter(cfg.max_rps), http, cfg.request_timeout_s,
                               cfg.max_pdf_bytes, download_limiter_for(cfg))
        else:
            print("watch needs a zapisnici URL or --parent", file=sys.stderr)
            return 2
        parent = client.election.parent
        only = None
        wanted = args.only.split(",") if args.only is not None else cfg.only_municipalities
        if args.only is not None and not any(n.strip() for n in wanted):
            print("--only needs at least one municipality name", file=sys.stderr)
            return 2
        if wanted:
            store.upsert_election(client.election)
            if not store.municipality_ids(parent):
                try:
                    store.save_municipalities(parent, client.municipalities())
                except (RikError, httpx.HTTPError) as exc:
                    print(f"cannot fetch the municipality list: {exc}", file=sys.stderr)
                    return 1
            try:
                only = resolve_municipalities(store.municipality_ids(parent), wanted)
            except MunicipalityError as exc:
                print(exc, file=sys.stderr)
                for name in exc.candidates:
                    print(f"  {name}", file=sys.stderr)
                return 2
            log.info("watching only %d municipality(ies): %s", len(only), ", ".join(sorted(only)))
            rate = filter_request_rate(cfg, len(only))
            log.info("estimated steady request rate %.2f/s of max_rps %.2f", rate, cfg.max_rps)
            if rate > cfg.max_rps / 2:
                log.warning("this selection needs about %.0f%% of max_rps for counting alone; "
                            "raise probe_interval_s or select fewer municipalities",
                            100 * rate / cfg.max_rps)
        notifier = Notifier(cfg, _notifier_http(), out=sys.stdout)

        pipeline = None
        analyze = (args.analyze or cfg.analysis_enabled) and not args.no_analyze
        if args.analyze_backlog and not analyze:
            print("--analyze-backlog needs analysis on (--analyze or analysis_enabled = true)",
                  file=sys.stderr)
            return 2
        if args.once:
            log.info("watch --once is a one-shot diff check: analysis is off, new RG-2 forms "
                     "stay queued for the next live run")
        elif not analyze:
            log.info("analysis off (opt in with --analyze or analysis_enabled = true): new RG-2 "
                     "forms are recorded, not analysed")
        else:
            ai, why = _analysis_client(cfg)
            if ai is None:
                log.warning("analysis disabled: %s", why)
            else:
                def on_findings(found: list[Finding]) -> None:
                    for f in found:
                        _store_finding(store, parent, f)

                pipeline = AnalysisPipeline(client, store, notifier, ai, cfg, on_findings,
                                            _cache_dir(cfg), backlog=args.analyze_backlog,
                                            **({"municipalities": set(only)} if only else {}))
        on_events, deliver = _handlers(store, notifier, parent, pipeline)
        watcher = Watcher(client, store, cfg, on_events, initial_safety=args.once,
                          on_status=notifier.status, baseline=args.baseline, only=only)
        stop = threading.Event()
        server, threads = None, []
        try:  # everything after pipeline.start() is inside the try: the pipeline cannot leak
            if pipeline is not None:
                pipeline.start()
            if not args.once:  # `--once` is a diff check: no page build, no server
                port = cfg.serve_port if args.serve_port is None else args.serve_port
                if port:
                    server = start_server(_browse_path(cfg).parent, _cache_dir(cfg), port,
                                          *_review_api(store))
                url = f"http://127.0.0.1:{server.server_port}" if server else None
                live = _live(watcher, pipeline, notifier, url)
                threads.append(_background("status", stop, STATUS_EVERY_S, lambda: write_status(
                    store, _browse_path(cfg), parent, live()),
                    first=lambda: copy_static(_browse_path(cfg).parent)))
                if cfg.browse_interval_s > 0:
                    opinions: dict = {}  # evaluated second opinions, kept between builds
                    threads.append(_background("browse", stop, cfg.browse_interval_s, lambda: build_page(
                        store, _browse_path(cfg), _cache_dir(cfg), None, cfg, cfg.browse_interval_s,
                        False, opinions)))
            while True:
                try:
                    watcher.run_once()
                except Exception:
                    log.exception("tick failed; retrying next tick")
                try:
                    deliver()
                except Exception:
                    log.exception("delivery failed; retrying next tick")
                if args.once:
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("interrupted, shutting down")
        finally:
            stop.set()
            if server is not None:
                server.shutdown()
                server.server_close()
            for t in threads:  # a build in flight must finish before the store is closed
                t.join(10)
            if pipeline is not None:
                pipeline.stop()
        return 0
    finally:
        store.close()


def _rik_client(cfg: Config, election) -> RikClient:
    http = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    return RikClient(election, AdaptiveLimiter(cfg.max_rps), http, cfg.request_timeout_s,
                     cfg.max_pdf_bytes, download_limiter_for(cfg))


def _csv(text: str | None) -> list[str] | None:
    return [x.strip() for x in text.split(",") if x.strip()] if text else None


def _cmd_analyze_batch(args, cfg: Config) -> int:
    store = _open_store(cfg)
    try:
        election = store.get_election(args.parent)
        if election is None:
            print(f"unknown election {args.parent}; run `rg2watch add <zapisnici-url>` first",
                  file=sys.stderr)
            return 2
        try:
            sel = select_forms(store, args.parent, municipalities=_csv(args.municipality),
                               stations=_csv(args.station),
                               rules=[r.upper() for r in _csv(args.rule) or []] or None,
                               severity=args.severity, state=args.state, limit=args.limit)
        except MunicipalityError as exc:
            print(exc, file=sys.stderr)
            for name in exc.candidates:
                print(f"  {name}", file=sys.stderr)
            return 2
        model = stored_model(cfg.model)
        todo = [d for d in sel.docs if store.latest_report(d.url, model, PROMPT_VERSION) is None]
        print(f"{len(sel.docs)} form(s) selected, {len(sel.docs) - len(todo)} already analysed by "
              f"{model} (cached: rules re-applied, free)")
        max_usd = cfg.max_spend_usd if args.max_usd is None else args.max_usd
        if is_ollama(cfg.model):
            print("local model, no API cost")
        elif todo:
            line, usd = describe_estimate(todo, _cache_dir(cfg), cfg)
            print(line)
            print(f"the batch stops at ${max_usd:.2f} (--max-usd, default max_spend_usd)")
            if usd > max_usd:
                print(f"warning: the estimate ${usd:.2f} exceeds ${max_usd:.2f}: the batch will "
                      "stop before every form is analysed")
            if not args.yes:
                print("not calling the API without --yes")
                return 0
        ai, why = _analysis_client(cfg)
        if ai is None:
            print(f"analysis unavailable: {why}", file=sys.stderr)
            return 2
        res = run_batch(sel.docs, store, _rik_client(cfg, election), ai, cfg, _cache_dir(cfg),
                        max_usd, sys.stdout, sys.stderr)
    finally:
        store.close()
    print(f"{res['analysed']} analysed, {res['cached']} cached, {res['failed']} failed, "
          f"${res['cost_usd']:.3f} spent")
    if res["unrecorded"]:
        print(f"warning: {res['unrecorded']} analysis result(s) could not be recorded (spend "
              "missing from the totals; see the log)", file=sys.stderr)
    return 1 if res["stopped"] or res["failed"] or res["unrecorded"] else 0


def _cmd_analyze(args) -> int:
    cfg = _config(args)
    batch_flags = (args.municipality, args.station, args.rule, args.severity, args.state, args.limit,
                   args.yes, args.max_usd)
    if args.parent and args.source:
        print("give either a source or --parent, not both", file=sys.stderr)
        return 2
    if not args.parent and not args.source:
        print("analyze needs a source (PDF path or URL) or --parent for batch mode", file=sys.stderr)
        return 2
    if args.max_usd is not None and not (math.isfinite(args.max_usd) and args.max_usd > 0):
        print("--max-usd must be a positive number", file=sys.stderr)
        return 2
    if args.parent:
        return _cmd_analyze_batch(args, cfg)
    if any(batch_flags):
        print("--municipality/--station/--rule/--severity/--state/--limit/--yes/--max-usd need "
              "--parent", file=sys.stderr)
        return 2
    ai, why = _analysis_client(cfg)
    if ai is None:
        print(f"analysis unavailable: {why}", file=sys.stderr)
        return 2
    src = args.source
    if urlsplit(src).scheme in ("http", "https"):
        store = _open_store(cfg)
        try:
            found = store.find_document(src)
            if found is not None:
                election, doc = store.get_election(found[0]), found[1]
            else:
                host = urlsplit(src)[:2]
                election = next((e for e in store.elections() if urlsplit(e.base_url)[:2] == host),
                                None)
                doc = Doc("", "", "", "", 6, src, "")
        finally:
            store.close()
        if election is None:
            print(f"refusing to download {src}: not on the host of a registered election "
                  "(run `rg2watch add` first)", file=sys.stderr)
            return 2
        try:
            path = fetch_pdf(_rik_client(cfg, election), src, _cache_dir(cfg))
        except RikError as exc:
            print(f"download failed: {exc}", file=sys.stderr)
            return 1
    else:
        path = Path(src)
        if not path.is_file():
            print(f"no such file: {src}", file=sys.stderr)
            return 2
        doc = Doc("", "", path.name, "", 6, str(path), "")
    try:
        result = analyze_document(render_tiles(path), ai, cfg)
    except (RenderError, AnalysisError, OllamaUnavailable) as exc:
        cost = getattr(exc, "cost_usd", 0.0)
        if cost:
            error = str(exc)[:200]
            _record_adhoc(cfg, doc.url, lambda s: s.save_failed_analysis(
                doc.url, stored_model(cfg.model), PROMPT_VERSION, cost, error, parent=ADHOC))
        print(f"analysis failed: {exc}" + (f" (billed ${cost:.3f})" if cost else ""), file=sys.stderr)
        return 1
    _record_adhoc(cfg, doc.url, lambda s: s.save_analysis(doc.url, result, PROMPT_VERSION,
                                                          parent=ADHOC))
    findings = evaluate_report(result.report, doc, cfg)
    if args.json:
        print(json.dumps({
            "report": result.report, "model": result.model, "cost_usd": result.cost_usd,
            "findings": [{"rule": f.rule, "severity": f.severity, "summary": f.summary}
                         for f in findings]}, ensure_ascii=False, indent=2))
        return 0
    vis = result.report["visual"]
    print(f"{doc.url}\nmodel {result.model}, {result.input_tokens} in / {result.output_tokens} out "
          f"tokens, ${result.cost_usd:.3f}\nconfidence {result.report['confidence']}, "
          f"page quality {vis['page_quality']}")
    for f in findings:
        print(f"[{f.severity}] {f.rule} {f.summary}")
    if not findings:
        print("no findings")
    return 0


def _record_adhoc(cfg: Config, url: str, save) -> None:
    """`analyze` spend is recorded under its own parent, never against an election's cap."""
    try:
        store = _open_store(cfg)
        try:
            save(store)
        finally:
            store.close()
    except Exception:
        log.error("spend of the analysis of %s could not be recorded", url, exc_info=True)


def _cmd_eval(args) -> int:
    cfg = _config(args)
    try:
        labels = load_labels(args.labels)
    except (OSError, ValueError) as exc:
        print(f"cannot read labels: {exc}", file=sys.stderr)
        return 2
    if args.limit:
        labels = labels[:args.limit]
    ai = None
    local = is_ollama(cfg.model)
    if not args.cached:
        ai, why = _analysis_client(cfg)
        if ai is None:
            print(f"evaluation unavailable: {why}", file=sys.stderr)
            return 2
    store = _open_store(cfg)
    try:
        if not args.cached and local:
            print("local model, no API cost")
        elif not args.cached:
            forms, tiles, usd = estimate_cost(labels, args.pdf_dir, cfg)
            remaining = max(0.0, cfg.max_spend_usd - store.total_spend(EVAL_PARENT))
            print(f"{forms} form(s), {tiles} tiles: estimated cost ${usd:.2f} (estimate, actual "
                  f"may differ; {cfg.model}, ~{TOKENS_PER_TILE // 1000}k tokens in per tile + "
                  f"~{OUTPUT_TOKENS // 1000}k tokens out per form)")
            print(f"remaining cap ${remaining:.2f} (max_spend_usd ${cfg.max_spend_usd:.2f} for "
                  f"eval runs in this DB)")
            if not args.yes:
                print("refusing to call the API without --yes", file=sys.stderr)
                return 2
            if usd > remaining and not args.force_over_cap:
                print(f"the estimate ${usd:.2f} exceeds the remaining cap ${remaining:.2f}: the run "
                      "would stop at the cap. Run again with --yes --force-over-cap to go ahead, "
                      "or raise max_spend_usd / use --limit", file=sys.stderr)
                return 2
        result = run_eval(labels, args.pdf_dir, store, ai, cfg, cached=args.cached, out=sys.stdout)
    finally:
        store.close()
    scored = [e for e in labels if e.get("expect") and e["file"] in result["files"]]
    if result["score"]:
        print(format_table(result["score"]))
        unverified = sum(1 for e in scored if not e.get("verified"))
        if unverified:
            print(f"{unverified} of {len(scored)} scored form(s) are not marked verified")
    elif not any(e.get("expect") for e in labels):
        print("no verified labels yet - fill tests/golden/labels.jsonl")
    else:
        print("no labelled form was analysed: nothing to score")
    for name, why in result["errors"].items():
        print(f"{name}: {why}", file=sys.stderr)
    print(f"{len(result['files'])} analysed, {len(result['errors'])} failed, "
          f"${result['cost_usd']:.3f} spent")
    out_dir = _cache_dir(cfg).parent / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (time.strftime("%Y%m%dT%H%M%S") + ".json")
    out.write_text(json.dumps({**result, "cached": args.cached, "model": cfg.model},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {out}")
    return 1 if result["errors"] else 0


def _cmd_requeue(args) -> int:
    store = _open_store(_config(args))
    try:
        n = store.requeue_failed(args.parent, all_failed=args.failed)
        if args.skipped:
            n += store.requeue_skipped(args.parent)
    finally:
        store.close()
    print(f"requeued {n} document(s) for analysis")
    return 0


def _fixture_root(path: Path) -> Path:
    if (path / "election.json").exists():
        return path
    subs = [d for d in path.glob("*/") if (d / "election.json").exists()]
    if len(subs) == 1:
        return subs[0]
    raise SystemExit(f"no election.json in {path} (point --fixtures at the <out>/<parent> directory)")


def _cmd_replay(args) -> int:
    speed = float(str(args.speed).rstrip("xX"))
    server = make_replay_server(_fixture_root(args.fixtures), args.host, args.port, speed, args.faults)
    port = server.server_address[1]
    print(f"replay server on http://{args.host}:{port} (zapisnici: "
          f"http://{args.host}:{port}/zapisnici/{server.fake.election.parent})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        server.server_close()
    return 0


def _cmd_fixtures(args) -> int:
    cfg = _config(args)
    cfg.max_rps = args.max_rps
    client = RikClient.from_url(args.url, cfg)
    root = fetch_fixtures(client, args.out, with_pdfs=args.pdfs, limit_pdfs=args.limit_pdfs)
    print(f"fixtures written to {root}")
    return 0


def _sigterm(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True,
        level=logging.DEBUG if args.verbose else logging.INFO if args.cmd in ("watch", "serve") else logging.WARNING)
    handler = {"add": _cmd_add, "watch": _cmd_watch, "replay": _cmd_replay,
               "fixtures": _cmd_fixtures, "analyze": _cmd_analyze, "eval": _cmd_eval,
               "requeue": _cmd_requeue, "browse": _cmd_browse, "serve": _cmd_serve, "tally": _cmd_tally,
               "review": _cmd_review, "labels": _cmd_labels}[args.cmd]
    previous = None
    if args.cmd in ("watch", "replay", "serve"):  # SIGTERM shuts down like Ctrl-C (DB closed, exit 0)
        previous = signal.signal(signal.SIGTERM, _sigterm)
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 0
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
