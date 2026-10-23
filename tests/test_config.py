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
    assert c.ntfy_medium is True
    assert c.medium_digest_s == 600
    assert c.request_timeout_s == 20.0
    assert c.outside_share_medium == 0.10
    assert c.browse_interval_s == 120
    assert c.arithmetic_recheck_enabled is False
    assert c.arithmetic_local_model == "ollama:bonsai-2:27b"
    assert c.arithmetic_sonnet_model == "claude-sonnet-5-5"
    assert c.arithmetic_local_queue_limit == 10


def test_toml_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("RG2W_NTFY_TOPIC", raising=False)
    p = tmp_path / "c.toml"
    p.write_text('max_rps = 0.5\nwatchlist = ["Zaječar"]\ndb_path = "x.sqlite"\n', encoding="utf-8")
    c = load_config(p)
    assert c.max_rps == 0.5
    assert c.watchlist == ["Zaječar"]
    assert c.db_path == Path("x.sqlite")
    assert c.probe_interval_s == 30


def test_medium_ntfy_can_be_disabled(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("ntfy_medium = false\n", encoding="utf-8")
    assert load_config(p).ntfy_medium is False


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


def test_only_municipalities_must_be_list_of_strings(tmp_path):
    import pytest
    f = tmp_path / "c.toml"
    f.write_text('only_municipalities = ["Beograd", "Нови Сад"]\n')
    assert load_config(f).only_municipalities == ["Beograd", "Нови Сад"]
    for bad in ('"Beograd"', "[1]"):
        f.write_text(f"only_municipalities = {bad}\n")
        with pytest.raises(ValueError, match="only_municipalities"):
            load_config(f)


def test_list_names_and_tally_scope_shapes(tmp_path):
    import pytest
    f = tmp_path / "c.toml"
    f.write_text('tally_scope = {"1" = "national", "2" = "municipal"}\n'
                 '[list_names]\n"1" = ["A"]\n"2" = {Beograd = ["X", "Y"]}\n')
    c = load_config(f)
    assert c.tally_scope == {"1": "national", "2": "municipal"}
    assert c.list_names == {"1": ["A"], "2": {"Beograd": ["X", "Y"]}}
    for bad in ('[list_names]\n"1" = {B = "x"}', '[list_names]\n"1" = {B = [1]}', '[list_names]\n"1" = 3'):
        f.write_text(bad + "\n")
        with pytest.raises(ValueError, match="list_names"):
            load_config(f)
    for bad in ('{"1" = "x"}', '{"1" = 1}', '["national"]'):
        f.write_text(f"tally_scope = {bad}\n")
        with pytest.raises(ValueError, match="tally_scope"):
            load_config(f)


def test_list_names_municipalities_folding_together_are_rejected(tmp_path):
    import pytest
    f = tmp_path / "c.toml"
    f.write_text('[list_names]\n"2" = {"Novi Sad" = ["A"], "Нови Сад" = ["B"]}\n')
    with pytest.raises(ValueError, match='"Novi Sad" and "Нови Сад" name the same municipality'):
        load_config(f)
