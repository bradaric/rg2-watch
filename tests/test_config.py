from pathlib import Path

from rg2watch.config import load_config


def test_defaults_without_file(monkeypatch):
    monkeypatch.delenv("RG2W_NTFY_TOPIC", raising=False)
    c = load_config(None)
    assert c.base_url == "https://www.rik.parlament.gov.rs"
    assert c.db_path == Path("data/rg2watch.sqlite")
    assert c.max_rps == 2.0
    assert c.probe_interval_s == 30
    assert c.rare_interval_s == 60
    assert c.safety_sweep_interval_s == 300
    assert c.tick_request_budget == 40
    assert c.rare_resweep_interval_s == 300
    assert c.reserved_share == 0.25
    assert c.blocked_pause_s == 300
    assert c.blind_alert_s == 300
    assert c.missing_station_threshold == 50
    assert c.watchlist == []
    assert c.ntfy_url == "https://ntfy.sh"
    assert c.ntfy_topic is None
    assert c.medium_digest_s == 600
    assert c.request_timeout_s == 20.0
    assert c.outside_share_medium == 0.10
    assert c.browse_interval_s == 120


def test_toml_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("RG2W_NTFY_TOPIC", raising=False)
    p = tmp_path / "c.toml"
    p.write_text('max_rps = 0.5\nwatchlist = ["Zaječar"]\ndb_path = "x.sqlite"\n', encoding="utf-8")
    c = load_config(p)
    assert c.max_rps == 0.5
    assert c.watchlist == ["Zaječar"]
    assert c.db_path == Path("x.sqlite")
    assert c.probe_interval_s == 30


def test_env_overrides_topic(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('ntfy_topic = "from-file"\n', encoding="utf-8")
    monkeypatch.setenv("RG2W_NTFY_TOPIC", "from-env")
    assert load_config(p).ntfy_topic == "from-env"
    assert load_config(None).ntfy_topic == "from-env"


def test_sweep_pages_per_tick_is_alias_for_budget(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("sweep_pages_per_tick = 7\n", encoding="utf-8")
    assert load_config(p).tick_request_budget == 7


def test_reserved_share_clamped_with_warning(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text("reserved_share = 1.5\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="rg2watch.config"):
        assert load_config(p).reserved_share == 0.9
    assert "reserved_share" in caplog.text
    p.write_text("reserved_share = -0.1\n", encoding="utf-8")
    assert load_config(p).reserved_share == 0.0


def test_negative_browse_interval_rejected(tmp_path):
    import pytest

    f = tmp_path / "c.toml"
    f.write_text("browse_interval_s = -1\n")
    with pytest.raises(ValueError, match="browse_interval_s"):
        load_config(f)
