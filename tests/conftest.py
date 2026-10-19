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


@pytest.fixture(autouse=True)
def _no_real_header_ocr(monkeypatch):
    """No tesseract by default: header OCR finds nothing, so every page is kept."""
    from rg2watch import render

    monkeypatch.setattr(render, "_ocr_header", lambda img, timeout=10: None)
