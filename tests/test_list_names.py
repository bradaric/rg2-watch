import hashlib
import tomllib

import pytest

from rg2watch.config import Config
from rg2watch.list_names import Extraction, extract_all, format_toml, select_candidates
from rg2watch.models import Doc, Election, Event
from rg2watch.store import Store


PARENT = "123"


def add_doc(store, doc):
    store.apply_events(PARENT, store.start_sweep(PARENT, "test"), [Event("added", doc)])


def cached(cache, doc):
    path = cache / (hashlib.sha256(doc.url.encode()).hexdigest() + ".pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF fake")
    return path


@pytest.mark.parametrize(("scope", "docs", "configured", "expected_group"), [
    ("national", [Doc("A", "1", "S", "RG-2", 6, "https://x/n.pdf", "2026-01-01")],
     {}, None),
    ("municipal", [Doc("A", "1", "S", "RG-2", 6, "https://x/a.pdf", "2026-01-01"),
                   Doc("B", "1", "S", "RG-2", 6, "https://x/b.pdf", "2026-01-01")],
     {"A": ["Configured"]}, "B"),
    ("council", [Doc("A", "1", "S", "RG-2", 6, "https://x/a.pdf", "2026-01-01", "Council A"),
                 Doc("B", "1", "S", "RG-2", 6, "https://x/b.pdf", "2026-01-01", "Council B")],
     {"Council A": ["Configured"]}, "Council B"),
])
def test_select_candidates_supports_every_tally_scope(tmp_path, scope, docs, configured,
                                                       expected_group):
    store = Store(":memory:")
    store.upsert_election(Election(PARENT, "1", "5", "Election", "https://x"))
    for doc in docs:
        add_doc(store, doc)
        cached(tmp_path, doc)
    cfg = Config(tally_scope={PARENT: scope}, list_names={PARENT: configured})

    selection = select_candidates(store, cfg, PARENT, tmp_path)

    assert selection.scope == scope
    assert [candidate.group for candidate in selection.candidates] == [expected_group]
    assert selection.unavailable == []


def test_select_candidates_prefers_successfully_analyzed_form(tmp_path):
    store = Store(":memory:")
    store.upsert_election(Election(PARENT, "1", "3", "Election", "https://x"))
    first = Doc("A", "1", "S", "RG-2", 6, "https://x/first.pdf", "2026-01-01")
    done = Doc("A", "2", "S", "RG-2", 6, "https://x/done.pdf", "2026-01-02")
    for doc in (first, done):
        add_doc(store, doc)
        cached(tmp_path, doc)
    store._db.execute("UPDATE documents SET analysis_status='done' WHERE url=?", (done.url,))
    store._db.commit()

    selection = select_candidates(
        store, Config(tally_scope={PARENT: "municipal"}), PARENT, tmp_path)

    assert selection.candidates[0].doc.url == done.url


def test_format_toml_keeps_configured_groups_and_adds_review_comments():
    extraction = Extraction(
        group="Council B", names=["Листа \"Б\"", "List 2"], confidence="medium",
        note="Second row slightly blurred", source_url="https://x/b.pdf")

    text = format_toml(PARENT, "council", {"Council A": ["Existing"]}, [extraction])
    parsed = tomllib.loads("[list_names]\n" + text)

    assert parsed["list_names"][PARENT] == {
        "Council A": ["Existing"], "Council B": ["Листа \"Б\"", "List 2"]}
    assert "[list_names" not in text
    assert f'"{PARENT}" = {{' in text
    assert "confidence: medium" in text
    assert "Second row slightly blurred" in text
    assert "https://x/b.pdf" in text


def test_extract_all_continues_after_one_failure():
    candidates = [type("Candidate", (), {"group": name})() for name in ("A", "B")]

    def extract(candidate):
        if candidate.group == "A":
            raise RuntimeError("unreadable")
        return Extraction(candidate.group, ["List"], "high", "", "https://x/b.pdf")

    results, errors = extract_all(candidates, extract)

    assert [result.group for result in results] == ["B"]
    assert errors == [("A", "unreadable")]
