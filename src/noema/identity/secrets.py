"""`SecretsManagerProvider` (spec-02 §3, spec-09 §4): platform and tenant secrets from AWS Secrets Manager.

References: `aws-sm://<name>` (tenant ExternalIds, `aws-sm://platform/tenants/<t>/external-id`, ADR-001 §3) and the
platform secrets `noema/<name>` (`noema/api-key-pepper`, `noema/input-digest-pepper`, `noema/db-dsn`). `version`
None reads the current value (`AWSCURRENT`); a UUID selects a `VersionId`, any other string a `VersionStage`
(e.g. `AWSPREVIOUS`, read by the API-key authenticator during a pepper rotation). Values are cached in memory for
300 s per (ref, version) and registered with the redactor while cached; they are never written anywhere else.
A missing secret or version raises `core.errors.SecretMissing`, an unreachable backend a plain
`NoemaError(internal_error)` (both 500 `internal_error`; the message never names the value).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from botocore.exceptions import BotoCoreError, ClientError

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError, SecretMissing
from noema.core.secret import SecretStr
from noema.identity.aws import LazyClient, StaticCredentials, client_config

CACHE_TTL_S: Final = 300.0
AWS_SM_PREFIX: Final = "aws-sm://"
PLATFORM_PREFIX: Final = "noema/"
_UUID: Final = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_NAME: Final = re.compile(r"[A-Za-z0-9/_+=.@-]{1,512}")
_MISSING_CODES: Final = frozenset({"ResourceNotFoundException"})


def secret_id(ref: str) -> str:
    """The Secrets Manager SecretId of a reference; NoemaError(internal_error) for an unsupported reference."""
    name = ref[len(AWS_SM_PREFIX) :] if ref.startswith(AWS_SM_PREFIX) else ref
    if not ref.startswith((AWS_SM_PREFIX, PLATFORM_PREFIX)) or not _NAME.fullmatch(name):
        raise NoemaError(ErrorCode.INTERNAL_ERROR, "Unsupported secret reference.")
    return name


@dataclass(frozen=True, slots=True)
class _Cached:
    at: float
    value: bytes
    held: SecretStr  # keeps the value registered with the redactor while it is cached


class SecretsManagerProvider:
    """Implements `SecretsProvider` over one lazily created Secrets Manager client (see the module docstring).
    `clock` drives the cache TTL; `aclose()` closes the client (lifespan)."""

    def __init__(
        self,
        clock: Clock,
        *,
        region: str,
        endpoint_url: str | None = None,
        credentials: StaticCredentials | None = None,
        ttl_s: float = CACHE_TTL_S,
    ) -> None:
        self._clock = clock
        self._ttl_s = ttl_s
        self._cache: dict[tuple[str, str | None], _Cached] = {}
        self.client = LazyClient(
            "secretsmanager",
            region=region,
            endpoint_url=endpoint_url,
            credentials=credentials,
            config=client_config(connect_timeout_s=1.0, read_timeout_s=2.0, max_attempts=2),
        )
        self.backend_reads = 0

    async def get(self, ref: str, *, version: str | None = None) -> bytes:
        """The secret's bytes (SecretString as UTF-8, or SecretBinary)."""
        key = (ref, version)
        now = self._clock.monotonic()
        hit = self._cache.get(key)
        if hit is not None and now - hit.at < self._ttl_s:
            return hit.value
        params: dict[str, str] = {"SecretId": secret_id(ref)}
        if version is None:
            params["VersionStage"] = "AWSCURRENT"  # AWS's default, explicit (moto otherwise ignores label moves)
        else:
            params["VersionId" if _UUID.fullmatch(version) else "VersionStage"] = version
        self.backend_reads += 1
        try:
            client = await self.client.get()
            response = await client.get_secret_value(**params)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in _MISSING_CODES:
                raise SecretMissing() from None
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "A platform secret is unavailable.") from None
        except (BotoCoreError, OSError):
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "The secrets backend is unavailable.") from None
        text = response.get("SecretString")
        value = text.encode() if isinstance(text, str) else bytes(response.get("SecretBinary") or b"")
        if not value:
            raise SecretMissing()
        held = SecretStr(value.decode("utf-8", "replace"), register=True)
        self._cache[key] = _Cached(now, value, held)
        return value

    async def start(self) -> None:
        """Lifespan hook: create the client now (its botocore data loads in a worker thread), not on first use."""
        await self.client.get()

    async def aclose(self) -> None:
        """Close the client and clear the cache."""
        self._cache.clear()
        await self.client.aclose()
