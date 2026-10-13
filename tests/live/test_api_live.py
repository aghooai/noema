"""Live smoke, API level (spec-10 §6): L5 `/v1/decisions` on `lightweight-model-decide` (3 questions, jev, escalation
off), L6 `/v1/decisions` on `router-default` (1 question: the three elicitation shapes), L7 one turn (router-default,
new session, verify), L8 the pinned turns interleaved with direct `Adapter.stream()` calls (gate L6). Wave E3: one
verified turn on the GLM race under the smoke router (CR #26's verification quorum and the race at low, 7 calls).
Worst case 44 + 3 x L8 pairs calls.

The api runs in-process (uvicorn on port 0) on `composition.build("dev", wrap_adapters=...)` with
`NOEMA_CREDENTIALS_BROKER=ambient` (identity's `AmbientBroker`, the shell's AWS_BEARER_TOKEN_BEDROCK for Mantle);
the counting registry wraps the real adapter registry before the engines capture it, so one ledger sees every
call. Needs the compose stack of `tests/live/apistack.py` (skipped without it). Every response is validated by the
contract validator (client side and the server's test-mode middleware).
"""

from __future__ import annotations

import socket
import statistics
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import anyio
import httpx
import pytest
from tests.live import bearer
from tests.live.apistack import (
    Checked,
    Json,
    activate_live_config,
    patched_environ,
    principal_key,
    stack_environ,
    stack_state,
)
from tests.live.budget import CountingAdapterRegistry, Ledger
from tests.live.conftest import MOCK, SCENARIO, Live
from tests.live.test_adapters_live import L8_PAIRS, TOPICS, assert_well_formed, req, stream_all
from tools.mockbedrock import MockBedrock

from noema.core.protocols.adapters import AdapterRegistry
from noema.core.values import TextBlock

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]
QUESTIONS: Final = [
    {"type": "choice", "name": "department", "instructions": "Which team should handle this message?",
     "choices": [{"value": "billing"}, {"value": "shipping"}, {"value": "other"}]},
    {"type": "score", "name": "urgency", "instructions": "How urgent is the request?",
     "levels": [{"label": "low"}, {"label": "medium"}, {"label": "high"}]},
    {"type": "predicate", "name": "refund_requested", "instructions": "The user asks for money back."},
]  # fmt: skip
MESSAGE: Final = "My package says delivered but I was also charged twice. Can you fix this today?"
RUN_TAG: Final = f"(ref {uuid.uuid4().hex[:8]})"  # a unique input per run: no L2 decision-cache hit across runs
INPUT: Final = f"{MESSAGE} {RUN_TAG}"


def user(text: str) -> list[Json]:
    return [{"role": "user", "content": [{"type": "input_text", "text": f"{text} {RUN_TAG}"}]}]


@dataclass
class Api:
    client: Checked
    services: Any
    app: Any
    state: Json
    account: str
    tmp: Path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


async def _ready(api: Api, version: int) -> None:
    with anyio.fail_after(30):  # the config loads in start(); the watcher re-reads every 5 s or on config_changed
        while api.services.config.current().version != version:  # noqa: ASYNC110 - no activation event
            await anyio.sleep(0.2)
        await api.client.request("GET", "/readyz")


@pytest.fixture(scope="session")
async def api(live: Live, ledger: Ledger, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Api]:
    import uvicorn

    from noema import composition
    from noema.api.app import create_app

    state = stack_state()
    if state is None:
        pytest.skip("no compose stack: make e2e-up E2E_PROJECT=noema-live (tests/live/apistack.py)")
    assert live.broker.caller_account is not None
    account, tmp = live.broker.caller_account, tmp_path_factory.mktemp("live-config")
    version = await activate_live_config(state, tmp, account)

    def wrap(inner: AdapterRegistry) -> AdapterRegistry:
        return CountingAdapterRegistry(inner, ledger)

    async with AsyncExitStack() as stack:
        mock_url = None
        if MOCK:
            mock_url = (await stack.enter_async_context(MockBedrock(scenario=SCENARIO))).endpoint_url
        stack.enter_context(
            patched_environ(
                stack_environ(
                    state,
                    credentials_file=live.credentials_file,
                    sts=live.sts_endpoint,
                    mock_url=mock_url,
                    bearer=None if MOCK else bearer.TOKEN,
                )
            )
        )
        services = composition.build("dev", wrap_adapters=wrap)
        assert {services.wiring[k] for k in ("adapters", "broker", "decisions", "generation")} == {"real"}
        assert type(services.broker).__name__ == "AmbientBroker"
        app = create_app(services=services, env="dev", validate=True)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=_free_port(), lifespan="on", log_level="warning")
        )
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve)
            while not server.started:  # noqa: ASYNC110 - uvicorn exposes no startup event
                await anyio.sleep(0.05)
            port = server.servers[0].sockets[0].getsockname()[1]
            headers = {"authorization": f"Bearer {principal_key(state)}", "aghoo-version": "2026-10-01"}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=180) as http:
                await composition.start(services, "api")
                try:
                    out = Api(Checked(http), services, app, state, account, tmp)
                    await _ready(out, version)
                    yield out
                    assert app.state.contract_violations == []
                finally:
                    await composition.stop(services)
            server.should_exit = True


async def decide(api: Api, body: Json) -> Json:
    payload: Json = (await api.client.request("POST", "/v1/decisions", json=body)).json()
    assert api.app.state.contract_violations == []
    return payload


def calls_since(ledger: Ledger, before: int) -> list[dict[str, Any]]:
    return ledger.calls[before:]


async def test_l5_lightweight_decide_jev(api: Api, ledger: Ledger) -> None:
    """L5: one member (Luna, verbalized) x 3 questions = 3 calls; the Jev answer of every question."""
    ledger.need(3)
    before, seen = ledger.count, len(ledger.calls)
    body = {"profile": "lightweight-model-decide", "output_format": "jev", "escalation": "off", "input": INPUT,
            "questions": QUESTIONS}  # fmt: skip
    result = await decide(api, body)
    assert ledger.count - before == 3
    assert set(result["answers"]) == {"department", "urgency", "refund_requested"}, result
    assert result["answers"]["department"]["choice"] in {"billing", "shipping", "other"}
    calls = calls_since(ledger, seen)
    assert {c["alias"] for c in calls} == {"luna-6"} and all(c["text_chars"] > 0 for c in calls)


async def test_l6_router_default_one_question(api: Api, ledger: Ledger) -> None:
    """L6: router-default on one question: DeepSeek (token logprobs, Mantle), Luna and Haiku (verbalized, Converse),
    plus at most one DeepSeek backup; quorum 2 of 3."""
    ledger.need(4)
    before, seen = ledger.count, len(ledger.calls)
    body = {"profile": "router-default", "escalation": "off", "input": INPUT, "questions": QUESTIONS[:1],
            "include": ["members"]}  # fmt: skip
    result = await decide(api, body)
    assert 3 <= ledger.count - before <= 4
    (answer,) = result["answers"]
    assert answer["choice"] in {"billing", "shipping", "other"} and answer["quorum"]["responded"] >= 2, answer
    shapes = {m["model"]: (m["elicitation"], m["status"]) for m in answer.get("members", [])}
    calls = calls_since(ledger, seen)
    answered = [c for c in calls if "usage" in c]  # not cancelled (a backup's loser, a member past the deadline)
    deepseek = [c for c in answered if c["alias"] == "deepseek-3.2"]
    assert deepseek and all(c["logprob_tokens"] for c in deepseek), calls  # token_logprobs on the wire
    for alias in ("luna-6", "haiku-5.5"):  # verbalized JSON text, no logprobs
        assert all(c["logprob_tokens"] is None and c["text_chars"] for c in answered if c["alias"] == alias)
    ledger.write_report("decision-shapes.json", {"members": shapes, "calls": len(calls), "status": result["status"]})


async def test_l7_one_routed_turn_with_verification(api: Api, ledger: Ledger) -> None:
    """L7: one turn (router-default, a new session, verify): 15 decision calls (+ <= 5 backups), the generation
    (+ <= 1 restart, <= 2 failure attempts), verification 2 questions x 2 members; completed and stored."""
    ledger.need(28)
    before = ledger.count
    body = {"input": user(MESSAGE), "verify": True, "stream": True}
    events, marks = await api.client.sse("/v1/turns", body)
    assert api.app.state.contract_violations == []
    kinds = [e.event for e in events]
    assert kinds[-1] == "turn.completed" and "generation.delta" in kinds, kinds
    taken = ledger.taken[before:]
    assert len(taken) <= 28
    by_kind = Counter(f"{t['call_kind']}:{t['alias']}" for t in taken)
    report = {"calls": len(taken), "by_call_kind": dict(by_kind), "events": dict(Counter(kinds)), "marks": marks}
    ledger.write_report("l7-turn.json", report)


def _member_rows(calls: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    keys = ("alias", "effort", "max_tokens", "stop", "usage", "text_chars", "e2e_ms", "cancelled", "error")
    return [{k: c.get(k) for k in keys} for c in calls if c["call_kind"] == kind]


@pytest.mark.timeout(300)
async def test_l7_verified_turn_on_the_glm_race(api: Api, ledger: Ledger) -> None:
    """Wave E3 items 3 and 4 in one turn (7 calls, not L7's 20-28): CR #26's verification quorum (answer-verifier:
    Haiku at low with its 600-token budget and Luna, 2 questions x 2 members) on `race-sol-glm` (Sol low and GLM
    low: one wins, the loser is cancelled), under the smoke router (1 call, Luna), conditioning off (no restart)."""
    ledger.need(9)
    version = await activate_live_config(api.state, api.tmp, api.account, smoke_router=True)
    await _ready(api, version)
    before, seen = ledger.count, len(ledger.calls)
    body = {"input": user(MESSAGE), "verify": True, "conditioning": "off", "stream": True,
            "generation": {"profile": "race-sol-glm", "max_output_tokens": 512}}  # fmt: skip
    events, marks = await api.client.sse("/v1/turns", body)
    assert api.app.state.contract_violations == []
    kinds = [e.event for e in events]
    calls = calls_since(ledger, seen)
    data = {e.event: e.data for e in events if isinstance(e.data, dict)}
    verification: Any = (data.get("turn.verified") or {}).get("verification") or {}
    answers = verification.get("answers") or []
    started = [e.data for e in events if e.event == "generation.started" and isinstance(e.data, dict)]
    report: Any = {
        "calls": ledger.count - before, "events": dict(Counter(kinds)), "marks": marks,
        "race": {"started": [d.get("model") for d in started],
                 "cancelled": [e.data for e in events if e.event == "generation.cancelled"],
                 "generation": _member_rows(calls, "generation")},
        "verification": {"status": verification.get("status"),
                         "answers": {a.get("name"): {"quorum": a.get("quorum"), "status": a.get("status"),
                                                     "members": [(m.get("model"), m.get("status")) for m in
                                                                 a.get("members") or []]} for a in answers},
                         "members": _member_rows(calls, "verification_member")},
    }  # fmt: skip
    ledger.write_report("l7-verified-race.json", report)
    assert kinds[-1] == "turn.completed" and "generation.delta" in kinds and "turn.verified" in kinds, kinds
    assert report["calls"] <= 9
    assert sorted(report["race"]["started"]) == ["us.openai.gpt-6.1-sol", "us.zai.glm-5.3"], report["race"]
    assert not any(r["error"] for r in report["race"]["generation"]), report["race"]  # GLM at low: accepted
    assert verification.get("status") == "completed" and len(answers) == 2, report["verification"]
    for answer in answers:  # CR #26: both members respond, so the 2-of-2 quorum holds
        assert answer["quorum"]["responded"] >= answer["quorum"]["required"] == 2, report["verification"]


CALLS_PER_PAIR: Final = 3  # the direct stream + the pinned turn's 2 (the smoke router's member, sol-fast)


def _pairs_report(rows: list[Json]) -> Json:
    """p50 of the pinned client first delta / e2e over the direct first text / e2e, and the per-pair ratios."""
    report: Json = {"profile": "sol-fast", "effort": "low", "n": len(rows), "pairs": rows}
    for key, dkey in (("first_delta", "first_text"), ("e2e", "e2e")):
        pinned, direct = [r["pinned"][key] for r in rows], [r["direct"][dkey] for r in rows]
        ratios = sorted(p / d for p, d in zip(pinned, direct, strict=True))
        report[f"p50_pinned_{key}_ms"], report[f"p50_direct_{dkey}_ms"] = (
            statistics.median(pinned),
            statistics.median(direct),
        )
        report[f"p50_ratio_{key}"] = round(statistics.median(pinned) / statistics.median(direct), 3)
        report[f"pair_ratio_{key}"] = {"median": round(statistics.median(ratios), 3), "min": round(ratios[0], 3),
                                       "max": round(ratios[-1], 3)}  # fmt: skip
    return report


@pytest.mark.timeout(1800)  # ~15 s per pair live (two ~300-token Sol answers)
async def test_l8_pinned_turns(api: Api, live: Live, ledger: Ledger) -> None:
    """L8 (gate L6, spec-10 §6): pairs of one direct `Adapter.stream()` (sol-6.1, effort low, 512 tokens) and one pinned
    turn (the smoke router: 1 commit-critical question, Luna; pinned sol-fast, conditioning off: exactly 2 calls) on
    the same prompt, interleaved, the order alternating per pair. As many pairs as the budget allows, at most
    `NOEMA_LIVE_L8_PAIRS`; the report is rewritten after every pair."""
    pairs = min(L8_PAIRS, ledger.remaining() // CALLS_PER_PAIR)
    ledger.need(CALLS_PER_PAIR * max(pairs, 1))
    version = await activate_live_config(api.state, api.tmp, api.account, smoke_router=True)
    await _ready(api, version)
    rows: list[Json] = []
    for i, topic in enumerate(TOPICS[:pairs]):
        text = f"In about 300 tokens, explain {topic} to a new engineer."
        row: Json = {"pair": i + 1, "order": "direct-first" if i % 2 == 0 else "pinned-first"}  # no prompt text
        for leg in ("direct", "pinned") if i % 2 == 0 else ("pinned", "direct"):
            seen = len(ledger.calls)
            if leg == "direct":
                request = req("sol-6.1", TextBlock(f"{text} {RUN_TAG}"), effort="low", max_tokens=512)
                stream_events, row["direct"] = await stream_all(live, request)
                assert_well_formed(stream_events)
            else:
                before = ledger.count
                body = {"input": user(text), "conditioning": "off", "stream": True,
                        "generation": {"profile": "sol-fast", "effort": "low", "max_output_tokens": 512}}  # fmt: skip
                events, row["pinned"] = await api.client.sse("/v1/turns", body)
                assert events[-1].event == "turn.completed" and ledger.count - before == 2
            generation = [c for c in calls_since(ledger, seen) if c["call_kind"] == "generation"]
            row[f"{leg}_output_tokens"] = [(c.get("usage") or {}).get("output_tokens") for c in generation]
        rows.append(row)
        ledger.write_report("e2e-pairs.json", _pairs_report(rows))
    assert api.app.state.contract_violations == []
