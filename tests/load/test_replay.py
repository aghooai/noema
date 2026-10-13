"""R4 (06 §3.1, §8; spec-10 §3: "a replay set of multi-turn sessions on the mock") and idempotent replay at load
(spec-04 G12, spec-11 idempotency): `make qa-replay`.

- `test_r4_spec_restarts_and_waste`: SESSIONS sessions of TURNS_PER_SESSION turns, run concurrently, each session's
  turns in order. Each turn's intent comes from a seeded script (first turn coding with p 0.3, then the previous
  intent kept with p 0.8): coding decides opus-deep, factual sol-fast, so a turn restarts exactly when its decided
  profile differs from the session's sticky one (spec-04 §5). The mock answers each turn's intent question by the
  turn's own marker (rules in descending turn order, so the history's older markers never match first); members
  answer before DeepSeek's backup fires (so the `waste` line holds only cancelled generation attempts, 06 §8
  "speculation waste") and Sol's first text comes before S1, so a restart wastes buffered output. Pass: every turn
  served; the measured restarts equal the script's; the waste line holds at least the turns'
  `timings.wasted_output_tokens` (any excess is a member backup's, reported); the spec-restart share
  (`timings.spec_restarts`) and the waste share (cost of the sessions' `waste` usage lines / cost of their
  `generation` lines) are computed and reported against 06's 10% / 5% (the thresholds need the real agreement
  rate: gate P0, not asserted).
- `test_idempotent_replay_makes_no_model_call_and_no_billing`: KEYED keyed requests (streamed turns, JSON turns,
  decisions) run once; after their usage lines settled, each is sent again with the same key and body on the other
  replica. Pass: every replay is 200 with `x-aghoo-idempotent-replay: true` and the original's body (a stream: the
  same frames), the mock receives no model call (sampled labeling jobs aside) and the project's usage report is
  unchanged.
"""

from __future__ import annotations

import os
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

import anyio
import orjson
import pytest
from tests.e2e.conftest import CheckedClient, Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import live_turn, mock_calls, report, tag, turn_body
from tests.e2e.suite.turns import new_session
from tests.fault import kit
from tests.fault.kit import TurnRecord
from tests.load import gatekit

pytestmark = [pytest.mark.e2e, pytest.mark.perf, pytest.mark.timeout(1800)]

SESSIONS: Final = int(os.environ.get("NOEMA_REPLAY_SESSIONS", "40"))
TURNS_PER_SESSION: Final = 5
CONC: Final = 20
SEED: Final = 7
MEMBER_MS: Final = 200  # well below DeepSeek's backup_after_ms (400): no member backup, `waste` = speculation
SOL_TEXT_MS, OPUS_TEXT_MS = 120, 400  # Sol's text starts before S1: a restart cancels buffered output
R4_TENANT, R4_PROJECT = "t_beta", "second"
REPLAY: Final = "x-aghoo-idempotent-replay"


def script(seed: int = SEED) -> list[list[bool]]:
    """`coding` per (session, turn): the first turn coding with p 0.3, then the previous intent kept with p 0.8."""
    rnd = random.Random(seed)
    sessions: list[list[bool]] = []
    for _ in range(SESSIONS):
        turns = [rnd.random() < 0.3]
        while len(turns) < TURNS_PER_SESSION:
            turns.append(turns[-1] if rnd.random() < 0.8 else not turns[-1])
        sessions.append(turns)
    return sessions


def expected_restarts(turns: Sequence[bool]) -> int:
    """spec0 = the sticky profile (sol-fast before the first turn); a turn restarts when its decided one differs."""
    sticky, restarts = False, 0
    for coding in turns:
        restarts += coding != sticky
        sticky = coding
    return restarts


def marker(run: str, s: int, t: int) -> str:
    return f"«{run}-s{s}t{t}»"


def r4_rules(run: str, plan: Sequence[Sequence[bool]]) -> list[gatekit.Rule]:
    """The labeler and the streams (matched by model and op), the non-intent router questions (constant answers),
    then one intent rule per turn marker in descending turn order."""
    ms = MEMBER_MS
    rules = [gatekit.labeler(), kit.gen(sc.SOL, first_text_ms=SOL_TEXT_MS)]
    rules.append(kit.gen(sc.OPUS, first_text_ms=OPUS_TEXT_MS))
    for text, dist in ((sc.COMPLEXITY, sc.MODERATE), (sc.INJECTION, sc.LOW), (sc.SENSITIVE, sc.LOW)):
        rules.append(sc.answer({"prompt_contains": text}, dist, ms))
    rules.append(sc.answer({"prompt_contains": "newer than the model"}, sc.LOW, ms))
    for t in reversed(range(TURNS_PER_SESSION)):
        for s, turns in enumerate(plan):
            intent = sc.CODING if turns[t] else sc.FACTUAL
            rules.append(sc.answer({"prompt_contains": marker(run, s, t)}, intent, ms))
    rules.append(sc.answer({}, sc.LOW, ms))
    return rules


async def usage_by_session(client: CheckedClient, sessions: set[str]) -> dict[str, dict[str, float]]:
    """{line: {cost_usd, output_tokens}} summed over the getUsage `session` rows of `sessions`."""
    out: dict[str, dict[str, float]] = {}
    for row in await report(client, "session"):
        if row["key"] in sessions:
            line = out.setdefault(row["line"], {"cost_usd": 0.0, "output_tokens": 0.0})
            line["cost_usd"] += float(row["usage"]["cost_usd"])
            line["output_tokens"] += float(row["usage"]["output_tokens"])
    return out


async def settled[T](read: Callable[[], Awaitable[T]], *, quiet_s: float = 3.0, timeout_s: float = 90.0) -> T:
    """`await read()` until two reads `quiet_s` apart are equal (usage lines are written behind, IMPL-040)."""
    previous = await read()
    with anyio.fail_after(timeout_s):
        while True:
            await anyio.sleep(quiet_s)
            current = await read()
            if current == previous:
                return current
            previous = current


async def test_r4_spec_restarts_and_waste(stack: Stack, mock: Mock, gate_config: int) -> None:
    run, plan = uuid.uuid4().hex[:8], script()
    await gatekit.fresh_mock(mock, kit.scenario("R4-replay", r4_rules(run, plan)))
    clients = [stack.client(r, tenant=R4_TENANT, project=R4_PROJECT) for r in (0, 1)]
    records: list[list[TurnRecord]] = [[] for _ in plan]
    session_ids: list[str] = [""] * len(plan)
    limiter = anyio.Semaphore(CONC)
    headers = {"aghoo-deadline-ms": str(kit.DEADLINE_MS)}

    async def session(s: int) -> None:
        async with limiter:
            client = clients[s % 2]
            session_ids[s] = await new_session(client)
            for t in range(TURNS_PER_SESSION):
                record = TurnRecord(s * TURNS_PER_SESSION + t, R4_TENANT, s % 2)
                body = turn_body(f"Question {t} {marker(run, s, t)} ({tag('R4')})", session_id=session_ids[s])
                await kit.one_turn(client, record, body, headers)
                records[s].append(record)

    async with anyio.create_task_group() as tg:
        for s in range(len(plan)):
            tg.start_soon(session, s)
    every = [r for turns in records for r in turns]
    kit.assert_s1(every)
    restarts = [[int(r.of("turn.completed")[0]["timings"]["spec_restarts"]) for r in turns] for turns in records]
    measured = [sum(per) for per in restarts]
    expected = [expected_restarts(turns) for turns in plan]
    usage = await settled(lambda: usage_by_session(clients[1], set(session_ids)))
    generation, waste = usage.get("generation", {}), usage.get("waste", {})
    restart_share = gatekit.share(sum(measured), len(every))
    wasted_tokens = sum(int(r.of("turn.completed")[0]["timings"]["wasted_output_tokens"] or 0) for r in every)
    waste_share = round(waste.get("cost_usd", 0.0) / generation["cost_usd"], 4) if generation else None
    numbers = {
        "sessions": len(plan),
        "turns": len(every),
        "served": sum(r.completed for r in every),
        "agreement_rate": round(1 - sum(expected) / len(every), 4),
        "spec_restarts": sum(measured),
        "spec_restart_share": restart_share,
        "waste_share_of_generation_cost": waste_share,
        "timings_wasted_output_tokens": wasted_tokens,
        "member_waste_output_tokens": waste.get("output_tokens", 0) - wasted_tokens,
        "usage_lines": usage,
        "p0_thresholds": {"spec_restart_share_le": 0.10, "waste_share_le": 0.05},
        "p0_met": {"restarts": restart_share <= 0.10, "waste": (waste_share or 0.0) <= 0.05},
    }
    gatekit.record("R4 replay set (restarts, waste)", **numbers)
    assert numbers["served"] == len(every), kit.counts(kit.outcome(r) for r in every)
    assert measured == expected, [(s, m, e) for s, (m, e) in enumerate(zip(measured, expected, strict=True)) if m != e]
    assert waste_share is not None and (waste_share > 0) == (sum(measured) > 0), numbers
    assert waste.get("output_tokens", 0) >= wasted_tokens, numbers  # the line holds every cancelled attempt


KEYED: Final = int(os.environ.get("NOEMA_REPLAY_KEYED", "60"))  # a third each: streamed turns, JSON turns, decisions
ID_TENANT, ID_PROJECT = "t_acme", "second"
QUESTION: Final = {"type": "predicate", "name": "callback", "instructions": "The user asks for a call back."}


def keyed_request(i: int) -> tuple[str, str, gatekit.Json]:
    """(kind, path, body) of the i-th keyed request."""
    text = f"What is the capital of France? ({tag(f'idem-{i}')})"
    if i % 3 == 0:
        return "sse", "/v1/turns", turn_body(text)
    if i % 3 == 1:
        return "json", "/v1/turns", turn_body(text, stream=False)
    body = {"profile": "router-default", "escalation": "off", "input": text, "questions": [QUESTION]}
    return "decision", "/v1/decisions", body


async def send(client: CheckedClient, kind: str, path: str, body: gatekit.Json, key: str) -> tuple[bool, object]:
    """One keyed request, validated; (the replay header was set, the body: frames of a stream, else the JSON)."""
    headers = {"Idempotency-Key": key}
    if kind == "sse":
        async with live_turn(client, body, headers=headers) as turn:
            await turn.rest()
        replayed = turn.response.headers.get(REPLAY) == "true"
        return replayed, [(e.id, e.event, e.data) for e in turn.events]
    response = await client.request("POST", path, json=body, headers=headers, expect=200)
    return response.headers.get(REPLAY) == "true", response.json()


async def billed(client: CheckedClient) -> list[tuple[str, int, int, float]]:
    """The project's usage by line, without `labeling` (sampled labeling jobs of the originals may land late)."""
    rows = await report(client, "line")
    return sorted(
        (r["line"], int(r["usage"]["input_tokens"]), int(r["usage"]["output_tokens"]), float(r["usage"]["cost_usd"]))
        for r in rows
        if r["line"] != "labeling"
    )


async def test_idempotent_replay_makes_no_model_call_and_no_billing(stack: Stack, mock: Mock, gate_config: int) -> None:
    rules = [gatekit.labeler(), kit.gen(sc.SOL, first_text_ms=200), *sc.router(ms=100, haiku_hangs_on=None)]
    await gatekit.fresh_mock(mock, kit.scenario("idempotent-replay", rules))
    first, other = (stack.client(r, tenant=ID_TENANT, project=ID_PROJECT) for r in (0, 1))
    planned = [(*keyed_request(i), f"qa-replay-{uuid.uuid4()}") for i in range(KEYED)]
    originals: list[object] = [None] * KEYED
    replays: list[tuple[bool, object]] = [(False, None)] * KEYED
    limiter = anyio.Semaphore(10)

    async def original(i: int) -> None:
        async with limiter:
            replayed, originals[i] = await send(first, *planned[i])
            assert not replayed, planned[i]

    async def replay(i: int) -> None:
        async with limiter:
            replays[i] = await send(other, *planned[i])

    async with anyio.create_task_group() as tg:
        for i in range(KEYED):
            tg.start_soon(original, i)
    before = await settled(lambda: billed(first))
    seq = int((await mock.requests(limit=0))["last_seq"])
    async with anyio.create_task_group() as tg:
        for i in range(KEYED):
            tg.start_soon(replay, i)
    after = await settled(lambda: billed(first))
    calls = [c for c in await mock_calls(mock, seq) if kit.LABELER_PROMPT not in orjson.dumps(c["body"]).decode()]
    kinds = kit.counts(kind for kind, *_ in planned)
    numbers = {
        "keyed": kinds,
        "replay_header": sum(flag for flag, _ in replays),
        "identical": sum(body == originals[i] for i, (_flag, body) in enumerate(replays)),
        "model_calls_during_replay": len(calls),
        "usage_before": before,
        "usage_after": after,
    }
    gatekit.record("Idempotent replay (no model call, no billing)", **numbers)
    assert numbers["replay_header"] == KEYED and numbers["identical"] == KEYED, numbers
    assert calls == [], kit.counts(f"{c['model']}:{c['op']}" for c in calls)
    assert after == before and before != [], numbers
