import json

import httpx
import pytest
from test_analyze import REPORT, TILES
from test_evaluate import cli_args, env, overwritten
from test_pipeline import resp

from rg2watch import cli
from rg2watch.analyze import OLLAMA_SCHEMA, PROMPT, SCHEMA, AnalysisError, OllamaUnavailable, analyze_document
from rg2watch.cli import _config, _parser, main
from rg2watch.config import Config
from rg2watch.pipeline import PROMPT_VERSION, _is_overload
from rg2watch.store import Store

CFG = Config(model="ollama:qwen3.8:27b-64k")


def fake(handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), seen


def ok(report=REPORT, **extra):
    body = {"message": {"role": "assistant", "content": json.dumps(report)},
            "prompt_eval_count": 1234, "eval_count": 56, "done_reason": "stop", **extra}
    return lambda request: httpx.Response(200, json=body)


def test_request_shape_and_success():
    http, seen = fake(ok())
    result = analyze_document(TILES, http, CFG)
    req = seen[0]
    assert req.url == "http://localhost:11434/api/chat"
    body = json.loads(req.content)
    assert body["model"] == "qwen3.8:27b-64k" and body["stream"] is False
    assert body["options"]["num_ctx"] == 65536 and body["options"]["num_predict"] == 16000
    assert body["format"] == OLLAMA_SCHEMA and OLLAMA_SCHEMA["required"] == SCHEMA["required"]
    assert body["format"]["properties"]["numbers"]["maxItems"] == 40
    assert "maxItems" not in SCHEMA["properties"]["numbers"]
    (msg,) = body["messages"]
    assert msg["role"] == "user" and msg["content"].startswith(PROMPT)
    assert "p1a, p1b" in msg["content"].splitlines()[-1]
    assert len(msg["images"]) == 2
    import base64
    assert base64.b64decode(msg["images"][0]) == TILES[0].jpeg
    assert (result.input_tokens, result.output_tokens, result.cost_usd) == (1234, 56, 0.0)
    assert result.model == "qwen3.8:27b-64k"
    assert set(result.report["numbers"]) >= {"registered", "list_votes"}


def test_config_overrides_url_and_ctx():
    http, seen = fake(ok())
    analyze_document(TILES, http, Config(model="ollama:m", ollama_url="http://h:1/", ollama_num_ctx=8192))
    assert str(seen[0].url) == "http://h:1/api/chat"
    assert json.loads(seen[0].content)["options"]["num_ctx"] == 8192


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_connection_problems_are_transient(exc):
    def boom(request):
        raise exc

    http, _ = fake(boom)
    with pytest.raises(OllamaUnavailable) as info:
        analyze_document(TILES, http, CFG)
    assert _is_overload(info.value)


def test_server_error_is_transient():
    http, _ = fake(lambda r: httpx.Response(503, text="busy"))
    with pytest.raises(OllamaUnavailable):
        analyze_document(TILES, http, CFG)


def test_bad_json_raises_analysis_error_keeping_tokens():
    body = {"message": {"content": "not json {"}, "prompt_eval_count": 7, "eval_count": 3}
    http, _ = fake(lambda r: httpx.Response(200, json=body))
    with pytest.raises(AnalysisError, match="not valid JSON") as info:
        analyze_document(TILES, http, CFG)
    assert (info.value.input_tokens, info.value.output_tokens) == (7, 3)


def test_wrong_keys_raise_analysis_error():
    http, _ = fake(ok({"header": {}}))
    with pytest.raises(AnalysisError):
        analyze_document(TILES, http, CFG)


def test_model_flag_overrides_config():
    args = _parser().parse_args(["analyze", "x.pdf", "--model", "ollama:foo"])
    assert _config(args).model == "ollama:foo"
    args = _parser().parse_args(["analyze", "x.pdf"])
    assert _config(args).model == "claude-opus-5-5"  # pinned pre-calibration default, see conftest
    for cmd in (["watch", "http://x"], ["eval", "--labels", "l", "--pdf-dir", "p"]):
        assert _parser().parse_args([*cmd, "--model", "m"]).model == "m"


def test_eval_ollama_needs_no_yes_or_credentials(tmp_path, monkeypatch, capsys):
    pdfs, labels, _, _ = env(tmp_path, None)
    http, seen = fake(ok(overwritten_raw()))
    monkeypatch.setattr(cli, "_analysis_client", lambda cfg: (http, None))
    code = main(cli_args(tmp_path, labels, pdfs, "--model", "ollama:qwen3.8:27b-64k", "--limit", "1"))
    out = capsys.readouterr()
    assert code == 0 and "local model, no API cost" in out.out
    assert "estimated cost" not in out.out and len(seen) == 1
    store = Store(tmp_path / "db" / "x.sqlite")
    row = store._db.execute("SELECT model, cost_usd FROM analyses").fetchone()
    assert (row["model"], row["cost_usd"]) == ("qwen3.8:27b-64k", 0.0)


def overwritten_raw():
    return {**REPORT, "visual": {
        "results_block_different_pen": False, "results_block_different_hand": False,
        "pasted_or_patched_area": False, "page_quality": "good", "other_anomalies": []},
        "header": {"form_code": None, "station_no": None, "date": None, "sections_present": [],
                   "pages_match_same_form": True, "rotated_or_upside_down": False},
        "signatures": {"rows_total": 5, "rows_with_name": 5, "rows_with_signature": 5,
                       "rows_name_without_signature": 0, "president_signed": True,
                       "signatures_look_same_hand": False, "note": ""},
        "times": {"open_time": None, "close_time": None, "completed_at": None},
        "confidence": "high", "remarks_text_present": False, "remarks_summary": "",
        "numbers": [
        {"field": "voted", "row": None, "value": 800, "original_value": None, "legible": True,
         "mark": "overwritten", "note": ""}]}


def test_latest_report_keyed_by_model_and_prompt_version():
    store = Store(":memory:")
    from rg2watch.analyze import AnalysisResult
    from rg2watch.models import Doc, Event
    doc = Doc("A", "1", "", "", 6, "file:///a.pdf", "")
    store.apply_events("eval", store.start_sweep("eval", "eval"), [Event("added", doc)])
    save = lambda model, v, tag: store.save_analysis(  # noqa: E731
        doc.url, AnalysisResult({"tag": tag}, 0, 0, 0.0, model, "stop"), v)
    save("claude-x", "v6", "claude")
    assert store.latest_report(doc.url, "qwen", "v6") is None
    save("qwen", "v5", "old-prompt")
    assert store.latest_report(doc.url, "qwen", "v6") is None
    save("qwen", "v6", "qwen")
    save("claude-x", "v6", "claude2")
    assert store.latest_report(doc.url, "qwen", "v6") == {"tag": "qwen"}
    assert store.latest_report(doc.url, "claude-x", "v6") == {"tag": "claude2"}
    assert store.latest_report(doc.url, "qwen", "v5") == {"tag": "old-prompt"}


def test_eval_cached_does_not_reuse_other_models_report(tmp_path, monkeypatch, capsys):
    from rg2watch.evaluate import run_eval
    import io
    pdfs, _, entries, ai = env(tmp_path, resp(overwritten()))
    store = Store(":memory:")
    sink = io.StringIO()
    run_eval(entries[:1], pdfs, store, ai, Config(), cached=False, out=sink)
    again = run_eval(entries[:1], pdfs, store, None, CFG, cached=True, out=sink)
    assert again["files"] == {} and again["errors"]["a.pdf"].startswith("no stored analysis")
    assert PROMPT_VERSION
