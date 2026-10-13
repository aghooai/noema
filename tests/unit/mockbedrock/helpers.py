"""Shared helpers for the wire-mock tests: real clients (aiobotocore, httpx, openai) pointed at an in-process mock."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final, cast

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from aws_bedrock_token_generator import BedrockTokenGenerator
from botocore.credentials import Credentials
from tools.mockbedrock import VirtualClock
from types_aiobotocore_bedrock_runtime import BedrockRuntimeClient
from types_aiobotocore_bedrock_runtime.type_defs import MessageTypeDef

AKID: Final = "ASIAMOCKTENANTA00001"
AKID_B: Final = "ASIAMOCKTENANTB00002"
SECRET: Final = "mock-secret-access-key"
TOKEN: Final = "mock-session-token"
HAIKU: Final = "us.anthropic.claude-haiku-5-5"
OPUS: Final = "us.anthropic.claude-opus-5-5"
SONNET: Final = "us.anthropic.claude-sonnet-5-5"
LUNA: Final = "us.openai.gpt-6-luna"
SOL: Final = "us.openai.gpt-6.1-sol"
GLM: Final = "us.zai.glm-5.3"
DEEPSEEK: Final = "deepseek.v3.2"


def messages(*items: Mapping[str, Any]) -> list[MessageTypeDef]:
    """Converse messages from plain dicts (typed for the aiobotocore stubs)."""
    return cast("list[MessageTypeDef]", [dict(m) for m in items])


def user(*blocks: Mapping[str, Any]) -> list[MessageTypeDef]:
    """One user message with the given content blocks."""
    return messages({"role": "user", "content": [dict(b) for b in blocks]})


USER_HI: Final = user({"text": "hi"})


def scenario(*rules: Mapping[str, Any], seed: int = 7, fixed_ms: float | None = 0.0, **extra: Any) -> dict[str, Any]:
    """A scenario document; `fixed_ms=None` keeps the probe latency defaults."""
    doc: dict[str, Any] = {"scenario": "unit", "seed": seed, "rules": [dict(r) for r in rules], **extra}
    if fixed_ms is not None:
        doc["defaults"] = {"latency": {"fixed_ms": fixed_ms}}
    return doc


def app_question(values: Sequence[str], instructions: str = "Which intent?") -> str:
    """A decision prompt fragment with option letters A, B, ... (spec-06 §5.2 rendering)."""
    options = [{"letter": chr(65 + i), "value": v, "description": None} for i, v in enumerate(values)]
    body = json.dumps({"instructions": instructions, "options": options}, separators=(", ", ": "))
    return f"<app_question>{body}</app_question>\n<untrusted_input>\nhello\n</untrusted_input>\nReply."


@asynccontextmanager
async def bedrock_client(
    endpoint_url: str, *, akid: str = AKID, session_token: str | None = TOKEN, read_timeout: float = 5.0
) -> AsyncIterator[BedrockRuntimeClient]:
    """A real aiobotocore bedrock-runtime client with static STS-like credentials and no retries."""
    config = AioConfig(retries={"total_max_attempts": 1}, read_timeout=read_timeout, connect_timeout=2)
    async with get_session().create_client(
        "bedrock-runtime",
        region_name="us-east-1",
        endpoint_url=endpoint_url,
        aws_access_key_id=akid,
        aws_secret_access_key=SECRET,
        aws_session_token=session_token,
        config=config,
    ) as client:
        yield client


def short_term_token(akid: str = AKID, session_token: str | None = TOKEN) -> str:
    """A Mantle short-term bearer token (aws-bedrock-token-generator, offline presign)."""
    token: str = BedrockTokenGenerator().get_token(Credentials(akid, SECRET, session_token), "us-east-1")
    return token


def sigv4_headers(akid: str = AKID, *, session_token: bool = True) -> dict[str, str]:
    """Headers that look like a SigV4-signed request (the mock records but never verifies signatures)."""
    headers = {
        "authorization": f"AWS4-HMAC-SHA256 Credential={akid}/20261010/us-east-1/bedrock/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=" + "0" * 64,
        "x-amz-date": "20261010T000000Z",
    }
    if session_token:
        headers["x-amz-security-token"] = TOKEN
    return headers


def bearer_headers(akid: str = AKID) -> dict[str, str]:
    return {"authorization": "Bearer " + short_term_token(akid)}


def sigv4_scoped(region: str, service: str, akid: str = AKID) -> dict[str, str]:
    """SigV4-looking headers with the given credential scope (and a session token)."""
    headers = sigv4_headers(akid)
    headers["authorization"] = headers["authorization"].replace("/us-east-1/bedrock/", f"/{region}/{service}/")
    return headers


async def until(predicate: Callable[[], bool], what: str) -> None:
    """Wait (bounded, about 5 s) until the in-process mock reaches a state; fails the test otherwise."""
    for _ in range(5000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"timed out waiting for {what}")


class GatedClock(VirtualClock):
    """A VirtualClock whose sleeps past `gate_ns` block until `opened` is set, so a stream can be held mid-way."""

    def __init__(self) -> None:
        super().__init__()
        self.gate_ns: int | None = None
        self.opened = asyncio.Event()

    async def sleep_until_ns(self, t_ns: int) -> None:
        if self.gate_ns is not None and t_ns > self.gate_ns:
            await self.opened.wait()
        await super().sleep_until_ns(t_ns)
