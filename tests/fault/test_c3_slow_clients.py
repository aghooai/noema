"""Slow clients (06 §5, spec-10 C3, run here at fault-suite scale): SLOW clients reading at 10% of the stream's byte
rate (small receive buffer, straight to the api containers) among NORMAL clients, all on 90 s streams of 2,700
4,000-character chunks (30 chunks/s, ~126 KB/s of SSE). The streams are that large because the server's kernel send
buffer autotunes up to tcp_wmem max (4 MiB here) before the writer's send() blocks or its queue (256 events /
1 MiB) fills: only then does the 30 s too-slow timer start.

Pass: the server coalesces the slow streams' deltas (frames carrying several chunks, id gaps) and then ends them as
`client_too_slow` (stored `failed`, spec-04 §12), while the normal clients' streams all complete with the whole text
and their TTFT p95 stays within 10% of a baseline run without slow clients.
"""

from __future__ import annotations

import os
import time

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.fault import kit
from tests.fault.slowclient import SlowClient
from tests.fault.stackctl import sql

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
CHUNK_CHARS, N_CHUNKS, CHUNKS_PER_S = 4000, 2700, 30.0
LONG = [f"{i:05d}" + "x" * (CHUNK_CHARS - 5) for i in range(N_CHUNKS)]
SLOW, NORMAL = 4, 8
WIRE_BPS = CHUNKS_PER_S * (CHUNK_CHARS + 160)  # a delta frame is ~160 bytes of SSE and JSON around the text
DEADLINE_MS = 150_000


def scenario() -> kit.Json:
    long_stream = {
        "match": {"model": kit.SOL, "op": "converse-stream", "prompt_contains": "c3-"},
        "stream": {
            "first_event_ms": 100,
            "first_text_ms": kit.FIRST_TEXT_MS,
            "chunks": LONG,
            "chunk_tokens": 4,
            "tokens_per_s": 4 * CHUNKS_PER_S,
        },
    }
    return kit.scenario("fault-C3", [long_stream], kit.members())


async def stored_status(stack: Stack, ids: list[str], *, timeout_s: float) -> dict[str, tuple[str, float]]:
    """{turn id: (status, completed_at - created_at in s)} once every turn is stored."""
    listed = ",".join(f"'{i}'" for i in ids)
    query = f"select id, status, extract(epoch from completed_at - created_at) from noema.turn where id in ({listed})"
    with anyio.fail_after(timeout_s):
        while True:
            rows = await sql(stack, query)
            if len(rows) >= len(ids):
                return {i: (s, float(d)) for i, s, d in rows}
            await anyio.sleep(1.0)


async def test_c3_slow_clients_coalesce_then_client_too_slow(stack: Stack, mock: Mock) -> None:
    direct = [ip for ip in os.environ.get("NOEMA_FAULT_DIRECT", "").split(",") if ip]
    assert len(direct) == 2, "NOEMA_FAULT_DIRECT must list the api-1 and api-2 container IPs (make qa-fault)"
    await kit.fresh_retry_budget()
    await mock.load(scenario())
    expected = "".join(LONG)

    def normal(i: int, t: str) -> kit.Json:
        return kit.body(f"c3-normal-{t}")

    baseline = await kit.run_turns(stack, NORMAL, normal, concurrency=NORMAL, deadline_ms=DEADLINE_MS)
    kit.assert_s1(baseline, expected=expected)
    stop = anyio.Event()
    slow = [
        SlowClient(
            direct[i % 2],
            8000,
            str(stack.principal(kit.TENANTS[i % 2])["key"]),
            kit.body("c3-slow"),
            rate_bps=0.1 * WIRE_BPS,
        )
        for i in range(SLOW)
    ]
    t0 = time.monotonic()
    async with anyio.create_task_group() as tg:
        for client in slow:
            tg.start_soon(client.run, stop, 200.0)
        await anyio.sleep(1.0)
        records = await kit.run_turns(stack, NORMAL, normal, concurrency=NORMAL, deadline_ms=DEADLINE_MS)
        parsed = [c.parse(stack.validator) for c in slow]
        ids = [str(kit.data(events[0])["turn_id"]) for _, events in parsed if events]
        stored = await stored_status(stack, ids, timeout_s=200.0)
        stop.set()
    kit.mark_budget_used()
    parsed = [c.parse(stack.validator) for c in slow]
    deltas = [[kit.data(e) for e in events if e.event == "generation.delta"] for _, events in parsed]
    base_ttft = [r.ttft_ms for r in baseline if r.ttft_ms is not None]
    ttft = [r.ttft_ms for r in records if r.ttft_ms is not None]
    kit.record_result(
        "C3-slow-clients",
        slow=SLOW,
        normal=NORMAL,
        slow_read_bps=round(0.1 * WIRE_BPS),
        slow_status=kit.counts(status for status, _ in parsed),
        slow_frames_received=[len(events) for _, events in parsed],
        slow_bytes_received=[len(c.raw) for c in slow],
        slow_coalesced_frames=[sum(1 for d in ds if len(str(d["text"])) > CHUNK_CHARS) for ds in deltas],
        slow_frame_in_flight_bytes=[c.tail_bytes for c in slow],  # > one frame (~4.2 KB): a coalesced frame
        slow_error_frames=[[kit.data(e) for e in events if e.event == "error"] for _, events in parsed],
        slow_stored=kit.counts(s for s, _ in stored.values()),
        slow_server_end_s=sorted(round(d, 1) for _, d in stored.values()),
        normal_outcomes=kit.counts(kit.outcome(r) for r in records),
        normal_e2e_s=kit.summary([r.e2e_ms / 1000 for r in records]),
        baseline_ttft=kit.summary(base_ttft),
        normal_ttft=kit.summary(ttft),
        run_s=round(time.monotonic() - t0, 1),
    )
    kit.assert_s1(records, expected=expected)
    assert all(status == 200 for status, _ in parsed) and len(ids) == SLOW
    coalesced = [
        n > 0 or c.tail_bytes > 2 * (CHUNK_CHARS + 160)
        for n, c in zip([sum(1 for d in ds if len(str(d["text"])) > CHUNK_CHARS) for ds in deltas], slow, strict=True)
    ]
    assert all(coalesced), "no coalesced frame reached a slow client"
    assert all(s == "failed" for s, _ in stored.values()), stored  # ended by client_too_slow, not completed
    assert all(r.completed for r in records)
    assert kit.pct(ttft, 95) <= 1.10 * kit.pct(base_ttft, 95), (kit.summary(ttft), kit.summary(base_ttft))
