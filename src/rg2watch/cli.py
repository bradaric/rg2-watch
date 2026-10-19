import argparse
import dataclasses
import json
import logging
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .analyze import AnalysisError, analyze_document
from .config import Config, load_config
from .evaluate import OUTPUT_TOKENS, TOKENS_PER_TILE, estimate_cost, format_table, load_labels
from .evaluate import PARENT as EVAL_PARENT
from .evaluate import run_eval
from .fetch import fetch_pdf
from .findings import findings_for
from .models import Doc, Event, Finding
from .notify import Notifier
from .pipeline import PROMPT_VERSION, AnalysisPipeline
from .render import RenderError, render_tiles
from .replay import fetch_fixtures, make_replay_server
from .rik_client import USER_AGENT, AdaptiveLimiter, RikClient, RikError, download_limiter_for
from .rules import evaluate_report
from .store import Store
from .watcher import Watcher

log = logging.getLogger("rg2watch.cli")
ADHOC = "adhoc"  # parent that `analyze` records its spend under


def _positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {text}")
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
    s.add_argument("--baseline", action="store_true",
                   help="record what is already published silently on each lane's first full pass")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--analyze", action="store_true",
                      help="analyse new RG-2 forms with Claude (paid API; also config "
                           "analysis_enabled = true)")
    mode.add_argument("--no-analyze", action="store_true",
                      help="do not analyse, even if the config enables it")
    s.add_argument("--analyze-backlog", action="store_true",
                   help="with analysis on, also analyse forms seen before analysis was enabled")

    s = sub.add_parser("analyze", parents=[common],
                       help="analyse one RG-2 PDF (local path or URL of a registered election)")
    s.add_argument("source", help="local PDF path or document URL")
    s.add_argument("--json", action="store_true", help="print report and findings as JSON")

    s = sub.add_parser("eval", parents=[common],
                       help="score the rules against hand labels on local PDFs")
    s.add_argument("--labels", type=Path, required=True, help="labels JSONL (tests/golden/labels.jsonl)")
    s.add_argument("--pdf-dir", type=Path, required=True, help="directory holding the labelled PDFs")
    s.add_argument("--limit", type=_positive_int, help="only the first N labelled forms (N >= 1)")
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
            ai, why = _anthropic_client()
            if ai is None:
                log.warning("analysis disabled: %s", why)
            else:
                def on_findings(found: list[Finding]) -> None:
                    for f in found:
                        _store_finding(store, parent, f)

                pipeline = AnalysisPipeline(client, store, notifier, ai, cfg, on_findings,
                                            _cache_dir(cfg), backlog=args.analyze_backlog)
        on_events, deliver = _handlers(store, notifier, parent, pipeline)
        watcher = Watcher(client, store, cfg, on_events, initial_safety=args.once,
                          on_status=notifier.status, baseline=args.baseline)
        if pipeline is not None:
            pipeline.start()
        try:
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
            if pipeline is not None:
                pipeline.stop()
        return 0
    finally:
        store.close()


def _cmd_analyze(args) -> int:
    cfg = _config(args)
    ai, why = _anthropic_client()
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
        http = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        client = RikClient(election, AdaptiveLimiter(cfg.max_rps), http, cfg.request_timeout_s,
                           cfg.max_pdf_bytes, download_limiter_for(cfg))
        try:
            path = fetch_pdf(client, src, _cache_dir(cfg))
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
    except (RenderError, AnalysisError) as exc:
        cost = getattr(exc, "cost_usd", 0.0)
        if cost:
            _record_adhoc(cfg, doc.url, lambda s: s.save_failed_analysis(
                doc.url, cfg.model, PROMPT_VERSION, cost, str(exc)[:200], parent=ADHOC))
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
    if not args.cached:
        ai, why = _anthropic_client()
        if ai is None:
            print(f"evaluation unavailable: {why}", file=sys.stderr)
            return 2
    store = _open_store(cfg)
    try:
        if not args.cached:
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
        level=logging.DEBUG if args.verbose else logging.INFO if args.cmd == "watch" else logging.WARNING)
    handler = {"add": _cmd_add, "watch": _cmd_watch, "replay": _cmd_replay,
               "fixtures": _cmd_fixtures, "analyze": _cmd_analyze, "eval": _cmd_eval,
               "requeue": _cmd_requeue}[args.cmd]
    previous = None
    if args.cmd in ("watch", "replay"):  # SIGTERM shuts down like Ctrl-C (DB closed, exit 0)
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
