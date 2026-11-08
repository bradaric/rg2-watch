import json
from datetime import datetime, timedelta

import pytest

from rg2watch import estimate_replay as replay
from rg2watch.cli import main
from rg2watch.config import Config
from rg2watch.estimates import make_sample, store_frame
from rg2watch.models import Election
from rg2watch.store import Store
from test_browse import BASE, P
from test_estimates import frame, reading
from test_tally import form, M
from test_rules import clean_report


def timed_rows():
    rows = []
    for i, station in enumerate(frame()):
        row = reading(station["m"], station["no"], [20 + i * 5, 80 - i * 5])
        row["uploaded_at"] = (datetime(2026, 10, 25, 20) + timedelta(minutes=i // 2)).isoformat()
        rows.append(row)
    return rows


def test_arrival_replay_groups_timestamp_ties_and_is_not_a_live_replay():
    rows = timed_rows()
    out = replay.run_replay(frame(), rows, ["One", "Two"], "P", sizes=[4, 8], seeds=["a", "b"])
    assert out["mode"] == "final_readings_by_upload_time"
    assert out["reference"]["complete"] is True
    assert out["reference"]["reporting"] == 10
    assert [r["available"] for r in out["timeline"]] == [2, 4, 6, 8, 10]
    assert out["timeline"][-1]["arrivals"]["max_error_pp"] == 0
    assert out["timeline"][-1]["raw"]["max_error_pp"] == 0
    assert len(out["sample_sizes"]) == 2
    assert out == replay.run_replay(frame(), list(reversed(rows)), ["One", "Two"], "P",
                                     sizes=[4, 8], seeds=["a", "b"])
    assert out["accuracy_validated"] is False  # one fitted historical election cannot validate future error


def test_missing_reference_or_bad_times_are_explicit_not_accuracy_claims():
    rows = timed_rows()[:-1]
    rows[0]["uploaded_at"] = None
    rows[1]["uploaded_at"] = "not a date"
    out = replay.run_replay(frame(), rows, ["One", "Two"], "P", sizes=[4], seeds=["s"])
    assert out["reference"]["complete"] is False
    assert out["reference"]["reporting"] == 9
    assert out["missing_timestamps"] == 2
    assert out["sample_sizes"][0]["meets_1pp"] is None
    assert any("partial" in w.lower() for w in out["warnings"])


def test_undersized_samples_are_reported_without_dropping_strata():
    out = replay.run_replay(frame(), timed_rows(), ["One", "Two"], "P", sizes=[2, 4], seeds=["s"])
    assert out["sample_sizes"][0]["status"] == "invalid_size"
    assert out["sample_sizes"][1]["size"] == 4


def make_db(path, with_result=True):
    s = Store(path)
    s.upsert_election(Election(P, "1", "2", "National election", BASE))
    s.save_municipalities(P, {"a": "A", "b": "B"})
    s.save_stations(P, "a", {str(i): f"a{i}" for i in range(1, 7)})
    s.save_stations(P, "b", {str(i): f"b{i}" for i in range(1, 5)})
    if with_result:
        form(s, 1, clean_report(), muni="A")
    s.close()


def test_sample_cli_is_read_only_requires_retrospective_opt_in_and_never_overwrites(tmp_path, capsys):
    db = tmp_path / "election.sqlite"
    make_db(db)
    original = db.read_bytes()
    args = ["estimate-sample", "--db", str(db), "--parent", P, "--size", "6", "--seed", "fixed"]
    assert main(args) == 2
    assert "retrospective" in capsys.readouterr().err
    assert db.read_bytes() == original
    assert main(args + ["--retrospective"]) == 0
    path = db.with_suffix(".sample.json")
    sample = json.loads(path.read_text())
    assert sample["retrospective"] is True and sample["size"] == 6
    saved = path.read_bytes()
    assert main(args + ["--retrospective"]) == 2
    assert saved == path.read_bytes()
    assert db.read_bytes() == original


def test_sample_cli_before_results_and_roster_mismatch(tmp_path):
    db = tmp_path / "election.sqlite"
    make_db(db, False)
    assert main(["estimate-sample", "--db", str(db), "--size", "4", "--seed", "s"]) == 0
    assert json.loads(db.with_suffix(".sample.json").read_text())["retrospective"] is False
    s = Store(db)
    s.save_municipalities(P, {"empty": "No roster"})
    with pytest.raises(ValueError, match="roster missing"):
        store_frame(s, P)
    s.close()


def test_replay_cli_reads_explicit_snapshot_without_reanalysis_or_writes(tmp_path, monkeypatch, capsys):
    from rg2watch.browse import build_data
    db = tmp_path / "election.sqlite"
    make_db(db)
    s = Store(db)
    data = build_data(s, tmp_path, P, Config(model=M))
    s.close()
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(data))
    original = db.read_bytes()
    output = tmp_path / "replay.json"
    # Loading a saved snapshot must not rebuild readings or contact a provider.
    monkeypatch.setattr("rg2watch.tally.build_tally", lambda *a, **k: pytest.fail("rebuilt tally"))
    args = ["estimate-replay", "--db", str(db), "--snapshot", str(snapshot),
            "--sizes", "4,8", "--trials", "2", "--out", str(output)]
    assert main(args) == 0
    result = json.loads(output.read_text())
    assert result["reference"]["complete"] is False
    assert result["source_snapshot_built"] == data["built"]
    assert "partial" in capsys.readouterr().out.lower()
    assert original == db.read_bytes()
    assert main(args) == 2
    data["db"] = "other-db"
    snapshot.write_text(json.dumps(data))
    assert main(args[:-2]) == 2


def test_replay_rejects_unknown_identity_and_duplicate_results():
    rows = timed_rows()
    for extra in (rows[0], {**rows[0], "m": "Other"}):
        with pytest.raises(ValueError, match="roster|duplicate"):
            replay.run_replay(frame(), rows + [extra], ["One", "Two"], "P", sizes=[4], seeds=["s"])


def test_upload_times_require_a_clock_and_use_serbian_timezone():
    assert replay.upload_time("2026-10-25T20:00") == replay.upload_time("2026-10-25T19:00Z")
    assert replay.upload_time("2026-07-01T20:00") == replay.upload_time("2026-07-01T18:00Z")
    for value in (None, "2026-10-25", "2026-10-25T20", "bad", "2026-99-99T20:00"):
        assert replay.upload_time(value) is None


def test_presidential_scope_override_uses_the_same_national_sample_framework(tmp_path):
    db = tmp_path / "presidential.sqlite"
    make_db(db, False)
    s = Store(db)
    s.upsert_election(Election(P, "1", "0", "Presidential ballot", BASE))
    s.close()
    cfg = tmp_path / "scope.toml"
    cfg.write_text(f'tally_scope = {{"{P}" = "national"}}')
    args = ["estimate-sample", "--db", str(db), "--size", "4", "--seed", "presidential"]
    assert main(args) == 2
    assert main(args + ["--config", str(cfg)]) == 0


def test_commands_refuse_observed_out_of_frame_station_without_numeric_analysis(tmp_path):
    from rg2watch.browse import build_data
    db = tmp_path / "election.sqlite"
    make_db(db)
    s = Store(db)
    form(s, 99, muni="A")  # no analysis, so not present in tally station_results
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(build_data(s, tmp_path, P, Config(model=M))))
    s.close()
    assert main(["estimate-sample", "--db", str(db), "--size", "6", "--seed", "s",
                 "--retrospective"]) == 2
    assert main(["estimate-replay", "--db", str(db), "--snapshot", str(snapshot),
                 "--sizes", "4", "--trials", "1"]) == 2
