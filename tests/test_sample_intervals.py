import copy
import math
from statistics import mean, pvariance

import pytest

from rg2watch import historical_design as hd
from rg2watch.estimates import estimate
from test_estimates import reading
from test_historical_design import profiles, roster


def sample_data(size=10, census=False):
    ps = profiles(20, 0)
    sample = hd.make_historical_sample(ps, "PRIOR", len(ps) if census else size, "interval")
    sample = hd.activate_sample(sample, roster(ps), "NEXT")
    rows = [reading(r["m"], r["no"], [20 + i * 3, 80 - i * 3], invalid=i % 3)
            for i, r in enumerate(sample["stations"])]
    return ps, sample, rows


def test_weighted_sampling_interval_uses_hh_residual_variance_without_srs_fpc():
    ps, sample, rows = sample_data()
    result = estimate(roster(ps), rows, ["One", "Two"], sample)
    assert result["status"] == "experimental"
    weights = {(r["m"], r["no"]): r["response_weight"] for r in result["sample_stations"]}
    denominator = sum(weights[r["m"], r["no"]] * (r["valid"] + r["invalid"]) for r in rows)
    p = result["results"][0]["share"]
    contributions = [weights[r["m"], r["no"]] * (r["lists"][0] - p * (r["valid"] + r["invalid"]))
                     for r in rows]
    variance = len(rows) / (len(rows) - 1) * sum((v - mean(contributions)) ** 2 for v in contributions)
    se = math.sqrt(variance) / denominator
    row = result["results"][0]
    assert row["se"] == pytest.approx(se)
    assert row["low"] == pytest.approx(max(0, p - 1.96 * se))
    assert row["high"] == pytest.approx(min(1, p + 1.96 * se))
    assert row["margin_pp"] == pytest.approx(196 * se)
    assert result["interval_kind"] == "conditional_95_pivotal_hh"
    assert any("reading errors" in w for w in result["warnings"])
    assert any("release gate" in w for w in result["warnings"])


def test_sampling_interval_ignores_outside_sample_readings_and_prior_votes_as_variance_data():
    ps, sample, rows = sample_data()
    result = estimate(roster(ps), rows, ["One", "Two"], sample)
    outside = [p for p in ps if (p["m"], p["no"]) not in {(r["m"], r["no"]) for r in rows}]
    extra = [reading(p["m"], p["no"], [100, 0]) for p in outside]
    assert estimate(roster(ps), rows + extra, ["One", "Two"], sample)["results"] == result["results"]
    # Four new candidates instead of the two source lists must use only the new readings.
    four = [reading(r["m"], r["no"], [i, 10, 20, 70 - i]) for i, r in enumerate(rows)]
    assert len(estimate(roster(ps), four, ["A", "B", "C", "D"], sample)["results"]) == 4


def test_census_has_zero_sampling_error_but_zero_observed_sample_variance_is_not_certainty():
    ps, sample, rows = sample_data(census=True)
    out = estimate(roster(ps), rows, ["One", "Two"], sample)
    assert out["interval_kind"] == "census"
    assert all(r["low"] == r["share"] == r["high"] and r["margin_pp"] == 0 for r in out["results"])
    ps, sample, rows = sample_data()
    constant = [reading(r["m"], r["no"], [50, 50]) for r in rows]
    out = estimate(roster(ps), constant, ["One", "Two"], sample)
    assert out["results"] and all(r["low"] is None and r["margin_pp"] is None for r in out["results"])


def test_partial_response_interval_is_conditional_and_gates_still_apply():
    ps, sample, rows = sample_data()
    partial = estimate(roster(ps), rows[:-1], ["One", "Two"], sample)
    assert partial["results"] and partial["results"][0]["se"] > 0
    assert any("exchangeable" in w for w in partial["warnings"])
    held = estimate(roster(ps), rows[:3], ["One", "Two"], sample)
    assert held["results"] == []


def test_historical_panel_benchmark_uses_entire_frozen_sample_not_current_reporting_subset():
    ps, sample, rows = sample_data()
    result = estimate(roster(ps), rows, ["One", "Two"], sample)
    partial = estimate(roster(ps), rows[:-1], ["One", "Two"], sample)
    baseline = result["historical_benchmark"]
    assert baseline == partial["historical_benchmark"]
    assert baseline["source_parent"] == "PRIOR"
    assert baseline["reference_usable"] == baseline["reference_population"] == len(ps)
    expected = hd.historical_return_balance(sample, {(r["m"], r["no"]) for r in sample["stations"]})
    assert baseline["sample_max_error_pp"] == pytest.approx(expected["share_error_pp"])
    assert baseline["design_p95_max_error_pp"] is None


def test_trial_benchmark_provenance_survives_activation_without_changing_selection():
    ps = profiles()
    report = hd.benchmark_design(ps, "PRIOR", [8], [0], ["a", "b", "c"], "fixed")
    frozen = hd.attach_design_diagnostics(report["proposal"], report)
    assert frozen["stations"] == report["proposal"]["stations"]
    active = hd.activate_sample(frozen, roster(ps), "NEXT")
    rs = [reading(r["m"], r["no"], [40 + i, 60 - i]) for i, r in enumerate(active["stations"])]
    out = estimate(roster(ps), rs, ["A", "B"], active)
    benchmark = out["historical_benchmark"]
    assert benchmark["design_trials"] == 3
    assert benchmark["design_p95_max_error_pp"] == report["recommendation"]["p95_full_error_pp"]
    corrupted = copy.deepcopy(active)
    corrupted["design_diagnostics"]["source_hash"] = "wrong"
    with pytest.raises(ValueError):
        hd.validate_historical_sample(corrupted, roster(ps), "NEXT")


def test_hh_variance_is_conservative_in_pivotal_randomization_experiment():
    pi, y = [.1, .3, .6, 1], [10, 60, -20, 100]
    totals, variances = [], []
    for seed in map(str, range(2000)):
        chosen = hd.pivotal_draw(pi, seed)
        values = [y[i] / pi[i] for i in chosen]
        totals.append(sum(values))
        variances.append(len(values) / (len(values) - 1) * sum((x - mean(values)) ** 2 for x in values))
    assert mean(totals) == pytest.approx(sum(y), abs=5)
    assert mean(variances) >= pvariance(totals)


def test_hh_groups_are_independent_and_do_not_treat_between_group_differences_as_variance():
    ps = profiles()
    sample = hd.activate_sample(hd.make_historical_sample(ps, "PRIOR", 8, "s"), roster(ps), "NEXT")
    rows = [reading(r["m"], r["no"], [30 + i, 70 - i] if r["m"] == "A" else [70 + i, 30 - i])
            for i, r in enumerate(sample["stations"])]
    out = estimate(roster(ps), rows, ["One", "Two"], sample)
    weights = {(r["m"], r["no"]): r["response_weight"] for r in out["sample_stations"]}
    p = out["results"][0]["share"]
    variance = 0
    for m in ("A", "B"):
        contributions = [weights[r["m"], r["no"]] * (r["lists"][0] - 100 * p) for r in rows if r["m"] == m]
        variance += len(contributions) / (len(contributions) - 1) * sum((x - mean(contributions))**2 for x in contributions)
    assert out["results"][0]["se"] == pytest.approx(math.sqrt(variance) / out["expanded_ballots"])


def test_partial_prior_reference_is_labelled_and_invalid_trial_values_fail_closed():
    ps, sample, rows = sample_data()
    changed = copy.deepcopy(ps)
    changed[0].update(votes=None, ballots=None)
    sample = hd.make_historical_sample(changed, "PRIOR", 10, "interval")
    out = estimate(roster(changed), rows, ["One", "Two"], sample)
    assert out["historical_benchmark"]["reference_usable"] == 19
    sample["design_diagnostics"] = {k: sample[k] for k in ("source_hash", "size", "early_strength")}
    for value in (float("nan"), float("inf"), -1, True, 101):
        sample["design_diagnostics"].update(trials=30, p95_full_error_pp=value)
        with pytest.raises(ValueError):
            hd.validate_historical_sample(sample, roster(changed), "PRIOR")
