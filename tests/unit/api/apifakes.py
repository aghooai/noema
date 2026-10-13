"""Minimal in-memory services for the API tests (the shared tests/fakes package belongs to another role; these
cover only what the ingress middleware and the stubs touch)."""

from __future__ import annotations

import functools
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import anyio.lowlevel
import httpx
import orjson
import yaml
from asgi_lifespan import LifespanManager
from fastapi import APIRouter, FastAPI, Request
from starlette.responses import Response

from noema.api import deps
from noema.api.app import create_app
from noema.api.sse_frame import encode_frame
from noema.config.loader import load_config, load_config_file
from noema.config.registry import ConfigSnapshotImpl, InMemoryConfigRegistry
from noema.contracts.bundle import VENDOR_DIR, RawOperation, default_bundle
from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import UlidIdGen
from noema.core.principal import ALL_SCOPES, Principal
from noema.core.protocols.events import SseFrame
from noema.core.protocols.storage import Conflict, IdemToken, InFlight, New, Replay, StoredResponse
from noema.core.values import JsonValue

EXAMPLE_CONFIG = VENDOR_DIR / "examples" / "platform.example.yaml"
DEV_KEY = "ak_test_01J0000000000000000000DEV0." + "d" * 43
ADMIN_KEY = "ak_test_01J000000000000000000ADM00." + "a" * 43


@functools.cache
def example_snapshot() -> ConfigSnapshotImpl:
    """The vendored example config, loaded once per test process."""
    return load_config_file(EXAMPLE_CONFIG, env="test")


class ManualClock:
    """A Clock whose time moves only through `advance()`; `sleep()` waits until then."""

    def __init__(self, start: float = 1000.0) -> None:
        self._start = start
        self._mono = start
        self._sleepers: list[tuple[float, anyio.Event]] = []

    def now(self) -> datetime:
        return datetime(2026, 10, 10, tzinfo=UTC) + timedelta(seconds=self._mono - self._start)

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await anyio.lowlevel.checkpoint()
            return
        entry = (self._mono + seconds, anyio.Event())
        self._sleepers.append(entry)
        try:
            await entry[1].wait()
        finally:
            if entry in self._sleepers:  # cancelled before its time
                self._sleepers.remove(entry)

    @property
    def sleepers(self) -> int:
        return len(self._sleepers)

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        due = [s for s in self._sleepers if s[0] <= self._mono]
        self._sleepers = [s for s in self._sleepers if s[0] > self._mono]
        for _, event in due:
            event.set()


def principal(key: str, *, roles: frozenset[Any] = frozenset({"developer"}), project: str = "prj_1") -> Principal:
    return Principal(
        tenant_id="t_acme",
        project_id=project,
        principal_id=f"prn_{hashlib.sha256(key.encode()).hexdigest()[:8]}",
        key_id=key.split(".")[0][8:],
        roles=roles,
        scopes=ALL_SCOPES,
        request_id="",
    )


class FakeAuthenticator:
    """Accepts `Authorization: Bearer <key>` for the known keys; records every call."""

    def __init__(self, principals: Mapping[str, Principal] | None = None) -> None:
        self.principals = dict(principals or {DEV_KEY: principal(DEV_KEY)})
        self.calls: list[tuple[dict[str, str], str]] = []
        self.error: NoemaError | None = None  # raise this instead (e.g. 429 rpm_exceeded)

    async def authenticate(self, headers: Mapping[str, str], client_ip: str) -> Principal:
        self.calls.append((dict(headers), client_ip))
        if self.error is not None:
            raise self.error
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or token not in self.principals:
            raise NoemaError(ErrorCode.INVALID_API_KEY, "Invalid API key.")
        return self.principals[token]


@dataclass
class _Record:
    token: IdemToken
    fingerprint: str
    resource_id: str | None
    state: str = "in_flight"  # in_flight | completed | not_stored
    response: StoredResponse | None = None
    done: anyio.Event | None = None


class FakeIdempotencyStore:
    """The spec-11 §5 state machine in memory (no TTLs or leases)."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str, str], _Record] = {}
        self.begins: list[dict[str, Any]] = []
        self.completes: list[tuple[IdemToken, StoredResponse | None]] = []
        self.heartbeats = 0
        self.heartbeat_result = True

    async def begin(
        self,
        principal: Principal,
        method: str,
        route: str,
        key: str,
        fingerprint: str,
        *,
        resource_id: str | None = None,
    ) -> New | InFlight | Replay | Conflict:
        self.begins.append({"method": method, "route": route, "key": key, "fp": fingerprint, "rid": resource_id})
        scope_hash = hashlib.sha256(f"{principal.project_id}\n{method}\n{route}".encode()).hexdigest()
        k = (principal.tenant_id, principal.project_id, scope_hash, key)
        rec = self.records.get(k)
        if rec is not None and rec.fingerprint != fingerprint:
            return Conflict()
        if rec is not None and rec.state == "completed":
            assert rec.response is not None
            return Replay(rec.response)
        if rec is not None and rec.state == "in_flight":
            return InFlight(rec.token, rec.resource_id)
        token = IdemToken(scope_hash, key, f"idem_{len(self.records) + 1}")
        self.records[k] = _Record(token, fingerprint, resource_id, done=anyio.Event())
        return New(token)

    def _by_token(self, token: IdemToken) -> _Record:
        return next(r for r in self.records.values() if r.token == token)

    async def await_result(self, token: IdemToken, *, deadline: Deadline) -> StoredResponse:
        rec = self._by_token(token)
        assert rec.done is not None
        await rec.done.wait()
        if rec.state != "completed" or rec.response is None:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "The original request was not stored.")
        return rec.response

    async def heartbeat(self, token: IdemToken) -> bool:
        self.heartbeats += 1
        return self.heartbeat_result

    async def complete(self, token: IdemToken, resp: StoredResponse | None) -> None:
        self.completes.append((token, resp))
        rec = self._by_token(token)
        rec.state, rec.response = ("completed", resp) if resp is not None else ("not_stored", None)
        if rec.done is not None:
            rec.done.set()


class FakeEventLog:
    """Local logs of complete frame lists; `remote` holds the mirrors of turns running elsewhere."""

    def __init__(self) -> None:
        self.local: dict[str, list[SseFrame]] = {}
        self.remote: dict[str, list[SseFrame]] = {}

    def append(self, turn_id: str, event: Mapping[str, JsonValue]) -> int:
        frames = self.local.setdefault(turn_id, [])
        frames.append(encode_frame(len(frames) + 1, event))
        return len(frames)

    async def _iterate(self, frames: list[SseFrame], after: int) -> AsyncIterator[SseFrame]:
        for f in frames:
            if f.id > after:
                yield f

    def subscribe(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame]:
        return self._iterate(self.local.get(turn_id, []), after)

    def is_local(self, turn_id: str) -> bool:
        return turn_id in self.local

    def seal(self, turn_id: str, ttl_s: int) -> None:
        return None

    async def attach_remote(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame] | None:
        frames = self.remote.get(turn_id)
        return None if frames is None else self._iterate(frames, after)

    async def idle_seconds(self, turn_id: str) -> float | None:
        return None


class FakeSseWriter:
    """Writes every frame as is (no backpressure)."""

    async def run(self, frames: AsyncIterator[SseFrame], send: Callable[[bytes], Awaitable[None]], **_: Any) -> str:
        async for frame in frames:
            await send(frame.wire)
        return "completed"


@dataclass
class FakeServices:
    """The subset of composition.Services the API layer touches."""

    clock: ManualClock = field(default_factory=ManualClock)
    authn: FakeAuthenticator = field(default_factory=FakeAuthenticator)
    idempotency: FakeIdempotencyStore = field(default_factory=FakeIdempotencyStore)
    events: FakeEventLog = field(default_factory=FakeEventLog)
    sse: FakeSseWriter = field(default_factory=FakeSseWriter)
    config: InMemoryConfigRegistry = field(default_factory=lambda: InMemoryConfigRegistry(example_snapshot()))
    ids: UlidIdGen = field(init=False)

    def __post_init__(self) -> None:
        self.ids = UlidIdGen(self.clock, randbytes=lambda n: bytes(n))


def _stub_handler(operation_id: str) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        if operation_id == "healthz":
            return Response(status_code=200)
        if operation_id == "readyz":
            return Response(status_code=503)
        return deps.not_implemented(request)

    return handler


@functools.cache
def _stub_router() -> APIRouter:
    router = APIRouter()
    for op in default_bundle().operations():
        router.add_api_route(op.path, _stub_handler(op.operation_id), methods=[op.method], name=op.operation_id)
    return router


def stub_routers() -> list[APIRouter]:
    """Test-only stubs on every bundle path (healthz 200, readyz 503, every other operation the typed 501), so the
    middleware tests never run the route modules the C-wave owners fill in."""
    return [_stub_router()]


def make_app(
    services: FakeServices | None = None, *, routers: list[APIRouter] | None = None, **kwargs: Any
) -> tuple[FastAPI, FakeServices]:
    """`create_app` over fake services with the response validator on (unless `validate=False`); `routers`
    defaults to `stub_routers()`."""
    services = services or FakeServices()
    kwargs.setdefault("validate", True)
    kwargs.setdefault("trusted_proxies", ())
    mounted = stub_routers() if routers is None else routers
    return create_app(services=services, routers=mounted, **kwargs), services  # type: ignore[arg-type]


@asynccontextmanager
async def asgi_client(
    app: FastAPI, *, client: tuple[str, int] = ("203.0.113.7", 50000), check_contract: bool = True
) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client over the app in-process, with the lifespan run. At exit, asserts that the test-mode
    validator recorded no contract violation."""
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
    if check_contract:
        assert app.state.contract_violations == []


def auth(key: str = DEV_KEY, **extra: str) -> dict[str, str]:
    """Request headers with a bearer key."""
    return {"authorization": f"Bearer {key}", **extra}


_EXAMPLES = VENDOR_DIR / "examples"
PATH_VALUES = {
    "turn_id": "turn_01J00000000000000000000000",
    "decision_id": "dec_01J00000000000000000000000",
    "session_id": "ses_01J00000000000000000000000",
    "file_id": "file_01J00000000000000000000000",
    "kind": "text",
}
TURN_BODY: dict[str, Any] = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}]}


def valid_body(operation_id: str) -> tuple[str | None, bytes]:
    """(Content-Type, body) of a schema-valid request for each operation (no body for the others)."""
    if operation_id == "createTurn":
        return "application/json", orjson.dumps(TURN_BODY)
    if operation_id == "createDecision":
        return "application/json", (_EXAMPLES / "decision-request.valid.json").read_bytes()
    if operation_id == "createDecisionOpenAICompat":
        return "application/json", (_EXAMPLES / "openai-request.valid.json").read_bytes()
    if operation_id == "createSession":
        return "application/json", b"{}"
    if operation_id == "updateSession":
        return "application/json", b'{"metadata": {}}'
    if operation_id == "uploadFile":
        boundary = "noematestboundary"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\ninput\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\nhello\r\n--{boundary}--\r\n"
        ).encode()
        return f"multipart/form-data; boundary={boundary}", body
    return None, b""


def request_for(op: RawOperation) -> tuple[str, str, dict[str, str], bytes]:
    """(method, url, headers, body) of a valid authenticated request to `op`."""
    url = op.path
    for name, value in PATH_VALUES.items():
        url = url.replace("{" + name + "}", value)
    ctype, body = valid_body(op.operation_id)
    headers = auth() if op.authenticated else {}
    if ctype is not None:
        headers["content-type"] = ctype
    return op.method, url, headers, body


def snapshot_with(**platform_limits: Any) -> ConfigSnapshotImpl:
    """The example config with `platform.limits` overrides (e.g. `max_json_body_bytes=1048576`)."""
    cfg = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    cfg.setdefault("platform", {}).setdefault("limits", {}).update(platform_limits)
    return load_config(cfg, env="test")


def services_with(snapshot: ConfigSnapshotImpl) -> FakeServices:
    return FakeServices(config=InMemoryConfigRegistry(snapshot))
