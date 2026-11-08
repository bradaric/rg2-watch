"""Offline arrival-bias experiments on already stored numeric readings, not a live replay."""
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .estimates import (ballots, checked_frame, estimate, identity, make_sample, usable)


def upload_time(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d", value):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # RIK upload timestamps without an offset are Serbian local time, not UTC.
        return at.replace(tzinfo=at.tzinfo or ZoneInfo("Europe/Belgrade")).astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError):
        return None


def _raw(readings: list[dict], names: list[str]) -> dict:
    denominator = sum(ballots(r) for r in readings)
    return {"results": [{"row": i + 1, "name": name,
                         "share": sum(r["lists"][i] for r in readings) / denominator,
                         "low": None, "high": None}
                        for i, name in enumerate(names)] if denominator else []}


def _score(result: dict, reference: list[float]) -> dict:
    rows = result["results"]
    if not rows or not reference:
        return {"status": result.get("status", "unavailable"), "shares": [],
                "max_error_pp": None, "interval_coverage": None}
    intervals = [r for r in rows if r.get("low") is not None]
    return {"status": result.get("status", "raw"), "shares": [r["share"] for r in rows],
            "max_error_pp": max(abs(r["share"] - p) * 100 for r, p in zip(rows, reference)),
            "interval_coverage": (sum(r["low"] <= reference[r["row"] - 1] <= r["high"]
                                      for r in intervals) / len(intervals)) if intervals else None}


def run_replay(roster: list[dict], readings: list[dict], names: list[str], parent: str,
               sizes: list[int], seeds: list[str], max_points: int = 25) -> dict:
    roster = checked_frame(roster)
    identities = {identity(r) for r in roster}
    seen = set()
    for row in readings:
        if identity(row) not in identities or identity(row) in seen:
            raise ValueError("result identity outside roster or duplicate result")
        seen.add(identity(row))
    if not seeds or max_points < 2:
        raise ValueError("replay needs seeds and at least two timeline points")
    usable_rows = [r for r in readings if usable(r, len(names))]
    reference = _raw(usable_rows, names)
    shares = [r["share"] for r in reference["results"]]
    complete = len(usable_rows) == len(roster)
    warnings = [
        "Uses final stored readings/corrections, holds and repeat selection ordered by the selected "
        "form's upload time. Not an as-of/live replay; ignores analysis delays and later knowledge.",
        "Reference is the current usable stored tally, not independently verified official results.",
        "Sample-size trials use retrospectively drawn samples and current usable returns; choosing "
        "a method/size on this election is in-sample exploration, not future accuracy validation.",
    ]
    if not complete:
        warnings.append("Partial reference: errors are diagnostics versus available stored votes, "
                        "not national final-result errors. ±1/±2pp target assessment disabled.")
    timed = [(at, r) for r in usable_rows if (at := upload_time(r.get("uploaded_at"))) is not None]
    times = sorted({at for at, _ in timed})
    if len(times) > max_points:
        times = [times[round(i * (len(times) - 1) / (max_points - 1))] for i in range(max_points)]
    samples = []
    sizes_out = []
    for size in sizes:
        try:
            trials = [make_sample(roster, parent, size, seed, retrospective=True) for seed in seeds]
        except ValueError as exc:
            sizes_out.append({"size": size, "status": "invalid_size", "reason": str(exc)})
            continue
        samples.append(trials[0])
        scores = [_score(estimate(roster, usable_rows, names, sample), shares) for sample in trials]
        errors = sorted(s["max_error_pp"] for s in scores if s["max_error_pp"] is not None)
        coverages = [s["interval_coverage"] for s in scores if s["interval_coverage"] is not None]
        sizes_out.append({
            "size": size, "status": "diagnostic", "trials": len(trials), "released": len(errors),
            "p95_max_error_pp": errors[math.ceil(.95 * len(errors)) - 1] if errors else None,
            "worst_error_pp": max(errors) if errors else None,
            "mean_pointwise_interval_coverage": sum(coverages) / len(coverages) if coverages else None,
            "meets_1pp": sum(e <= 1 for e in errors) / len(trials) if complete else None,
            "meets_2pp": sum(e <= 2 for e in errors) / len(trials) if complete else None,
        })
    # Freeze the largest admissible requested size and first seed for the illustrative timeline.
    timeline_sample = max(samples, key=lambda sample: sample["size"]) if samples else None
    timeline = []
    for at in times:
        available = [r for uploaded, r in timed if uploaded <= at]
        timeline.append({"at": at.isoformat(), "available": len(available),
                         "raw": _score(_raw(available, names), shares),
                         "arrivals": _score(estimate(roster, available, names), shares),
                         "sample": _score(estimate(roster, available, names, timeline_sample), shares)
                         if timeline_sample else None})
    return {"version": 1, "parent": parent, "mode": "final_readings_by_upload_time",
            "accuracy_validated": False, "warnings": warnings, "names": names,
            "reference": {"complete": complete, "reporting": len(usable_rows),
                          "population": len(roster), "shares": shares},
            "missing_timestamps": len(usable_rows) - len(timed), "timeline": timeline,
            "timeline_sample_size": timeline_sample["size"] if timeline_sample else None,
            "timeline_sample_seed": timeline_sample["seed"] if timeline_sample else None,
            "sample_sizes": sizes_out}


def summary(report: dict) -> str:
    ref = report["reference"]
    lines = [f"EXPERIMENTAL arrival-bias replay — election {report['parent']}",
             f"Reference: {'complete stored' if ref['complete'] else 'PARTIAL stored'} "
             f"{ref['reporting']} / {ref['population']} stations; not official final results.",
             *report["warnings"], "", "Sample size | released trials | p95 max error (pp) | worst (pp)"]
    for row in report["sample_sizes"]:
        if row["status"] == "invalid_size":
            lines.append(f"{row['size']}: {row['reason']}")
        else:
            fmt = lambda x: "unavailable" if x is None else f"{x:.3f}"
            lines.append(f"{row['size']} | {row['released']}/{row['trials']} | "
                         f"{fmt(row['p95_max_error_pp'])} | {fmt(row['worst_error_pp'])}")
    lines.append("Errors above are versus the stored usable reference only. No future accuracy validated.")
    lines.extend(["", f"Arrival timeline (fixed sample {report['timeline_sample_size']}; UTC timestamps)",
                  "Upload cutoff | usable stations | raw error pp | projection error pp | sample error pp"])
    for point in report["timeline"]:
        errors = [point[key]["max_error_pp"] if point[key] else None
                  for key in ("raw", "arrivals", "sample")]
        lines.append(f"{point['at']} | {point['available']} | " +
                     " | ".join("withheld" if e is None else f"{e:.3f}" for e in errors))
    return "\n".join(lines)
