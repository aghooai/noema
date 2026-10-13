"""Wave E3: the wire mock's ConverseStream bodies (tools/mockbedrock in-process on a virtual clock) decode with
`EventStreamDecoder` exactly as with botocore's `EventStreamBuffer`: the L1 load stream (tests/load/scenarios/
overhead.yaml, stamped), Claude and OpenAI-family reasoning, padded payloads, every declared exception type, a
corrupt message or prelude CRC, truncated JSON, refusals and reasoning-only stops; each in the chunks the socket
delivered, whole, in 1, 7 and 64 byte chunks and split at random points. Then the full reader (`_ConverseRaw`)
yields the same events through either decoder."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from tests.unit.adapters.eventstream_compare import botocore_outcome, noema_outcome
from tests.unit.adapters.test_lean_converse_stream import lean
from tests.unit.mockbedrock.helpers import GLM, OPUS, SOL, USER_HI, scenario, sigv4_headers
from tools.mockbedrock import MockBedrock, VirtualClock
from tools.mockbedrock.eventstream import EXCEPTION_TYPES

import noema.adapters.converse as converse_module

L1 = Path(__file__).parents[2] / "load" / "scenarios" / "overhead.yaml"
STREAM = {"text": "The capital of France is Paris.", "usage": "auto"}
SCENARIOS: dict[str, tuple[str, dict[str, Any]]] = {
    "l1_load_stream": (SOL, {}),
    "claude_reasoning": (OPUS, {"stream": {"reasoning": {"deltas": 3}, **STREAM}}),
    "glm_reasoning": (GLM, {"stream": {"reasoning": {"deltas": 2}, **STREAM}}),
    "openai_hidden_reasoning": (SOL, {"stream": {"reasoning": {"deltas": 2}, **STREAM}}),
    "padded": (SOL, {"stream": {"text": "padded", "pad": 7}}),
    "chunks": (SOL, {"stream": {"chunks": ["a", "é", "漢字", "\U0001f600"], "pad": 3}}),
    **{
        f"exception_{t}": (SOL, {"stream": {"chunks": ["a", "b"]}, "fault": {"kind": "exception_event", "type": t}})
        for t in sorted(EXCEPTION_TYPES)
    },
    "bad_message_crc": (SOL, {"stream": {"text": "abc"}, "fault": {"kind": "malformed", "variant": "bad_crc"}}),
    "bad_prelude_crc": (SOL, {"stream": {"text": "abc"}, "fault": {"kind": "malformed", "variant": "bad_sse_line"}}),
    "truncated_json": (SOL, {"stream": {"text": "abc"}, "fault": {"kind": "truncated_json"}}),
    "refusal": (OPUS, {"stream": {"text": "x"}, "fault": {"kind": "refusal"}}),
    "reasoning_only": (
        OPUS,
        {"stream": {"reasoning": {"deltas": 2}, "text": "x"}, "fault": {"kind": "reasoning_only"}},
    ),
}


@pytest.fixture
async def mock() -> AsyncIterator[MockBedrock]:
    async with MockBedrock(clock=VirtualClock()) as server:
        yield server


async def _received(mock: MockBedrock, name: str) -> list[bytes]:
    """The body of the scenario's ConverseStream response, in the chunks the client socket read."""
    model, rule = SCENARIOS[name]
    if name == "l1_load_stream":
        mock.load_scenario(yaml.safe_load(L1.read_text()))
    else:
        mock.load_scenario(scenario({"match": {"model": model}, **rule}))
    async with httpx.AsyncClient(base_url=mock.endpoint_url, timeout=10.0) as http:
        request = http.build_request(
            "POST", f"/model/{model}/converse-stream", json={"messages": USER_HI}, headers=sigv4_headers()
        )
        response = await http.send(request, stream=True)
        assert response.status_code == 200
        return [chunk async for chunk in response.aiter_raw()]


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_wire_mock_streams_decode_as_botocore_decodes_them(mock: MockBedrock, name: str) -> None:
    received = await _received(mock, name)
    data = b"".join(received)
    rng = random.Random(name)
    chunkings = [received, [data], *([data[i : i + n] for i in range(0, len(data), n)] for n in (1, 7, 64))]
    cuts = sorted(rng.sample(range(1, len(data)), 8))
    chunkings.append([data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)], strict=True)])
    for chunks in chunkings:
        assert noema_outcome(chunks) == botocore_outcome(chunks), (name, len(chunks))
    messages, failure = botocore_outcome([data])
    assert messages or failure is not None
    if name.startswith(("exception_", "bad_")):
        assert failure is not None or messages[-1][0][":message-type"] == "exception"


class _BotocoreDecoder:
    """botocore's EventStreamBuffer behind the decoder interface (the reference for the full reader)."""

    def __init__(self) -> None:
        from botocore.eventstream import EventStreamBuffer

        self._buffer = EventStreamBuffer()

    def add_data(self, data: bytes) -> None:
        self._buffer.add_data(data)

    def next(self) -> tuple[Any, bytes] | None:
        try:
            message = self._buffer.next()
        except StopIteration:
            return None
        return message.headers, message.payload


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_reader_yields_the_same_events_with_either_decoder(
    mock: MockBedrock, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    received = await _received(mock, name)
    ours = await lean(received)
    monkeypatch.setattr(converse_module, "EventStreamDecoder", _BotocoreDecoder)
    assert await lean(received) == ours
