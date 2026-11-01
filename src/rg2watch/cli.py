import argparse
import dataclasses
import fcntl
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .analyze import (AnalysisError, OllamaUnavailable, analyze_document, is_ollama, is_opencode,
                      parse_opencode_model, stored_model)
from .batch import ARITHMETIC_PROMPT_VERSION, ADHOC, describe_estimate, run_batch, select_forms
from .browse import (build_page, copy_static, default_parent, record_review, review_record,
                     reviews_payload, write_status)
from .config import Config, UnknownPrice, load_config, model_prices
from .evaluate import OUTPUT_TOKENS, TOKENS_PER_TILE, estimate_cost, format_table, load_labels
from .evaluate import PARENT as EVAL_PARENT
from .evaluate import run_eval
from .fetch import fetch_pdf
from .findings import findings_for
from .models import Doc, Event, Finding
from .names import MunicipalityError, resolve_municipalities
from .notify import Notifier
from .opencode_client import OpenCodeClient, OpenCodeError
from .official_results import (OfficialResultsClient, official_results_supported,
                               write_official_results)
from .operator_corrections import OperatorCorrections, Type13Corrections
from .operator_feed import OperatorFeed
from .pipeline import PROMPT_VERSION, RG7_PROMPT_VERSION, AnalysisPipeline
from .public_results import write_public_results
from .render import RenderError, render_tiles
from .replay import fetch_fixtures, make_replay_server
from .rik_client import USER_AGENT, AdaptiveLimiter, RikClient, RikError, download_limiter_for
from .rules import change_text, evaluate_report, evaluate_rg7
from .server import start_server
from .status import WATCHER_FRESH_S, format_status, to_belgrade
from .station_documents import StationDocuments
from .store import NOTE_MAX, VERDICTS, Store
from .tally import build_tally, format_tally
from .watcher import Watcher, filter_request_rate

log = logging.getLogger("rg2watch.cli")
OFFICIAL_RESULTS_EVERY_S = 60


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
    stopper = s.add_mutually_exclusive_group()
    stopper.add_argument("--once", action="store_true",
                         help="bootstrap, run every lane once (full passes, no page budget), exit")
    stopper.add_argument("--until-done", action="store_true",
                         help="rehearsals/audits of past elections: exit when every lane has "
                              "completed a full pass and the analysis queue is empty (exit 1 if "
                              "analysis is stuck for 10 minutes)")
    s.add_argument("--only", help="comma-separated municipality names (Cyrillic or Latin): watch "
                   "only these (config only_municipalities)")
    s.add_argument("--baseline", action="store_true",
                   help="record what is already published silently on each lane's first full pass")
    s.add_argument("--force-baseline", action="store_true",
                   help="allow --baseline on a database that already holds documents of this election")
    s.add_argument("--serve-port", type=_port, help="port of the local web server (config serve_port, "
                   "default 8770; 0 = off)")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--analyze", action="store_true",
                       help="analyse new RG-2 forms with the configured vision model (also config "
                            "analysis_enabled = true)")
    mode.add_argument("--no-analyze", action="store_true",
                      help="do not analyse, even if the config enables it")
    s.add_argument("--model", help="override config model (including ollama: or opencode: selectors)")
    s.add_argument("--analyze-backlog", action="store_true",
                   help="with analysis on, also analyse forms seen before analysis was enabled")
    s.add_argument("--no-ntfy", action="store_true",
                   help="print and mark alerts handled without sending ntfy (including pending alerts)")

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
    s.add_argument("--arithmetic-only", action="store_true",
                   help="batch: targeted second read of A1-A3 result numbers only")
    s.add_argument("--json", action="store_true", help="print report and findings as JSON")
    s.add_argument("--model", help="override config model (including ollama: or opencode: selectors)")
    s.add_argument("--doc-type", choices=["rg2", "rg7"], default="rg2",
                   help="single-file mode: rg7 reads a correction decision (default rg2)")

    s = sub.add_parser("eval", parents=[common],
                       help="score the rules against hand labels on local PDFs")
    s.add_argument("--labels", type=Path, required=True, help="labels JSONL (tests/golden/labels.jsonl)")
    s.add_argument("--pdf-dir", type=Path, required=True, help="directory holding the labelled PDFs")
    s.add_argument("--limit", type=_positive_int, help="only the first N labelled forms (N >= 1)")
    s.add_argument("--model", help="override config model (including ollama: or opencode: selectors)")
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
    g = s.add_mutually_exclusive_group()
    g.add_argument("--as-filed", action="store_true", help="totals from the RG-2 forms as filed, "
                   "without RG-7 corrections")
    g.add_argument("--diff", action="store_true", help="difference: with RG-7 corrections minus as filed")

    s = sub.add_parser("export-results", parents=[common],
                       help="write a standalone public snapshot of unofficial results")
    s.add_argument("--parent", help="election (default: the newest registered)")
    s.add_argument("--out", type=Path, default=Path("public-results"),
                   help="output directory (default: public-results)")

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

    s = sub.add_parser("status", parents=[common], help="compact text status of an election from the DB")
    s.add_argument("--parent", help="election (default: the newest registered)")

    s = sub.add_parser("serve", parents=[common], help="serve the browse page on 127.0.0.1 (no watching)")
    s.add_argument("--port", type=_port, help="port (config serve_port, default 8770)")

    sub.add_parser("drive-auth", parents=[common],
                    help="authorize the operator's Google account for app-created Drive files")
    sub.add_parser("drive-root", parents=[common],
                   help="create or verify the private shared RG2 Watch Drive root")

    s = sub.add_parser("revise-viber",
                       help="preview or confirm the guarded local Књажевац 61 Viber correction")
    s.add_argument("--db", type=Path, required=True, help="explicit on-disk SQLite database")
    s.add_argument("--url", required=True, help="exact document URL from the local case page")
    s.add_argument("--reason", required=True, help="exact operator-approved short reason")
    s.add_argument("--confirm", action="store_true", help="update only the saved Viber text")
    s.add_argument("--verbose", "-v", action="store_true", help=argparse.SUPPRESS)

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
    if getattr(args, "no_ntfy", False):
        cfg.ntfy_topic = None
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
    """(client, None) for cfg.model's backend: Ollama, OpenCode, or Anthropic.
    (None, reason) when the model has no known price: spend could not be tracked."""
    try:
        model_prices(cfg)
        if is_opencode(cfg.model):
            parse_opencode_model(cfg.model)
    except UnknownPrice as exc:
        return None, str(exc)
    except ValueError as exc:
        return None, str(exc)
    if is_ollama(cfg.model):
        return httpx.Client(), None
    if is_opencode(cfg.model):
        try:
            return OpenCodeClient.from_service(), None
        except OpenCodeError as exc:
            return None, str(exc)
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
    """index.html in this database's own page folder: <db dir>/<db stem>/ (elections never share one)."""
    db = str(cfg.db_path)
    if db == ":memory:":
        return Path("data") / "memory" / "index.html"
    return Path(db).parent / Path(db).stem / "index.html"


PORT_TRIES = 11  # the configured port and the 10 above it


def _publication_api(store: Store, cfg: Config):
    from .case_draft import CaseDrafter
    from .case_handoff import CaseHandoff
    from .drive_publish import DrivePublisher, LazyConfiguredDrive
    from .drive_root import DriveRoot
    from .publication_service import PublicationJobs
    drafter = None if is_ollama(cfg.model) else CaseDrafter(store, cfg, _anthropic_client)
    cases = CaseHandoff(store, cfg, drafter)
    if cfg.drive_token_path.is_file():
        drive = LazyConfiguredDrive(cfg.drive_token_path)
        publisher = DrivePublisher(cases, drive, DriveRoot(cfg.drive_token_path, drive))
    else:
        publisher = None
    return PublicationJobs(cases, publisher)


def _start_web(page_dir: Path, cache_dir: Path, port: int, store: Store, cfg: Config | None = None,
               correction_change=None, rik_client: RikClient | None = None):
    """Serve the page on `port`, else the next free port up to +10; None when none is free."""
    case_api = _publication_api(store, cfg) if cfg is not None else None
    feed = OperatorFeed(store, cfg or Config(), cache_dir)
    if cfg is not None:
        expected_rows_cache = {}
        refresh = correction_change or (lambda: build_page(
            store, page_dir / "index.html", cache_dir, cfg=cfg,
            interval=cfg.browse_interval_s, with_status=False))
        correction_api = OperatorCorrections(
            store, cfg, stored_model(cfg.model), on_change=refresh,
            expected_rows_cache=expected_rows_cache)
        type13_api = Type13Corrections(
            store, cfg, stored_model(cfg.model), on_change=refresh,
            expected_rows_cache=expected_rows_cache)
    else:
        correction_api = None
        type13_api = None

    station_api = None
    if cfg is not None:
        station_clients = ({rik_client.election.parent: rik_client} if rik_client is not None else {})
        owned_station_clients = []
        station_clients_lock = threading.Lock()

        def station_client_for(parent: str):
            with station_clients_lock:
                client = station_clients.get(parent)
                if client is None:
                    election = store.get_election(parent)
                    if election is None:
                        raise ValueError("unknown election")
                    client = _rik_client(cfg, election)
                    station_clients[parent] = client
                    owned_station_clients.append(client)
                return client

        station_api = StationDocuments(store, station_client_for)
        station_api.close = lambda: [client.http.close() for client in owned_station_clients]

    def classification_api(url: str, action: str):
        row = store.add_classification_override(url, action)
        if row is not None and cfg is not None:
            refresh()
        return row

    for p in range(port, min(port + PORT_TRIES, 65536)):
        server = start_server(page_dir, cache_dir, p, *_review_api(store), case_api,
                               correction_api, classification_api, type13_api, station_api, feed)
        if server is not None:
            if p != port:
                log.warning("port %d busy, using %d", port, p)
            return server
    if case_api is not None:
        case_api.close()
    if station_api is not None:
        station_api.close()
    log.warning("no free web port in %d-%d", port, port + PORT_TRIES - 1)
    return None


def _watcher_live(page_dir: Path) -> bool:
    """A watcher is writing status.json here (watching, younger than WATCHER_FRESH_S): a one-off
    page build must then leave the status files to it."""
    try:
        live = json.loads((page_dir / "status.json").read_text(encoding="utf-8"))
        return bool(live.get("watching")) and time.time() - live["ts"] < WATCHER_FRESH_S
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _cmd_browse(args) -> int:
    cfg = _config(args)
    store = _open_store(cfg)
    try:
        out = args.out or _browse_path(cfg)
        n = build_page(store, out, _cache_dir(cfg), args.parent, cfg,
                       with_status=not _watcher_live(out.resolve().parent))
    finally:
        store.close()
    print(f"{out} ({n} form(s))")
    return 0


STATUS_EVERY_S = 10
HELPER_JOIN_S = 5  # shutdown: status/browse threads, together
FINAL_DELIVER_WAIT_S = 15  # shutdown: wait for an in-flight delivery (ntfy timeout is 10 s)
DELIVERY_JOIN_S = 3  # shutdown: delivery thread, after the pipeline stopped
DELIVER_EVERY_S = 5  # alert delivery and RIK health check run on their own clock, not per tick


def _live(watcher: Watcher, pipeline, notifier: Notifier, url: str | None):
    def live() -> dict:
        d = {"watcher": watcher.status_snapshot(), "ntfy": notifier.status_snapshot(), "url": url}
        if pipeline is not None:
            d["analysis"] = pipeline.status_snapshot()
        return d
    return live


def _background(name: str, stop: threading.Event, period: float, fn, first=None,
                wake: threading.Event | None = None) -> threading.Thread:
    """Run fn every `period` s in a daemon thread; failures are logged and swallowed, so the watch
    loop never waits for (or is hurt by) page building."""
    def run() -> None:
        if first is not None:
            try:
                first()
            except Exception:
                log.exception("%s setup failed", name)
        while not stop.is_set():
            if wake is not None:
                wake.clear()
            try:
                fn()
            except Exception:
                log.exception("%s update failed", name)
            (wake or stop).wait(period)

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
    print(format_tally(t, not args.clean, "diff" if args.diff else "filed" if args.as_filed else "corrected"))
    return 0


def _cmd_export_results(args) -> int:
    cfg = _config(args)
    store = _open_store(cfg)
    try:
        parent = default_parent(store, args.parent)
        election = store.get_election(parent)
        if election is None:
            print("export-results: no registered election found", file=sys.stderr)
            return 1
        try:
            out = write_public_results(store, cfg, parent, args.out)
        except ValueError as exc:
            print(f"export-results: {exc}", file=sys.stderr)
            return 1
    finally:
        store.close()
    print(f"{out} — {election.title} (static unofficial snapshot)")
    return 0


def _cmd_status(args) -> int:
    cfg = _config(args)
    store = _open_store(cfg)
    try:
        parent = default_parent(store, args.parent)
        if store.get_election(parent) is None:
            print(f"election {parent or '?'} is not registered in {cfg.db_path}", file=sys.stderr)
            return 1
        print(format_status(store, parent, _browse_path(cfg).parent / "status.json", cfg.max_spend_usd))
    finally:
        store.close()
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
            build_page(store, out, _cache_dir(cfg), cfg=cfg,
                       with_status=not _watcher_live(out.resolve().parent))
        server = _start_web(out.parent, _cache_dir(cfg), port, store, cfg)
        if server is None:
            print(f"no free port in {port}-{port + PORT_TRIES - 1}", file=sys.stderr)
            return 1
        print(f"page: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            log.info("interrupted, shutting down")
        finally:
            server.shutdown()
            server.server_close()
            if server.case_api is not None:
                server.case_api.close()
            if server.station_api is not None:
                server.station_api.close()
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


def _cmd_drive_auth(args) -> int:
    cfg = _config(args)
    if cfg.drive_client_secrets is None:
        print("drive-auth needs drive_client_secrets in the config", file=sys.stderr)
        return 2
    if not cfg.drive_client_secrets.is_file():
        print(f"Google OAuth client file not found: {cfg.drive_client_secrets}", file=sys.stderr)
        return 2
    try:
        from .drive_publish import authorize_drive
        authorize_drive(cfg.drive_client_secrets, cfg.drive_token_path)
    except Exception:
        log.exception("Google Drive authorization failed")
        print("Google Drive authorization failed; check the OAuth client and try again", file=sys.stderr)
        return 1
    print(f"Google Drive authorization saved to {cfg.drive_token_path} (mode 0600)")
    return 0


def _cmd_drive_root(args) -> int:
    cfg = _config(args)
    try:
        from .drive_publish import configured_drive
        from .drive_root import DriveRoot
        drive = configured_drive(cfg.drive_token_path)
        root_id = DriveRoot(cfg.drive_token_path, drive).setup()
    except Exception as exc:
        log.error("Google Drive root setup failed: %s", exc)
        print(f"Google Drive root setup failed: {exc}", file=sys.stderr)
        return 1
    print("https://drive.google.com/drive/folders/" + root_id)
    return 0


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
    lock = threading.Lock()  # one read-send-mark sequence at a time (delivery thread vs final shutdown run)

    def deliver(flush: bool = False, wait: float | None = None) -> None:
        """Called every few seconds by the delivery thread; `flush` also pushes the MEDIUM digest now.
        `wait` bounds the wait for an in-flight delivery (shutdown); on timeout findings stay unalerted."""
        if not lock.acquire(timeout=-1 if wait is None else wait):
            log.warning("alert delivery still in flight; findings stay unalerted until next start")
            return
        try:
            held = notifier.unmarked  # sent but the mark failed: mark again, never re-send
            fresh = [r for r in store.unalerted(parent, {"HIGH", "MEDIUM", "LOW"}) if r[0] not in held]
            notifier.send(fresh, flush)
            ids = sorted(notifier.unmarked)
            store.mark_alerted(ids)
            notifier.marked(ids)
        except Exception:
            log.exception("alert delivery failed; retrying next tick")
        finally:
            lock.release()

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


STUCK_AFTER_S = 600  # --until-done: no form leaves the analysis queue for this long -> give up
DONE_CONFIRM_S = 3  # --until-done: the done condition must hold this long (two checks) before exit


class _UntilDone:
    """`watch --until-done`: step() returns None while running, 0 when done, 1 when the watcher's
    first pass or the analysis is stuck (`why` says which)."""

    def __init__(self, watcher: Watcher, pipeline, store: Store, parent: str, only, clock=time.monotonic,
                 stuck_after: float = STUCK_AFTER_S, confirm: float = DONE_CONFIRM_S):
        self.watcher, self.pipeline, self.store, self.parent, self.only = watcher, pipeline, store, parent, only
        self.clock, self.stuck_after, self.confirm = clock, stuck_after, confirm
        self.remaining = 0
        self.why = ""
        self._ok_since: float | None = None
        self._progress: tuple[int, int] | None = None  # primary finished / arithmetic remaining
        self._progress_at = clock()
        self._lanes: list[str] | None = None  # first-pass lanes left / last RIK success, when last changed
        self._ok_at = None
        self._lanes_at = clock()

    def step(self) -> int | None:
        primary = (0 if self.pipeline is None
                   else self.store.unfinished_analysis(self.parent, self.only))
        arithmetic = (0 if self.pipeline is None
                      else self.store.unfinished_arithmetic_rechecks(self.parent, self.only))
        self.remaining = primary + arithmetic
        now = self.clock()
        if self.remaining == 0:
            self._progress = None
        else:
            progress = (self.store.finished_analysis(self.parent, self.only), arithmetic)
            if progress != self._progress:
                self._progress, self._progress_at = progress, now
        left, ok = self.watcher.first_pass_left, self.watcher.client.last_ok
        if (left, ok) != (self._lanes, self._ok_at):
            self._lanes, self._ok_at, self._lanes_at = left, ok, now
        if not left and not self.remaining:
            if self._ok_since is None:
                self._ok_since = now
            return 0 if now - self._ok_since >= self.confirm else None
        self._ok_since = None
        if left and now - self._lanes_at >= self.stuck_after:
            self.why = (f"watcher first pass incomplete: lanes {', '.join(left)} "
                        "(RIK blocked or unreachable?)")
            return 1
        if self.remaining and now - self._progress_at >= self.stuck_after:
            paused = self.pipeline.status_snapshot()["paused"]
            self.why = (f"analysis paused: {paused[0]}" if paused else
                        "no form analysed (model backend busy or PDF downloads failing; see the log)")
            return 1
        return None


def _print_summary(store: Store, db_path, parent: str, only, started: float, done: _UntilDone,
                   code: int) -> None:
    sm = store.run_summary(parent, only)
    elapsed = int(time.monotonic() - started)
    sev = sm["findings"]
    print(f"\n[until-done] {'finished' if code == 0 else 'STOPPED, stuck'} "
          f"after {elapsed // 60} min {elapsed % 60:02d} s")
    print("documents seen: " + (", ".join(f"{k} {v}" for k, v in sm["docs"].items()) or "none"))
    if done.pipeline is not None:
        print(f"RG-2 analysed {sm['done']}, failed {sm['failed']}, skipped {sm['skipped']}, "
              f"remaining {done.remaining}")
    print("findings: " + ", ".join(f"{k} {sev.get(k, 0)}" for k in ("HIGH", "MEDIUM", "LOW")))
    print(f"spend for this election: ${store.total_spend(parent):.2f}")
    unsent = len(store.unalerted(parent, {"HIGH", "MEDIUM"}))
    if unsent:
        print(f"warning: {unsent} HIGH/MEDIUM alert(s) not pushed (ntfy unreachable); they go out "
              "on the next run")
    if code:
        print(f"{done.why}; {done.remaining} form(s) not analysed. Fix the cause, then `rg2watch "
              f"requeue --parent {parent} --failed` if needed and rerun the same command: the "
              "queue resumes", file=sys.stderr)
    print(f"browse: rg2watch serve --db {db_path}")


def _watch_lock(db_path: Path):
    """Exclusive lock on <db>.lock (its pid inside) so two watchers never share a database;
    None (message printed) when another one holds it. Released when the file is closed."""
    path = Path(str(db_path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        pid = fh.read().strip() or "unknown"
        fh.close()
        print(f"another rg2watch watch is already running on {db_path} (pid {pid})", file=sys.stderr)
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _cmd_watch(args) -> int:
    cfg = _config(args)
    lock = _watch_lock(cfg.db_path)
    if lock is None:
        return 2
    try:
        return _run_watch(args, cfg)
    finally:
        lock.close()


def _run_watch(args, cfg: Config) -> int:
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
        if args.baseline and store.has_documents(parent) and not args.force_baseline:
            print(f"refusing --baseline: {cfg.db_path} already has documents of election {parent}, "
                  "so documents published since the last run would be recorded silently, without "
                  "alerts. Drop --baseline, or add --force-baseline if that is what you want",
                  file=sys.stderr)
            return 2
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
            if ai is None and args.until_done:
                print(f"--until-done needs analysis: {why}", file=sys.stderr)
                return 2
            if ai is None:
                log.warning("analysis disabled: %s", why)
            else:
                def on_findings(found: list[Finding]) -> None:
                    for f in found:
                        _store_finding(store, parent, f)

                separate_sonnet = (cfg.arithmetic_recheck_enabled
                                   and (is_ollama(cfg.model) or is_opencode(cfg.model)))
                sonnet = None
                if separate_sonnet:
                    sonnet, sonnet_why = _anthropic_client()
                    if sonnet is None:
                        log.warning("paid Sonnet arithmetic fallback unavailable: %s", sonnet_why)
                backend_args = ({"sonnet_client": sonnet} if separate_sonnet else {})
                pipeline = AnalysisPipeline(client, store, notifier, ai, cfg, on_findings,
                                             _cache_dir(cfg), backlog=args.analyze_backlog,
                                             **backend_args,
                                             **({"municipalities": set(only)} if only else {}))
        on_events, deliver = _handlers(store, notifier, parent, pipeline)
        watcher = Watcher(client, store, cfg, on_events,
                          initial_safety=args.once or args.until_done,
                          on_status=notifier.status, baseline=args.baseline, only=only,
                          on_mass=notifier.digest_only)
        stop = threading.Event()
        browse_wake = threading.Event() if cfg.browse_interval_s > 0 else None
        opinions: dict = {}  # evaluated second opinions, kept between builds
        stop_delivery = threading.Event()  # set after the pipeline stopped: its last findings still go out
        delivery = None
        official_http = None
        server, threads = None, []
        code, finished, reached, started = 0, None, False, time.monotonic()
        if args.until_done:
            finished = _UntilDone(watcher, pipeline, store, parent, set(only) if only else None,
                                  stuck_after=STUCK_AFTER_S, confirm=DONE_CONFIRM_S)
        try:  # everything after pipeline.start() is inside the try: the pipeline cannot leak
            if pipeline is not None:
                pipeline.start()
            if not args.once:  # `--once` is a diff check: no page build, no server, no delivery thread
                delivery = _background("deliver", stop_delivery, DELIVER_EVERY_S,
                                       lambda: (deliver(), watcher.check_health()))
                port = cfg.serve_port if args.serve_port is None else args.serve_port
                if port:
                    server = _start_web(
                        _browse_path(cfg).parent, _cache_dir(cfg), port, store, cfg,
                        correction_change=browse_wake.set if browse_wake is not None else None,
                        rik_client=client)
                url = f"http://127.0.0.1:{server.server_port}" if server else None
                if url:
                    notifier.page_url = url
                    print(f"page: {url}", flush=True)
                    election = store.get_election(parent)
                    if election is not None and official_results_supported(election):
                        official_http = httpx.Client(
                            headers={"User-Agent": USER_AGENT}, follow_redirects=True)
                        official = OfficialResultsClient(
                            election, official_http, cfg.request_timeout_s)
                        official_path = _browse_path(cfg).parent / "official-results.json"
                        threads.append(_background(
                            "official-results", stop, OFFICIAL_RESULTS_EVERY_S,
                            lambda: write_official_results(official_path, official.fetch())))
                live = _live(watcher, pipeline, notifier, url)
                threads.append(_background("status", stop, STATUS_EVERY_S, lambda: write_status(
                    store, _browse_path(cfg), parent, live(), _cache_dir(cfg)),
                    first=lambda: copy_static(_browse_path(cfg).parent)))
                if cfg.browse_interval_s > 0:
                    threads.append(_background("browse", stop, cfg.browse_interval_s, lambda: build_page(
                        store, _browse_path(cfg), _cache_dir(cfg), None, cfg, cfg.browse_interval_s,
                        False, opinions), wake=browse_wake))
            while True:
                try:
                    watcher.run_once()
                except Exception:
                    log.exception("tick failed; retrying next tick")
                if args.once:  # a one-shot check delivers by itself; the loop has the delivery thread
                    try:
                        deliver()
                    except Exception:
                        log.exception("delivery failed")
                    break
                if finished is not None:
                    try:
                        verdict = finished.step()
                    except Exception:
                        log.exception("done check failed; retrying next tick")
                        verdict = None
                    if verdict is not None:
                        code, reached = verdict, True
                        break
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("interrupted, shutting down")
        finally:
            stop.set()
            if browse_wake is not None:
                browse_wake.set()
            if server is not None:
                server.shutdown()
                server.server_close()
                if server.case_api is not None:
                    server.case_api.close()
                if server.station_api is not None:
                    server.station_api.close()
            helpers_by = time.monotonic() + HELPER_JOIN_S  # one shared deadline for all helpers
            for t in threads:  # a build in flight should finish before the store is closed
                t.join(max(0.0, helpers_by - time.monotonic()))
            if official_http is not None:
                official_http.close()
            if pipeline is not None:
                pipeline.stop()
            stop_delivery.set()
            if delivery is not None:
                delivery.join(DELIVERY_JOIN_S)
            if not args.once:  # every exit path: the pipeline's last findings still go out
                try:
                    deliver(flush=reached, wait=FINAL_DELIVER_WAIT_S)
                except BaseException:
                    log.exception("final delivery failed")
            if reached:  # --until-done verdict: last page, then the summary
                try:
                    try:
                        live = {"ntfy": notifier.status_snapshot(), "url": None}
                        if pipeline is not None:
                            live["analysis"] = pipeline.status_snapshot()
                        write_status(store, _browse_path(cfg), parent, live,
                                     _cache_dir(cfg))  # no "watcher": stopped
                        if cfg.browse_interval_s > 0:
                            build_page(store, _browse_path(cfg), _cache_dir(cfg), None, cfg,
                                       cfg.browse_interval_s, False, opinions)
                    except Exception:
                        log.exception("final delivery or page build failed")
                    _print_summary(store, cfg.db_path, parent, set(only) if only else None, started,
                                   finished, code)
                except KeyboardInterrupt:
                    print("[until-done] interrupted during shutdown", file=sys.stderr)
        return code
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
        kind = "arithmetic" if args.arithmetic_only else "rg2"
        version = ARITHMETIC_PROMPT_VERSION if args.arithmetic_only else PROMPT_VERSION
        todo = [d for d in sel.docs if store.latest_report(d.url, model, version) is None]
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
                        max_usd, sys.stdout, sys.stderr, kind=kind, prompt_version=version)
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
                   args.yes, args.max_usd, args.arithmetic_only)
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
        if args.doc_type != "rg2":
            print("--doc-type needs a single source, not --parent", file=sys.stderr)
            return 2
        return _cmd_analyze_batch(args, cfg)
    if any(batch_flags):
        print("--municipality/--station/--rule/--severity/--state/--limit/--yes/--max-usd/"
              "--arithmetic-only need --parent", file=sys.stderr)
        return 2
    ai, why = _analysis_client(cfg)
    if ai is None:
        print(f"analysis unavailable: {why}", file=sys.stderr)
        return 2
    src = args.source
    rg7 = args.doc_type == "rg7"
    version = RG7_PROMPT_VERSION if rg7 else PROMPT_VERSION
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
                doc = Doc("", "", "", "", 19 if rg7 else 6, src, "")
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
        doc = Doc("", "", path.name, "", 19 if rg7 else 6, str(path), "")
    try:
        result = analyze_document(render_tiles(path), ai, cfg, args.doc_type)
    except (RenderError, AnalysisError, OllamaUnavailable, OpenCodeError) as exc:
        cost = getattr(exc, "cost_usd", 0.0)
        if cost:
            error = str(exc)[:200]
            _record_adhoc(cfg, doc.url, lambda s: s.save_failed_analysis(
                doc.url, stored_model(cfg.model), version, cost, error, parent=ADHOC))
        print(f"analysis failed: {exc}" + (f" (billed ${cost:.3f})" if cost else ""), file=sys.stderr)
        return 1
    _record_adhoc(cfg, doc.url, lambda s: s.save_analysis(doc.url, result, version,
                                                          parent=ADHOC))
    findings = (evaluate_rg7 if rg7 else evaluate_report)(result.report, doc, cfg)
    if args.json:
        print(json.dumps({
            "report": result.report, "model": result.model, "cost_usd": result.cost_usd,
            "findings": [{"rule": f.rule, "severity": f.severity, "summary": f.summary}
                         for f in findings]}, ensure_ascii=False, indent=2))
        return 0
    if rg7:
        rep = result.report
        print(f"{doc.url}\nmodel {result.model}, {result.input_tokens} in / {result.output_tokens} out "
              f"tokens, ${result.cost_usd:.3f}\nconfidence {rep['confidence']}, decision "
              f"{rep['decision_date']}, station {rep['station_no']}")
        print("changes: " + ("; ".join(change_text(c) for c in rep["changes"]) or "none"))
        if rep["reason"]:
            print("reason: " + rep["reason"])
        for f in findings:
            print(f"[{f.severity}] {f.rule} {f.summary}")
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


def _read_viber_revision(path: Path, url: str, reason: str) -> dict:
    from .case_handoff import viber_revision

    if not path.is_file() or str(path) == ":memory:":
        raise ValueError("--db must name an existing on-disk SQLite database")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT * FROM publications WHERE url=?", (url,)).fetchall()
        if len(rows) != 1:
            raise ValueError("the exact document URL did not select one publication")
        return viber_revision(rows[0], url, reason)
    finally:
        db.close()


def _cmd_revise_viber(args) -> int:
    try:
        if args.confirm:
            revision = Store.revise_viber_text(args.db, args.url, args.reason)
        else:
            revision = _read_viber_revision(args.db, args.url, args.reason)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Saved Viber text was not changed: {exc}", file=sys.stderr)
        return 1
    case = revision["case"]
    print(f"Target: {case['municipality']}, БМ {case['station']}")
    print("\nSaved old message:\n" + revision["old"])
    print("\nProposed new message:\n" + revision["proposed"])
    if not revision["changed"]:
        print("\nThis case already has the approved saved Viber text; no change was made.")
    elif args.confirm:
        print("\nUpdated only the saved Viber text. No Drive or Viber operation was performed.")
    else:
        print("\nPreview only: confirmation would replace only the saved Viber text.")
    return 0


def _sigterm(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True,
        level=logging.DEBUG if args.verbose else logging.INFO if args.cmd in ("watch", "serve") else logging.WARNING)
    # httpx logs every request URL at INFO, which would leak the ntfy topic (it is the URL path):
    # kept at WARNING even with -v.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    handler = {"add": _cmd_add, "watch": _cmd_watch, "replay": _cmd_replay,
               "fixtures": _cmd_fixtures, "analyze": _cmd_analyze, "eval": _cmd_eval,
               "requeue": _cmd_requeue, "browse": _cmd_browse, "serve": _cmd_serve, "status": _cmd_status, "tally": _cmd_tally,
               "export-results": _cmd_export_results,
               "review": _cmd_review, "labels": _cmd_labels, "drive-auth": _cmd_drive_auth,
               "revise-viber": _cmd_revise_viber,
               "drive-root": _cmd_drive_root}[args.cmd]
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
