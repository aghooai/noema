"""Facade conformance with the official `openai` Python client (spec-06 §18, spec-10 §3 Q2): parsing, the
`x-aghoo-include: ensemble` mapping, `unavailable` -> `refusal`, and the SDK's retries on 429 and 503. The client
talks only to the local server (`base_url=http://127.0.0.1:<port>/openai/v1`); Noema never calls OpenAI (D1)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import openai
import pytest
from tests.contract_internal.kits import principal
from tests.integration.decision.server import real_clock_world, serve
from tests.unit.decision.app import KEY, make
from tests.unit.decision.world import DEEPSEEK, HAIKU, LUNA, answer

import noema.contracts
from noema.core.errors import ErrorCode, NoemaError

pytestmark = pytest.mark.integration
EXAMPLES = Path(noema.contracts.__file__).parent / "_vendor" / "examples"
SEVERITY = {"Cosmetic": 0.1, "Workaround available": 0.7, "Fully blocked": 0.2}


def example(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return loaded


def rules() -> list[dict[str, Any]]:
    # the first attempt fails in both stages (router-default, then its escalation to router-plus): 503. `nth`
    # counts the requests that reach a rule, so two `nth: 1` rules fail a model's first two calls.
    flaky = [
        {"match": {"model": m, "prompt_contains": "FLAKY", "nth": 1}, "fault": {"kind": "http_5xx"}, "latency_ms": 5}
        for m in (DEEPSEEK, LUNA, HAIKU)
        for _ in range(2)
    ]
    return [
        *flaky,
        answer(None, "FLAKY", {"true": 0.8, "false": 0.2}, 20),
        answer(None, "DOWN", {"true": 0.8, "false": 0.2}, 20),
        answer(None, "How severe", SEVERITY, 20),
        answer(None, "regression", {"true": 0.72, "false": 0.28}, 20),
    ]


class FlakyAdmission:
    """Rejects the first reservation of the `flaky` key with 429 rpm_exceeded (retry-after 1 s)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.rejected = 0

    async def reserve(self, who: Any, graph: Any, **kw: Any) -> Any:
        if who.key_id == "flaky" and not self.rejected:
            self.rejected += 1
            raise NoemaError(ErrorCode.RPM_EXCEEDED, "Rate limit exceeded.", retry_after_s=1)
        return await self._inner.reserve(who, graph, **kw)


async def test_facade_with_the_official_openai_client() -> None:
    ew = real_clock_world()
    ew.use(rules())
    app, svc, _ = make(ew)
    admission = FlakyAdmission(ew.world.admission)
    svc.admission = admission  # type: ignore[attr-defined]
    svc.authn.register("flaky-key", replace(principal(), key_id="flaky"))
    statuses: list[int] = []

    async def record(response: httpx.Response) -> None:
        statuses.append(response.status_code)

    async with serve(app) as base:

        def sdk(key: str) -> openai.AsyncOpenAI:
            http = httpx.AsyncClient(event_hooks={"response": [record]})
            return openai.AsyncOpenAI(
                api_key=key, base_url=f"{base}/openai/v1", http_client=cast(Any, http), max_retries=2
            )

        oa = example("openai-request.valid.json")
        client = sdk(KEY)
        decision = await client.decisions.create(
            model=oa["model"], input=oa["input"], questions=oa["questions"], safety_identifier=oa["safety_identifier"]
        )
        assert decision.model == "aghoo/router-default@1"
        assert [a.model_dump() for a in decision.answers] == example("openai-response.valid.json")["answers"]
        assert decision.usage.total_tokens == decision.usage.input_tokens + decision.usage.output_tokens

        ens = await client.decisions.create(
            model=oa["model"],
            input=oa["input"],
            questions=oa["questions"],
            extra_headers={"x-aghoo-include": "ensemble"},
        )
        x = (ens.model_extra or {})["x_aghoo"]
        assert set(x["calibration"]) == {"q0", "regression"} and x["decision_id"].startswith("dec_")

        named = example("openai-request.valid-non-pattern-name.json")
        nd = await client.decisions.create(
            model=named["model"],
            input=named["input"],
            questions=named["questions"],
            extra_headers={"x-aghoo-include": "ensemble"},
        )
        assert nd.answers[0].name == "Severity Level"
        assert set((nd.model_extra or {})["x_aghoo"]["calibration"]) == {"Severity Level", "regression"}

        flaky_q: Any = [{"type": "predicate", "name": "flag", "instructions": "FLAKY check"}]
        statuses.clear()
        retried = await client.decisions.create(model="aghoo/router-default", input="x", questions=flaky_q)
        assert statuses == [503, 200] and retried.answers[0].type == "predicate"

        statuses.clear()
        throttled = await sdk("flaky-key").decisions.create(
            model="aghoo/router-default", input="x", questions=cast(Any, [{**flaky_q[0], "instructions": "DOWN check"}])
        )
        assert statuses == [429, 200] and throttled.answers[0].type == "predicate" and admission.rejected == 1

        with pytest.raises(openai.BadRequestError) as bad:
            await client.decisions.create(model="router-default", input="x", questions=flaky_q)
        assert bad.value.status_code == 400
