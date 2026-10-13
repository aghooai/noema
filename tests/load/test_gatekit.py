"""Self-tests of the stack gates' pure parts (tests/load/gatekit.py, the replay script): no stack needed."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from tests.e2e.conftest import CheckedClient
from tests.fault.kit import TurnRecord
from tests.load import gatekit
from tests.load.test_replay import expected_restarts, script

from noema.api.middleware.routing import OperationTable
from noema.contracts.bundle import default_bundle
from noema.contracts.validate import ContractValidator


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


async def test_the_first_delta_is_found_across_a_chunk_boundary() -> None:
    body = (b"id: 1\nevent: turn.created\ndata: {}\n\nid: 2\nevent: generation.", b"delta\ndata: {}\n\n")
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, stream=Chunks(*body)))
    client = CheckedClient(
        httpx.AsyncClient(transport=transport, base_url="http://api"),
        ContractValidator(),
        OperationTable(default_bundle().operations()),
    )
    record = TurnRecord(0, "t_acme", 0)
    _headers, raw = await gatekit.timed_turn(client, record, {}, {})
    assert record.status == 200 and raw == b"".join(body) and record.ttft_ms is not None
    record2 = TurnRecord(1, "t_acme", 0)
    transport2 = httpx.MockTransport(lambda _r: httpx.Response(200, stream=Chunks(body[0])))
    client.http = httpx.AsyncClient(transport=transport2, base_url="http://api")
    await gatekit.timed_turn(client, record2, {}, {})
    assert record2.ttft_ms is None  # no delta frame, no TTFT


def test_a_bound_met_in_an_invalid_window_passes_a_miss_is_invalid_or_a_failure() -> None:
    invalid = gatekit.validity(3.5, [0.1, 0.2])
    gatekit.assert_within(invalid, 900.0, 950.0)
    with pytest.raises(AssertionError, match="invalid window"):
        gatekit.assert_within(invalid, 960.0, 950.0)
    valid = gatekit.validity(1.0, [0.5] * 99 + [1.9])
    assert valid["valid"]
    with pytest.raises(AssertionError, match=r"960\.0 > 950\.0"):
        gatekit.assert_within(valid, 960.0, 950.0)


def test_mock_lateness_per_rule_kind() -> None:
    scenario = {"rules": [{"stream": {"first_text_ms": 300}}, {"latency_ms": 900}, {"latency": {"p5": 1, "p95": 2}}]}
    ms = 1_000_000
    calls = [
        {"rule": 0, "outcome": "ok", "t_req_recv_ns": 0, "t_first_text_sent_ns": 301 * ms},
        {"rule": 1, "outcome": "ok", "t_req_recv_ns": 0, "t_done_ns": 905 * ms},
        {"rule": 1, "outcome": "disconnected", "t_req_recv_ns": 0, "t_done_ns": 100 * ms},
        {"rule": 2, "outcome": "ok", "t_req_recv_ns": 0, "t_done_ns": 7 * ms},  # a drawn latency: not judged
    ]
    assert gatekit.mock_lateness(scenario, calls) == [1.0, 5.0]


def test_steady_drops_the_first_sent() -> None:
    records = [TurnRecord(i, "t_acme", 0) for i in range(4)]
    for record, t in zip(records, (3.0, 1.0, 2.0, 0.5), strict=True):
        record.t_send = t
    assert [r.index for r in gatekit.steady(records, 2)] == [2, 0]


def test_the_replay_script_is_seeded_and_its_restarts_follow_the_sticky_profile() -> None:
    assert script() == script() and script(8) != script()
    assert expected_restarts([False, False, True, True, False]) == 2
    assert expected_restarts([True]) == 1 and expected_restarts([]) == 0
