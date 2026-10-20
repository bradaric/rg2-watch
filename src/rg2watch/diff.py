from rg2watch.models import Doc, Event, doc_key


def diff(known: dict[tuple[str, int | str], Doc], observed: list[Doc],
         complete: bool) -> list[Event]:
    """Removed events are only candidates; the watcher confirms them before storing."""
    events: list[Event] = []
    seen: dict[tuple[str, int | str], Doc] = {}
    for d in observed:
        key = doc_key(d)
        cur = seen.get(key)
        if cur is None or (d.uploaded_at, d.url) > (cur.uploaded_at, cur.url):
            seen[key] = d
    for key, d in seen.items():
        old = known.get(key)
        if old is None:
            events.append(Event("added", d))
        elif old.url != d.url:
            events.append(Event("replaced", d, previous_url=old.url))
    if complete:
        events.extend(Event("removed", d) for key, d in known.items() if key not in seen)
    return events
