import base64
import json
from types import SimpleNamespace

import pytest

from rg2watch import analyze
from rg2watch.analyze import SCHEMA, AnalysisError, AnalysisRefused, analyze_document
from rg2watch.config import Config
from rg2watch.render import Tile

REPORT = {k: None for k in SCHEMA["required"]}


def _resp(report=REPORT, stop="end_turn", tin=10, tout=5, text=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(report) if text is None else text)],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
        stop_reason=stop,
    )


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


TILES = [Tile("p1a", b"\xff\xd8aaa"), Tile("p1b", b"\xff\xd8bbb")]


def test_request_shape():
    c = FakeClient(_resp())
    res = analyze_document(TILES, c, Config())
    kw = c.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["max_tokens"] == 16000
    assert kw["output_config"]["effort"] == "high"
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"
    assert "thinking" not in kw
    content = kw["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text", "image", "text"]
    assert content[0]["text"] == "p1a:" and content[2]["text"] == "p1b:"
    src = content[1]["source"]
    assert src["type"] == "base64" and src["media_type"] == "image/jpeg"
    assert base64.b64decode(src["data"]) == TILES[0].jpeg
    assert content[-1]["text"] == analyze.PROMPT
    assert res.model == "claude-opus-5-5" and res.stop_reason == "end_turn"


def test_fallbacks_disabled_omits_params():
    c = FakeClient(_resp())
    analyze_document(TILES, c, Config(fallbacks=False))
    assert "betas" not in c.calls[0] and "fallbacks" not in c.calls[0]


def test_cost_computed():
    c = FakeClient(_resp(tin=1_000_000, tout=100_000))
    res = analyze_document(TILES, c, Config())
    assert res.cost_usd == pytest.approx(6.0)
    assert (res.input_tokens, res.output_tokens) == (1_000_000, 100_000)


def test_cache_tokens_counted_as_input():
    r = _resp(tin=100)
    r.usage.cache_read_input_tokens = 50
    r.usage.cache_creation_input_tokens = None
    res = analyze_document(TILES, FakeClient(r), Config())
    assert res.input_tokens == 150


def test_refusal_raises():
    with pytest.raises(AnalysisRefused):
        analyze_document(TILES, FakeClient(_resp(stop="refusal", text="")), Config())


def test_max_tokens_retries_once_doubled():
    c = FakeClient(_resp(stop="max_tokens", text="{"), _resp(tin=7, tout=3))
    res = analyze_document(TILES, c, Config())
    assert [k["max_tokens"] for k in c.calls] == [16000, 32000]
    assert res.input_tokens == 17 and res.output_tokens == 8  # both attempts billed


def test_max_tokens_twice_raises():
    c = FakeClient(_resp(stop="max_tokens", text="{"), _resp(stop="max_tokens", text="{"))
    with pytest.raises(AnalysisError):
        analyze_document(TILES, c, Config())
    assert len(c.calls) == 2


def test_non_json_raises():
    with pytest.raises(AnalysisError):
        analyze_document(TILES, FakeClient(_resp(text="<html>")), Config())


def test_key_mismatch_raises():
    with pytest.raises(AnalysisError):
        analyze_document(TILES, FakeClient(_resp(report={"header": None})), Config())


def test_schema_has_no_name_fields():
    allowed = {"rows_with_name", "rows_name_without_signature"}
    bad = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if "name" in k and k not in allowed:
                    bad.append(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(SCHEMA)
    assert bad == []


def test_schema_strict():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(SCHEMA)
    assert set(SCHEMA["properties"]["numbers"]["properties"]) >= {"registered", "list_votes", "outside_confirmations"}
