"""`FakeS3Client`: the subset of the aiobotocore S3 client Noema uses (spec-03 §2 layout, IMPL-085), in memory.

Operations: `create_bucket`, `put_object`, `get_object` (Body with async `read()`), `head_object`, `delete_object`,
`delete_objects`, `list_objects_v2`, and multipart uploads (`create_multipart_upload`, `upload_part`,
`complete_multipart_upload`, `abort_multipart_upload`; the streaming quarantine upload of spec-07 §3.1). Missing
buckets, keys or uploads raise botocore `ClientError` with the S3 codes (`NoSuchBucket`, `NoSuchKey`,
`NoSuchUpload`, `InvalidPart`); `fail_next(op)` injects one `ServiceUnavailable` failure. The client is duck-typed
against the aiobotocore `S3Client` (whose stubs cannot be matched structurally); `Services.s3` keeps that type.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Mapping
from typing import Any, Final

import anyio.lowlevel
from botocore.exceptions import ClientError


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        await anyio.lowlevel.checkpoint()
        return self._data

    async def __aenter__(self) -> _Body:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _etag(data: bytes) -> str:
    return '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'


MIN_PART_BYTES: Final = 5 * 1024 * 1024  # S3: every part but the last is at least 5 MiB


class FakeS3Client:
    """In-memory S3 (one instance = one endpoint). `calls` logs (operation, bucket, key)."""

    def __init__(self, buckets: tuple[str, ...] = ()) -> None:
        self.objects: dict[str, dict[str, tuple[bytes, Mapping[str, Any]]]] = {b: {} for b in buckets}
        self.calls: list[tuple[str, str, str | None]] = []
        self._fail: list[str] = []
        self.uploads: dict[str, tuple[str, str, dict[int, bytes], Mapping[str, Any]]] = {}  # id -> bucket, key, parts
        self._upload_ids = itertools.count(1)

    def fail_next(self, op: str) -> None:
        self._fail.append(op)

    async def _enter(self, op: str, bucket: str, key: str | None) -> dict[str, tuple[bytes, Mapping[str, Any]]]:
        await anyio.lowlevel.checkpoint()
        self.calls.append((op, bucket, key))
        if op in self._fail:
            self._fail.remove(op)
            raise _error("ServiceUnavailable", op)
        if bucket not in self.objects:
            raise _error("NoSuchBucket", op)
        return self.objects[bucket]

    async def create_bucket(self, *, Bucket: str, **_: Any) -> dict[str, Any]:  # noqa: N803 (boto names)
        self.objects.setdefault(Bucket, {})
        return {"Location": f"/{Bucket}"}

    async def put_object(self, *, Bucket: str, Key: str, Body: bytes, **extra: Any) -> dict[str, Any]:  # noqa: N803
        bucket = await self._enter("PutObject", Bucket, Key)
        bucket[Key] = (bytes(Body), dict(extra))
        return {"ETag": _etag(Body)}

    async def get_object(self, *, Bucket: str, Key: str, **_: Any) -> dict[str, Any]:  # noqa: N803
        bucket = await self._enter("GetObject", Bucket, Key)
        if Key not in bucket:
            raise _error("NoSuchKey", "GetObject")
        data, extra = bucket[Key]
        return {"Body": _Body(data), "ContentLength": len(data), **extra}

    async def head_object(self, *, Bucket: str, Key: str, **_: Any) -> dict[str, Any]:  # noqa: N803
        bucket = await self._enter("HeadObject", Bucket, Key)
        if Key not in bucket:
            raise _error("404", "HeadObject")
        return {"ContentLength": len(bucket[Key][0])}

    async def delete_object(self, *, Bucket: str, Key: str, **_: Any) -> dict[str, Any]:  # noqa: N803
        bucket = await self._enter("DeleteObject", Bucket, Key)
        bucket.pop(Key, None)  # S3 deletes are idempotent
        return {}

    async def delete_objects(self, *, Bucket: str, Delete: Mapping[str, Any], **_: Any) -> dict[str, Any]:  # noqa: N803
        bucket = await self._enter("DeleteObjects", Bucket, None)
        keys = [o["Key"] for o in Delete.get("Objects", [])]
        for key in keys:
            bucket.pop(key, None)
        return {"Deleted": [{"Key": k} for k in keys]}

    async def list_objects_v2(self, *, Bucket: str, Prefix: str = "", **_: Any) -> dict[str, Any]:  # noqa: N803
        bucket = await self._enter("ListObjectsV2", Bucket, None)
        keys = sorted(k for k in bucket if k.startswith(Prefix))
        return {"KeyCount": len(keys), "Contents": [{"Key": k, "Size": len(bucket[k][0])} for k in keys]}

    # --- multipart uploads -------------------------------------------------------------------------------------------
    async def create_multipart_upload(self, *, Bucket: str, Key: str, **extra: Any) -> dict[str, Any]:  # noqa: N803
        await self._enter("CreateMultipartUpload", Bucket, Key)
        upload_id = f"upload-{next(self._upload_ids)}"
        self.uploads[upload_id] = (Bucket, Key, {}, dict(extra))
        return {"Bucket": Bucket, "Key": Key, "UploadId": upload_id}

    def _upload(self, op: str, bucket: str, key: str, upload_id: str) -> dict[int, bytes]:
        found = self.uploads.get(upload_id)
        if found is None or found[:2] != (bucket, key):
            raise _error("NoSuchUpload", op)
        return found[2]

    async def upload_part(
        self,
        *,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        UploadId: str,  # noqa: N803
        PartNumber: int,  # noqa: N803
        Body: bytes,  # noqa: N803
        **_: Any,
    ) -> dict[str, Any]:
        await self._enter("UploadPart", Bucket, Key)
        if not 1 <= PartNumber <= 10_000:
            raise _error("InvalidArgument", "UploadPart")
        self._upload("UploadPart", Bucket, Key, UploadId)[PartNumber] = bytes(Body)
        return {"ETag": _etag(Body)}

    async def complete_multipart_upload(
        self,
        *,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        UploadId: str,  # noqa: N803
        MultipartUpload: Mapping[str, Any],  # noqa: N803
        **_: Any,
    ) -> dict[str, Any]:
        bucket = await self._enter("CompleteMultipartUpload", Bucket, Key)
        parts = self._upload("CompleteMultipartUpload", Bucket, Key, UploadId)
        listed = [(int(p["PartNumber"]), str(p["ETag"])) for p in MultipartUpload.get("Parts", [])]
        numbers = [n for n, _ in listed]
        if not listed or numbers != sorted(set(numbers)):
            raise _error("InvalidPartOrder", "CompleteMultipartUpload")
        for i, (number, etag) in enumerate(listed):
            data = parts.get(number)
            if data is None or _etag(data) != etag:
                raise _error("InvalidPart", "CompleteMultipartUpload")
            if i < len(listed) - 1 and len(data) < MIN_PART_BYTES:
                raise _error("EntityTooSmall", "CompleteMultipartUpload")
        extra = self.uploads.pop(UploadId)[3]
        bucket[Key] = (b"".join(parts[n] for n in numbers), extra)
        return {"Bucket": Bucket, "Key": Key, "ETag": _etag(bucket[Key][0])}

    async def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str, **_: Any) -> dict[str, Any]:  # noqa: N803
        await self._enter("AbortMultipartUpload", Bucket, Key)
        self._upload("AbortMultipartUpload", Bucket, Key, UploadId)
        del self.uploads[UploadId]  # the parts are discarded; nothing becomes visible
        return {}

    async def close(self) -> None:
        await anyio.lowlevel.checkpoint()
