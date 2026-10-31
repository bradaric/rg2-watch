#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

import httpx

from rg2watch.analyze import is_ollama, is_opencode
from rg2watch.config import load_config
from rg2watch.list_names import extract_all, extract_candidate, format_toml, select_candidates
from rg2watch.opencode_client import OpenCodeClient
from rg2watch.store import Store


def analysis_client(model: str):
    if is_ollama(model):
        return httpx.Client()
    if is_opencode(model):
        return OpenCodeClient.from_service()
    import anthropic
    return anthropic.Anthropic()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract a reviewable list_names TOML snippet from one cached RG-2 per ballot")
    parser.add_argument("--parent", required=True, help="election parent id")
    parser.add_argument("--config", type=Path, default=Path("rg2watch.toml"))
    parser.add_argument("--db", type=Path, help="SQLite database (defaults to config db_path)")
    parser.add_argument("--model", help="model selector override")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.db:
        cfg.db_path = args.db
    if args.model:
        cfg.model = args.model
    cache_dir = Path(cfg.db_path).resolve().parent / "pdfs"
    store = Store(cfg.db_path)
    client = None
    try:
        if store.get_election(args.parent) is None:
            print(f"election {args.parent} is not registered in {cfg.db_path}", file=sys.stderr)
            return 2
        selection = select_candidates(store, cfg, args.parent, cache_dir)
        if not selection.candidates and not selection.unavailable:
            print("all ballot groups already have configured list names", file=sys.stderr)
            return 0
        client = analysis_client(cfg.model)
        total_cost = 0.0

        def run(candidate):
            nonlocal total_cost
            label = candidate.group or "national ballot"
            print(f"reading {label}: {candidate.doc.url}", file=sys.stderr)
            extraction, result = extract_candidate(candidate, client, cfg)
            total_cost += result.cost_usd
            print(f"  {len(extraction.names)} list(s), confidence {extraction.confidence}, "
                  f"cost ${result.cost_usd:.4f}", file=sys.stderr)
            return extraction

        results, errors = extract_all(selection.candidates, run)
        if results:
            print(format_toml(args.parent, selection.scope,
                              cfg.list_names.get(args.parent), results), end="")
        for group in selection.unavailable:
            print(f"missing cached PDF: {group}", file=sys.stderr)
        for group, error in errors:
            print(f"failed: {group}: {error}", file=sys.stderr)
        print(f"total model cost: ${total_cost:.4f}", file=sys.stderr)
        return 1 if selection.unavailable or errors else 0
    finally:
        store.close()
        if client is not None and hasattr(client, "close"):
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
