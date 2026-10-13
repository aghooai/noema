"""The reference for the lean Converse path's equivalence tests: a real aiobotocore `bedrock-runtime` client (the
machinery `ConverseAdapter.complete` used before E sprint A) whose `before-send` hook captures the signed request and
answers with a scripted raw response, so its request bytes and its parse can be compared with
`noema.adapters._converse_wire`. Static credentials, no network."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from aiobotocore.awsrequest import AioAWSResponse
from aiobotocore.config import AioConfig
from aiobotocore.session import AioSession

from noema.adapters._converse_wire import ConverseModel
from noema.adapters._unary import RawResponse
from noema.adapters.converse import _isolated_session, _loaded_data_loader
from noema.core.protocols.credentials import AwsCredentials, MantleAuth, TenantCredentials
from noema.core.secret import SecretStr

FROZEN = datetime(2026, 10, 11, 7, 30, 15, tzinfo=UTC).replace(tzinfo=None)
AKID, SECRET, TOKEN = "ASIAEXAMPLEEXAMPLE01", "c2VjcmV0LXNlY3JldC1zZWNyZXQ=", "FwoGZXIvYXdzE" + "x" * 300
_LOADER: Any = None


def loader() -> Any:
    global _LOADER
    if _LOADER is None:
        _LOADER = _loaded_data_loader()
    return _LOADER


def converse_model() -> ConverseModel:
    return ConverseModel(loader())


def creds(region: str = "us-east-1", *, token: bool = True) -> TenantCredentials:
    aws = AwsCredentials(AKID, SecretStr(SECRET), SecretStr(TOKEN) if token else None, None)
    return TenantCredentials("t_acme", "111122223333", region, aws, MantleAuth("sigv4", None, None), None, "ambient")


def freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    """SigV4's clock at FROZEN for both paths (botocore.auth.get_current_datetime, the lean signer's `utcnow`)."""
    monkeypatch.setattr("botocore.auth.get_current_datetime", lambda *a, **k: FROZEN)
    monkeypatch.setattr("noema.adapters._sigv4.utcnow", lambda: FROZEN)


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self) -> bytes:
        return self.data


class _Chunks:
    """An eventstream body for aiobotocore: `content.iter_chunks()` yields the scripted chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks, self.content, self.closed = chunks, self, False

    async def iter_chunks(self) -> Any:
        for chunk in self.chunks:
            yield chunk, True

    def close(self) -> None:
        self.closed = True


@dataclass
class Reference:
    """The client and its captured requests; `answer` is the next raw response."""

    client: Any
    sent: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)
    answer: RawResponse = field(default_factory=lambda: RawResponse(200, {"content-type": "application/json"}, b"{}"))
    stream_chunks: list[bytes] | None = None  # a ConverseStream answer (200, eventstream) instead of `answer`

    async def _send(self, request: Any, **_: Any) -> AioAWSResponse:
        headers = {k: (v.decode() if isinstance(v, bytes) else str(v)) for k, v in request.headers.items()}
        body = request.body if isinstance(request.body, bytes) else bytes(request.body or b"")
        self.sent.append((request.url, headers, body))
        if self.stream_chunks is not None:
            headers = {"content-type": "application/vnd.amazon.eventstream"}
            return AioAWSResponse(request.url, 200, headers, _Chunks(self.stream_chunks))  # type: ignore[arg-type]
        raw = self.answer
        return AioAWSResponse(request.url, raw.status, dict(raw.headers), _Body(raw.body))  # type: ignore[arg-type]

    async def converse(self, params: Mapping[str, Any]) -> Any:
        return await self.client.converse(**params)


@asynccontextmanager
async def reference(
    region: str = "us-east-1", *, endpoint: str | None = None, token: bool = True
) -> AsyncIterator[Reference]:
    """The pre-sprint client config: SigV4 pinned, retries off, isolated session, the shared data loader."""
    session: AioSession = _isolated_session()
    session.register_component("data_loader", loader())
    config = AioConfig(
        connect_timeout=3.0,
        read_timeout=300.0,
        retries={"total_max_attempts": 1, "mode": "standard"},
        signature_version="v4",
        ignore_configured_endpoint_urls=True,
        user_agent_extra="noema",
    )
    ctx = session.create_client(
        "bedrock-runtime",
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=AKID,
        aws_secret_access_key=SECRET,
        aws_session_token=TOKEN if token else None,
        config=config,
    )
    async with ctx as client:
        ref = Reference(client)
        client.meta.events.register("before-send.bedrock-runtime", ref._send)  # type: ignore[arg-type]
        yield ref
