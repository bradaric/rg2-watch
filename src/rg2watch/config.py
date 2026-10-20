import logging
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .names import to_cyrillic


# USD per million tokens (input, output): Anthropic list prices as of 20/10/2026 (Haiku: prompts
# up to 100k tokens; ours are ~20k). Local ollama models are free.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}


class UnknownPrice(ValueError):
    pass


def table_prices(model: str) -> tuple[float, float] | None:
    """Built-in price of a model (dated names like claude-haiku-5-5-2026... match), else None."""
    if model.startswith("ollama:"):
        return (0.0, 0.0)
    for name, price in PRICES.items():
        if model == name or model.startswith(name + "-"):
            return price
    return None


@dataclass
class Config:
    base_url: str = "https://www.rik.parlament.gov.rs"
    db_path: Path = Path("data/rg2watch.sqlite")
    max_rps: float = 2.0
    probe_interval_s: int = 30
    rare_interval_s: int = 60
    safety_sweep_interval_s: int = 300
    tick_request_budget: int = 40
    rare_resweep_interval_s: int = 300
    reserved_share: float = 0.25
    blocked_pause_s: int = 300
    blind_alert_s: int = 300
    missing_station_threshold: int = 50
    missing_max_per_tick: int = 10
    probe_cost_guard: bool = True
    watchlist: list[str] = field(default_factory=list)
    only_municipalities: list[str] = field(default_factory=list)
    # parent -> names of list rows 1..N (national) or {municipality -> names} (municipal)
    list_names: dict[str, list[str] | dict[str, list[str]]] = field(default_factory=dict)
    tally_scope: dict[str, str] = field(default_factory=dict)  # parent -> "national" | "municipal"
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    medium_digest_s: int = 600
    high_push_max: int = 10  # individual HIGH pushes per window; the rest roll into a digest
    high_push_window_s: int = 300
    mass_replace_threshold: int = 20  # replaced documents in one sweep that trip the circuit breaker
    request_timeout_s: float = 20.0
    max_pdf_bytes: int = 50_000_000
    model: str = "claude-haiku-5-5"
    effort: str = "high"
    fallbacks: bool = True
    analysis_enabled: bool = False  # opt-in: `watch --analyze` also enables it
    max_spend_usd: float = 60.0  # per election (parent)
    ollama_url: str = "http://localhost:11434"  # used when model = "ollama:<name>"
    ollama_num_ctx: int = 65536
    ollama_timeout_s: float = 900.0
    price_in_per_mtok: float | None = None  # override of the built-in table (PRICES); None = table
    price_out_per_mtok: float | None = None
    analysis_workers: int = 4
    browse_interval_s: int = 120  # rebuild the browse page this often in `watch`; 0 = off
    serve_port: int = 8770  # local web server of `watch` (127.0.0.1); 0 = off
    drive_client_secrets: Path | None = None  # installed-app OAuth JSON; never served
    drive_token_path: Path = Path("data/drive-token.json")
    download_max_rps: float = 0.5
    analysis_pause_s: int = 600
    stop_wait_s: int = 80
    turnout_high: float = 0.95
    leader_share_high: float = 0.90
    assisted_share: float = 0.05
    min_registered_for_ratio: int = 50
    unsigned_rows_threshold: int = 2
    outside_share_high: float = 0.15  # initial value, tune after calibration
    outside_share_medium: float = 0.10  # initial value, tune after calibration
    large_correction_abs: int = 5
    large_correction_share: float = 0.3


def model_prices(cfg: Config, model: str | None = None) -> tuple[float, float]:
    """(input, output) USD per MTok for `model` (default cfg.model): the config values when set
    explicitly, else the built-in table; a model outside the table needs explicit prices."""
    model = model or cfg.model
    base = table_prices(model)
    p_in = cfg.price_in_per_mtok if cfg.price_in_per_mtok is not None else base and base[0]
    p_out = cfg.price_out_per_mtok if cfg.price_out_per_mtok is not None else base and base[1]
    if p_in is None or p_out is None:
        raise UnknownPrice(
            f"no built-in price for model {model!r}: set price_in_per_mtok and price_out_per_mtok "
            f"in the config (known: {', '.join(PRICES)})")
    return p_in, p_out


def load_config(path: Path | None) -> Config:
    data = {}
    if path is not None:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    if "sweep_pages_per_tick" in data:  # older name of the per-tick budget
        data.setdefault("tick_request_budget", data["sweep_pages_per_tick"])
        del data["sweep_pages_per_tick"]
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    for key in ("db_path", "drive_client_secrets", "drive_token_path"):
        if key in data and data[key] is not None:
            data[key] = Path(data[key])
    cfg = Config(**data)
    if not 0.0 <= cfg.reserved_share <= 0.9:
        clamped = min(0.9, max(0.0, cfg.reserved_share))
        logging.getLogger("rg2watch.config").warning(
            "reserved_share %s out of range [0.0, 0.9]; using %s", cfg.reserved_share, clamped)
        cfg.reserved_share = clamped
    if not (isinstance(cfg.only_municipalities, list)
            and all(isinstance(n, str) for n in cfg.only_municipalities)):
        raise ValueError("only_municipalities must be a list of strings")
    names = lambda v: isinstance(v, list) and all(isinstance(n, str) for n in v)
    if not (isinstance(cfg.list_names, dict) and all(
            isinstance(k, str) and (names(v) or (isinstance(v, dict) and all(
                isinstance(m, str) and names(n) for m, n in v.items())))
            for k, v in cfg.list_names.items())):
        raise ValueError("list_names must map an election id to a list of strings "
                         "or to {municipality = list of strings}")
    for k, v in cfg.list_names.items():  # municipal keys match as Latin or Cyrillic: no two may fold together
        seen: dict[str, str] = {}
        for m in v if isinstance(v, dict) else ():
            if (f := to_cyrillic(m)) in seen:
                raise ValueError(f'list_names "{k}": "{seen[f]}" and "{m}" name the same municipality')
            seen[f] = m
    if not (isinstance(cfg.tally_scope, dict) and all(
            isinstance(k, str) and v in ("national", "municipal") for k, v in cfg.tally_scope.items())):
        raise ValueError('tally_scope must map an election id to "national" or "municipal"')
    if cfg.high_push_max < 1 or cfg.high_push_window_s < 1 or cfg.mass_replace_threshold < 1:
        raise ValueError("high_push_max, high_push_window_s and mass_replace_threshold must be >= 1")
    if cfg.browse_interval_s < 0:
        raise ValueError(f"browse_interval_s must be >= 0: {cfg.browse_interval_s}")
    if not 0 <= cfg.serve_port <= 65535:
        raise ValueError(f"serve_port must be 0-65535: {cfg.serve_port}")
    topic = os.environ.get("RG2W_NTFY_TOPIC")
    if topic:
        cfg.ntfy_topic = topic
    return cfg
