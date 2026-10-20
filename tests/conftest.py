import pytest


@pytest.fixture(autouse=True)
def _no_real_anthropic(monkeypatch):
    """Tests never reach the real Anthropic API, whatever credentials the machine has."""
    from rg2watch import cli

    monkeypatch.setattr(cli, "_anthropic_client", lambda: (None, "disabled in tests"), raising=False)


@pytest.fixture(autouse=True)
def _pre_calibration_cli_defaults(monkeypatch):
    """CLI runs without --config keep the pre-calibration model, prices and cap that the cost tests assert."""
    from rg2watch import cli
    from rg2watch.config import Config, load_config

    def load(path):
        if path is not None:
            return load_config(path)
        return Config(model="claude-opus-5-5", price_in_per_mtok=4.0, price_out_per_mtok=20.0,
                      max_spend_usd=20.0)

    monkeypatch.setattr(cli, "load_config", load)


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
