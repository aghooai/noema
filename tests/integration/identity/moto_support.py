"""In-process moto for the identity integration tests (ADR-001 "How tests simulate cross-account access").

`ThreadedMotoServer` on port 0 in this process, so tests can read `moto.sts.models.sts_backends` and the
`trust_enforcer` can read the moto IAM backend; aiobotocore talks to it over HTTP. One server per process (xdist
worker), started lazily and stopped at session end. Tenants: t_a -> 111111111111, t_b -> 222222222222 (roles
`AghooBedrockInvoke` trusting the API data-plane role with `StringEquals sts:ExternalId`), t_c -> 333333333333
(trusting only `aghoo-jobs`, SEC-STS-10). Secrets live in the platform account 123456789012.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Mapping
from typing import Any, Final

import pytest
from aiobotocore.session import get_session
from moto.iam.models import iam_backends
from moto.server import ThreadedMotoServer
from tests.fakes.config import patched_example, snapshot_from

from noema.config.registry import ConfigSnapshotImpl
from noema.core.secret import SecretStr
from noema.identity.aws import StaticCredentials

PLATFORM_ACCOUNT: Final = "123456789012"
API_ROLE: Final = f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/noema-api"
JOBS_ROLE: Final = f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/aghoo-jobs"
TENANTS: Final[Mapping[str, str]] = {"t_a": "111111111111", "t_b": "222222222222", "t_c": "333333333333"}
EXTERNAL_IDS: Final[Mapping[str, str]] = {
    "t_a": "aghoo-t_a-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "t_b": "aghoo-t_b-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    "t_c": "aghoo-t_c-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
}
CREDS: Final = StaticCredentials("testing", SecretStr("testing"))
REGION: Final = "us-east-1"

_lock = threading.Lock()
_server: dict[str, Any] = {}


def moto_url(config: pytest.Config) -> str:
    """The process's moto server URL (started on first use, stopped at session end)."""
    with _lock:
        if "url" not in _server:
            logging.getLogger("werkzeug").setLevel(logging.ERROR)  # no per-request access lines
            server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
            server.start()
            host, port = server.get_host_and_port()
            _server["url"] = f"http://{host}:{port}"
            config.add_cleanup(server.stop)
            _seed_roles()
        return str(_server["url"])


def role_arn(tenant_id: str) -> str:
    return f"arn:aws:iam::{TENANTS[tenant_id]}:role/AghooBedrockInvoke"


def secret_name(tenant_id: str) -> str:
    return f"platform/tenants/{tenant_id}/external-id"


def _seed_roles() -> None:
    for tenant_id, account in TENANTS.items():
        trusted = JOBS_ROLE if tenant_id == "t_c" else API_ROLE
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": trusted},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"sts:ExternalId": EXTERNAL_IDS[tenant_id]}},
                }
            ],
        }
        backend = iam_backends[account]["global"]
        if not any(r.name == "AghooBedrockInvoke" for r in backend.roles.values()):
            backend.create_role("AghooBedrockInvoke", json.dumps(policy), "/", None, "", [], None)


def tenants_config(**tenant_patches: Mapping[str, Any]) -> ConfigSnapshotImpl:
    """The example config plus t_a, t_b, t_c (refs to their own secrets unless patched)."""
    tenants: dict[str, Any] = {}
    for tenant_id, account in TENANTS.items():
        tenants[tenant_id] = {
            "aws_account_id": account,
            "invoke_role_arn": role_arn(tenant_id),
            "external_id_ref": f"aws-sm://{secret_name(tenant_id)}",
            "home_region": REGION,
            "defaults": {"decision_profile": "lightweight-model-decide", "generation_profile": "sol-fast"},
            **tenant_patches.get(tenant_id, {}),
        }
    return snapshot_from(patched_example({"tenants": tenants}))


async def put_secret(url: str, name: str, value: str, *, stages: list[str] | None = None) -> None:
    """Create the secret or add a version (current unless `stages` says otherwise)."""
    async with get_session().create_client(
        "secretsmanager",
        region_name=REGION,
        endpoint_url=url,
        aws_access_key_id=CREDS.access_key_id,
        aws_secret_access_key=CREDS.secret_access_key.get_secret_value(),
    ) as client:
        try:
            await client.describe_secret(SecretId=name)
        except client.exceptions.ResourceNotFoundException:
            await client.create_secret(Name=name, SecretString=value)
            version = await _current(client, name)
            for stage in stages or []:
                if stage != "AWSCURRENT":
                    await client.update_secret_version_stage(SecretId=name, VersionStage=stage, MoveToVersionId=version)
            return
        kwargs: dict[str, Any] = {"SecretId": name, "SecretString": value}
        if stages:
            kwargs["VersionStages"] = stages
        await client.put_secret_value(**kwargs)


async def delete_secret(url: str, name: str) -> None:
    async with get_session().create_client(
        "secretsmanager",
        region_name=REGION,
        endpoint_url=url,
        aws_access_key_id=CREDS.access_key_id,
        aws_secret_access_key=CREDS.secret_access_key.get_secret_value(),
    ) as client:
        with contextlib.suppress(client.exceptions.ResourceNotFoundException):
            await client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)


async def _current(client: Any, name: str) -> str:
    versions = await client.list_secret_version_ids(SecretId=name)
    for v in versions["Versions"]:
        if "AWSCURRENT" in v.get("VersionStages", []):
            return str(v["VersionId"])
    raise AssertionError("no AWSCURRENT version")


async def seed_external_ids(url: str) -> None:
    """Create the tenants' ExternalId secrets that don't exist yet (tests that change one restore it)."""
    async with get_session().create_client(
        "secretsmanager",
        region_name=REGION,
        endpoint_url=url,
        aws_access_key_id=CREDS.access_key_id,
        aws_secret_access_key=CREDS.secret_access_key.get_secret_value(),
    ) as client:
        for tenant_id, value in EXTERNAL_IDS.items():
            with contextlib.suppress(client.exceptions.ResourceExistsException):
                await client.create_secret(Name=secret_name(tenant_id), SecretString=value)
