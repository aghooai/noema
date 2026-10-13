"""Structured, redacted logging (08 §7 "Logs", spec-09 §4).

`RedactingFilter` rewrites every record so that its message (with the arguments merged in) and every string field
pass through `redact()`; `JsonFormatter` writes one JSON object per line with the 08 §7 keys (`ts`, `level`,
`trace_id`, `tenant`, `turn_id`, `event`, `code`) plus `logger` and `message`. `configure_logging` installs both on
the root logger. Logs are content-free by default: callers pass ids and codes, never prompt or model text.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Final

import orjson
from opentelemetry import trace

from .redact import Redactor

STRUCTURED_KEYS: Final = ("tenant", "turn_id", "event", "code")
_RESERVED: Final = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class RedactingFilter(logging.Filter):
    """Redacts the formatted message and every extra string field of each record; never drops a record."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must not lose the record
            message = str(record.msg)
        record.msg = self._redactor.redact(message)
        record.args = None
        for key, value in list(vars(record).items()):
            if key not in _RESERVED and isinstance(value, str):
                setattr(record, key, self._redactor.redact(value))
        if record.exc_info and not record.exc_text:
            record.exc_text = self._redactor.redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)
        record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record (the 08 §7 keys; `trace_id` from the current span when a span is active)."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": f"{ctx.trace_id:032x}" if ctx.is_valid else getattr(record, "trace_id", None),
        }
        for key in STRUCTURED_KEYS:
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_text:
            out["exc"] = record.exc_text
        return orjson.dumps(out, default=str).decode()


def configure_logging(redactor: Redactor, *, level: int = logging.INFO) -> logging.Handler:
    """Install one stderr handler with `RedactingFilter` + `JsonFormatter` on the root logger (replacing the
    handlers it had) and return it."""
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter(redactor))
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    return handler
