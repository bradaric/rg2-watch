import json

import pytest

from rg2watch.analyze import AnalysisResult, stored_model
from rg2watch.config import Config
from rg2watch.models import Doc, Election, Event
from rg2watch.operator_feed import OperatorFeed, OperatorGroupTooLarge
from rg2watch.analysis_selection import preferred_models
from rg2watch.store import Store
from rg2watch.pipeline import PROMPT_VERSION
from test_rules import clean_report

P = "662405"


def doc(url="https://x/a.pdf", no="1", doc_type=6):
    return Doc("Сечањ", no, "ОШ", "lbl", doc_type, url, "2026-10-19T20:15")


def setup_feed(tmp_path):
    store = Store(tmp_path / "e.sqlite")
    store.upsert_election(Election(P, "1", "national", "Election", "https://x"))
    return store, OperatorFeed(store, Config(), tmp_path / "pdfs")


def add(store, value, kind="added", previous=None):
    store.apply_events(P, store.start_sweep(P, "rg2"), [Event(kind, value, previous)])


def test_bootstrap_is_paged_tagged_and_selective_rows_are_tally_stale(tmp_path):
    store, feed = setup_feed(tmp_path)
    add(store, doc())

    page = feed.bootstrap(P)

    assert page["done"] is True and page["cursor"] is None
    assert len(page["generation"]) == 64
    assert [(group["kind"], group["key"]) for group in page["groups"]] == [
        ("station", "Сечањ|1")]
    row = page["groups"][0]["rows"][0]
    assert row["group_kind"] == "station" and row["group_key"] == "Сечањ|1"
    assert row["rec"]["status"] == "updating"
    assert row["rec"]["result"]["corrected"]["included"] is None


def test_feed_is_bounded_incremental_and_returns_tombstones(tmp_path):
    store, feed = setup_feed(tmp_path)
    first = doc()
    add(store, first)
    since = store.operator_feed_state(P)[1]
    store.set_review(first.url, "ok", "checked")

    changed = feed.feed(P, since)

    assert changed["reset_required"] is False and changed["through"] > since
    assert changed["groups"][0]["rows"][0]["rv"]["v"] == "ok"
    assert json.dumps(changed).count(first.url) < 5

    since = changed["through"]
    with store._lock, store._db:
        store._db.execute("DELETE FROM documents WHERE url=?", (first.url,))
    removed = feed.feed(P, since)
    assert removed["groups"] == []
    assert removed["tombstones"] == [{"kind": "station", "key": first.station_key,
                                      "changed_revision": removed["through"]}]


def test_feed_rejects_stale_future_and_global_revisions(tmp_path):
    store, feed = setup_feed(tmp_path)
    add(store, doc())
    current = store.operator_feed_state(P)[1]
    assert feed.feed(P, current + 1)["reset_required"] is True

    with store._lock, store._db:
        store._db.execute("UPDATE operator_feed_floors SET floor=? WHERE parent=?", (current, P))
    assert feed.feed(P, current - 1)["reset_required"] is True

    with store._lock, store._db:
        store._db.execute("UPDATE operator_feed_meta SET revision=revision+1")
        revision = store._db.execute("SELECT revision FROM operator_feed_meta").fetchone()[0]
        store._db.execute("INSERT INTO operator_feed_changes VALUES(?,?,?,?)",
                          (revision, P, "global", "*"))
    assert feed.feed(P, current)["reset_required"] is True


def test_generation_changes_for_config_and_database_but_not_empty_poll(tmp_path):
    store, feed = setup_feed(tmp_path)
    add(store, doc())
    current = store.operator_feed_state(P)[1]
    first = feed.feed(P, current)
    assert feed.feed(P, current)["generation"] == first["generation"]

    feed.cfg.list_names = {P: ["A"]}
    assert feed.feed(P, current)["generation"] != first["generation"]

    other = Store(tmp_path / "other.sqlite")
    other.upsert_election(Election(P, "1", "national", "Election", "https://x"))
    other_feed = OperatorFeed(other, Config(), tmp_path / "pdfs")
    assert other_feed.generation(P) != first["generation"]


def test_invalid_bootstrap_cursor_and_unknown_parent_are_controlled(tmp_path):
    _, feed = setup_feed(tmp_path)
    with pytest.raises(ValueError, match="cursor"):
        feed.bootstrap(P, "not-a-cursor")
    with pytest.raises(KeyError):
        feed.feed("missing", 0)


def test_selective_rows_use_election_wide_model_coverage_order(tmp_path):
    store, _ = setup_feed(tmp_path)
    docs = [doc(f"https://x/{i}.pdf", no=str(i)) for i in range(1, 4)]
    for item in docs:
        add(store, item)
    # In station 1 the models tie locally, so local ordering would choose "a-model". Across the
    # election z-model has greater coverage and must be selected, exactly as the full builder does.
    for item in docs[:2]:
        store.save_analysis(item.url, AnalysisResult(
            clean_report(), 0, 0, 0, "a-model", "stop"), PROMPT_VERSION, parent=P)
    for item in docs:
        store.save_analysis(item.url, AnalysisResult(
            clean_report(), 0, 0, 0, "z-model", "stop"), PROMPT_VERSION, parent=P)
    feed = OperatorFeed(store, Config(model="missing-preferred"), tmp_path / "pdfs")

    page = feed.bootstrap(P)
    station = next(group for group in page["groups"] if group["key"] == docs[0].station_key)

    assert station["rows"][0]["analysis_model"] == "z-model"


def test_effective_model_order_signature_only_changes_when_order_changes(tmp_path):
    store, feed = setup_feed(tmp_path)
    docs = [doc(f"https://x/signature-{i}.pdf", no=str(i)) for i in range(1, 4)]
    for item in docs:
        add(store, item)
    initial = feed.feed(P, store.operator_feed_state(P)[1])["model_order_signature"]
    store.save_analysis(docs[0].url, AnalysisResult(
        clean_report(), 0, 0, 0, stored_model(Config().model), "stop"), PROMPT_VERSION, parent=P)
    after_first = feed.feed(P, store.operator_feed_state(P)[1])
    assert after_first["model_order_signature"] == initial
    store.save_analysis(docs[0].url, AnalysisResult(
        clean_report(), 0, 0, 0, "fallback-z", "stop"), PROMPT_VERSION, parent=P)
    one_fallback = feed.feed(P, store.operator_feed_state(P)[1])["model_order_signature"]
    assert one_fallback != initial
    store.save_analysis(docs[1].url, AnalysisResult(
        clean_report(), 0, 0, 0, "fallback-a", "stop"), PROMPT_VERSION, parent=P)
    tied = feed.feed(P, store.operator_feed_state(P)[1])["model_order_signature"]
    assert tied != one_fallback
    store.save_analysis(docs[2].url, AnalysisResult(
        clean_report(), 0, 0, 0, "fallback-z", "stop"), PROMPT_VERSION, parent=P)
    reordered = feed.feed(P, store.operator_feed_state(P)[1])["model_order_signature"]
    assert reordered != tied
    assert feed.bootstrap(P)["model_order_signature"] == reordered


def test_equal_coverage_model_order_places_empty_before_named_and_null_last(tmp_path):
    store, _ = setup_feed(tmp_path)
    documents = [doc(f"https://x/tie-{i}.pdf", no=str(i)) for i in range(1, 4)]
    for value in documents:
        add(store, value)
    with store._lock, store._db:
        for document_id, model in enumerate((None, "", "named"), 1):
            store._db.execute(
                "INSERT INTO analyses(document_id,model,prompt_version,result_json,status,parent) "
                "VALUES(?,?,?,'{}','done',?)", (document_id, model, PROMPT_VERSION, P))

    rows = store._db.execute("SELECT model FROM analyses ORDER BY id DESC").fetchall()
    assert preferred_models(rows, "preferred") == ["preferred", "", "named", None]
    order, _ = __import__("rg2watch.operator_feed", fromlist=["effective_model_order"]).effective_model_order(
        store, Config(model="preferred"), P)
    assert order == ["preferred", "", "named", None]


def test_bootstrap_skips_post_start_group_and_feed_replays_true_revision(tmp_path, monkeypatch):
    import rg2watch.operator_feed as feed_module

    monkeypatch.setattr(feed_module, "BOOTSTRAP_ROWS", 1)
    store, feed = setup_feed(tmp_path)
    docs = [doc(f"https://x/page-{i}.pdf", no=str(i)) for i in range(1, 4)]
    for item in docs:
        add(store, item)
    first = feed.bootstrap(P)
    start = first["revision"]
    store.set_review(docs[1].url, "ok", "during bootstrap")
    changed_revision = store.operator_feed_state(P)[1]

    second = feed.bootstrap(P, first["cursor"])
    assert second["groups"] == []
    assert second["cursor"] is not None
    third = feed.bootstrap(P, second["cursor"])
    assert third["done"] is True

    replay = feed.feed(P, start)
    changed = next(group for group in replay["groups"] if group["key"] == docs[1].station_key)
    assert changed["rows"][0]["changed_revision"] == changed_revision


def test_bootstrap_returns_one_lossless_oversized_soft_group_and_rejects_hard_limit(
        tmp_path, monkeypatch):
    store, feed = setup_feed(tmp_path)
    for number in (1, 2):
        add(store, doc(f"https://x/large-{number}.pdf", no=str(number)))
    soft_row = [{"payload": "x" * 1_050_000}]
    monkeypatch.setattr(feed, "_rows", lambda *_args: soft_row)

    first = feed.bootstrap(P)
    assert len(first["groups"]) == 1 and first["cursor"] is not None
    second = feed.bootstrap(P, first["cursor"])
    assert len(second["groups"]) == 1 and second["done"] is True

    monkeypatch.setattr(feed, "_rows", lambda *_args: [{"payload": "x" * 2_050_000}])
    with pytest.raises(OperatorGroupTooLarge):
        feed.bootstrap(P)
