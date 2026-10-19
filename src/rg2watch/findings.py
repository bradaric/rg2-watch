from rg2watch.models import DOC_TYPES, RARE_TYPES, Event, Finding

_HIGH_ADDED = (8, 9, 27, 28)


def _name(event: Event) -> str:
    t = event.doc.doc_type
    return DOC_TYPES[t] if t in DOC_TYPES else event.doc.label


def findings_for(events: list[Event]) -> list[Finding]:
    out: list[Finding] = []
    for e in events:
        t = e.doc.doc_type
        if e.kind == "added" and t in RARE_TYPES:
            sev = "HIGH" if t in _HIGH_ADDED else "MEDIUM"
            out.append(Finding("D1", sev, e.doc, f"{_name(e)} published"))
        elif e.kind == "replaced":
            sev = "HIGH" if t == 6 else "MEDIUM"
            out.append(Finding("D2", sev, e.doc, f"{_name(e)} file replaced"))
        elif e.kind == "removed":
            out.append(Finding("D4", "MEDIUM", e.doc, "document removed from listing"))
    return out
