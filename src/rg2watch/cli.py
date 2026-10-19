import argparse
import dataclasses
import logging
import sys
import threading
from pathlib import Path

import httpx

from .config import Config, load_config
from .findings import findings_for
from .models import Event
from .notify import Notifier
from .replay import fetch_fixtures, make_replay_server
from .rik_client import USER_AGENT, AdaptiveLimiter, RikClient
from .store import Store
from .watcher import Watcher

log = logging.getLogger("rg2watch.cli")


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
    s.add_argument("--once", action="store_true", help="bootstrap, run every lane once, exit")

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
    client = RikClient.from_url(args.url, cfg)
    store = _open_store(cfg)
    try:
        store.upsert_election(client.election)
    finally:
        store.close()
    e = client.election
    print(f"parent={e.parent} round={e.round} title={e.title}")
    return 0


def _cmd_watch(args) -> int:
    cfg = _config(args)
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
            client = RikClient(stored, AdaptiveLimiter(cfg.max_rps), http, cfg.request_timeout_s)
        else:
            print("watch needs a zapisnici URL or --parent", file=sys.stderr)
            return 2
        parent = client.election.parent
        notifier = Notifier(cfg, httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT}),
                            out=sys.stdout)

        def deliver() -> None:
            pending = store.unalerted(parent, {"HIGH", "MEDIUM", "LOW"})
            if pending:
                store.mark_alerted(notifier.send(pending))

        def on_events(events: list[Event]) -> None:
            for f in findings_for(events):
                store.add_finding(parent, f, f.doc.url)
            deliver()

        watcher = Watcher(client, store, cfg, on_events, initial_safety=args.once)
        stop = threading.Event()
        try:
            while True:
                watcher.run_once()
                deliver()
                if args.once or stop.wait(1):
                    break
        except KeyboardInterrupt:
            log.info("interrupted, shutting down")
        return 0
    finally:
        store.close()


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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True,
        level=logging.DEBUG if args.verbose else logging.INFO if args.cmd == "watch" else logging.WARNING)
    handler = {"add": _cmd_add, "watch": _cmd_watch, "replay": _cmd_replay,
               "fixtures": _cmd_fixtures}[args.cmd]
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
