"""Platform aiobotocore clients of identity (STS, Secrets Manager), created lazily so that construction stays sync
(`composition.build`) and closed with `aclose()` (lifespan).

Platform clients run with the pod's workload credentials (the default chain) unless static credentials are given
(tests, moto). Endpoint overrides (`ProcessSettings.endpoint_overrides`, validated dev/test-only by the composition
root, ADR-001 §8) are passed in as `endpoint_url`. Tenant credentials never go through here.
"""

from __future__ import annotations

import contextlib
from typing import Any, Final

import anyio
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session

from noema.core.secret import SecretStr

PLATFORM_REGION: Final = "us-east-1"


class StaticCredentials:
    """Explicit platform credentials (tests and local moto); the default chain is used when absent."""

    __slots__ = ("access_key_id", "secret_access_key", "session_token")

    def __init__(
        self, access_key_id: str, secret_access_key: SecretStr, session_token: SecretStr | None = None
    ) -> None:
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.session_token = session_token


def _preload(session: Any, service: str) -> None:
    """Load the botocore data a client needs (service model, endpoint rules, partitions): synchronous file reads and
    JSON parsing, about 40-50 ms per client, done in a worker thread (creating the client then takes 2-3 ms on the
    loop)."""
    loader = session.get_component("data_loader")
    loader.load_service_model(service, "service-2")
    loader.load_service_model(service, "endpoint-rule-set-1")
    for name in ("endpoints", "partitions", "sdk-default-configuration", "_retry"):
        loader.load_data(name)


class LazyClient:
    """One aiobotocore client, entered on first use under a lock (its botocore data is loaded in a worker thread
    first, so creating it does not block the loop); `aclose()` leaves it (idempotent)."""

    def __init__(
        self,
        service: str,
        *,
        region: str = PLATFORM_REGION,
        endpoint_url: str | None = None,
        credentials: StaticCredentials | None = None,
        config: AioConfig | None = None,
    ) -> None:
        self._service = service
        self._region = region
        self._endpoint_url = endpoint_url
        self._credentials = credentials
        self._config = config
        self._stack = contextlib.AsyncExitStack()
        self._client: Any = None
        self._lock = anyio.Lock()

    async def get(self) -> Any:
        """The entered client."""
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                kwargs: dict[str, Any] = {"region_name": self._region, "config": self._config}
                if self._endpoint_url is not None:
                    kwargs["endpoint_url"] = self._endpoint_url
                creds = self._credentials
                if creds is not None:
                    kwargs["aws_access_key_id"] = creds.access_key_id
                    kwargs["aws_secret_access_key"] = creds.secret_access_key.get_secret_value()
                    if creds.session_token is not None:
                        kwargs["aws_session_token"] = creds.session_token.get_secret_value()
                session: Any = get_session()  # create_client is overloaded per literal service name
                await anyio.to_thread.run_sync(_preload, session, self._service)
                cm = session.create_client(self._service, **kwargs)
                self._client = await self._stack.enter_async_context(cm)
            return self._client

    async def aclose(self) -> None:
        """Close the client (a later `get()` opens a new one)."""
        async with self._lock:
            self._client = None
            await self._stack.aclose()
            self._stack = contextlib.AsyncExitStack()


def client_config(*, connect_timeout_s: float, read_timeout_s: float, max_attempts: int) -> AioConfig:
    """Timeouts and the standard retry mode with `max_attempts` total attempts (1 = no retry)."""
    return AioConfig(
        connect_timeout=connect_timeout_s,
        read_timeout=read_timeout_s,
        retries={"mode": "standard", "max_attempts": max_attempts},
    )
