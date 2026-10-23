"""Conservative promotion of targeted arithmetic second reads into a full RG-2 report."""
import copy


SUMMARY = ("voted", "ballots_in_box", "invalid", "valid")


def arithmetic_read(report: dict) -> dict:
    """Extract the targeted arithmetic shape from a full normalised RG-2 report."""
    numbers = report["numbers"]
    return {
        **{key: {"value": numbers[key]["value"], "legible": numbers[key]["legible"]}
           for key in SUMMARY},
        "list_votes": [{"row": item["row"], "value": item["field"]["value"],
                        "legible": item["field"]["legible"]}
                       for item in numbers["list_votes"]],
    }


def _values(read: dict) -> tuple[dict[str, int], dict[int, int]] | None:
    summary = {}
    for key in SUMMARY:
        field = read.get(key, {})
        if not field.get("legible") or not isinstance(field.get("value"), int):
            return None
        summary[key] = field["value"]
    rows = read.get("list_votes") or []
    if [item.get("row") for item in rows] != list(range(1, len(rows) + 1)):
        return None
    if any(not item.get("legible") or not isinstance(item.get("value"), int) for item in rows):
        return None
    return summary, {item["row"]: item["value"] for item in rows}


def reconciles(read: dict) -> bool:
    values = _values(read)
    if values is None:
        return False
    summary, rows = values
    return (summary["ballots_in_box"] <= summary["voted"]
            and summary["invalid"] + summary["valid"] == summary["ballots_in_box"]
            and sum(rows.values()) == summary["valid"])


def adoptable_merge(primary: dict, candidate: dict, model: str,
                    max_changes: int = 1) -> dict | None:
    """Merge a bounded number of independent reads when all others agree and A1-A3 reconcile."""
    original = arithmetic_read(primary)
    left, right = _values(original), _values(candidate)
    if left is None or right is None or set(left[1]) != set(right[1]) or not reconciles(candidate):
        return None
    diffs = [(key, left[0][key], right[0][key]) for key in SUMMARY
             if left[0][key] != right[0][key]]
    diffs += [(f"list:{row}", left[1][row], right[1][row]) for row in left[1]
              if left[1][row] != right[1][row]]
    if not 1 <= len(diffs) <= max_changes:
        return None
    merged = copy.deepcopy(primary)
    changes = []
    for key, old, new in diffs:
        if key.startswith("list:"):
            row = int(key.split(":", 1)[1])
            field = next(item["field"] for item in merged["numbers"]["list_votes"]
                         if item["row"] == row)
            field["value"] = new
            if new != 0 and field.get("mark") in ("dash", "empty"):
                field["mark"] = "clean"
        else:
            field = merged["numbers"][key]
            field["value"] = new
            if new != 0 and field.get("mark") == "dash":
                field["mark"] = "clean"
        changes.append({"field": key, "from": old, "to": new})
    provenance = {"model": model, "changes": changes}
    if len(changes) == 1:  # retain the original provenance shape for existing consumers
        provenance.update(changes[0])
    merged["arithmetic_recheck"] = provenance
    return merged
