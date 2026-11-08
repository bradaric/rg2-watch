"""Experimental national estimates. Pure station-cluster statistics; no network or model calls.

The frame is a complete stored roster, not received forms. Geography-only adjustment cannot
remove within-municipality arrival bias. Intervals are conditional, pointwise approximations.
"""
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


METHOD = "municipality-station-expansion-v1"
WARNINGS = [
    "Experimental: municipality adjustment does not remove within-municipality arrival bias.",
    "Shares use valid + invalid ballots, matching counted results; not legal threshold calculations.",
    "Intervals are pointwise normal-approximation 95% intervals conditional on exchangeable "
    "reporting within municipalities; they exclude systematic bias and reading errors.",
    "No verified pre-election station sizes or urban/rural calibration; electorate coverage unknown.",
    "Coverage gates are diagnostic safeguards, not validated ±1 or ±2 percentage-point accuracy.",
]


def identity(row: dict) -> tuple[str, str]:
    return row["m"], row["no"]


def checked_frame(roster: list[dict]) -> list[dict]:
    if not roster or any(not isinstance(r.get(k), str) or not r[k].strip()
                         for r in roster for k in ("m", "no")):
        raise ValueError("station roster is empty or contains missing identities")
    rows = sorted(({"m": r["m"], "no": r["no"]} for r in roster), key=identity)
    if len({identity(r) for r in rows}) != len(rows):
        raise ValueError("duplicate station identities in roster")
    return rows


def frame_hash(roster: list[dict]) -> str:
    value = json.dumps(checked_frame(roster), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def make_sample(roster: list[dict], parent: str, size: int, seed: str,
                retrospective: bool = False) -> dict:
    roster = checked_frame(roster)
    groups = defaultdict(list)
    for r in roster:
        groups[r["m"]].append(r)
    allocation = {m: min(2, len(rs)) for m, rs in groups.items()}
    minimum = sum(allocation.values())
    if type(size) is not int or not minimum <= size <= len(roster):
        raise ValueError(f"sample size must be at least {minimum} and at most {len(roster)}")
    if not isinstance(seed, str) or not seed.strip():
        raise ValueError("sample seed must be a nonempty string")
    capacity = {m: len(groups[m]) - allocation[m] for m in groups}
    remaining = size - minimum
    total = sum(capacity.values())
    if remaining:
        extras = {m: remaining * n // total for m, n in capacity.items()}
        left = remaining - sum(extras.values())
        order = sorted(groups, key=lambda m: (-(remaining * capacity[m] % total), m))
        for m in order[:left]:
            extras[m] += 1
        allocation = {m: n + extras[m] for m, n in allocation.items()}

    def rank(r):
        # Independent stable pseudo-random ranking within each municipality; no result inputs.
        return hashlib.sha256(json.dumps([seed, *identity(r)], ensure_ascii=False).encode()).digest()

    stations = []
    for m, rs in groups.items():
        n = allocation[m]
        stations.extend({**r, "population": len(rs), "selected": n,
                         "design_weight": len(rs) / n}
                        for r in sorted(rs, key=lambda r: (rank(r), identity(r)))[:n])
    return {"version": 1, "method": METHOD, "parent": parent, "size": size, "seed": seed,
            "retrospective": retrospective, "frame_hash": frame_hash(roster),
            "stations": sorted(stations, key=identity)}


def validate_sample(sample: dict, roster: list[dict], parent: str) -> dict:
    if isinstance(sample, dict) and sample.get("version") == 3:
        from .speed_samples import validate_speed_bundle
        return validate_speed_bundle(sample, roster, parent)
    if isinstance(sample, dict) and sample.get("version") == 2:
        from .historical_design import validate_historical_sample
        return validate_historical_sample(sample, roster, parent)
    try:
        if sample["parent"] != parent or type(sample["retrospective"]) is not bool:
            raise ValueError("sample election or retrospective flag invalid")
        expected = make_sample(roster, parent, sample["size"], sample["seed"], sample["retrospective"])
        if any(sample[k] != v for k, v in expected.items()):
            raise ValueError("sample manifest changed or does not match the current station roster")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid sample manifest") from exc
    return sample


def ballots(row: dict) -> int:
    """The shared tally denominator, even when the separate box field is unreadable."""
    return row["valid"] + row["invalid"]


def usable(row: dict, nlists: int) -> bool:
    values = [row.get(k) for k in ("registered", "voted", "valid", "invalid")]
    votes = row.get("lists")
    if not row.get("included") or not isinstance(votes, list) or len(votes) != nlists or not nlists:
        return False
    if any(type(v) is not int or v < 0 for v in [*values, *votes]):
        return False
    registered, voted, valid, invalid = values
    box = row.get("ballots_in_box")
    return (registered >= voted >= valid + invalid and registered > 0
            and sum(votes) == valid
            and (box is None or (type(box) is int and box == valid + invalid)))


def unavailable(reason: str, status: str = "unavailable") -> dict:
    return {"status": status, "blockers": [reason], "warnings": WARNINGS,
            "results": [], "strata": [], "sample_stations": [],
            "reporting": 0, "population": 0, "electorate_coverage": None}


def estimate(roster: list[dict], readings: list[dict], names: list[str],
             sample: dict | None = None) -> dict:
    """Ratio of expanded totals. SRS variance for legacy/arrivals; conservative HH for pivotal.

    Pivotal samples retain inverse-inclusion weights, calibrated to response-group station totals.
    Both partial-response adjustments assume exchangeable response; missing groups are not dropped.
    """
    if isinstance(sample, dict) and sample.get("version") == 3:
        return unavailable("Choose Quick or Morning through the speed estimator; not a probability sample.", "unsupported")
    try:
        roster = checked_frame(roster)
        if sample is not None:
            validate_sample(sample, roster, sample["parent"])
    except (ValueError, KeyError, TypeError) as exc:
        return unavailable(str(exc), "invalid_frame")
    roster_ids = {identity(r) for r in roster}
    seen = set()
    for row in readings:
        key = identity(row)
        if key not in roster_ids or key in seen:
            return unavailable("result station is outside the roster or duplicated", "invalid_frame")
        seen.add(key)
    selected = {identity(r) for r in sample["stations"]} if sample is not None else roster_ids
    historical = sample is not None and sample.get("version") == 2
    design_weights = ({identity(r): r["design_weight"] for r in sample["stations"]}
                      if historical else {})
    stratum_by_station = ({identity(r): r["stratum"] for r in sample["frame"]} if historical else {})
    def group_of(row):
        return stratum_by_station.get(identity(row), row["m"])
    groups = defaultdict(list)
    for row in readings:
        if identity(row) in selected and usable(row, len(names)):
            groups[group_of(row)].append(row)
    population = Counter(group_of(r) for r in roster)
    targets = Counter(stratum_by_station.get(key, key[0]) for key in selected)
    strata = [{"m": m, "population": n, "selected": targets[m], "reporting": len(groups[m]),
               "weight": n / len(groups[m]) if groups[m] else None}
              for m, n in sorted(population.items())]
    scales = {m: n / sum(design_weights.get(identity(r), 1) for r in groups[m])
              if groups[m] else None for m, n in population.items()}

    def weight(row):
        return design_weights.get(identity(row), 1) * scales[group_of(row)]
    reporting = sum(len(rs) for rs in groups.values())
    out = {"status": "insufficient_coverage", "method": METHOD, "population": len(roster),
           "reporting": reporting, "selected": len(selected),
           "station_coverage": reporting / len(roster),
           "response_rate": reporting / len(selected), "electorate_coverage": None,
           "retrospective": bool(sample and sample["retrospective"]),
           "frame_hash": frame_hash(roster), "warnings": list(WARNINGS),
           "strata_kind": "historical_geography" if historical else "municipal",
           "blockers": [], "strata": strata, "results": [], "sample_stations": []}
    if sample is not None:
        reporting_ids = {identity(r) for rs in groups.values() for r in rs}
        out.update(seed=sample["seed"], sample_size=sample["size"],
                    sample_stations=[{**r, "reporting": identity(r) in reporting_ids,
                                      "response_weight": (r["design_weight"] * scales[group_of(r)]
                                                          if historical and scales[group_of(r)] is not None
                                                          else scales[group_of(r)])}
                                    for r in sample["stations"]])
        if historical:
            from .historical_design import (design_balance, historical_benchmark,
                                            historical_return_balance, return_balance_ready)
            prior_mass = sum(r["design_weight"] * r["prior_size_proxy"] for r in sample["stations"])
            prior_received = sum(r["design_weight"] * r["prior_size_proxy"] for r in sample["stations"]
                                 if identity(r) in reporting_ids)
            out.update(source_parent=sample["source_parent"], early_strength=sample["early_strength"],
                       design_kind="prior_election_early", source_hash=sample["source_hash"],
                        method=sample["method"], historical_balance=design_balance(sample),
                        historical_benchmark=historical_benchmark(sample),
                       prior_electorate_response=prior_received / prior_mass,
                       historical_return_balance=historical_return_balance(sample, reporting_ids))
            if prior_received / prior_mass < .8 - 1e-12:
                out["blockers"].append("less than 80% of the selected design-weighted prior-electorate proxy has reported")
            if reporting < len(roster) and not return_balance_ready(sample, out["historical_return_balance"]):
                out["blockers"].append("returned selected stations do not yet match frozen prior political/geographic balance limits")
            out["warnings"] = [w for w in out["warnings"] if not w.startswith("Intervals are")
                               and not w.startswith("No verified pre-election") and not w.startswith("Experimental:")]
            out["warnings"].append("Design-weighted response adjustment does not eliminate reporting bias.")
            out["warnings"].append("Readiness also checks frozen prior political/geographic profiles. "
                                   "Historical release errors after this check are not independent validation "
                                   "and do not guarantee upcoming-election accuracy.")
            out["warnings"].append("Prior electorate sizes/political profiles inform selection, not upcoming "
                                   "votes. Current electorate coverage and urban/rural calibration remain unknown.")
            out["warnings"].append("Fixed sample designed from PRIOR election " + sample["source_parent"] +
                                    "; uses frozen inverse-inclusion weights with historical-stratum response adjustment. "
                                    "Intervals use conservative Hansen–Hurwitz ratio linearization for pivotal sampling.")
            out["warnings"].append("Approximate pointwise 95% sampling intervals assume exchangeable response "
                                   "within sampling groups. They exclude reporting bias and reading errors; "
                                   "the prior-balance release gate and repeated checking mean unconditional 95% "
                                   "forecast coverage is not validated.")
        if reporting < len(selected) * .8:
            out["blockers"].append("less than 80% of selected stations have usable results")
        if reporting < len(selected):
            out["warnings"].append("Sample nonresponse adjustment assumes missing selected stations "
                                   "resemble reporting stations within each sampling stratum.")
        if sample["retrospective"]:
            out["warnings"].append("Retrospective sample: selected after results existed, not a "
                                   "prospectively registered election-night sample.")
    sparse = [s["m"] for s in strata if s["reporting"] < min(2, s["population"])]
    if sparse:
        out["blockers"].append(f"{len(sparse)} " + ("sampling strata" if historical else "municipalities") +
                               " have missing or insufficient station returns")
    if not names:
        out["blockers"].append("ballot rows unavailable")
    if out["blockers"]:
        return out
    denominator = sum(weight(r) * ballots(r) for rs in groups.values() for r in rs)
    if denominator <= 0:
        out["blockers"].append("no usable ballots")
        return out
    census = reporting == len(roster)
    results = []
    for index, name in enumerate(names):
        numerator = sum(weight(r) * r["lists"][index] for rs in groups.values() for r in rs)
        p = numerator / denominator
        variance = 0.0
        for s in strata:
            n, N = s["reporting"], s["population"]
            if n == N:
                continue
            residuals = [r["lists"][index] - p * ballots(r) for r in groups[s["m"]]]
            if historical:
                # Chauvet & Ruiz-Gazen (2016), eq. (9): HH is conservative for pivotal totals.
                # Ratio linearization / estimated response calibration remain approximations.
                contributions = [weight(r) * e for r, e in zip(groups[s["m"]], residuals)]
                mean = sum(contributions) / n
                variance += n / (n - 1) * sum((e - mean)**2 for e in contributions)
                continue
            mean = sum(residuals) / n
            variance += N**2 * (1 - n / N) * sum((e - mean)**2 for e in residuals) / (n * (n - 1))
        se = math.sqrt(variance) / denominator
        results.append({"row": index + 1, "name": name, "share": p, "se": se,
                         "low": max(0, p - 1.96 * se) if se > 0 or census else None,
                         "high": min(1, p + 1.96 * se) if se > 0 or census else None})
        if historical:
            results[-1]["margin_pp"] = 196 * se if se > 0 or census else None
    if not historical and not census and all(r["se"] == 0 for r in results):
        out["blockers"].append("zero observed non-census variance: uncertainty cannot be assessed")
        return out
    out.update(status="complete" if census else "experimental", results=results,
                interval_kind=("census" if census else "conditional_95_pivotal_hh"
                              if historical else "conditional_95_pointwise"),
               expanded_ballots=denominator,
               expanded_registered=sum(weight(r) * r["registered"] for rs in groups.values() for r in rs))
    return out


def sample_path(store) -> Path | None:
    return None if store.path == ":memory:" else Path(store.path).with_suffix(".sample.json")


def _sample_input(store) -> tuple[bytes | None, str | None]:
    path = sample_path(store)
    if path is None:
        return None, None
    try:
        return path.read_bytes(), None
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, type(exc).__name__


def estimate_input_signature(store, parent: str, sample_input=None) -> str:
    """Invalidate derived estimates on frozen-file/roster changes, without resetting form feeds."""
    raw, error = _sample_input(store) if sample_input is None else sample_input
    value = {"sample": hashlib.sha256(raw).hexdigest() if raw is not None else None,
             "sample_error": error,
             "roster": sorted((r["m"] or "", r["no"] or "") for r in store.station_roster(parent))}
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def store_frame(store, parent: str) -> list[dict]:
    roster = store.station_roster(parent)
    if not roster:
        raise ValueError("no election-specific municipality/station roster stored")
    if any(not r["no"] for r in roster):
        raise ValueError("station roster missing for a stored municipality")
    return checked_frame(roster)


def tally_readings(tally: dict) -> list[dict]:
    return [{"m": r["m"], "no": r["no"], "id": r["id"], **r["corrected"]}
            for r in tally["station_results"]]


def check_observed_frame(roster: list[dict], docs: list, parent: str) -> None:
    ids = {identity(r) for r in roster}
    if any((d["municipality"], d["station_no"]) not in ids for d in docs
           if d["parent"] == parent and d["doc_type"] in (6, 10, 15, 16)):
        raise ValueError("observed RG-2 station outside stored roster; check frame completeness")


def operator_estimates(store, tally: dict | None, docs: list) -> dict:
    """Private snapshot only: no call from build_tally or the public export path."""
    if not tally or tally["scope"] != "national":
        reason = "Estimates support national parliamentary/presidential ballots only."
        return {k: unavailable(reason, "unsupported") for k in ("sample", "arrivals")}
    sample_input = _sample_input(store)
    signature = estimate_input_signature(store, tally["parent"], sample_input)
    try:
        roster = store_frame(store, tally["parent"])
        check_observed_frame(roster, docs, tally["parent"])
    except ValueError as exc:
        return {"input_signature": signature, **{
            k: unavailable(str(exc), "invalid_frame") for k in ("sample", "arrivals")}}
    readings = tally_readings(tally)
    arrivals = estimate(roster, readings, tally["names"])
    raw, error = sample_input
    if error:
        sampled = unavailable(f"Sample unreadable: {error}", "invalid_sample")
    elif raw is None:
        sampled = unavailable("No frozen sample. Create one with rg2watch estimate-sample.", "not_configured")
    else:
        try:
            sample = validate_sample(json.loads(raw), roster, tally["parent"])
            if sample.get("version") == 3:
                from .speed_samples import speed_estimate
                samples = {role: speed_estimate(sample, role, readings, tally["names"])
                           for role in ("quick", "morning")}
                return {"input_signature": signature, "sample": samples["quick"],
                        "samples": samples, "arrivals": arrivals}
            sampled = estimate(roster, readings, tally["names"], sample)
        except (OSError, ValueError, TypeError) as exc:
            sampled = unavailable(f"Sample refused: {exc}", "invalid_sample")
    return {"input_signature": signature, "sample": sampled, "arrivals": arrivals}
