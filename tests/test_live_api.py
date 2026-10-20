import os

import anthropic
import pytest

from rg2watch.analyze import SCHEMA
from rg2watch.config import Config

pytestmark = [
    pytest.mark.live_api,
    pytest.mark.skipif(os.environ.get("RG2W_LIVE_API") != "1", reason="set RG2W_LIVE_API=1 to hit the real API"),
]


def test_schema_compiles_on_real_api():
    cfg = Config()
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=cfg.model,
        max_tokens=3000,
        messages=[{"role": "user", "content": "Fill the schema with placeholder values. No document is attached."}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    ) as stream:
        msg = stream.get_final_message()
    assert msg.stop_reason in ("end_turn", "max_tokens", "refusal")
