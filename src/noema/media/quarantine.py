"""Object storage of files (spec-03 §2 S3 layout, spec-09 §6.7): originals in quarantine, derivatives per file.

    s3://<bucket>/<tenant>/quarantine/<file_id>                      the original, never served
    s3://<bucket>/<tenant>/derivatives/<file_id>/<kind>              one object per single-file derivative
    s3://<bucket>/<tenant>/derivatives/<file_id>/page_images/page-NNNN.png

The pdf.sanitize facts (active content) live on the `pdf_sanitized` derivative row (`derivative.facts`).

Every put carries the tenant's server-side encryption (`noema.core.objects.sse_args`). Uploads stream with a size cap
and a streaming sha256: one PutObject up to 8 MiB, else a multipart upload that is aborted on any failure (no partial
quarantine object survives, SEC-EDGE-01). No cross-file dedupe: storage is per `file_id`.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import anyio
from botocore.exceptions import ClientError

from noema.core.errors import ErrorCode, NoemaError
from noema.core.objects import sse_args

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

PART_BYTES: Final = 8 * 1024 * 1024
READ_CHUNK: Final = 1024 * 1024
_MISSING: Final = frozenset({"NoSuchKey", "404", "NotFound"})


def quarantine_key(tenant_id: str, file_id: str) -> str:
    """The original's key."""
    return f"{tenant_id}/quarantine/{file_id}"


def derivative_prefix(tenant_id: str, file_id: str) -> str:
    """The prefix of every derivative object of a file (trailing slash)."""
    return f"{tenant_id}/derivatives/{file_id}/"


def derivative_key(tenant_id: str, file_id: str, kind: str, page: int | None = None) -> str:
    """One derivative object (`page`: a page of `page_images`)."""
    base = derivative_prefix(tenant_id, file_id) + kind
    return base if page is None else f"{base}/page-{page:04d}.png"


@dataclass(frozen=True, slots=True)
class Stored:
    """A stored upload: its size and sha256 (hex)."""

    bytes: int
    sha256: str


def _too_large(cap: int) -> NoemaError:
    return NoemaError(
        ErrorCode.FILE_TOO_LARGE, f"The file exceeds {cap} bytes for its type.", details={"max_bytes": cap}
    )


def is_missing(exc: ClientError) -> bool:
    """True for a NoSuchKey / 404 error."""
    return str(exc.response.get("Error", {}).get("Code", "")) in _MISSING


async def _chunks(body: Any) -> AsyncIterator[bytes]:
    async with body:
        if hasattr(body, "iter_chunks"):
            async for chunk in body.iter_chunks(READ_CHUNK):
                yield chunk
        else:
            yield await body.read()


class ObjectStore:
    """The file objects of one bucket (see the module docstring)."""

    def __init__(self, s3: S3Client, *, bucket: str, env: str) -> None:
        self._s3: Any = s3
        self._bucket = bucket
        self._env = env

    async def put(self, key: str, tenant_id: str, data: bytes, content_type: str) -> None:
        """One PutObject with the tenant's encryption."""
        await self._s3.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type, **sse_args(self._env, tenant_id)
        )

    async def write_stream(self, key: str, tenant_id: str, chunks: AsyncIterator[bytes], *, cap: int) -> Stored:
        """Store a byte stream under `key` with a size cap (413 `file_too_large` mid-stream) and a streaming
        sha256. Any failure (the cap, a stream error, cancellation) leaves no object behind."""
        digest = hashlib.sha256()
        size = 0
        buf = bytearray()
        upload_id: str | None = None
        parts: list[dict[str, Any]] = []
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > cap:
                    raise _too_large(cap)
                digest.update(chunk)
                buf.extend(chunk)
                while len(buf) > PART_BYTES:  # more than one part: multipart (the last part may be short)
                    if upload_id is None:
                        upload_id = await self._create_multipart(key, tenant_id)
                    part = bytes(buf[:PART_BYTES])
                    del buf[:PART_BYTES]
                    parts.append(await self._part(key, upload_id, len(parts) + 1, part))
            if upload_id is None:
                await self.put(key, tenant_id, bytes(buf), "application/octet-stream")
            else:
                if buf:
                    parts.append(await self._part(key, upload_id, len(parts) + 1, bytes(buf)))
                await self._s3.complete_multipart_upload(
                    Bucket=self._bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
                )
        except BaseException:
            if upload_id is not None:
                with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
                    await self._s3.abort_multipart_upload(Bucket=self._bucket, Key=key, UploadId=upload_id)
            raise
        return Stored(size, digest.hexdigest())

    async def _create_multipart(self, key: str, tenant_id: str) -> str:
        created = await self._s3.create_multipart_upload(
            Bucket=self._bucket, Key=key, ContentType="application/octet-stream", **sse_args(self._env, tenant_id)
        )
        return str(created["UploadId"])

    async def _part(self, key: str, upload_id: str, number: int, data: bytes) -> dict[str, Any]:
        resp = await self._s3.upload_part(
            Bucket=self._bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=data
        )
        return {"PartNumber": number, "ETag": resp["ETag"]}

    async def get(self, key: str) -> bytes | None:
        """The whole object, or None when it does not exist."""
        try:
            resp = await self._s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if is_missing(exc):
                return None
            raise
        async with resp["Body"] as body:
            data: bytes = await body.read()
        return data

    async def open(self, key: str) -> AsyncIterator[bytes]:
        """Start reading the object (NoemaError `not_found` now when it does not exist) and return its chunks; the
        body is closed when the iterator ends or is closed."""
        try:
            resp = await self._s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if is_missing(exc):
                raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not found.") from None
            raise
        return _chunks(resp["Body"])

    async def download_to(self, key: str, path: Path) -> int:
        """Write the object to `path` (the spool input); returns the byte count. NoemaError `not_found` if absent."""
        size = 0
        with path.open("wb") as fh:
            async for chunk in await self.open(key):
                fh.write(chunk)
                size += len(chunk)
        return size

    async def delete(self, key: str) -> None:
        """Delete one object (absent: no-op)."""
        await self._s3.delete_object(Bucket=self._bucket, Key=key)

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every object under `prefix`; returns how many. A key S3 keeps (a per-key `Errors` entry, e.g.
        AccessDenied) raises RuntimeError, so the job fails and is retried with backoff instead of re-listing the
        same keys forever."""
        removed = 0
        while True:
            listed = await self._s3.list_objects_v2(Bucket=self._bucket, Prefix=prefix)
            keys = [{"Key": o["Key"]} for o in listed.get("Contents", [])]
            if not keys:
                return removed
            resp = await self._s3.delete_objects(Bucket=self._bucket, Delete={"Objects": keys, "Quiet": True})
            errors = resp.get("Errors") or []
            if errors:
                codes = sorted({str(e.get("Code")) for e in errors})
                raise RuntimeError(f"S3 kept {len(errors)} of {len(keys)} objects under {prefix}: {codes}")
            removed += len(keys)
