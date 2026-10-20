import logging

import httpx
import pytest

from rg2watch import cli


@pytest.mark.parametrize("verbose", [[], ["-v"]])
def test_httpx_request_urls_stay_out_of_logs(monkeypatch, verbose):
    lines: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    def fake_status(args):
        logging.getLogger().addHandler(Grab())  # main() reset the root handlers: attach ours after
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
            c.post("http://ntfy.test/secret-topic-123", content=b"x")
        return 0

    monkeypatch.setattr(cli, "_cmd_status", fake_status)
    try:
        assert cli.main(["status", *verbose]) == 0
    finally:
        logging.getLogger().handlers.clear()
    assert not any("secret-topic-123" in m for m in lines)
