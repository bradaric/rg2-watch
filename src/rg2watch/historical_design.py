"""Fixed probability samples designed from a completed PRIOR election, never incoming votes.

Ordered pivotal sampling preserves the specified inclusion probabilities and fixed sample size.
Historical electorate/political ordering spreads the sample; strictly positive early-reporting
preferences favour quick returns without silently excluding late-reporting stations.
"""
import copy
import csv
import hashlib
import io
import json
import math
import random
from collections import Counter, defaultdict
from bisect import bisect_right
from statistics import median

from .estimate_replay import upload_time
from .estimates import ballots, checked_frame, frame_hash, identity, usable


METHOD = "prior-election-ordered-pivotal-v1"
RESPONSE_POLICY = {"max_prior_vote_gap_pp": 2.0, "max_prior_geography_gap_pp": 3.0,
                   "max_unknown_prior_mass": .05}


def inclusion_probabilities(scores: list[float], size: int) -> list[float]:
    """Capped PPS probabilities, each >0 and <=1, summing to the fixed sample size."""
    if (type(size) is not int or not 1 <= size <= len(scores)
            or any(not math.isfinite(v) or v <= 0 for v in scores)):
        raise ValueError("positive finite scores and a valid fixed sample size are required")
    probabilities = [0.0] * len(scores)
    active = list(range(len(scores)))
    remaining = size
    while active:
        scale = remaining / sum(scores[i] for i in active)
        certainty = [i for i in active if scores[i] * scale >= 1]
        if not certainty:
            for i in active:
                probabilities[i] = scores[i] * scale
            break
        for i in certainty:
            probabilities[i] = 1.0
        remaining -= len(certainty)
        active = [i for i in active if probabilities[i] != 1]
    return probabilities


def pivotal_draw(probabilities: list[float], seed: str) -> list[int]:
    """Ordered two-unit pivotal updates: preserve each marginal pi and the integer total."""
    if (not probabilities or any(not 0 < p <= 1 for p in probabilities)
            or not math.isclose(sum(probabilities), round(sum(probabilities)), abs_tol=1e-8)):
        raise ValueError("pivotal probabilities must be in (0,1] and sum to an integer")
    rng = random.Random(seed)
    selected = []
    carry = None
    for index, q in enumerate(probabilities):
        if q >= 1 - 1e-12:
            selected.append(index)
            continue
        if carry is None:
            carry = (index, q)
            continue
        previous, p = carry
        total = p + q
        if total < 1:
            carry = (previous, total) if rng.random() < p / total else (index, total)
        else:
            if rng.random() < (1 - q) / (2 - total):
                selected.append(previous)
                carry = (index, total - 1)
            else:
                selected.append(index)
                carry = (previous, total - 1)
            if carry[1] <= 1e-12:
                carry = None
    if carry is not None:
        if not math.isclose(carry[1], 1, abs_tol=1e-8):
            raise ValueError("pivotal draw ended with a fractional station")
        selected.append(carry[0])
    if len(selected) != round(sum(probabilities)):
        raise ValueError("pivotal sample size mismatch")
    return sorted(selected)


def _profiles(profiles: list[dict]) -> list[dict]:
    checked_frame(profiles)
    rows = []
    widths = set()
    for p in profiles:
        registered, votes, box = p.get("registered"), p.get("votes"), p.get("ballots")
        if registered is not None and (type(registered) is not int or registered <= 0):
            raise ValueError("historical registered voters must be positive integers or unknown")
        if votes is not None:
            if (not isinstance(votes, list) or not votes or any(type(v) is not int or v < 0 for v in votes)
                    or type(box) is not int or box < sum(votes) or registered is None or box > registered):
                raise ValueError("historical numeric profile is incomplete or impossible")
            widths.add(len(votes))
        if p.get("first_upload") is not None and upload_time(p["first_upload"]) is None:
            raise ValueError("historical first upload must have a valid clock time or be unknown")
        stratum = p.get("stratum", p["m"])
        if not isinstance(stratum, str) or not stratum.strip():
            raise ValueError("historical sampling stratum is missing")
        geography = p.get("geography", stratum)
        if not isinstance(geography, str) or not geography.strip():
            raise ValueError("historical geographic cell is missing")
        rows.append({"m": p["m"], "no": p["no"], "stratum": stratum, "geography": geography,
                     "registered": registered,
                     "votes": list(votes) if votes is not None else None,
                     "ballots": box if votes is not None else None,
                     "first_upload": (upload_time(p["first_upload"]).isoformat()
                                      if p.get("first_upload") else None)})
    if len(widths) > 1:
        raise ValueError("historical profiles mix different ballots")
    return sorted(rows, key=identity)


def geographic_strata(roster: list[dict], coordinates=None) -> dict:
    """Fixed coarse geographic cells, not a requirement to sample every municipality.

    Use the median cached station coordinate for each municipality so that one municipality
    does not straddle cells. Foreign/institutional/unlocated stations remain explicit domains.
    These are sampling cells, not claims about official regional or urban/rural boundaries.
    """
    from .names import to_cyrillic
    from .station_locations import station_coordinates
    coordinates = coordinates or station_coordinates
    locations = defaultdict(list)
    cache_available = True
    for r in roster:
        try:
            point = coordinates(r["m"], r["no"]) if cache_available else None
        except (OSError, ValueError, KeyError):
            cache_available, point = False, None
        if point and all(isinstance(point.get(k), (float, int)) and math.isfinite(point[k])
                         for k in ("latitude", "longitude")):
            locations[r["m"]].append((point["latitude"], point["longitude"]))
    strata = {}
    for r in roster:
        name = to_cyrillic(r["m"]).upper()
        if "ИНОСТРАН" in name:
            label = "geo: foreign"
        elif "ЗАВОД" in name or "УПРАВА ЗА ИЗВРШЕЊЕ" in name:
            label = "geo: institutional"
        elif locations[r["m"]]:
            lat = median(p[0] for p in locations[r["m"]])
            lon = median(p[1] for p in locations[r["m"]])
            y = ("south", "central-south", "central-north", "north")[bisect_right((43.5, 44.5, 45.5), lat)]
            x = ("west", "central", "east")[bisect_right((20.5, 21.5), lon)]
            label = f"geo: {y} / {x}"
        else:
            label = "geo: unlocated"
        strata[identity(r)] = label
    return strata


def historical_profiles(roster: list[dict], readings: list[dict], docs: list,
                         source_parent: str, nlists: int, geography: str = "ordered") -> list[dict]:
    """Final prior readings + first STORED original upload, not the final repeat's upload."""
    by_station = {identity(r): r for r in readings}
    if geography not in ("ordered", "grid", "municipal"):
        raise ValueError("historical geography must be ordered, grid or municipal")
    strata = geographic_strata(roster) if geography != "municipal" else {}
    first = {}
    for d in docs:
        if d["parent"] != source_parent or d["doc_type"] != 6:
            continue
        at = upload_time(d["uploaded_at"])
        key = (d["municipality"], d["station_no"])
        if at is not None:
            first[key] = min(first.get(key, at), at)
    profiles = []
    for station in checked_frame(roster):
        key = identity(station)
        row = by_station.get(key, {})
        registered = row.get("registered")
        known = usable(row, nlists)
        profiles.append({**station,
                         "stratum": "national: prior-balanced" if geography == "ordered"
                         else strata.get(key, station["m"]),
                         "geography": strata.get(key, station["m"]),
                         "registered": registered if type(registered) is int and registered > 0 else None,
                         "votes": row["lists"] if known else None,
                         "ballots": ballots(row) if known else None,
                         "first_upload": first[key].isoformat() if key in first else None})
    return _profiles(profiles)


def _allocation(groups: dict, size: int) -> dict[str, int]:
    allocation = {m: min(2, len(ps)) for m, ps in groups.items()}
    minimum, maximum = sum(allocation.values()), sum(map(len, groups.values()))
    if type(size) is not int or not minimum <= size <= maximum:
        raise ValueError(f"sample size must be at least {minimum} and at most {maximum}")
    masses = {m: sum(p["size_proxy"] for p in ps) for m, ps in groups.items()}
    remaining = size - minimum
    while remaining:
        active = [m for m, ps in groups.items() if allocation[m] < len(ps)]
        total = sum(masses[m] for m in active)
        quotas = {m: remaining * masses[m] / total for m in active}
        for m in active:
            extra = min(len(groups[m]) - allocation[m], int(quotas[m]))
            allocation[m] += extra
            remaining -= extra
        for m in sorted(active, key=lambda m: (-(quotas[m] % 1), m)):
            if remaining and allocation[m] < len(groups[m]):
                allocation[m] += 1
                remaining -= 1
    return allocation


def make_historical_sample(profiles: list[dict], source_parent: str, size: int, seed: str,
                            early_strength: float = 1.0) -> dict:
    profiles = _profiles(profiles)
    if not isinstance(seed, str) or not seed.strip() or not isinstance(source_parent, str) or not source_parent:
        raise ValueError("historical design needs an explicit source election and nonempty seed")
    if type(early_strength) not in (int, float) or not math.isfinite(early_strength) or not 0 <= early_strength <= 4:
        raise ValueError("early preference must be finite and between 0 and 4")
    groups = defaultdict(list)
    default_size = median([p["registered"] for p in profiles if p["registered"] is not None] or [1])
    for p in profiles:
        groups[p["stratum"]].append(p)
    for ps in groups.values():
        proxy = median([p["registered"] for p in ps if p["registered"] is not None] or [default_size])
        for p in ps:
            p["size_proxy"] = p["registered"] or proxy
    allocation = _allocation(groups, size)
    frame, stations = [], []
    for m, ps in groups.items():
        known_times = sorted({p["first_upload"] for p in ps if p["first_upload"] is not None})
        ranks = {at: i / max(1, len(known_times) - 1) for i, at in enumerate(known_times)}
        n = allocation[m]
        scores = [p["size_proxy"] * math.exp(-early_strength * ranks.get(p["first_upload"], 1.0))
                  for p in ps]
        probabilities = inclusion_probabilities(scores, n)
        # Political profiles are auxiliary information from the PRIOR election, not upcoming votes.
        order = sorted(range(len(ps)), key=lambda i: (
            ps[i]["geography"],
            tuple(v / max(1, sum(ps[i]["votes"])) for v in ps[i]["votes"])
            if ps[i]["votes"] is not None else (), ps[i]["size_proxy"], identity(ps[i])))
        chosen = {order[i] for i in pivotal_draw([probabilities[i] for i in order], seed + "\0" + m)}
        for i, p in enumerate(ps):
            row = {"m": p["m"], "no": p["no"], "population": len(ps), "selected": n,
                   "stratum": p["stratum"], "geography": p["geography"],
                   "inclusion_probability": probabilities[i], "design_weight": 1 / probabilities[i],
                   "prior_registered": p["registered"], "size_imputed": p["registered"] is None,
                   "prior_size_proxy": p["size_proxy"],
                   "prior_first_upload": p["first_upload"]}
            frame.append(row)
            if i in chosen:
                stations.append(row)
    # Keep the frozen source inputs, not the temporary size proxies added for score construction.
    for p in profiles:
        del p["size_proxy"]
    inputs = profiles
    source_hash = hashlib.sha256(json.dumps([source_parent, inputs], ensure_ascii=False,
                                            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"version": 2, "method": METHOD, "parent": source_parent, "source_parent": source_parent,
            "source_profiles": inputs, "source_hash": source_hash,
            "frame_hash": frame_hash(profiles), "size": size, "seed": seed,
            "early_strength": early_strength, "retrospective": True,
            "response_policy": dict(RESPONSE_POLICY),
            "frame": sorted(frame, key=identity), "stations": sorted(stations, key=identity)}


def validate_historical_sample(sample: dict, roster: list[dict], parent: str) -> dict:
    try:
        if sample["parent"] != parent or type(sample["retrospective"]) is not bool:
            raise ValueError("historical sample election or retrospective flag invalid")
        if parent == sample["source_parent"] and not sample["retrospective"]:
            raise ValueError("same-source evaluation must be labelled retrospective")
        if sample["frame_hash"] != frame_hash(roster):
            raise ValueError("upcoming roster differs from historical design; station mapping required")
        expected = make_historical_sample(sample["source_profiles"], sample["source_parent"],
                                          sample["size"], sample["seed"], sample["early_strength"])
        expected.update(parent=parent, retrospective=sample["retrospective"])
        if any(sample[k] != v for k, v in expected.items()):
            raise ValueError("historical design manifest changed or cannot be reproduced")
        if "design_diagnostics" in sample:
            diagnostic = sample["design_diagnostics"]
            if (not isinstance(diagnostic, dict)
                    or any(diagnostic[k] != sample[k] for k in ("source_hash", "size", "early_strength"))
                    or type(diagnostic["trials"]) is not int or diagnostic["trials"] < 1
                    or type(diagnostic["p95_full_error_pp"]) not in (int, float)
                    or not math.isfinite(diagnostic["p95_full_error_pp"])
                    or not 0 <= diagnostic["p95_full_error_pp"] <= 100):
                raise ValueError("historical trial diagnostics do not match the frozen design")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid historical design manifest") from exc
    return sample


def activate_sample(sample: dict, roster: list[dict], parent: str, retrospective: bool = False) -> dict:
    validate_historical_sample(sample, roster, sample["parent"])
    if parent == sample["source_parent"] and not retrospective:
        raise ValueError("source election cannot be its own prospective target; use retrospective evaluation")
    out = copy.deepcopy(sample)
    out.update(parent=parent, retrospective=retrospective)
    return validate_historical_sample(out, roster, parent)


def attach_design_diagnostics(sample: dict, report: dict) -> dict:
    """Carry recorded trial evidence with its matching design, without changing the draw."""
    candidate = report["recommendation"]
    if (report["source_parent"] != sample["source_parent"]
            or report["proposal"]["source_hash"] != sample["source_hash"]
            or any(candidate[k] != sample[k] for k in ("size", "early_strength"))):
        raise ValueError("trial report does not match the frozen historical design")
    out = copy.deepcopy(sample)
    out["design_diagnostics"] = {k: sample[k] for k in ("source_hash", "size", "early_strength")}
    out["design_diagnostics"].update(trials=candidate["trials"],
                                      p95_full_error_pp=candidate["p95_full_error_pp"])
    return validate_historical_sample(out, out["source_profiles"], out["parent"])


def historical_benchmark(sample: dict) -> dict:
    """Historical reconstruction, independent of which selected stations report NOW."""
    known_ids = {identity(p) for p in sample["source_profiles"] if p["votes"] is not None}
    full = historical_return_balance(sample, known_ids)
    trial = sample.get("design_diagnostics") or {}
    return {"source_parent": sample["source_parent"], "reference_usable": len(known_ids),
            "reference_population": len(sample["source_profiles"]),
            "sample_max_error_pp": full["share_error_pp"],
            "design_p95_max_error_pp": trial.get("p95_full_error_pp"),
            "design_trials": trial.get("trials")}


def _quantile(values: list[float], q: float) -> float | None:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else None


def design_balance(sample: dict) -> dict:
    """Prior design-weighted auxiliary totals, not current-election response-calibrated totals."""
    totals, observed = Counter(), Counter()
    population = Counter(p["geography"] for p in sample["source_profiles"])
    selected = Counter(p["geography"] for p in sample["stations"])
    for p in sample["source_profiles"]:
        if p["registered"] is not None:
            totals[p["geography"]] += p["registered"]
    for r in sample["stations"]:
        if r["prior_registered"] is not None:
            observed[r["geography"]] += r["prior_registered"] * r["design_weight"]
    actual, sampled = sum(totals.values()), sum(observed.values())
    rows = [{"geography": g, "population": population[g], "selected": selected[g],
             "prior_electorate_share": totals[g] / actual if actual else None,
             "sample_prior_electorate_share": observed[g] / sampled if sampled else None}
            for g in sorted(population)]
    return {"registered_relative_error": abs(sampled - actual) / actual if actual else None,
            "geography_error_pp": (max(abs(r["prior_electorate_share"] - r["sample_prior_electorate_share"]) * 100
                                       for r in rows) if actual and sampled else None),
            "geography": rows}


def _return_reference(sample: dict) -> dict:
    profiles = {identity(p): p for p in sample["source_profiles"]}
    known = [p for p in profiles.values() if p["votes"] is not None]
    box = sum(p["ballots"] for p in known)
    shares = ([sum(p["votes"][i] for p in known) / box for i in range(len(known[0]["votes"]))]
              if box else [])
    geography = Counter()
    for r in sample["frame"]:
        geography[r["geography"]] += r["prior_size_proxy"]
    return {"rows": {identity(r): {**profiles[identity(r)], **r} for r in sample["stations"]},
            "population": Counter(r["stratum"] for r in sample["frame"]),
            "shares": shares, "geography": geography}


def historical_return_balance(sample: dict, reporting_ids, reference: dict | None = None) -> dict:
    """Readiness from frozen PRIOR covariates only; never inspect upcoming vote values."""
    reference = _return_reference(sample) if reference is None else reference
    rows = [reference["rows"][key] for key in reporting_ids if key in reference["rows"]]
    base = Counter()
    for r in rows:
        base[r["stratum"]] += r["design_weight"]
    votes = [0.0] * len(reference["shares"])
    box, unknown = 0.0, 0.0
    geography = Counter()
    for r in rows:
        weight = r["design_weight"] * reference["population"][r["stratum"]] / base[r["stratum"]]
        mass = weight * r["prior_size_proxy"]
        geography[r["geography"]] += mass
        if r["votes"] is None:
            unknown += mass
        else:
            box += weight * r["ballots"]
            for i, value in enumerate(r["votes"]):
                votes[i] += weight * value
    total, target = sum(geography.values()), sum(reference["geography"].values())
    return {"share_error_pp": (max(abs(v / box - p) * 100 for v, p in zip(votes, reference["shares"]))
                               if box and votes else None),
            "geography_error_pp": (max(abs(geography[g] / total - value / target) * 100
                                       for g, value in reference["geography"].items())
                                   if total and target else None),
            "unknown_prior_mass": unknown / total if total else 1.0}


def return_balance_ready(sample: dict, balance: dict) -> bool:
    policy = sample["response_policy"]
    return (balance["share_error_pp"] is not None and balance["geography_error_pp"] is not None
            and balance["share_error_pp"] <= policy["max_prior_vote_gap_pp"] + 1e-12
            and balance["geography_error_pp"] <= policy["max_prior_geography_gap_pp"] + 1e-12
            and balance["unknown_prior_mass"] <= policy["max_unknown_prior_mass"] + 1e-12)


def _timing(sample: dict, profiles: list[dict], origin) -> dict:
    by_station = {identity(p): p for p in profiles}
    rows = sorted(((upload_time(p["first_upload"]), p) for r in sample["stations"]
                   if (p := by_station[identity(r)])["votes"] is not None
                   and upload_time(p["first_upload"]) is not None),
                  key=lambda item: (item[0], identity(item[1])))
    # Timestamp ties enter together, not in an arbitrary station order.
    by_time = defaultdict(list)
    for at, p in rows:
        by_time[at].append(p)
    population = Counter(p["stratum"] for p in profiles)
    counts = Counter()
    out = {"time_to_80_s": None, "time_to_90_s": None, "release_s": None, "release_ids": []}
    masses = {identity(r): r["design_weight"] * r["prior_size_proxy"] for r in sample["stations"]}
    total_mass, received_mass = sum(masses.values()), 0.0
    reference = _return_reference(sample)
    received = []
    for at, ps in sorted(by_time.items()):
        received.extend(ps)
        received_mass += sum(masses[identity(p)] for p in ps)
        counts.update(p["stratum"] for p in ps)
        elapsed = (at - origin).total_seconds()
        for fraction, key in ((.8, "time_to_80_s"), (.9, "time_to_90_s")):
            if out[key] is None and len(received) >= sample["size"] * fraction:
                out[key] = elapsed
        if (out["release_s"] is None and len(received) >= sample["size"] * .8
                and received_mass / total_mass >= .8 - 1e-12
                and all(counts[m] >= min(2, n) for m, n in population.items())):
            ids = [identity(p) for p in received]
            if return_balance_ready(sample, historical_return_balance(sample, ids, reference)):
                out["release_s"] = elapsed
                out["release_ids"] = ids
    return out


def benchmark_design(profiles: list[dict], source_parent: str, sizes: list[int],
                      strengths: list[float], seeds: list[str], proposal_seed: str) -> dict:
    from .estimates import estimate
    profiles = _profiles(profiles)
    if not seeds or not strengths or not sizes:
        raise ValueError("historical design needs sample sizes, preferences and trial seeds")
    roster = checked_frame(profiles)
    known = [p for p in profiles if p["votes"] is not None]
    if not known or not sum(p["ballots"] for p in known):
        raise ValueError("no historical numerical reference available")
    names = [f"Row {i + 1}" for i in range(len(known[0]["votes"]))]
    denominator = sum(p["ballots"] for p in known)
    reference = [sum(p["votes"][i] for p in known) / denominator for i in range(len(names))]
    origin = min((upload_time(p["first_upload"]) for p in profiles if p["first_upload"]), default=None)
    if origin is None:
        raise ValueError("no historical upload timing reference available")
    readings = [{"m": p["m"], "no": p["no"], "included": True, "registered": p["registered"],
                 "voted": p["ballots"], "valid": sum(p["votes"]),
                 "invalid": p["ballots"] - sum(p["votes"]), "ballots_in_box": p["ballots"],
                 "lists": p["votes"]} for p in known]
    candidates = []
    for size in sizes:
        for strength in strengths:
            full_errors, release_errors, registrations, geo_errors = [], [], [], []
            t80, t90, releases = [], [], []
            try:
                for seed in seeds:
                    sample = make_historical_sample(profiles, source_parent, size, seed, strength)
                    result = estimate(roster, readings, names, sample)
                    # Score EVERY full sample, including those whose prior-balance gate stays closed.
                    # Otherwise conditioning on release would hide poorly balanced draws.
                    full_gap = result["historical_return_balance"]["share_error_pp"]
                    if full_gap is not None:
                        full_errors.append(full_gap)
                    balance = result["historical_balance"]
                    if balance["registered_relative_error"] is not None:
                        registrations.append(balance["registered_relative_error"])
                    if balance["geography_error_pp"] is not None:
                        geo_errors.append(balance["geography_error_pp"])
                    timing = _timing(sample, profiles, origin)
                    if timing["time_to_80_s"] is not None:
                        t80.append(timing["time_to_80_s"])
                    if timing["time_to_90_s"] is not None:
                        t90.append(timing["time_to_90_s"])
                    if timing["release_s"] is not None:
                        ids = set(timing["release_ids"])
                        early = estimate(roster, [r for r in readings if identity(r) in ids], names, sample)
                        if early["results"]:
                            releases.append(timing["release_s"])
                            release_errors.append(max(abs(r["share"] - p) * 100
                                                      for r, p in zip(early["results"], reference)))
            except ValueError as exc:
                candidates.append({"size": size, "early_strength": strength,
                                   "status": "invalid_size", "reason": str(exc)})
                continue
            candidates.append({"size": size, "early_strength": strength, "status": "diagnostic",
                               "trials": len(seeds), "released": len(releases),
                               "release_rate": len(releases) / len(seeds),
                               "p95_full_error_pp": _quantile(full_errors, .95),
                               "p95_release_error_pp": _quantile(release_errors, .95),
                               "p95_registered_relative_error": _quantile(registrations, .95),
                               "p95_geography_error_pp": _quantile(geo_errors, .95),
                               "p90_time_to_80_s": _quantile(t80, .9),
                               "p90_time_to_90_s": _quantile(t90, .9),
                               "p90_release_s": _quantile(releases, .9)})
    valid = [c for c in candidates if c["status"] == "diagnostic" and c["p95_full_error_pp"] is not None]
    if not valid:
        raise ValueError("no historical sample size produced a usable geographic estimate")
    feasible = [c for c in valid if c["release_rate"] >= .9 and c["p95_full_error_pp"] <= 1
                and c["p95_release_error_pp"] is not None and c["p95_release_error_pp"] <= 2
                and c["p95_registered_relative_error"] is not None
                and c["p95_registered_relative_error"] <= .05
                and c["p95_geography_error_pp"] is not None and c["p95_geography_error_pp"] <= 3]
    chosen = (min(feasible, key=lambda c: (c["size"], c["p90_release_s"], c["p95_full_error_pp"]))
              if feasible else min(valid, key=lambda c: (-c["release_rate"], c["p95_full_error_pp"], c["size"])))
    proposal = make_historical_sample(profiles, source_parent, chosen["size"], proposal_seed,
                                      chosen["early_strength"])
    actual = estimate(roster, readings, names, proposal)
    timing = _timing(proposal, profiles, origin)
    proposal_diagnostics = {
        "full_error_pp": actual["historical_return_balance"]["share_error_pp"],
        "registered_relative_error": actual["historical_balance"]["registered_relative_error"],
        "geography_error_pp": actual["historical_balance"]["geography_error_pp"],
        **{k: timing[k] for k in ("time_to_80_s", "time_to_90_s", "release_s")}}
    report = {"version": 1, "source_parent": source_parent, "accuracy_validated": False,
            "source_population": len(profiles), "source_usable": len(known),
            "source_reference_complete": len(known) == len(profiles),
            "source_first_upload": origin.isoformat(), "candidates": candidates,
            "strata_population": dict(sorted(Counter(p["stratum"] for p in profiles).items())),
            "geography_population": dict(sorted(Counter(p["geography"] for p in profiles).items())),
            "recommendation": {**chosen, "meets_historical_targets": bool(feasible)},
            "proposal": proposal, "proposal_diagnostics": proposal_diagnostics,
            "warnings": ["Designed and evaluated on the same PRIOR election; not independent future validation.",
                         "Earliest stored original upload is a timing predictor, not verified as-of numeric availability.",
                         "Unknown prior profiles remain eligible with positive inclusion probability and labelled size proxies.",
                         "No upcoming results or arrival order enter selection; confirm station mapping before activation.",
                         "Unequal-probability pivotal sampling does not use the old SRS confidence-interval formula.",
                         "Release requires 80% of both station count and design-weighted prior-electorate mass; "
                         "that mass is a historical proxy, not current-electorate coverage.",
                         "Release also requires returned stations to match prior vote/geographic profiles. "
                         "The release residual is a balance-gate diagnostic, not independent forecast validation."]}
    report["proposal"] = attach_design_diagnostics(proposal, report)
    return report


def design_summary(report: dict) -> str:
    lines = [f"PRIOR-election fixed-sample design — source {report['source_parent']}",
             f"Historical numeric reference: {report['source_usable']} / {report['source_population']} stations.",
              "size | early preference | release trials | p95 full/release error pp | p90 count-80% / release (hours)"]
    fmt = lambda n: "unavailable" if n is None else f"{n:.3f}"
    hours = lambda n: fmt(n / 3600 if n is not None else None)
    for row in report["candidates"]:
        if row["status"] == "invalid_size":
            lines.append(f"{row['size']} / {row['early_strength']}: {row['reason']}")
            continue
        lines.append(f"{row['size']} | {row['early_strength']} | {row['released']}/{row['trials']} | "
                     f"{fmt(row['p95_full_error_pp'])} / {fmt(row['p95_release_error_pp'])} | "
                     f"{hours(row['p90_time_to_80_s'])} / {hours(row['p90_release_s'])}")
    chosen = report["recommendation"]
    lines.append(f"Proposed size {chosen['size']}, early preference {chosen['early_strength']}; "
                 f"historical diagnostic targets {'met' if chosen['meets_historical_targets'] else 'NOT met'}.")
    actual = report["proposal_diagnostics"]
    lines.append(f"Predeclared proposal seed: {report['proposal']['seed']}; "
                 f"actual prior full-share error {fmt(actual['full_error_pp'])} pp; "
                 f"time to 80% / release {hours(actual['time_to_80_s'])} / {hours(actual['release_s'])} hours.")
    lines.extend(report["warnings"])
    if not report["source_reference_complete"]:
        lines.append("Partial prior reference: errors are versus known stored votes, not certified national accuracy.")
    return "\n".join(lines)


def proposal_csv(sample: dict) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    fields = ("m", "no", "stratum", "geography", "population", "selected", "inclusion_probability",
              "design_weight", "prior_registered", "prior_first_upload", "size_imputed")
    writer.writerow([*fields, "source_parent", "seed", "source_hash"])
    def safe(value):
        return "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
    for row in sample["stations"]:
        writer.writerow([safe(row[k]) for k in fields] +
                        [safe(sample[k]) for k in ("source_parent", "seed", "source_hash")])
    return out.getvalue()
