import difflib

_DIGRAPHS = {"lj": "љ", "nj": "њ", "dž": "џ", "dj": "ђ"}
_LATIN = dict(zip("abvgdđežzijklmnoprstćufhcčš", "абвгдђежзијклмнопрстћуфхцчш"))


class MunicipalityError(ValueError):
    """No or ambiguous match; `candidates` are the names to show the operator."""

    def __init__(self, message: str, candidates: list[str]):
        super().__init__(message)
        self.candidates = candidates


def to_cyrillic(text: str) -> str:
    """Serbian Latin -> Cyrillic, casefolded; Cyrillic and other characters pass through."""
    text, out, i = text.casefold(), [], 0
    while i < len(text):
        pair = text[i:i + 2]
        if pair in _DIGRAPHS:
            out.append(_DIGRAPHS[pair])
            i += 2
        else:
            out.append(_LATIN.get(text[i], text[i]))
            i += 1
    return "".join(out)


def resolve_municipalities(names: dict[str, str], queries: list[str]) -> dict[str, str]:
    """names: stored name -> id. Each query picks one municipality: an exact (casefolded) name
    match wins, else a unique substring match. Returns the selected name -> id."""
    folded = {n: to_cyrillic(n) for n in names}
    chosen: dict[str, str] = {}
    for query in queries:
        q = to_cyrillic(query.strip())
        if not q:
            continue
        exact = [n for n, f in folded.items() if f == q]
        hits = exact or [n for n, f in folded.items() if q in f]
        if len(hits) == 1:
            chosen[hits[0]] = names[hits[0]]
        elif hits:
            raise MunicipalityError(f"ambiguous municipality {query!r}", sorted(hits))
        else:
            close = difflib.get_close_matches(q, list(folded.values()), n=5, cutoff=0.5)
            raise MunicipalityError(f"no municipality matches {query!r}",
                                    sorted(n for n, f in folded.items() if f in close))
    if not chosen:
        raise MunicipalityError("no municipality given", [])
    return chosen
