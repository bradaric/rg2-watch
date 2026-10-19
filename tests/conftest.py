import pytest


@pytest.fixture(autouse=True)
def _no_real_anthropic(monkeypatch):
    """Tests never reach the real Anthropic API, whatever credentials the machine has."""
    from rg2watch import cli

    monkeypatch.setattr(cli, "_anthropic_client", lambda: (None, "disabled in tests"), raising=False)


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """cli.main() reconfigures root logging onto the test's captured stderr; undo it."""
    import logging

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
