"""Gate Q2 with the official `openai` npm client (unmodified, the root node_modules) against the stack's facade:
the same cases as test_facade_python.py and test_facade_retries.py, run by facade_conformance.mjs in one node
process per group; every response the client saw is contract-validated here. Pass criterion: every case (100%).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import anyio
import httpx
import orjson
import pytest
from tests.e2e.conftest import REPO, Mock, Stack
from tests.e2e.suite.kit import admin
from tests.e2e.suite.multipod import mint_key, patched_config
from tests.qa.contract.facadekit import ALL_DOWN, DOWN, example, facade, scenario, unique

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
]
SCRIPT = REPO / "tests" / "qa" / "contract" / "facade_conformance.mjs"
Q: Any = [{"type": "predicate", "name": "flag", "instructions": "Is this a regression of the export?"}]
MIXED: Any = [*Q, {"type": "predicate", "name": "gone", "instructions": f"{DOWN}?"}]
ENSEMBLE = {"x-aghoo-include": "ensemble"}
BAD_KEY = "ak_test_" + "x" * 40


def cases() -> list[dict[str, Any]]:
    simple = {"model": "aghoo/router-default", "input": "Export fails.", "questions": Q}
    return [
        {"name": "valid", "body": unique(example("openai-request.valid.json"))},
        {
            "name": "non_pattern",
            "body": unique(example("openai-request.valid-non-pattern-name.json")),
            "headers": ENSEMBLE,
        },
        {"name": "ensemble", "body": unique(example("openai-request.valid.json")), "headers": ENSEMBLE},
        {"name": "refusal", "body": unique({**simple, "questions": MIXED})},
        {"name": "refusal_ensemble", "body": unique({**simple, "questions": MIXED}), "headers": ENSEMBLE},
        {"name": "lightweight", "body": unique({**simple, "model": "aghoo/lightweight-model-decide"})},
        {"name": "model_not_available", "body": {**simple, "model": "router-default"}},
        {"name": "profile_not_allowed", "body": {**simple, "model": "aghoo/answer-verifier"}},
        {"name": "remote_image_url", "body": example("openai-request.invalid-remote-image-url.json")},
        {"name": "invalid_key", "body": simple, "key": BAD_KEY},
        {"name": "retry_503", "body": unique({**simple, "input": f"{ALL_DOWN}: export fails."}), "maxRetries": 2},
    ]


def answers(name: str) -> Any:
    return example(name)["answers"]


EXPECT: dict[str, Any] = {
    "valid": lambda r: r["ok"]["answers"] == answers("openai-response.valid.json") and "x_aghoo" not in r["ok"],
    "non_pattern": lambda r: (
        r["ok"]["answers"] == answers("openai-response.valid-non-pattern-name.json")
        and set(r["ok"]["x_aghoo"]["calibration"]) == {"Severity Level", "regression"}
    ),
    "ensemble": lambda r: (
        r["ok"]["x_aghoo"]["decision_id"].startswith("dec_")
        and set(r["ok"]["x_aghoo"]["calibration"]) == {"q0", "regression"}
    ),
    "refusal": lambda r: r["ok"]["answers"][1] == {"type": "refusal", "name": "gone"} and "x_aghoo" not in r["ok"],
    "refusal_ensemble": lambda r: (
        r["ok"]["answers"][1] == {"type": "refusal", "name": "gone"}
        and set(r["ok"]["x_aghoo"]["mapped_unavailable"]) == {"gone"}
    ),
    "lightweight": lambda r: r["ok"]["model"] == "aghoo/lightweight-model-decide@1",
    "model_not_available": lambda r: (
        r["error"] == {"name": "BadRequestError", "status": 400, "code": "model_not_available"}
    ),
    "profile_not_allowed": lambda r: (
        r["error"] == {"name": "PermissionDeniedError", "status": 403, "code": "profile_not_allowed"}
    ),
    "remote_image_url": lambda r: r["error"] == {"name": "BadRequestError", "status": 400, "code": "schema_violation"},
    "invalid_key": lambda r: r["error"] == {"name": "AuthenticationError", "status": 401, "code": "invalid_api_key"},
    "retry_503": lambda r: (
        r["error"] == {"name": "InternalServerError", "status": 503, "code": "all_routes_unavailable"}
    ),
}
ATTEMPTS = {"retry_503": [503, 503, 503], "retry_429": [429, 200]}
_RUNS: dict[str, Any] = {}


async def run_node(stack: Stack, group: list[dict[str, Any]], key: str | None = None) -> Any:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "NOEMA_QA_FACADE": f"{stack.apis[1]}/openai/v1",
        "NOEMA_QA_KEY": key or str(stack.principal()["key"]),
        "NOEMA_QA_CASES": orjson.dumps(group).decode(),
    }
    with anyio.fail_after(120):
        done = await anyio.run_process(["node", str(SCRIPT)], env=env, cwd=REPO, check=False)
    assert done.returncode == 0, done.stderr.decode()[-2000:]
    report = orjson.loads(done.stdout)
    checker = stack.client(1, role=None)
    for seen in report["seen"]:  # every response the npm client received, against the contract
        match = checker.table.match("POST", httpx.URL(seen["url"]).path)
        assert match is not None, seen["url"]
        request_json = orjson.loads(seen["request"]) if seen["request"] else None
        checker.validator.check_response(
            match.operation.operation_id, seen["status"], seen["headers"], seen["body"].encode(), request_json
        )
    return report


def statuses(report: Any, name: str) -> list[int]:
    return [int(s["status"]) for s in report["seen"] if s["case"] == name]


@pytest.mark.parametrize("name", list(EXPECT))
async def test_npm_client_case(stack: Stack, default_scenario: None, mock: Mock, name: str) -> None:
    if "report" not in _RUNS:
        await mock.load(scenario())
        _RUNS["report"] = await run_node(stack, cases())
    report = _RUNS["report"]
    assert EXPECT[name](report["results"][name]), report["results"][name]
    assert statuses(report, name) == ATTEMPTS.get(name, statuses(report, name)[:1])  # no retry on 2xx/4xx


async def test_npm_client_retries_429_after_retry_after(stack: Stack, default_scenario: None, tmp_path: Path) -> None:
    minted = await mint_key(stack, "t_beta", "main", "developer2")

    def limit(doc: dict[str, Any]) -> None:
        doc["tenants"]["t_beta"]["limits"] = {"rpm_per_key": 30}

    async with patched_config(stack, tmp_path, limit):
        drain = facade(stack, key=str(minted["key"]))
        with anyio.fail_after(60):
            while not drain.seen or drain.seen[-1][0] != 429:
                body = unique({"model": "aghoo/router-default", "input": "Export fails.", "questions": Q})
                try:
                    await drain.client.decisions.create(**body)
                except Exception as exc:  # openai.RateLimitError ends the loop through `seen`
                    assert getattr(exc, "status_code", None) == 429, exc
        body = unique({"model": "aghoo/router-default", "input": "Export fails.", "questions": Q})
        report = await run_node(stack, [{"name": "retry_429", "body": body, "maxRetries": 2}], key=str(minted["key"]))
        assert report["results"]["retry_429"]["ok"]["answers"][0]["type"] == "predicate"
        assert statuses(report, "retry_429") == ATTEMPTS["retry_429"]
    await admin(stack, ["keys", "revoke", str(minted["key_id"])])
