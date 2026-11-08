import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from rg2watch import speed_samples as speed
from rg2watch.estimates import sample_path, store_frame, validate_sample
from test_estimates import reading


def profiles():
    return [{"m": "A", "no": str(i+1), "stratum": "national", "geography": "geo: main",
             "registered": 1000, "votes": [50, 50], "ballots": 100,
             "first_upload": (datetime(2026, 10, 25, 19, tzinfo=timezone.utc)
                              + timedelta(minutes=15*i)).isoformat()} for i in range(36)]


def make_bundle():
    return speed.make_speed_bundle(profiles(), "PRIOR", 8, "declared", iterations=100)


def test_two_exact_frozen_sizes_meet_distinct_prior_deadlines_and_are_reproducible():
    ps = profiles()
    bundle = make_bundle()
    assert bundle == speed.make_speed_bundle(list(reversed(ps)), "PRIOR", 8, "declared", iterations=100)
    assert set(bundle["samples"]) == {"quick", "morning"}
    assert bundle["samples"]["quick"]["cutoff"] == "2026-10-25T23:00:00+01:00"
    assert bundle["samples"]["morning"]["cutoff"] == "2026-10-26T08:00:00+01:00"
    for role, sample in bundle["samples"].items():
        assert len(sample["stations"]) == 8
        assert len({(r["m"], r["no"]) for r in sample["stations"]}) == 8
        cutoff = datetime.fromisoformat(sample["cutoff"])
        assert all(datetime.fromisoformat(r["prior_first_upload"]) <= cutoff for r in sample["stations"])
        assert all("inclusion_probability" not in r and "design_weight" not in r for r in sample["stations"])
    assert speed.validate_speed_bundle(bundle, ps, "PRIOR") == bundle
    assert validate_sample(bundle, ps, "PRIOR") == bundle


@pytest.mark.parametrize("field", ["stations", "cutoff", "source_profiles", "parent", "size"])
def test_changed_bundle_fails_closed(field):
    bundle = make_bundle()
    if field == "stations":
        bundle["samples"]["quick"]["stations"][0]["no"] = "999"
    elif field == "cutoff":
        bundle["samples"]["morning"]["cutoff"] = "2026-10-30T08:00:00+01:00"
    elif field == "source_profiles":
        bundle[field][0]["votes"][0] += 1
    else:
        bundle[field] = "OTHER"
    with pytest.raises(ValueError):
        speed.validate_speed_bundle(bundle, profiles(), "PRIOR")


def test_future_activation_checks_complete_roster_and_keeps_both_memberships():
    bundle = make_bundle()
    future = speed.activate_speed_bundle(bundle, profiles(), "FUTURE")
    assert future["samples"] == bundle["samples"]
    assert future["source_parent"] == "PRIOR" and future["parent"] == "FUTURE"
    assert not future["retrospective"]
    assert speed.validate_speed_bundle(future, profiles(), "FUTURE") == future
    with pytest.raises(ValueError, match="roster"):
        speed.activate_speed_bundle(bundle, profiles()[:-1], "FUTURE")
    with pytest.raises(ValueError, match="retrospective|source"):
        speed.activate_speed_bundle(bundle, profiles(), "PRIOR")


def test_speed_estimate_uses_only_fixed_members_and_never_emits_probability_intervals():
    bundle = make_bundle()
    bundle = speed.activate_speed_bundle(bundle, profiles(), "FUTURE")
    chosen = bundle["samples"]["quick"]["stations"]
    rows = [reading(r["m"], r["no"], [20+i*3, 80-i*3], invalid=i%2) for i, r in enumerate(chosen)]
    result = speed.speed_estimate(bundle, "quick", rows, ["One", "Two"])
    assert result["status"] == "experimental" and result["sample_complete"] is True
    assert result["interval_kind"] == "not_probability_sample"
    assert result["results"][0]["share"] == pytest.approx(sum(r["lists"][0] for r in rows)/sum(r["valid"]+r["invalid"] for r in rows))
    assert all(r["low"] is None and r["high"] is None and r["se"] is None and r["margin_pp"] is None for r in result["results"])
    other = next(p for p in profiles() if (p["m"], p["no"]) not in {(r["m"], r["no"]) for r in chosen})
    assert speed.speed_estimate(bundle, "quick", rows+[reading(other["m"],other["no"],[100,0])], ["One", "Two"])["results"] == result["results"]
    held = speed.speed_estimate(bundle, "quick", rows[:4], ["One", "Two"])
    assert held["results"] == [] and len(held["sample_stations"]) == 8
    assert held["sample_complete"] is False
    # New candidate counts never map onto old list numbers.
    presidential = [reading(r["m"],r["no"],[20,30,50]) for r in chosen]
    assert len(speed.speed_estimate(bundle, "quick", presidential, ["A","B","C"])["results"]) == 3


def test_speed_snapshot_integrates_both_samples_but_never_changes_public_results(tmp_path):
    from rg2watch.browse import build_data
    from rg2watch.config import Config
    from rg2watch.public_results import build_public_results
    from test_operator_estimates import populate
    from test_browse import P
    from test_tally import M
    s = populate(tmp_path/"election.sqlite")
    ps = [{**r,"registered":1000,"votes":[50,50],"ballots":100,"geography":"geo: main",
           "first_upload":"2026-10-25T20:00:00Z"} for r in store_frame(s,P)]
    cfg=Config(model=M)
    public_before=build_public_results(s,cfg,P)
    bundle=speed.make_speed_bundle(ps,P,6,"s",iterations=10)
    sample_path(s).write_text(json.dumps(bundle))
    data=build_data(s,tmp_path,P,cfg)
    out=data["tally"]["estimates"]
    assert set(out["samples"]) == {"quick","morning"}
    assert out["sample"] == out["samples"]["quick"]
    assert all(v["reporting"]==6 and v["status"]=="experimental" for v in out["samples"].values())
    assert out["arrivals"]["status"]=="complete"
    public_after=build_public_results(s,cfg,P)
    assert {k:v for k,v in public_after.items() if k!="built"}=={k:v for k,v in public_before.items() if k!="built"}
    s.close()


def test_unknown_profiles_and_late_domains_are_explicitly_excluded_not_zero_filled():
    ps=profiles()
    ps[-1].update(geography="geo: foreign",first_upload="2026-10-28T12:00:00Z")
    ps[0].update(votes=None,ballots=None)
    bundle=speed.make_speed_bundle(ps,"PRIOR",8,"s",iterations=20)
    result=speed.speed_estimate(bundle,"quick",[],["One","Two"])
    assert result["historical_benchmark"]["reference_usable"]==35
    assert "geo: foreign" in result["speed_fit"]["missing_domains"]
    assert all(r["no"]!="1" for sample in bundle["samples"].values() for r in sample["stations"])
    with pytest.raises(ValueError,match="eligible|cutoff|stations"):
        speed.make_speed_bundle(ps,"PRIOR",100,"s",iterations=20)


def test_speed_builder_cli_is_offline_and_never_overwrites(tmp_path, monkeypatch):
    from rg2watch.cli import main
    from rg2watch.historical_design import make_historical_sample
    source=tmp_path/"prior.sample.json"
    source.write_text(json.dumps(make_historical_sample(profiles(),"PRIOR",8,"s")))
    before=source.read_bytes()
    out=tmp_path/"speed.json"
    monkeypatch.setattr("rg2watch.cli.Store",lambda *a,**k: pytest.fail("offline builder must not open any DB"))
    args=["estimate-speed-samples","--history-sample",str(source),"--seed","declared","--size","8",
          "--iterations","100","--out",str(out)]
    assert main(args)==0
    bundle=json.loads(out.read_text())
    assert set(bundle["samples"])=={"quick","morning"}
    assert source.read_bytes()==before
    assert main(args)==2


def test_future_speed_cli_activation_uses_roster_not_current_votes(tmp_path, monkeypatch):
    from rg2watch.cli import main
    from rg2watch.models import Election
    from rg2watch.store import Store
    path=tmp_path/"speed.json"
    path.write_text(json.dumps(make_bundle()))
    db=tmp_path/"next.sqlite"
    s=Store(db)
    s.upsert_election(Election("NEXT","1","2","Next","http://rik.example"))
    s.save_municipalities("NEXT",{"a":"A"})
    s.save_stations("NEXT","a",{p["no"]:"id"+p["no"] for p in profiles()})
    s.close()
    monkeypatch.setattr(Store,"browse_analyses",lambda *a,**k: pytest.fail("future votes must not select stations"))
    args=["estimate-sample","--db",str(db),"--parent","NEXT","--history-design",str(path),"--seed","declared"]
    assert main(args)==2
    assert main(args+["--confirm-station-mapping"])==0
    active=json.loads(db.with_suffix('.sample.json').read_text())
    assert active["samples"]==json.loads(path.read_text())["samples"]
    assert active["parent"]=="NEXT" and not active["retrospective"]


def test_speed_bundle_cannot_accidentally_enter_probability_estimator():
    from rg2watch.estimates import estimate
    out=estimate(profiles(),[],["One","Two"],make_bundle())
    assert out["status"]=="unsupported" and out["results"]==[]


def test_fit_and_membership_do_not_mutate_inputs_and_exact_pool_size_is_legal():
    ps=profiles()
    before=copy.deepcopy(ps)
    bundle=speed.make_speed_bundle(ps,"PRIOR",13,"s",iterations=100)
    assert ps==before
    assert len(bundle["samples"]["quick"]["stations"])==13
    assert speed.validate_speed_bundle(bundle,ps,"PRIOR")==bundle
