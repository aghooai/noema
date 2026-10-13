from __future__ import annotations

import pytest

from aghoo._sse import SseMessage, SseParser


def parse(chunks: list[bytes]) -> list[SseMessage]:
    p = SseParser()
    out = [m for c in chunks for m in p.push(c)]
    return out + p.end()


def test_id_event_and_data() -> None:
    assert parse([b'id: 1\nevent: turn.created\ndata: {"type":"turn.created"}\n\n']) == [
        SseMessage("1", "turn.created", '{"type":"turn.created"}')
    ]


def test_multi_line_data_default_event_and_persistent_id() -> None:
    assert parse([b"id: 7\ndata: a\ndata: b\n\ndata: c\n\n"]) == [
        SseMessage("7", "message", "a\nb"),
        SseMessage("7", "message", "c"),
    ]


def test_comments_retry_and_unknown_fields_are_ignored() -> None:
    assert parse([b": ping\n\nretry: 100\nfoo: bar\n\nid: 2\nevent: x\ndata: {}\n\n"]) == [SseMessage("2", "x", "{}")]


def test_line_endings_including_crlf_split_across_chunks() -> None:
    assert parse([b"id: 1\r", b"\nevent: e\rdata: d\r\n\r", b"\n"]) == [SseMessage("1", "e", "d")]


def test_bom_leading_space_and_field_without_colon() -> None:
    assert parse(["\ufeffdata:  two\n\ndata\n\n".encode()]) == [
        SseMessage(None, "message", " two"),
        SseMessage(None, "message", ""),
    ]


@pytest.mark.parametrize("size", range(1, 9))
def test_any_chunking_gives_the_same_events(size: int) -> None:
    raw = 'id: 1\nevent: a\ndata: {"x":"é…"}\n\nid: 2\nevent: b\ndata: 2\n\n'.encode()
    chunks = [raw[i : i + size] for i in range(0, len(raw), size)]  # splits multi-byte characters too
    assert parse(chunks) == parse([raw])


def test_unterminated_event_is_dropped_and_nul_ids_ignored() -> None:
    assert parse([b"id: 1\ndata: done\n\nid: 2\x00\ndata: partial"]) == [SseMessage("1", "message", "done")]
    p = SseParser()
    p.push(b"id: 1\ndata: x\n\nid: 9\x00\n")
    assert p.last_event_id == "1"


def test_final_line_ended_by_a_lone_cr() -> None:
    assert parse([b"data: x\n\r"]) == [SseMessage(None, "message", "x")]


def test_a_large_frame_in_small_chunks_is_parsed_in_linear_time() -> None:
    """2 MiB of data in 64-byte chunks: a parser that re-scans its buffer per chunk would hit the test timeout."""
    raw = b"id: 1\nevent: decision.completed\ndata: " + b"x" * (2 << 20) + b"\n\n"
    p = SseParser()
    out = [m for i in range(0, len(raw), 64) for m in p.push(raw[i : i + 64])]
    assert len(out) == 1
    assert len(out[0].data) == 2 << 20


def test_invalid_utf8_is_replaced_with_u_fffd() -> None:
    assert parse([b"data: a\xffb\n\n", b"data: \xe2\x82", b"\n\n"]) == [
        SseMessage(None, "message", "a\ufffdb"),
        SseMessage(None, "message", "\ufffd"),  # a truncated sequence cut by the line end
    ]
