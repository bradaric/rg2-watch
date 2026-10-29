from datetime import datetime


def bind_repeat_round(at: datetime | None, starts: dict[int, datetime]) -> int | None:
    """Round whose start strictly precedes a decision; equality with any start is ambiguous."""
    if at is None or at in starts.values():
        return None
    preceding = [(start, kind) for kind, start in starts.items() if start < at]
    return max(preceding)[1] if preceding else None


RG7_FIRST_TYPES = (19, 20, 21, 22)
RG7_REPEAT_TYPES = (23, 24, 25, 26)
DECISION_FIRST_TYPES = (8, 9)
DECISION_REPEAT_TYPES = (27, 28)


def rg7_group(doc_type: int, report: dict | None) -> str | None:
    if doc_type in RG7_FIRST_TYPES:
        return "original"
    if doc_type in RG7_REPEAT_TYPES:
        return "original" if report and report.get("target_round") == "original" else "repeat"
    return None


def rg7_applies(doc_type: int, report: dict | None, rg2_type: int,
                at: datetime | None, repeat_starts: dict[int, datetime]) -> bool:
    """Whether an RG-7 applies to an RG-2 round, allowing a visibly original decision
    to override a mistaken repeat-vote RIK category."""
    if rg7_group(doc_type, report) == "original":
        return rg2_type == 6
    return (doc_type in RG7_REPEAT_TYPES and rg2_type in (10, 15, 16)
            and bind_repeat_round(at, repeat_starts) == rg2_type)


def decision_group(doc_type: int, report: dict | None) -> str | None:
    if doc_type in DECISION_FIRST_TYPES:
        return "original"
    if doc_type in DECISION_REPEAT_TYPES:
        return "original" if report and report.get("target_round") == "original" else "repeat"
    return None


def decision_applies(doc_type: int, report: dict | None, rg2_type: int,
                     at: datetime | None, repeat_starts: dict[int, datetime]) -> bool:
    if decision_group(doc_type, report) == "original":
        return rg2_type == 6
    return (doc_type in DECISION_REPEAT_TYPES and rg2_type in (10, 15, 16)
            and bind_repeat_round(at, repeat_starts) == rg2_type)
