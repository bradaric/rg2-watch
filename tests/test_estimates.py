import copy
import math

import pytest

from rg2watch import estimates as est


def frame(a=6, b=4):
    return [{"m": m, "no": str(i)} for m, n in (("A", a), ("B", b))
            for i in range(1, n + 1)]


def reading(m, no, votes, invalid=0):
    return {"m": m, "no": str(no), "lists": votes, "valid": sum(votes),
            "invalid": invalid, "registered": 1000, "voted": sum(votes) + invalid,
            "ballots_in_box": sum(votes) + invalid, "included": True, "clean": True}


def test_sample_reproducible_without_order_or_outcomes_and_exact_size():
    roster = frame()
    sample = est.make_sample(roster, "P", 7, "fixed", retrospective=False)
    assert sample == est.make_sample(list(reversed(roster)), "P", 7, "fixed", retrospective=False)
    assert len(sample["stations"]) == 7
    assert len({(r["m"], r["no"]) for r in sample["stations"]}) == 7
    assert all(r["population"] / r["selected"] == r["design_weight"] for r in sample["stations"])
    assert {r["m"] for r in sample["stations"]} == {"A", "B"}
    assert est.validate_sample(sample, roster, "P") == sample


def test_sample_minimum_and_census_and_singleton():
    with pytest.raises(ValueError, match="at least 4"):
        est.make_sample(frame(), "P", 3, "s")
    census = est.make_sample(frame(), "P", 10, "s")
    assert len(census["stations"]) == 10
    assert all(r["design_weight"] == 1 for r in census["stations"])
    assert len(est.make_sample(frame(1, 4), "P", 3, "s")["stations"]) == 3
    with pytest.raises(ValueError):
        est.make_sample(frame(), "P", 11, "s")


@pytest.mark.parametrize("change", ["roster", "parent", "membership", "weight", "seed"])
def test_stale_or_modified_manifest_refused(change):
    roster = frame()
    sample = est.make_sample(roster, "P", 5, "seed")
    parent = "P"
    if change == "roster":
        roster.append({"m": "A", "no": "99"})
    elif change == "parent":
        parent = "OTHER"
    elif change == "membership":
        sample["stations"][0]["no"] = "999"
    elif change == "weight":
        sample["stations"][0]["design_weight"] = 999
    else:
        sample["seed"] = "changed"
    with pytest.raises(ValueError):
        est.validate_sample(sample, roster, parent)


def test_duplicate_or_empty_frame_refused():
    for roster in ([], frame() + [frame()[0]], [{"m": "", "no": "1"}]):
        with pytest.raises(ValueError):
            est.make_sample(roster, "P", 4, "s")


def test_projection_expands_votes_not_station_percentages_and_uses_box_denominator():
    roster = frame(4, 2)
    rows = [reading("A", 1, [90, 10]), reading("A", 2, [10, 190]),
            reading("B", 1, [50, 30], 20), reading("B", 2, [60, 40])]
    result = est.estimate(roster, rows, ["One", "Two"])
    assert result["status"] == "experimental"
    # Expanded A votes [200,400], B [110,70]; denominator 600+200.
    assert result["results"][0]["share"] == pytest.approx(310 / 800)
    assert result["results"][1]["share"] == pytest.approx(470 / 800)
    assert result["reporting"] == 4 and result["population"] == 6
    assert result["electorate_coverage"] is None
    p = 310 / 800
    residual = [90 - p * 100, 10 - p * 200]
    variance = 4**2 * (1 - 2 / 4) * sum((x - sum(residual) / 2)**2 for x in residual) / 2
    assert result["results"][0]["se"] == pytest.approx(math.sqrt(variance) / 800)


def test_missing_or_single_reporting_stratum_suppresses_all_national_numbers():
    rows = [reading("A", 1, [80, 20]), reading("A", 2, [20, 80])]
    result = est.estimate(frame(), rows, ["One", "Two"])
    assert result["status"] == "insufficient_coverage" and result["results"] == []
    assert result["strata"][1]["reporting"] == 0
    rows.append(reading("B", 1, [80, 20]))
    assert est.estimate(frame(), rows, ["One", "Two"])["results"] == []


def test_census_matches_raw_and_has_no_sampling_error():
    roster = frame(2, 1)
    rows = [reading("A", 1, [80, 20]), reading("A", 2, [20, 80]), reading("B", 1, [70, 30])]
    result = est.estimate(roster, rows, ["One", "Two"])
    assert result["status"] == "complete"
    assert result["results"][0]["share"] == pytest.approx(170 / 300)
    assert result["results"][0]["se"] == 0


def test_missing_standalone_box_reading_uses_reconciled_valid_plus_invalid_like_tally():
    rows = [reading("A", 1, [80, 20]), reading("A", 2, [20, 80]), reading("B", 1, [70, 30])]
    rows[0]["ballots_in_box"] = None
    result = est.estimate(frame(2, 1), rows, ["One", "Two"])
    assert result["status"] == "complete"
    assert result["results"][0]["share"] == pytest.approx(170 / 300)
    rows[0]["ballots_in_box"] = 999
    assert est.estimate(frame(2, 1), rows, ["One", "Two"])["results"] == []


def test_sample_ignores_nonsample_forms_and_retains_missing_members():
    roster = frame(8, 8)
    sample = est.make_sample(roster, "P", 8, "s", retrospective=True)
    rows = [reading(r["m"], r["no"], [20 + i, 80 - i]) for i, r in enumerate(sample["stations"])]
    sample_ids = {(r["m"], r["no"]) for r in rows}
    other = next(r for r in roster if (r["m"], r["no"]) not in sample_ids)
    result = est.estimate(roster, rows + [reading(other["m"], other["no"], [100, 0])],
                          ["One", "Two"], sample=sample)
    assert result["reporting"] == 8
    assert result["retrospective"] is True
    assert len(result["sample_stations"]) == 8
    partial = est.estimate(roster, rows[:4], ["One", "Two"], sample=sample)
    assert partial["results"] == []
    assert sum(r["reporting"] for r in partial["sample_stations"]) == 4


def test_impossible_excluded_or_incomplete_readings_do_not_contribute():
    rows = [reading("A", i, [20 + i, 80 - i]) for i in range(1, 5)]
    rows += [reading("B", i, [50 + i, 50 - i]) for i in (1, 2)]
    bad = copy.deepcopy(rows)
    bad[0]["included"] = False
    bad[1]["voted"] = 1001
    bad[2]["lists"] = [None, 50]
    result = est.estimate(frame(4, 2), bad, ["One", "Two"])
    assert result["reporting"] == 3
    assert result["results"] == []


def test_unknown_or_duplicate_station_blocks_instead_of_distorting_weights():
    rows = [reading("A", 1, [80, 20]), reading("A", 2, [20, 80])]
    for extra in (rows[0], reading("UNKNOWN", 1, [80, 20])):
        result = est.estimate(frame(), rows + [extra], ["One", "Two"])
        assert result["status"] == "invalid_frame" and result["results"] == []


def test_non_census_zero_variance_is_not_certainty():
    rows = [reading("A", i, [50, 50]) for i in (1, 2)]
    rows += [reading("B", i, [50, 50]) for i in (1, 2)]
    result = est.estimate(frame(), rows, ["One", "Two"])
    assert result["results"] == []
    assert any("variance" in warning for warning in result["blockers"])


def test_store_frame_rejects_duplicate_identities_and_orphan_roster_rows():
    from rg2watch.store import Store
    s = Store(":memory:")
    s.save_municipalities("P", {"a": "A"})
    s.save_stations("P", "a", {"1": "id1", "2": "id2"})
    assert len(est.store_frame(s, "P")) == 2
    # A second station ID for the same number must not collapse silently into a dict.
    s.save_stations("P", "a", {"1": "id3"})
    with pytest.raises(ValueError, match="duplicate"):
        est.store_frame(s, "P")
    s.close()
    s = Store(":memory:")
    s.save_municipalities("P", {"a": "A"})
    s.save_stations("P", "a", {"1": "id1"})
    s.save_stations("P", "missing-municipality", {"1": "id2"})
    with pytest.raises(ValueError):
        est.store_frame(s, "P")
    s.close()
