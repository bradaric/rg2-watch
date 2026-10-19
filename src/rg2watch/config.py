import logging
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


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
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    medium_digest_s: int = 600
    request_timeout_s: float = 20.0
    max_pdf_bytes: int = 50_000_000
    model: str = "claude-opus-5-5"
    effort: str = "high"
    fallbacks: bool = True
    analysis_enabled: bool = False  # opt-in: `watch --analyze` also enables it
    max_spend_usd: float = 20.0  # per election (parent)
    ollama_url: str = "http://localhost:11434"  # used when model = "ollama:<name>"
    ollama_num_ctx: int = 65536
    ollama_timeout_s: float = 900.0
    price_in_per_mtok: float = 4.0
    price_out_per_mtok: float = 20.0
    analysis_workers: int = 4
    download_max_rps: float = 0.5
    analysis_pause_s: int = 600
    stop_wait_s: int = 80
    turnout_high: float = 0.95
    leader_share_high: float = 0.90
    assisted_share: float = 0.05
    min_registered_for_ratio: int = 50
    unsigned_rows_threshold: int = 2
    outside_share_high: float = 0.15  # initial value, tune after calibration
    outside_share_medium: float = 0.05  # initial value, tune after calibration
    large_correction_abs: int = 5
    large_correction_share: float = 0.3


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
    if "db_path" in data:
        data["db_path"] = Path(data["db_path"])
    cfg = Config(**data)
    if not 0.0 <= cfg.reserved_share <= 0.9:
        clamped = min(0.9, max(0.0, cfg.reserved_share))
        logging.getLogger("rg2watch.config").warning(
            "reserved_share %s out of range [0.0, 0.9]; using %s", cfg.reserved_share, clamped)
        cfg.reserved_share = clamped
    topic = os.environ.get("RG2W_NTFY_TOPIC")
    if topic:
        cfg.ntfy_topic = topic
    return cfg
