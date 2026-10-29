import contextlib
import json
from types import SimpleNamespace

from rg2watch.analyze import DECISION_ROUND_SCHEMA, analyze_document
from rg2watch.browse import STATIC
from rg2watch.config import Config
from rg2watch.models import Event
from rg2watch.pipeline import DECISION_PROMPT_VERSION
from rg2watch.store import Store
from test_browse import P, add, doc
from test_pipeline import Env, doc as pipeline_doc


def decision_report(target="original"):
    return {
        "target_round": target,
        "target_evidence": "The decision contains no repeat-voting wording.",
    }


def test_decision_round_analysis_uses_small_dedicated_schema_and_prompt():
    seen = {}

    def stream(**kwargs):
        seen.update(kwargs)

        @contextlib.contextmanager
        def cm():
            yield SimpleNamespace(get_final_message=lambda: SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(decision_report()))],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                stop_reason="end_turn", model="m"))
        return cm()

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    result = analyze_document([SimpleNamespace(name="p1", jpeg=b"x")], client,
                              Config(fallbacks=False), "decision_round")

    assert seen["output_config"]["format"]["schema"] == DECISION_ROUND_SCHEMA
    prompt = seen["messages"][0]["content"][-1]["text"]
    assert "RG-8 or RG-9" in prompt and "standard decision with no repeat-voting wording" in prompt
    assert result.report == decision_report()


def test_pipeline_analyses_rg8_rg9_round_target(tmp_path):
    reply = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(decision_report()))],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        stop_reason="end_turn", model="claude-opus-5-5")
    env = Env(tmp_path, reply)
    source = pipeline_doc("a.pdf", dt=28)

    env.publish(source)
    assert env.drain() == 1

    report = env.store.latest_report(source.url, "claude-opus-5-5", DECISION_PROMPT_VERSION)
    assert {key: report[key] for key in decision_report()} == decision_report()
    assert env.status(source.url)[0] == "done"


def test_existing_skipped_rg8_rg9_is_requeued_for_round_analysis():
    store = Store(":memory:")
    source = doc("/e/rg9-r.pdf", no="111", dt=28)
    add(store, source)
    store._db.execute("UPDATE documents SET analysis_status='skipped' WHERE url=?", (source.url,))

    assert store.requeue_decision_rounds(P, prompt_version=DECISION_PROMPT_VERSION) == 1
    assert [item.url for item in store.next_for_analysis(1, parent=P)] == [source.url]


def test_page_shows_rg8_rg9_round_evidence_in_document_status():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "function decisionTargetText(" in html
    status = html[html.index("function documentStatusEl("):html.index("function resultValue(")]
    assert "decisionTargetText(r.rec.annulled)" in status
    assert "decisionTargetText(r.rec.undetermined)" in status
