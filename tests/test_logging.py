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


def test_second_watcher_on_the_same_database_is_refused(tmp_path, capsys):
    import os

    db = tmp_path / "w.sqlite"
    held = cli._watch_lock(db)
    assert held is not None and (tmp_path / "w.sqlite.lock").read_text() == str(os.getpid())
    try:
        assert cli.main(["watch", "--parent", "1", "--db", str(db)]) == 2
        err = capsys.readouterr().err
        assert f"another rg2watch watch is already running on {db} (pid {os.getpid()})" in err
        assert cli._watch_lock(db) is None
    finally:
        held.close()
    # released: a watcher on that database gets past the lock (then fails on the unknown election)
    assert cli.main(["watch", "--parent", "1", "--db", str(db)]) == 2
    assert "unknown election" in capsys.readouterr().err
    again = cli._watch_lock(db)
    assert again is not None
    again.close()
