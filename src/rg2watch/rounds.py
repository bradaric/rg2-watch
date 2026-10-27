from datetime import datetime


def bind_repeat_round(at: datetime | None, starts: dict[int, datetime]) -> int | None:
    """Round whose start strictly precedes a decision; equality with any start is ambiguous."""
    if at is None or at in starts.values():
        return None
    preceding = [(start, kind) for kind, start in starts.items() if start < at]
    return max(preceding)[1] if preceding else None
