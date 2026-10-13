"""Incremental server-sent events parser (WHATWG HTML §9.2 parsing rules)."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class SseMessage:
    """One dispatched event. `id` is the stream's last event id at dispatch (it persists across events)."""

    id: str | None
    event: str
    data: str


_EOL: Final = re.compile(r"\r\n?|\n")


class SseParser:
    """Feed raw bytes with `push`; it returns the events completed by that chunk. Comments (`: ping`), `retry:` and
    unknown fields are ignored, a CRLF may be split across chunks, and an event without its trailing blank line at end
    of stream is discarded. Invalid UTF-8 decodes to U+FFFD, as the SSE spec requires. Each byte is scanned once, so
    large frames in small chunks stay linear."""

    __slots__ = ("_data", "_decoder", "_event", "_last_id", "_pending", "_skip_lf", "_started")

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending: list[str] = []  # the current line's text so far
        self._skip_lf = False  # the previous chunk ended with a CR: a leading LF completes that CRLF
        self._started = False
        self._data: list[str] = []
        self._event = ""
        self._last_id: str | None = None

    @property
    def last_event_id(self) -> str | None:
        return self._last_id

    def push(self, chunk: bytes) -> list[SseMessage]:
        text = self._decoder.decode(chunk)
        if not text:
            return []
        if not self._started:
            self._started = True
            if text[0] == "\ufeff":
                text = text[1:]
        if self._skip_lf:
            self._skip_lf = False
            if text.startswith("\n"):
                text = text[1:]
        out: list[SseMessage] = []
        pos = 0
        for m in _EOL.finditer(text):
            if self._pending:
                self._pending.append(text[pos : m.start()])
                line = "".join(self._pending)
                self._pending.clear()
            else:
                line = text[pos : m.start()]
            self._line(line, out)
            pos = m.end()
        if pos < len(text):
            self._pending.append(text[pos:])
        self._skip_lf = text.endswith("\r")
        return out

    def end(self) -> list[SseMessage]:
        """End of stream: an unterminated line or event is dropped (as the spec requires)."""
        self._decoder.decode(b"", final=True)
        self._pending.clear()
        self._data = []
        return []

    def _line(self, line: str, out: list[SseMessage]) -> None:
        if not line:
            if self._data:
                out.append(SseMessage(self._last_id, self._event or "message", "\n".join(self._data)))
            self._data = []
            self._event = ""
            return
        if line[0] == ":":
            return
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id" and "\x00" not in value:
            self._last_id = value
