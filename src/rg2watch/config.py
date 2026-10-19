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
    sweep_pages_per_tick: int = 20
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


def load_config(path: Path | None) -> Config:
    data = {}
    if path is not None:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    if "db_path" in data:
        data["db_path"] = Path(data["db_path"])
    cfg = Config(**data)
    topic = os.environ.get("RG2W_NTFY_TOPIC")
    if topic:
        cfg.ntfy_topic = topic
    return cfg
