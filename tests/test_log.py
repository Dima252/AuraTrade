import io
import json
import logging

import structlog

from auratrade.log import REDACTED, Redactor, configure_logging


def test_redactor_replaces_longest_secret_first() -> None:
    redact = Redactor(["abc", "abcdef", ""])
    assert redact("x abcdef y abc") == f"x {REDACTED} y {REDACTED}"


def test_processor_scrubs_nested_values() -> None:
    redact = Redactor(["s3cret"])
    out = redact.processor(
        None,
        "info",
        {
            "event": "s3cret here",
            "nested": {"url": "https://x/bots3cret/send", "n": 3},
            "items": ["s3cret", 1],
            "exc": ValueError("token s3cret rejected"),
        },
    )
    assert out == {
        "event": f"{REDACTED} here",
        "nested": {"url": f"https://x/bot{REDACTED}/send", "n": 3},
        "items": [REDACTED, 1],
        "exc": f"ValueError: token {REDACTED} rejected",
    }


def test_configure_logging_emits_redacted_json() -> None:
    stream = io.StringIO()
    configure_logging(["tok-999"], stream=stream)
    structlog.get_logger("t").info("hello", detail="uses tok-999")
    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert line["event"] == "hello"
    assert line["detail"] == f"uses {REDACTED}"
    assert line["level"] == "info"
    assert line["timestamp"].endswith("Z")
    assert logging.getLogger("httpx").level == logging.WARNING
