"""Structured, redacted logs (08 §7 "Logs", spec-09 §4): the filter redacts messages, arguments, extra string
fields and tracebacks; the JSON formatter emits the 08 §7 keys and the active trace id."""

from __future__ import annotations

import io
import logging

import orjson

from noema.telemetry import JsonFormatter, OtelTelemetry, RedactingFilter, Redactor, configure_logging, setup_telemetry

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"


def capture() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter(Redactor()))
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.telemetry.logs")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [orjson.loads(line) for line in stream.getvalue().splitlines()]


def test_message_args_and_fields_are_redacted() -> None:
    logger, stream = capture()
    logger.warning(
        "auth failed for %s",
        f"Bearer {JWT}",
        extra={"tenant": "t_acme", "event": "authn", "code": "invalid_api_key", "turn_id": JWT},
    )
    [rec] = lines(stream)
    assert JWT not in str(rec) and "[REDACTED:" in str(rec["message"])
    assert rec["tenant"] == "t_acme" and rec["event"] == "authn" and rec["code"] == "invalid_api_key"
    assert rec["level"] == "warning" and str(rec["ts"]).endswith("Z")


def test_tracebacks_are_redacted() -> None:
    logger, stream = capture()
    try:
        raise RuntimeError("dsn postgresql://u:hunter2@db/x")
    except RuntimeError:
        logger.exception("boom")
    [rec] = lines(stream)
    assert "hunter2" not in str(rec) and "RuntimeError" in str(rec["exc"])


def test_trace_id_of_the_active_span() -> None:
    logger, stream = capture()
    setup = setup_telemetry(service_name="t", env="test", memory=True)
    tel = OtelTelemetry(setup, env="test")
    with tel.span("turn"):
        logger.info("inside")
    logger.info("outside")
    inside, outside = lines(stream)
    assert isinstance(inside["trace_id"], str) and len(inside["trace_id"]) == 32
    assert outside["trace_id"] is None


def test_configure_logging_installs_one_handler() -> None:
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        handler = configure_logging(Redactor(), level=logging.INFO)
        assert root.handlers == [handler] and isinstance(handler.formatter, JsonFormatter)
    finally:
        root.handlers, root.level = saved[0], saved[1]
