"""The OpenAI-compatible facade (`POST /openai/v1/decisions`) through the official, unmodified `openai` clients
(Python and the npm package) pointed at the stack: `base_url = <api>/openai/v1`. This is not an OpenAI API call:
the SDKs talk to Noema, which answers with Bedrock decision profiles (the wire mock here).

Every response the SDKs receive is contract-validated: the Python client through an httpx response hook, the TS
client by recording each response in its `fetch` (tests/e2e/suite/facade_openai.mjs) and validating it here.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, cast

import anyio
import httpx
import openai
import orjson
import pytest
from tests.e2e.conftest import REPO, CheckedClient, Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, tag

pytestmark = [pytest.mark.e2e]

SEVERITY = {"Cosmetic": 0.1, "Workaround available": 0.7, "Fully blocked": 0.2}
DOWN = "Is the warehouse robot offline"
TS_CLIENT = REPO / "tests" / "e2e" / "suite" / "facade_openai.mjs"
EXAMPLES = REPO / "src" / "noema" / "contracts" / "_vendor" / "examples"


def example(name: str) -> Json:
    loaded: Json = orjson.loads((EXAMPLES / name).read_bytes())
    return loaded


def facade_scenario() -> dict[str, Any]:
    return sc.document(
        "e2e-facade",
        [
            sc.answer({"prompt_contains": "How severe"}, SEVERITY, 50),
            sc.answer({"prompt_contains": "regression"}, {"true": 0.72, "false": 0.28}, 50),
            sc.fault({"prompt_contains": DOWN}, "http_5xx", 20, status=500),
            sc.answer({}, sc.LOW, 50),
        ],
    )


def request_body() -> Json:
    """openai-request.valid.json with a unique message (no L2 hit)."""
    body = example("openai-request.valid.json")
    body["input"][0]["content"] = f"{body['input'][0]['content']} ({tag()})"
    return body


def sdk(stack: Stack, replica: int = 0, role: str = "developer") -> tuple[openai.AsyncOpenAI, CheckedClient]:
    checker = stack.client(replica, role=None)

    async def validate(response: httpx.Response) -> None:
        await response.aread()
        sent = response.request.content
        checker.check(response, orjson.loads(sent) if sent else None)

    http = httpx.AsyncClient(event_hooks={"response": [validate]}, timeout=60.0)
    stack.clients.append(http)
    key = str(stack.principal(role=role)["key"])
    base_url = f"{stack.apis[replica]}/openai/v1"  # the facade on Noema (never OpenAI)
    client = openai.AsyncOpenAI(api_key=key, base_url=base_url, http_client=cast(Any, http))
    return client, checker


async def test_python_openai_client(stack: Stack, mock: Mock) -> None:
    await mock.load(facade_scenario())
    client, checker = sdk(stack, 0)
    body = request_body()
    decision = await client.decisions.create(**body)
    assert decision.model == "aghoo/router-default@1"
    assert [a.model_dump() for a in decision.answers] == example("openai-response.valid.json")["answers"]
    assert decision.usage.total_tokens == decision.usage.input_tokens + decision.usage.output_tokens
    assert "x_aghoo" not in (decision.model_extra or {})

    other, _ = sdk(stack, 1)
    ens = await other.decisions.create(**request_body(), extra_headers={"x-aghoo-include": "ensemble"})
    x_aghoo = (ens.model_extra or {})["x_aghoo"]
    assert set(x_aghoo["calibration"]) == {"q0", "regression"} and x_aghoo["decision_id"].startswith("dec_")
    stored = await stack.client(0).request("GET", f"/v1/decisions/{x_aghoo['decision_id']}", expect=200)
    assert stored.json()["provenance"]["profile_id"] == "router-default"

    mixed: Any = [
        {"type": "predicate", "name": "flag", "instructions": "Is this a regression of the export?"},
        {"type": "predicate", "name": "gone", "instructions": f"{DOWN} ({tag()})?"},
    ]
    refused = await client.decisions.create(model="aghoo/router-default", input="Export fails.", questions=mixed)
    assert refused.answers[0].type == "predicate" and refused.answers[1].model_dump() == {
        "type": "refusal",
        "name": "gone",
    }

    with pytest.raises(openai.BadRequestError) as bad:
        await client.decisions.create(model="router-default", input="x", questions=mixed[:1])
    assert (bad.value.status_code, bad.value.code) == (400, "model_not_available")
    with pytest.raises(openai.PermissionDeniedError) as denied:
        await client.decisions.create(model="aghoo/answer-verifier", input="x", questions=mixed[:1])
    assert (denied.value.status_code, denied.value.code) == (403, "profile_not_allowed")
    statuses = [status for op, status in checker.checked if op == "createDecisionOpenAICompat"]
    assert statuses == [200, 200, 400, 403]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_typescript_openai_client(stack: Stack, mock: Mock) -> None:
    await mock.load(facade_scenario())
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "NOEMA_E2E_FACADE": f"{stack.apis[1]}/openai/v1",
        "NOEMA_E2E_KEY": str(stack.principal()["key"]),
        "NOEMA_E2E_BODY": orjson.dumps(request_body()).decode(),
        "NOEMA_E2E_BODY2": orjson.dumps(request_body()).decode(),
    }
    with anyio.fail_after(60):
        done = await anyio.run_process(["node", str(TS_CLIENT)], env=env, cwd=REPO, check=False)
    assert done.returncode == 0, done.stderr.decode()[-2000:]
    report = orjson.loads(done.stdout)
    checker = stack.client(1, role=None)
    for seen in report["seen"]:
        match = checker.table.match("POST", httpx.URL(seen["url"]).path)
        assert match is not None, seen["url"]
        request_json = orjson.loads(seen["request"]) if seen["request"] else None
        status, headers, raw = seen["status"], seen["headers"], seen["body"].encode()
        checker.validator.check_response(match.operation.operation_id, status, headers, raw, request_json)
    assert [s["status"] for s in report["seen"]] == [200, 200, 400]
    out = report["out"]
    assert out["plain"]["answers"] == example("openai-response.valid.json")["answers"]
    assert "x_aghoo" not in out["plain"] and out["ensemble"]["x_aghoo"]["decision_id"].startswith("dec_")
    assert out["error"] == {"name": "BadRequestError", "status": 400, "code": "model_not_available"}
