"""Object-storage conventions shared by media, ops and identity (spec-03 §2 S3 layout, spec-09 §6.7, IMPL-085).

One bucket per environment (`noema-<env>`, `ProcessSettings.s3_bucket`); every object is under its tenant's prefix
and encrypted with the tenant's KMS key in staging and prod (`alias/noema-<env>-<tenant_id>` in the platform
account); dev and test use SSE-S3 (moto/minio have no tenant keys).
"""

from __future__ import annotations

from typing import Final

_KMS_ENVS: Final = frozenset({"staging", "prod"})


def default_bucket(env: str) -> str:
    """`noema-<env>` (spec-03 §2)."""
    return f"noema-{env}"


def tenant_kms_key_alias(env: str, tenant_id: str) -> str:
    """The KMS key alias of one tenant's objects: `alias/noema-<env>-<tenant_id>`."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    return f"alias/noema-{env}-{tenant_id}"


def sse_args(env: str, tenant_id: str) -> dict[str, str]:
    """The server-side-encryption arguments of every PutObject / CreateMultipartUpload of a tenant object:
    SSE-KMS with the tenant key in staging/prod, SSE-S3 (AES256) in dev/test."""
    if env in _KMS_ENVS:
        return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": tenant_kms_key_alias(env, tenant_id)}
    return {"ServerSideEncryption": "AES256"}
