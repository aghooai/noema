"""Stored response bodies over 1 MiB in object storage (IMPL-085, spec-03 §2 S3 layout, spec-09 §6.7).

Key `<tenant_id>/idempotency/<record_id>` (`noema.db.repos.idempotency_body_key`) in the environment bucket, encrypted
with the tenant's KMS key in staging/prod (`noema.core.objects.sse_args`). The object lives exactly as long as its
record: the retention sweeper deletes it with the row, a take-over deletes the previous owner's object, and the new
owner's `complete` overwrites the same key.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from noema.core.objects import sse_args
from noema.db.repos import idempotency_body_key

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

log: Final = logging.getLogger("noema.ops.idempotency")
PUT_ATTEMPTS: Final = 2  # one retry (IMPL-085)


class BodyStore:
    """put/get/delete of large idempotent response bodies (see the module docstring)."""

    __slots__ = ("_bucket", "_env", "_s3")

    def __init__(self, s3: S3Client, *, bucket: str, env: str) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._env = env

    @staticmethod
    def key(tenant_id: str, record_id: str) -> str:
        """`<tenant_id>/idempotency/<record_id>`."""
        return idempotency_body_key(tenant_id, record_id)

    async def put(self, tenant_id: str, record_id: str, body: bytes) -> bool:
        """PutObject with one retry; False when both attempts failed (the caller stores not_stored)."""
        key = self.key(tenant_id, record_id)
        for attempt in range(1, PUT_ATTEMPTS + 1):
            try:
                await self._s3.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/octet-stream",
                    **sse_args(self._env, tenant_id),  # type: ignore[arg-type]
                )
                return True
            except Exception as exc:
                log.warning("idempotency body put failed (attempt %d, %s)", attempt, type(exc).__name__)
        return False

    async def get(self, key: str) -> bytes:
        """GetObject of a stored key (any failure propagates; the caller answers 500, retryable)."""
        response = await self._s3.get_object(Bucket=self._bucket, Key=key)
        async with response["Body"] as stream:
            return await stream.read()

    async def delete(self, tenant_id: str, record_id: str) -> None:
        """DeleteObject of a record's key (a no-op when absent; failures propagate)."""
        await self._s3.delete_object(Bucket=self._bucket, Key=self.key(tenant_id, record_id))
