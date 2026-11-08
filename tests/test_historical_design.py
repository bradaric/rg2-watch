import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from rg2watch import historical_design as hd
from rg2watch.estimates import estimate
from test_estimates import reading


def profiles(a=12, b=8):
    return [{"m": m, "no": str(i + 1), "registered": 500 + i * 40,
             "votes": [40 + (i % 20) * 3, 100 - (i % 20) * 3], "ballots": 145,
             "first_upload": (datetime(2026, 10, 25, 20, tzinfo=timezone.utc)
                              + timedelta(minutes=i * 10)).isoformat()}
            for m, n in (("A", a), ("B", b)) for i in range(n)]


def roster(ps):
    return [{"m": p["m"], "no": p["no"]} for p in ps]


def test_fixed_size_unequal_inclusion_probabilities_are_positive_and_sum_to_size():
    for scores, size in (([1, 2, 3, 4], 2), ([100, 1, 1, 1], 2), ([1, 1], 2)):
        pi = hd.inclusion_probabilities(scores, size)
        assert sum(pi) == pytest.approx(size)
        assert all(0 < p <= 1 for p in pi)
    with pytest.raises(ValueError):
        hd.inclusion_probabilities([1, 0], 1)


def test_pivotal_draw_has_fixed_size_and_empirical_marginals_match_design():
    pi = [.1, .3, .6, 1.0]
    counts = Counter()
    for i in range(2000):
        selected = hd.pivotal_draw(pi, str(i))
        assert len(selected) == 2 and len(set(selected)) == 2
        counts.update(selected)
    assert [counts[i] / 2000 for i in range(4)] == pytest.approx(pi, abs=.025)


def test_historical_design_is_reproducible_and_early_preference_changes_probabilities():
    ps = profiles()
    base = hd.make_historical_sample(ps, "PRIOR", 8, "fixed", early_strength=0)
    early = hd.make_historical_sample(ps, "PRIOR", 8, "fixed", early_strength=2)
    assert early == hd.make_historical_sample(list(reversed(ps)), "PRIOR", 8, "fixed", early_strength=2)
    assert len(early["stations"]) == 8
    assert set(r["m"] for r in early["stations"]) == {"A", "B"}
    by = {(r["m"], r["no"]): r for r in early["frame"]}
    neutral = {(r["m"], r["no"]): r for r in base["frame"]}
    assert by["A", "1"]["inclusion_probability"] > neutral["A", "1"]["inclusion_probability"]
    assert by["A", "12"]["inclusion_probability"] < neutral["A", "12"]["inclusion_probability"]
    assert all(r["design_weight"] == pytest.approx(1 / r["inclusion_probability"])
               for r in early["stations"])
    assert hd.validate_historical_sample(early, roster(ps), "PRIOR") == early


def test_imputed_unknown_historical_profile_still_has_nonzero_selection_probability():
    ps = profiles()
    ps[0].update(registered=None, votes=None, ballots=None, first_upload=None)
    sample = hd.make_historical_sample(ps, "PRIOR", 8, "s", early_strength=2)
    row = next(r for r in sample["frame"] if r["m"] == "A" and r["no"] == "1")
    assert 0 < row["inclusion_probability"] <= 1
    assert row["size_imputed"] is True


@pytest.mark.parametrize("field", ["source_parent", "source_profiles", "stations", "early_strength"])
def test_modified_historical_manifest_is_rejected(field):
    ps = profiles()
    sample = hd.make_historical_sample(ps, "PRIOR", 8, "s", early_strength=1)
    if field == "source_profiles":
        sample[field][0]["registered"] += 1
    elif field == "stations":
        sample[field][0]["design_weight"] += .1
    elif field == "early_strength":
        sample[field] = 3
    else:
        sample[field] = "OTHER"
    with pytest.raises(ValueError):
        hd.validate_historical_sample(sample, roster(ps), "PRIOR")


def test_future_activation_does_not_use_future_results_and_requires_matching_frame():
    ps = profiles()
    historical = hd.make_historical_sample(ps, "PRIOR", 8, "s", early_strength=1)
    future = hd.activate_sample(historical, roster(ps), "FUTURE")
    assert future["parent"] == "FUTURE" and future["source_parent"] == "PRIOR"
    assert future["retrospective"] is False
    assert future["stations"] == historical["stations"]
    assert hd.validate_historical_sample(future, roster(ps), "FUTURE") == future
    with pytest.raises(ValueError, match="roster"):
        hd.activate_sample(historical, roster(ps) + [{"m": "B", "no": "99"}], "FUTURE")
    with pytest.raises(ValueError, match="source|retrospective"):
        hd.activate_sample(historical, roster(ps), "PRIOR")


def test_live_historical_sample_uses_frozen_inverse_probabilities_not_equal_weights():
    ps = profiles()
    sample = hd.make_historical_sample(ps, "PRIOR", 8, "s", early_strength=2)
    future = hd.activate_sample(sample, roster(ps), "FUTURE")
    rows = [reading(r["m"], r["no"], [30 + i * 3, 70 - i * 3])
            for i, r in enumerate(future["stations"])]
    out = estimate(roster(ps), rows, ["One", "Two"], future)
    assert out["status"] == "experimental"
    assert out["interval_kind"] == "conditional_95_pivotal_hh"
    assert all(r["low"] < r["share"] < r["high"] and r["se"] > 0 for r in out["results"])
    weights = {}
    for m, N in Counter(p["m"] for p in ps).items():
        source = [r for r in future["stations"] if r["m"] == m]
        scale = N / sum(r["design_weight"] for r in source)
        weights.update({(r["m"], r["no"]): r["design_weight"] * scale for r in source})
    p = sum(weights[r["m"], r["no"]] * r["lists"][0] for r in rows)
    denominator = sum(weights[r["m"], r["no"]] * 100 for r in rows)
    assert out["results"][0]["share"] == pytest.approx(p / denominator)


def test_historical_profiles_use_prior_first_original_upload_not_final_repeat_upload():
    ps = profiles(2, 2)
    rs = [reading(p["m"], p["no"], p["votes"], invalid=5) for p in ps]
    docs = [{"parent": "PRIOR", "municipality": p["m"], "station_no": p["no"],
             "doc_type": 6, "uploaded_at": p["first_upload"]} for p in ps]
    docs += [{**docs[0], "doc_type": 10, "uploaded_at": "2026-11-01T20:00:00Z"},
             {**docs[0], "uploaded_at": "2026-10-26T12:00:00Z"}]
    result = hd.historical_profiles(roster(ps), rs, docs, "PRIOR", 2)
    first = next(p for p in result if p["m"] == "A" and p["no"] == "1")
    assert first["first_upload"] == "2026-10-25T20:00:00+00:00"


def test_historical_benchmark_reports_early_completion_and_does_not_choose_lucky_seed():
    ps = profiles()
    design = hd.benchmark_design(ps, "PRIOR", sizes=[4, 8], strengths=[0, 2],
                                 seeds=["a", "b", "c"], proposal_seed="predeclared")
    assert len(design["candidates"]) == 4
    assert design["source_parent"] == "PRIOR"
    assert design["accuracy_validated"] is False
    assert design["proposal"]["seed"] == "predeclared"
    assert "full_error_pp" in design["proposal_diagnostics"]
    assert "release_s" in design["proposal_diagnostics"]
    assert all("p95_full_error_pp" in c and "p90_time_to_80_s" in c for c in design["candidates"])
    assert design == hd.benchmark_design(list(reversed(ps)), "PRIOR", sizes=[4, 8], strengths=[0, 2],
                                         seeds=["a", "b", "c"], proposal_seed="predeclared")


def test_design_cli_reads_only_explicit_prior_snapshot_and_freezes_fixed_proposal(tmp_path, monkeypatch):
    from rg2watch.browse import build_data
    from rg2watch.cli import main
    from rg2watch.config import Config
    from test_browse import P
    from test_operator_estimates import populate
    from test_tally import M
    db = tmp_path / "prior.sqlite"
    s = populate(db)
    snapshot = tmp_path / "prior.json"
    snapshot.write_text(json.dumps(build_data(s, tmp_path, P, Config(model=M))))
    s.close()
    before = db.read_bytes()
    out = tmp_path / "design.json"
    station_csv = tmp_path / "stations.csv"
    args = ["estimate-design", "--history-db", str(db), "--history-parent", P,
            "--history-snapshot", str(snapshot), "--sizes", "4,6", "--early-strengths", "0,2",
            "--trials", "3", "--seed", "declared-before-next-election", "--out", str(out),
            "--stations-out", str(station_csv)]
    monkeypatch.setattr("rg2watch.tally.build_tally", lambda *a, **k: pytest.fail("must reuse historical readings"))
    from rg2watch.store import Store
    original_snapshot, original_benchmark = Store.read_snapshot, hd.benchmark_design
    active_snapshots = []
    @contextmanager
    def tracked_snapshot(store):
        with original_snapshot(store) as source:
            active_snapshots.append(source)
            try:
                yield source
            finally:
                active_snapshots.pop()
    def unlocked_benchmark(*args, **kwargs):
        assert not active_snapshots, "CPU-heavy design trials must not pin the source WAL read transaction"
        return original_benchmark(*args, **kwargs)
    monkeypatch.setattr(Store, "read_snapshot", tracked_snapshot)
    monkeypatch.setattr(hd, "benchmark_design", unlocked_benchmark)
    assert main(args) == 0
    design = json.loads(out.read_text())
    assert design["source_parent"] == P
    assert design["proposal"]["seed"] == "declared-before-next-election"
    assert design["source_snapshot_built"]
    assert "inclusion_probability" in station_csv.read_text()
    assert len(station_csv.read_text().splitlines()) == design["proposal"]["size"] + 1
    assert db.read_bytes() == before
    assert main(args) == 2  # no overwrite
    second = tmp_path / "second.json"
    index = args.index("--out") + 1
    args[index] = str(second)
    assert main(args) == 2  # pre-existing CSV must be refused before creating the new JSON
    assert not second.exists()


def test_upcoming_sample_activation_requires_station_mapping_and_never_selects_from_target_returns(tmp_path, monkeypatch):
    from rg2watch.cli import main
    from rg2watch.models import Election
    from rg2watch.store import Store
    ps = profiles()
    proposal = hd.make_historical_sample(ps, "PRIOR", 8, "frozen", early_strength=2)
    design_path = tmp_path / "design.json"
    design_path.write_text(json.dumps({"proposal": proposal}))
    db = tmp_path / "upcoming.sqlite"
    s = Store(db)
    s.upsert_election(Election("NEXT", "1", "2", "Upcoming", "http://rik.example"))
    s.save_municipalities("NEXT", {"a": "A", "b": "B"})
    s.save_stations("NEXT", "a", {p["no"]: "a" + p["no"] for p in ps if p["m"] == "A"})
    s.save_stations("NEXT", "b", {p["no"]: "b" + p["no"] for p in ps if p["m"] == "B"})
    s.close()
    before = db.read_bytes()
    monkeypatch.setattr(Store, "browse_analyses", lambda *a, **k: pytest.fail("target votes must not affect selection"))
    args = ["estimate-sample", "--db", str(db), "--parent", "NEXT", "--history-design", str(design_path),
            "--seed", "frozen"]
    assert main(args) == 2
    assert main(args + ["--confirm-station-mapping"]) == 0
    active = json.loads(db.with_suffix(".sample.json").read_text())
    assert active["stations"] == proposal["stations"]
    assert active["parent"] == "NEXT" and active["source_parent"] == "PRIOR"
    assert active["retrospective"] is False
    assert before == db.read_bytes()


def test_proposal_csv_escapes_spreadsheet_formulas_and_preserves_frozen_weights():
    ps = profiles(2, 2)
    for p in ps:
        p["m"] = "=A" if p["m"] == "A" else "B"
    sample = hd.make_historical_sample(ps, "PRIOR", 4, "s", early_strength=2)
    text = hd.proposal_csv(sample)
    assert "'=A" in text
    assert "source_parent" in text and "PRIOR" in text


def test_geographic_strata_use_frozen_prior_locations_and_keep_special_groups():
    ps = profiles(2, 2)
    rs = roster(ps) + [{"m": "Иностранство", "no": "1"}, {"m": "No location", "no": "1"}]
    def coordinates(m, no):
        return {"latitude": 45.7 if m == "A" else 43.0, "longitude": 20.0,
                "location_confidence": 0} if m in ("A", "B") else None
    groups = hd.geographic_strata(rs, coordinates)
    assert groups["A", "1"] == groups["A", "2"]
    assert groups["A", "1"] != groups["B", "1"]
    assert groups["Иностранство", "1"] == "geo: foreign"
    assert groups["No location", "1"] == "geo: unlocated"


def test_coarser_historical_strata_do_not_require_every_municipality_in_small_sample():
    ps = profiles()
    for p in ps:
        p["stratum"] = "geo: region"
    sample = hd.make_historical_sample(ps, "PRIOR", 4, "s", early_strength=2)
    future = hd.activate_sample(sample, roster(ps), "NEXT")
    rows = [reading(r["m"], r["no"], [30 + i * 4, 70 - i * 4])
            for i, r in enumerate(future["stations"])]
    result = estimate(roster(ps), rows, ["One", "Two"], future)
    assert result["strata_kind"] == "historical_geography"
    assert len(result["strata"]) == 1
    assert result["strata"][0]["m"] == "geo: region"
    assert result["status"] == "experimental"
    assert all(r["stratum"] == "geo: region" for r in result["sample_stations"])


def test_historical_design_improves_timeliness_without_excluding_late_stations():
    ps = profiles(50, 50)
    early_average, neutral_average = [], []
    for seed in map(str, range(50)):
        neutral = hd.make_historical_sample(ps, "PRIOR", 20, seed, early_strength=0)
        early = hd.make_historical_sample(ps, "PRIOR", 20, seed, early_strength=3)
        # Synthetic index is the historical upload order within each stratum.
        neutral_average.append(sum(int(r["no"]) for r in neutral["stations"]))
        early_average.append(sum(int(r["no"]) for r in early["stations"]))
        assert all(r["inclusion_probability"] > 0 for r in early["frame"])
    assert sum(early_average) < sum(neutral_average) * .8


@pytest.mark.parametrize("recommendation", [None, {}, {"meets_historical_targets": False},
                                           {"meets_historical_targets": "false"}])
def test_failed_historical_diagnostics_are_not_silently_activated(tmp_path, recommendation):
    from rg2watch.cli import main
    from test_browse import P
    from test_estimate_replay import make_db
    db = tmp_path / "next.sqlite"
    make_db(db, False)
    proposal = hd.make_historical_sample(profiles(6, 4), "PRIOR", 6, "frozen", early_strength=2)
    path = tmp_path / "failed.json"
    path.write_text(json.dumps({"proposal": proposal, "recommendation": recommendation}))
    assert main(["estimate-sample", "--db", str(db), "--parent", P, "--history-design", str(path),
                 "--seed", "frozen", "--confirm-station-mapping"]) == 2
    assert not db.with_suffix(".sample.json").exists()


def test_prior_parliamentary_profile_does_not_map_list_rows_onto_new_presidential_candidates():
    ps = profiles()
    sample = hd.activate_sample(hd.make_historical_sample(ps, "PARLIAMENT", 8, "s"),
                                roster(ps), "PRESIDENT")
    rows = [reading(r["m"], r["no"], [20 + i, 40, 40 - i])
            for i, r in enumerate(sample["stations"])]
    out = estimate(roster(ps), rows, ["Candidate A", "Candidate B", "Candidate C"], sample)
    assert out["status"] == "experimental"
    assert [r["name"] for r in out["results"]] == ["Candidate A", "Candidate B", "Candidate C"]
    assert len(out["results"]) == 3


def test_ordered_geography_preserves_cells_without_forcing_a_return_from_every_domain(monkeypatch):
    ps = profiles()
    rs = [reading(p["m"], p["no"], p["votes"], invalid=5) for p in ps]
    docs = [{"parent": "PRIOR", "municipality": p["m"], "station_no": p["no"],
             "doc_type": 6, "uploaded_at": p["first_upload"]} for p in ps]
    monkeypatch.setattr(hd, "geographic_strata", lambda rows: {
        (r["m"], r["no"]): "geo: north" if r["m"] == "A" else "geo: foreign" for r in rows})
    result = hd.historical_profiles(roster(ps), rs, docs, "PRIOR", 2, geography="ordered")
    assert len({p["stratum"] for p in result}) == 1
    assert {p["geography"] for p in result} == {"geo: north", "geo: foreign"}
    sample = hd.make_historical_sample(result, "PRIOR", 8, "s")
    assert all(r["inclusion_probability"] > 0 for r in sample["frame"])
    assert len({r["stratum"] for r in sample["frame"]}) == 1


def test_historical_electorate_balance_uses_design_weights_not_response_calibrated_totals():
    ps = profiles(50, 50)
    for p in ps:
        p.update(stratum="national: prior-balanced", geography="geo: " + p["m"])
    result = hd.benchmark_design(ps, "PRIOR", sizes=[20], strengths=[0], seeds=["a", "b", "c"],
                                 proposal_seed="declared")
    # Uncapped PPS at strength zero gives b_i*registered_i = R_total/n for every selected unit.
    assert result["candidates"][0]["p95_registered_relative_error"] < 1e-10
    assert "p95_geography_error_pp" in result["candidates"][0]


def test_high_count_response_does_not_hide_missing_high_weight_prior_electorate():
    ps = profiles(6, 4)
    for p in ps:
        p.update(stratum="national: prior-balanced", geography="geo: " + p["m"])
    ps[0]["registered"] = 10000
    sample = hd.activate_sample(hd.make_historical_sample(ps, "PRIOR", 10, "s"), roster(ps), "NEXT")
    rows = [reading(r["m"], r["no"], [40, 60]) for r in sample["stations"]
            if (r["m"], r["no"]) != (ps[0]["m"], ps[0]["no"])]
    result = estimate(roster(ps), rows, ["One", "Two"], sample)
    assert result["response_rate"] == .9
    assert result["prior_electorate_response"] < .8
    assert result["results"] == []
    assert any("prior" in b.lower() for b in result["blockers"])


def test_historical_design_does_not_alias_or_mutate_caller_profiles():
    ps = profiles()
    original = json.dumps(ps, sort_keys=True)
    sample = hd.make_historical_sample(ps, "PRIOR", 8, "s")
    assert json.dumps(ps, sort_keys=True) == original
    assert all("size_proxy" not in p for p in sample["source_profiles"])
    sample["source_profiles"][0]["votes"][0] += 1
    assert json.dumps(ps, sort_keys=True) == original


def test_returned_prior_profile_not_upcoming_votes_controls_readiness():
    ps = profiles(5, 5)
    for p in ps:
        p.update(stratum="national: prior-balanced", geography="geo: common", registered=1000,
                 votes=[90, 10] if p["m"] == "A" else [10, 90], ballots=100)
    sample = hd.activate_sample(hd.make_historical_sample(ps, "PRIOR", 10, "s"), roster(ps), "NEXT")
    missing = ("B", "5")
    rows = [reading(r["m"], r["no"], [40, 60]) for r in sample["stations"]
            if (r["m"], r["no"]) != missing]
    early = estimate(roster(ps), rows, ["One", "Two"], sample)
    assert early["response_rate"] == .9 and early["prior_electorate_response"] == .9
    assert early["results"] == []
    assert early["historical_return_balance"]["share_error_pp"] > 2
    assert any("not independent" in w.lower() for w in early["warnings"])
    changed = [dict(r, lists=[0, 100]) for r in rows]
    other = estimate(roster(ps), changed, ["One", "Two"], sample)
    assert other["historical_return_balance"] == early["historical_return_balance"]
    full = estimate(roster(ps), rows + [reading("B", "5", [100, 0])], ["One", "Two"], sample)
    assert full["status"] == "complete" and full["results"]


def test_historical_release_waits_for_prior_balance_without_reselecting_stations():
    ps = profiles(5, 5)
    for p in ps:
        p.update(stratum="national: prior-balanced", geography="geo: common", registered=1000,
                 votes=[90, 10] if p["m"] == "A" else [10, 90], ballots=100,
                 first_upload=(datetime(2026, 10, 25, 20, tzinfo=timezone.utc)
                               + timedelta(minutes=(0 if p["m"] == "A" else 20) + int(p["no"]))).isoformat())
    result = hd.benchmark_design(ps, "PRIOR", sizes=[10], strengths=[0], seeds=["trial"], proposal_seed="fixed")
    row = result["candidates"][0]
    assert row["p90_release_s"] > row["p90_time_to_80_s"]
    assert result["proposal"]["size"] == 10
    assert result["proposal"]["seed"] == "fixed"
