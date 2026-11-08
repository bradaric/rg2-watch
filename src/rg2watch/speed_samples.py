"""Two frozen, deadline-constrained PRIOR-fit samples. Not probability sampling."""
import copy
import hashlib
import json
import random
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .estimates import ballots, checked_frame, frame_hash, identity, unavailable, usable
from .estimate_replay import upload_time
from .historical_design import _profiles


METHOD = "prior-deadline-balanced-purposive-v1"
LABELS = {"quick": "Quick", "morning": "Morning"}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _seal(bundle):
    bundle["manifest_hash"] = _hash({k: v for k, v in bundle.items() if k != "manifest_hash"})
    return bundle


def _deadlines(profiles):
    first = min((upload_time(p["first_upload"]) for p in profiles if p["first_upload"]), default=None)
    if first is None:
        raise ValueError("prior upload timestamps required")
    tz = ZoneInfo("Europe/Belgrade")
    midnight = datetime.combine(first.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    return {"quick": midnight.replace(hour=23), "morning": (midnight + timedelta(days=1)).replace(hour=8)}


def _reference(ps):
    known = [p for p in ps if p["votes"] is not None]
    denominator = sum(p["ballots"] for p in known)
    if denominator <= 0:
        raise ValueError("usable prior numerical reference required")
    registered = [p["registered"] for p in ps if p["registered"] is not None]
    geography, population = Counter(), Counter(p["geography"] for p in ps)
    for p in ps:
        if p["registered"] is not None:
            geography[p["geography"]] += p["registered"]
    total = sum(registered)
    return {"shares": [sum(p["votes"][i] for p in known) / denominator
                       for i in range(len(known[0]["votes"]))],
            "box_mean": denominator / len(known), "reg_mean": total / len(registered),
            "registered": total, "geography": {g: geography[g] / total for g in sorted(population)},
            "population": population, "usable": len(known)}


def _eligible(ps, cutoff):
    return [p for p in ps if p["votes"] is not None and p["first_upload"] is not None
            and upload_time(p["first_upload"]) <= cutoff]


def _fit(rows, ref):
    denominator = sum(p["ballots"] for p in rows)
    registered = sum(p["registered"] for p in rows)
    geography, counts = Counter(), Counter(p["geography"] for p in rows)
    for p in rows:
        geography[p["geography"]] += p["registered"]
    return {"share_error_pp": (max(abs(sum(p["votes"][i] for p in rows) / denominator - value) * 100
                                  for i, value in enumerate(ref["shares"])) if denominator else None),
            "registered_relative_error": abs(registered / len(rows) - ref["reg_mean"]) / ref["reg_mean"] if rows else None,
            "geography_error_pp": (max(abs(geography[g] / registered - share) * 100
                                       for g, share in ref["geography"].items()) if registered else None),
            "missing_domains": [g for g in ref["geography"] if not counts[g]],
            "geography": [{"geography": g, "population": ref["population"][g], "selected": counts[g],
                           "prior_electorate_share": share,
                           "sample_prior_electorate_share": geography[g] / registered if registered else None}
                          for g, share in ref["geography"].items()]}


def _select(pool, size, seed, iterations, ref):
    # Same simple seeded swap optimizer used by the predeclared feasibility proposal.
    vectors = [[(p["votes"][i] - share * p["ballots"]) / (ref["box_mean"] * .01)
                for i, share in enumerate(ref["shares"])]
               + [(p["registered"] - ref["reg_mean"]) / (ref["reg_mean"] * .05),
                  (p["ballots"] - ref["box_mean"]) / (ref["box_mean"] * .05)]
               + [p["registered"] * ((p["geography"] == g) - share) / (ref["reg_mean"] * .03)
                  for g, share in ref["geography"].items()] for p in pool]
    rng = random.Random(seed)
    indices = list(range(len(pool)))
    rng.shuffle(indices)
    selected, outside = indices[:size], indices[size:]
    totals = [sum(vectors[i][j] for i in selected) for j in range(len(vectors[0]))]
    objective = sum(v * v for v in totals)
    for _ in range(iterations if outside else 0):
        a, b = rng.randrange(len(selected)), rng.randrange(len(outside))
        left, right = vectors[selected[a]], vectors[outside[b]]
        candidate = [v - y + x for v, y, x in zip(totals, left, right)]
        value = sum(v * v for v in candidate)
        if value < objective:
            totals, objective = candidate, value
            selected[a], outside[b] = outside[b], selected[a]
    return sorted((pool[i] for i in selected), key=identity)


def _station(p):
    return {"m": p["m"], "no": p["no"], "prior_registered": p["registered"],
            "prior_first_upload": p["first_upload"]}


def make_speed_bundle(profiles, source_parent, size, seed, iterations=40000):
    ps = _profiles(profiles)
    if not isinstance(source_parent, str) or not source_parent.strip() or not isinstance(seed, str) or not seed.strip():
        raise ValueError("explicit prior source election and predeclared seed required")
    if type(size) is not int or size < 2 or type(iterations) is not int or iterations < 1:
        raise ValueError("sample size >=2 and positive optimizer iterations required")
    ref, deadlines = _reference(ps), _deadlines(ps)
    samples = {}
    for role, cutoff in deadlines.items():
        pool = _eligible(ps, cutoff)
        if size > len(pool):
            raise ValueError(f"{role}: only {len(pool)} eligible prior stations by cutoff")
        rows = _select(pool, size, seed, iterations, ref)
        samples[role] = {"label": LABELS[role], "cutoff": cutoff.isoformat(),
                         "stations": [_station(p) for p in rows]}
    return _seal({"version": 3, "method": METHOD, "parent": source_parent, "source_parent": source_parent,
                  "source_profiles": ps, "source_hash": _hash([source_parent, ps]),
                  "frame_hash": frame_hash(ps), "retrospective": True, "size": size,
                  "seed": seed, "iterations": iterations, "samples": samples})


def validate_speed_bundle(bundle, roster, parent):
    try:
        if (bundle["version"] != 3 or bundle["method"] != METHOD or bundle["parent"] != parent
                or type(bundle["retrospective"]) is not bool
                or (parent == bundle["source_parent"] and not bundle["retrospective"])):
            raise ValueError("invalid speed-sample election/method/retrospective flag")
        if bundle["frame_hash"] != frame_hash(roster):
            raise ValueError("speed-sample roster changed; station mapping required")
        if bundle["manifest_hash"] != _hash({k: v for k, v in bundle.items() if k != "manifest_hash"}):
            raise ValueError("speed-sample manifest changed")
        ps = _profiles(bundle["source_profiles"])
        if (ps != bundle["source_profiles"] or frame_hash(ps) != bundle["frame_hash"]
                or _hash([bundle["source_parent"], ps]) != bundle["source_hash"]
                or not isinstance(bundle["seed"], str) or not bundle["seed"].strip()
                or type(bundle["size"]) is not int or bundle["size"] < 2
                or type(bundle["iterations"]) is not int or bundle["iterations"] < 1
                or set(bundle["samples"]) != set(LABELS)):
            raise ValueError("invalid frozen speed-sample inputs")
        by_id = {identity(p): p for p in ps}
        for role, cutoff in _deadlines(ps).items():
            sample = bundle["samples"][role]
            rows = sample["stations"]
            checked_frame(rows)
            if sample["label"] != LABELS[role] or sample["cutoff"] != cutoff.isoformat() or len(rows) != bundle["size"]:
                raise ValueError("invalid speed-sample deadline/size")
            for row in rows:
                p = by_id[identity(row)]
                if (p["votes"] is None or p["first_upload"] is None or upload_time(p["first_upload"]) > cutoff
                        or row != _station(p)):
                    raise ValueError("speed-sample member differs from frozen eligible profile")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid speed-sample manifest") from exc
    return bundle


def activate_speed_bundle(bundle, roster, parent, retrospective=False):
    validate_speed_bundle(bundle, roster, bundle["parent"])
    if parent == bundle["source_parent"] and not retrospective:
        raise ValueError("same-source activation must be retrospective")
    out = copy.deepcopy(bundle)
    out.update(parent=parent, retrospective=retrospective)
    _seal(out)
    return validate_speed_bundle(out, roster, parent)


def speed_estimate(bundle, role, readings, names):
    ps, sample = bundle["source_profiles"], bundle["samples"][role]
    by_id, ref = {identity(p): p for p in ps}, _reference(ps)
    selected = {identity(r) for r in sample["stations"]}
    seen, rows = set(), []
    for r in readings:
        key = identity(r)
        if key not in by_id or key in seen:
            return unavailable("result station outside roster or duplicated", "invalid_frame")
        seen.add(key)
        if key in selected and usable(r, len(names)):
            rows.append(r)
    reporting_ids = {identity(r) for r in rows}
    prior_selected = [by_id[key] for key in sorted(selected)]
    prior_received = [by_id[key] for key in sorted(reporting_ids)]
    fit, received_fit = _fit(prior_selected, ref), _fit(prior_received, ref)
    prior_response = sum(p["registered"] for p in prior_received) / sum(p["registered"] for p in prior_selected)
    pool = _eligible(ps, upload_time(sample["cutoff"]))
    pool_domains = {p["geography"] for p in pool}
    warnings = [
        "Experimental purposive prior-balanced sample, NOT a national probability sample; no probability-based interval or margin.",
        "Historical fitting errors use the same source election as selection; not independent future validation.",
        "All-selected completion deadline is a historical target, not a future reporting-time guarantee or analysis-ready time.",
        "Late/incomplete prior profiles are excluded; missing domains and political change can bias the national estimate.",
        "Partial returns require exchangeable missing selected stations; a small historical residual does not guarantee accuracy.",
        "Shares use valid + invalid ballots, matching counted results; not legal threshold calculations.",
        "Quick and Morning may overlap and share training data; agreement is not independent corroboration."]
    if bundle["retrospective"]:
        warnings.append("Retrospective sample: built using this election's own final prior profiles.")
    out = {"status": "insufficient_coverage", "method": METHOD, "design_kind": "speed_balanced_nonprobability",
           "sample_label": sample["label"], "sample_role": role, "completion_target": sample["cutoff"],
           "source_parent": bundle["source_parent"], "source_hash": bundle["source_hash"],
           "frame_hash": bundle["frame_hash"], "seed": bundle["seed"], "retrospective": bundle["retrospective"],
           "population": len(ps), "sample_size": bundle["size"], "selected": len(selected), "reporting": len(rows),
           "response_rate": len(rows) / len(selected), "station_coverage": len(rows) / len(ps),
           "electorate_coverage": None, "prior_electorate_response": prior_response,
           "sample_complete": len(rows) == len(selected), "interval_kind": "not_probability_sample",
           "strata": [], "strata_kind": "purposive", "blockers": [], "warnings": warnings, "results": [],
           "sample_stations": [{**r, "reporting": identity(r) in reporting_ids} for r in sample["stations"]],
           "historical_balance": fit, "historical_return_balance": received_fit,
           "historical_benchmark": {"source_parent": bundle["source_parent"], "reference_usable": ref["usable"],
                                    "reference_population": len(ps), "sample_max_error_pp": fit["share_error_pp"],
                                    "design_p95_max_error_pp": None, "design_trials": None},
           "speed_fit": {**fit, "eligible_count": len(pool),
                         "eligible_prior_electorate_fraction": sum(p["registered"] for p in pool) / ref["registered"],
                         "pool_missing_domains": sorted(set(ref["geography"]) - pool_domains)}}
    if len(rows) < .8 * len(selected) or prior_response < .8 - 1e-12:
        out["blockers"].append("less than 80% of selected station count or selected prior-electorate proxy has reported")
    if (received_fit["share_error_pp"] is None or received_fit["share_error_pp"] > 2 + 1e-12
            or received_fit["geography_error_pp"] is None or received_fit["geography_error_pp"] > 3 + 1e-12):
        out["blockers"].append("returned selected stations do not yet meet prior political/geographic balance limits")
    denominator = sum(ballots(r) for r in rows)
    if denominator <= 0 or not names:
        out["blockers"].append("no usable current ballots/candidates")
    if not out["blockers"]:
        out["status"] = "experimental"
        out["results"] = [{"row": i + 1, "name": name,
                           "share": sum(r["lists"][i] for r in rows) / denominator,
                           "se": None, "low": None, "high": None, "margin_pp": None}
                          for i, name in enumerate(names)]
    return out
