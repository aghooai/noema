"""Shared pieces of the facade conformance tests (gate Q2): the mock scenario, the request bodies and an official
`openai.AsyncOpenAI` client on the stack's facade whose every response is contract-validated and recorded.

The clients talk to Noema only: `base_url = <api>/openai/v1` on 127.0.0.1 (pytest-socket allows loopback only);
Noema answers from Bedrock decision profiles (here the wire mock). Never the OpenAI API (D1).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, cast

import httpx
import openai
import orjson
from tests.e2e.conftest import REPO, CheckedClient, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, tag

EXAMPLES: Final = REPO / "src" / "noema" / "contracts" / "_vendor" / "examples"
SEVERITY: Final = {"Cosmetic": 0.1, "Workaround available": 0.7, "Fully blocked": 0.2}
DOWN: Final = "Is the warehouse robot offline"  # every member fails on it: that answer is unavailable
ALL_DOWN: Final = "QA-ALL-MEMBERS-DOWN"  # in the input: every member fails every question -> 503


def example(name: str) -> Json:
    loaded: Json = orjson.loads((EXAMPLES / name).read_bytes())
    return loaded


def scenario(*, all_down: bool = True) -> dict[str, Any]:
    rules = [sc.fault({"prompt_contains": ALL_DOWN}, "http_5xx", 20, status=503)] if all_down else []
    return sc.document(
        "qa-facade",
        rules,
        [
            sc.answer({"prompt_contains": "How severe"}, SEVERITY, 50),
            sc.answer({"prompt_contains": "regression"}, {"true": 0.72, "false": 0.28}, 50),
            sc.fault({"prompt_contains": DOWN}, "http_5xx", 20, status=500),
            sc.answer({}, sc.LOW, 50),
        ],
    )


def unique(body: Json) -> Json:
    """The body with a unique marker in its text input (no L2 decision-cache hit across tests)."""
    out: Json = orjson.loads(orjson.dumps(body))
    if isinstance(out["input"], str):
        out["input"] = f"{out['input']} ({tag('qa')})"
    else:
        first = out["input"][0]
        if isinstance(first["content"], str):
            first["content"] = f"{first['content']} ({tag('qa')})"
        else:
            first["content"].append({"type": "input_text", "text": f"({tag('qa')})"})
    return out


@dataclass
class Recorded:
    """The facade client plus every (status, error code) it received, in order."""

    client: openai.AsyncOpenAI
    checker: CheckedClient
    seen: list[tuple[int, str | None]] = field(default_factory=list)
    on_response: Callable[[httpx.Response], Awaitable[None]] | None = None


def facade(stack: Stack, *, replica: int = 0, key: str | None = None, max_retries: int = 0) -> Recorded:
    checker = stack.client(replica, role=None)
    recorded: Recorded

    async def validate(response: httpx.Response) -> None:
        await response.aread()
        sent = response.request.content
        checker.check(response, orjson.loads(sent) if sent else None)
        code = response.json().get("error", {}).get("code") if response.status_code >= 400 else None
        recorded.seen.append((response.status_code, code))
        if recorded.on_response is not None:
            await recorded.on_response(response)

    http = httpx.AsyncClient(event_hooks={"response": [validate]}, timeout=60.0)
    stack.clients.append(http)
    api_key = key or str(stack.principal()["key"])
    base_url = f"{stack.apis[replica]}/openai/v1"  # the facade on Noema (never OpenAI)
    client = openai.AsyncOpenAI(
        api_key=api_key, base_url=base_url, http_client=cast(Any, http), max_retries=max_retries
    )
    recorded = Recorded(client, checker)
    return recorded
