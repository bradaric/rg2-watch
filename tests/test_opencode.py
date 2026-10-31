import json

import httpx
import pytest

from rg2watch.analyze import AnalysisError, analyze_document, is_opencode, parse_opencode_model, stored_model
from rg2watch.config import Config, model_prices
from rg2watch.opencode_client import (OpenCodeBadRequest, OpenCodeClient, OpenCodeGlobalError,
                                      OpenCodeUnavailable)
from rg2watch.render import Tile
from test_analyze import REPORT


MODEL = "opencode:openai/gpt-6.1-terra#none"
TILES = [Tile("p1a", b"jpeg-one"), Tile("p1b", b"jpeg-two")]


def test_opencode_model_selector_and_zero_price():
    assert is_opencode(MODEL)
    assert parse_opencode_model(MODEL) == ("openai", "gpt-6.1-terra", "none")
    assert stored_model(MODEL) == "openai/gpt-6.1-terra#none"
    assert model_prices(Config(model=MODEL)) == (0.0, 0.0)
    for bad in ("opencode:gpt-6.1-terra", "opencode:openai/#none", "opencode:/terra#none",
                "opencode:openai/terra#"):
        with pytest.raises(ValueError, match="opencode model"):
            parse_opencode_model(bad)


def test_analysis_client_routes_opencode_models(monkeypatch):
    from rg2watch import cli

    marker = object()
    monkeypatch.setattr(cli.OpenCodeClient, "from_service", lambda directory=".": marker)
    client, why = cli._analysis_client(Config(model=MODEL))
    assert client is marker and why is None

    client, why = cli._analysis_client(Config(model="opencode:broken"))
    assert client is None and "opencode model" in why


def test_opencode_request_attaches_jpegs_and_cleans_up():
    requests = []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "ses_1"}})
        if request.method == "POST" and request.url.path.endswith("/prompt"):
            return httpx.Response(200, json={"data": {}})
        if request.method == "POST" and request.url.path.endswith("/wait"):
            return httpx.Response(200, json={"data": {}})
        if request.method == "GET" and request.url.path.endswith("/message"):
            return httpx.Response(200, json={"data": [{
                "type": "assistant", "finish": "stop", "cost": 0,
                "tokens": {"input": 123, "output": 45, "reasoning": 0,
                           "cache": {"read": 2, "write": 3}},
                "content": [{"type": "text", "text": json.dumps(REPORT)}],
            }]})
        if request.method == "DELETE" and request.url.path == "/api/session/ses_1":
            return httpx.Response(200, json={"data": True})
        raise AssertionError((request.method, request.url))

    http = httpx.Client(base_url="http://opencode.test", transport=httpx.MockTransport(handle))
    result = analyze_document(TILES, OpenCodeClient(http, directory="/project"), Config(model=MODEL))

    assert result.input_tokens == 128  # input plus cache reads/writes
    assert result.output_tokens == 45
    assert result.cost_usd == 0
    assert result.model == "openai/gpt-6.1-terra#none"
    assert result.stop_reason == "stop"
    assert [(m, p) for m, p, _ in requests] == [
        ("POST", "/api/session"),
        ("POST", "/api/session/ses_1/prompt"),
        ("POST", "/api/experimental/session/ses_1/wait"),
        ("GET", "/api/session/ses_1/message"),
        ("DELETE", "/api/session/ses_1"),
    ]
    create = requests[0][2]
    assert create["agent"] == "rg2-analyzer"
    assert create["model"] == {"providerID": "openai", "id": "gpt-6.1-terra", "variant": "none"}
    assert create["location"] == {"directory": "/project"}
    assert create["permissions"] == [{"action": "*", "resource": "*", "effect": "deny"}]
    prompt = requests[1][2]
    assert [f["name"] for f in prompt["files"]] == ["p1a.jpg", "p1b.jpg"]
    assert all(f["uri"].startswith("data:image/jpeg;base64,") for f in prompt["files"])
    assert "Return JSON matching this exact JSON Schema" in prompt["text"]


def test_opencode_invalid_output_still_deletes_session():
    deleted = []

    def handle(request):
        if request.method == "POST" and request.url.path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "ses_bad"}})
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"type": "assistant", "finish": "stop",
                "cost": 0, "tokens": {}, "content": [{"type": "text", "text": "not json"}]}]})
        if request.method == "DELETE":
            deleted.append(request.url.path)
        return httpx.Response(200, json={"data": {}})

    client = OpenCodeClient(httpx.Client(base_url="http://opencode.test",
                            transport=httpx.MockTransport(handle)), directory="/project")
    with pytest.raises(AnalysisError, match="not valid JSON"):
        analyze_document(TILES, client, Config(model=MODEL))
    assert deleted == ["/api/session/ses_bad"]


def test_opencode_non_stop_finish_is_rejected_with_usage():
    def handle(request):
        if request.method == "POST" and request.url.path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "ses_long"}})
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"type": "assistant", "finish": "length",
                "cost": 0, "tokens": {"input": 10, "output": 20},
                "content": [{"type": "text", "text": json.dumps(REPORT)}]}]})
        return httpx.Response(200, json={"data": {}})

    client = OpenCodeClient(httpx.Client(base_url="http://opencode.test",
                            transport=httpx.MockTransport(handle)), directory="/project")
    with pytest.raises(AnalysisError, match="did not finish normally") as info:
        analyze_document(TILES, client, Config(model=MODEL))
    assert (info.value.input_tokens, info.value.output_tokens) == (10, 20)


@pytest.mark.parametrize("status, error", [
    (400, OpenCodeBadRequest), (401, OpenCodeGlobalError), (403, OpenCodeGlobalError),
    (404, OpenCodeGlobalError), (429, OpenCodeUnavailable), (500, OpenCodeUnavailable),
])
def test_opencode_http_errors_are_classified(status, error):
    http = httpx.Client(base_url="http://opencode.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text="failed")))
    with pytest.raises(error):
        analyze_document(TILES, OpenCodeClient(http, directory="/project"), Config(model=MODEL))
