import base64
import json
from types import SimpleNamespace

import pytest

from rg2watch import analyze
from rg2watch.analyze import SCHEMA, AnalysisError, AnalysisRefused, analyze_document
from rg2watch.config import Config
from rg2watch.render import Tile

REPORT = {k: None for k in SCHEMA["required"]} | {"numbers": [], "yesno": []}


def _resp(report=REPORT, stop="end_turn", tin=10, tout=5, text=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(report) if text is None else text)],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
        stop_reason=stop,
    )


class FakeStream:
    """Stands in for the SDK's stream manager: the request happens on __enter__."""

    def __init__(self, reply):
        self.reply = reply

    def __enter__(self):
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.reply


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []  # streamed requests
        self.create_calls = []  # non-streamed requests
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create, stream=self._stream))

    def _create(self, **kw):
        self.create_calls.append(kw)
        return self.responses.pop(0)

    def _stream(self, **kw):
        self.calls.append(kw)
        return FakeStream(self.responses.pop(0))


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


def test_schema_passes_sdk_strict_transform():
    from anthropic.lib._parse._transform import transform_schema

    transform_schema(SCHEMA)
    assert '"type": ["' not in json.dumps(SCHEMA)


def test_schema_size_guard():
    assert len(json.dumps(SCHEMA)) < 4500


def test_prompt_v3_list_paragraph_before_names():
    para = ("Report numbers as a list: one entry per numeric item (registered, first_voter_number, voted, "
            "ballots_in_box, invalid, valid, assisted, outside_voted, outside_confirmations), and one entry "
            'with field "list_row" and its row number for each electoral list in 10.5. '
            "Report every yes/no item once in the yesno list.")
    assert para in analyze.PROMPT
    assert analyze.PROMPT.index(para) < analyze.PROMPT.index("Never transcribe personal names")


def _n(field, value=1, row=None, mark="clean", legible=True, note=""):
    return {"field": field, "row": row, "value": value, "legible": legible, "mark": mark, "note": note}


def test_normalise_missing_defaults():
    out = analyze._normalise({"numbers": [], "yesno": [], "x": 1})
    assert out["x"] == 1
    assert set(out["numbers"]) == set(analyze.NUM_KEYS) | {"list_votes"}
    assert out["numbers"]["voted"] == {"value": None, "legible": False, "mark": "empty",
                                       "note": "not reported by model"}
    assert out["numbers"]["list_votes"] == []
    assert out["yesno"] == {k: {"answer": "unclear"} for k in analyze.YESNO_KEYS}


def test_normalise_shape_duplicates_and_rows():
    raw = {"numbers": [
        _n("voted", 5, mark="overwritten"), _n("voted", 9),
        _n("list_row", 7, row=3), _n("list_row", 8, row=1), _n("list_row", 99, row=3),
        _n("list_row", 4, row=None)],
        "yesno": [{"item": "interruption", "answer": "DA"}, {"item": "interruption", "answer": "NE"}]}
    out = analyze._normalise(raw)
    assert out["numbers"]["voted"] == {"value": 5, "legible": True, "mark": "overwritten", "note": ""}
    assert [r["row"] for r in out["numbers"]["list_votes"]] == [1, 3]
    assert out["numbers"]["list_votes"][1]["field"]["value"] == 7
    assert out["yesno"]["interruption"] == {"answer": "DA"}
    assert out["yesno"]["committee_remarks"] == {"answer": "unclear"}


def test_analyze_document_returns_normalised_report():
    rep = REPORT | {"numbers": [_n("valid", 3)]}
    res = analyze_document(TILES, FakeClient(_resp(report=rep)), Config())
    assert res.report["numbers"]["valid"]["value"] == 3
    assert res.report["yesno"]["interruption"] == {"answer": "unclear"}


def test_malformed_array_raises_analysis_error():
    with pytest.raises(AnalysisError):
        analyze_document(TILES, FakeClient(_resp(report=REPORT | {"numbers": [{"field": "voted"}]})), Config())


def test_tokens_carried_on_each_exception_type():
    cases = [
        (AnalysisRefused, _resp(stop="refusal", text="", tin=10, tout=5)),
        (AnalysisError, _resp(text="<html>", tin=10, tout=5)),
        (AnalysisError, _resp(report={"header": None}, tin=10, tout=5)),
    ]
    for exc_type, r in cases:
        with pytest.raises(exc_type) as ei:
            analyze_document(TILES, FakeClient(r), Config())
        assert (ei.value.input_tokens, ei.value.output_tokens) == (10, 5)
        assert ei.value.cost_usd == pytest.approx((10 * 4 + 5 * 20) / 1e6)


def test_truncation_after_retry_carries_both_attempts():
    c = FakeClient(_resp(stop="max_tokens", text="{", tin=10, tout=5),
                   _resp(stop="max_tokens", text="{", tin=10, tout=5))
    with pytest.raises(AnalysisError) as ei:
        analyze_document(TILES, c, Config())
    assert (ei.value.input_tokens, ei.value.output_tokens) == (20, 10)


def test_refusal_on_retry_carries_both_attempts_and_category():
    r2 = _resp(stop="refusal", text="", tin=7, tout=3)
    r2.stop_details = SimpleNamespace(category="cyber", explanation=None)
    c = FakeClient(_resp(stop="max_tokens", text="{", tin=10, tout=5), r2)
    with pytest.raises(AnalysisRefused, match="cyber") as ei:
        analyze_document(TILES, c, Config())
    assert (ei.value.input_tokens, ei.value.output_tokens) == (17, 8)


def test_iterations_summed_and_model_recorded():
    r = _resp(tin=999, tout=999)
    r.model = "fallback-model"
    r.usage.iterations = [
        SimpleNamespace(input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0,
                        cache_creation_input_tokens=0),
        SimpleNamespace(input_tokens=0, output_tokens=100_000, cache_read_input_tokens=1_000_000,
                        cache_creation_input_tokens=0),
    ]
    res = analyze_document(TILES, FakeClient(r), Config())
    assert (res.input_tokens, res.output_tokens) == (2_000_000, 100_000)
    assert res.cost_usd == pytest.approx(4.0 + 0.2 + 2.0)
    assert res.model == "fallback-model"


def test_cache_pricing():
    r = _resp(tin=0, tout=0)
    r.usage.cache_read_input_tokens = 1_000_000
    r.usage.cache_creation_input_tokens = 1_000_000
    res = analyze_document(TILES, FakeClient(r), Config())
    assert res.cost_usd == pytest.approx(0.2 + 5.0)  # cache reads at 5% of input


def test_retry_api_error_carries_first_call_cost():
    err = RuntimeError("overloaded")
    c = FakeClient(_resp(stop="max_tokens", tin=1_000_000, tout=0), err)
    with pytest.raises(RuntimeError) as info:
        analyze_document(TILES, c, Config())
    assert info.value is err and info.value.cost_usd == pytest.approx(4.0)
    assert info.value.input_tokens == 1_000_000


def test_both_calls_streamed_with_same_params():
    c = FakeClient(_resp(stop="max_tokens", text="{"), _resp())
    analyze_document(TILES, c, Config())
    assert c.create_calls == []
    first, second = c.calls
    assert {k: v for k, v in first.items() if k != "max_tokens"} == \
        {k: v for k, v in second.items() if k != "max_tokens"}


def test_no_non_streamed_call_above_sdk_limit():
    # SDK 1.15 raises ValueError for non-streaming requests it expects to exceed ~10 minutes
    c = FakeClient(_resp(stop="max_tokens", text="{"), _resp(stop="max_tokens", text="{"))
    with pytest.raises(AnalysisError):
        analyze_document(TILES, c, Config())
    assert len(c.calls) == 2
    assert all(k["max_tokens"] <= 21000 for k in c.create_calls)


def test_prompt_v2_dash_paragraph_before_names():
    para = ('A dash or slash written in a numeric field means 0: report value 0 with mark "dash". '
            "A lone slash or dash in item 15 means no remarks (remarks_text_present=false).")
    assert para in analyze.PROMPT
    assert analyze.PROMPT.index(para) < analyze.PROMPT.index("Never transcribe personal names")
    from rg2watch.pipeline import PROMPT_VERSION
    assert PROMPT_VERSION == "v4"
