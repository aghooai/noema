"""Step 3: the JSON body cap (IMPL-064, spec-09 §1 SEC-EDGE-01).

JSON bodies are buffered up to `platform.limits.max_json_body_bytes` of the pinned snapshot (50 MiB by default);
one byte more is 413 `request_too_large`, decided from `Content-Length` when present and otherwise while reading,
so an oversized body is never buffered whole and never parsed. Multipart uploads are not buffered here (media
intake caps them per type while streaming, 413 `file_too_large`).
"""

from __future__ import annotations

from starlette.types import Message, Receive

from noema.core.errors import ErrorCode, NoemaError


class ClientDisconnected(Exception):
    """The client went away before the request body was complete; no response is sent."""


def too_large(limit: int) -> NoemaError:
    """The 413 `request_too_large` error for a body over `limit` bytes."""
    return NoemaError(ErrorCode.REQUEST_TOO_LARGE, f"Request body exceeds {limit} bytes.")


async def read_capped_body(receive: Receive, limit: int, content_length: str | None) -> bytes:
    """Read the whole request body, at most `limit` bytes.

    Raises `NoemaError(request_too_large)` as soon as the declared or received size exceeds `limit`, and
    `ClientDisconnected` on `http.disconnect`.
    """
    if content_length is not None and content_length.isdigit() and int(content_length) > limit:
        raise too_large(limit)
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ClientDisconnected
        if message["type"] != "http.request":
            continue
        chunk: bytes = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise too_large(limit)
        if chunk:
            chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def replay_receive(body: bytes, receive: Receive) -> Receive:
    """A `receive` that first yields the buffered `body` as one message, then defers to the original `receive`
    (which then only reports `http.disconnect`). The body is released once delivered."""
    pending: list[bytes] = [body]

    async def _receive() -> Message:
        if pending:
            return {"type": "http.request", "body": pending.pop(), "more_body": False}
        return await receive()

    return _receive
