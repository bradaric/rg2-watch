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


def adoptable_merge(primary: dict, candidate: dict, model: str) -> dict | None:
    """Merge one independently-read value only when every other value agrees and A1-A3 reconcile."""
    original = arithmetic_read(primary)
    left, right = _values(original), _values(candidate)
    if left is None or right is None or set(left[1]) != set(right[1]) or not reconciles(candidate):
        return None
    diffs = [(key, left[0][key], right[0][key]) for key in SUMMARY
             if left[0][key] != right[0][key]]
    diffs += [(f"list:{row}", left[1][row], right[1][row]) for row in left[1]
              if left[1][row] != right[1][row]]
    if len(diffs) != 1:
        return None
    key, old, new = diffs[0]
    merged = copy.deepcopy(primary)
    if key.startswith("list:"):
        row = int(key.split(":", 1)[1])
        next(item["field"] for item in merged["numbers"]["list_votes"]
             if item["row"] == row)["value"] = new
    else:
        merged["numbers"][key]["value"] = new
    merged["arithmetic_recheck"] = {"model": model, "field": key, "from": old, "to": new}
    return merged
