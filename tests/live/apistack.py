"""The API-level live rig (spec-10 §6 L5-L8): the api in-process (uvicorn on port 0) on the REAL composition,
`composition.build("dev", wrap_adapters=...)` with `NOEMA_CREDENTIALS_BROKER=ambient`, over the Postgres, Valkey
and moto of a compose stack of `make e2e-up` (`NOEMA_LIVE_STACK`, default `noema-live`; its bootstrap state
`~/.noema/stacks/<project>.json`). Only secretsmanager and s3 are overridden (moto); Bedrock and STS are the real
us-east-1 endpoints (the mock rehearsal adds the in-process wire mock for bedrock-runtime and mantle).

Tenant `t_acme` of the stack is mapped to the ambient caller account by a config version proposed and approved
through noema-admin (two operators), with `labeling.sample` rates 0; the L8 activation adds the smoke router.
Every response is contract-validated client side (`Checked`) and by the server's test-mode validator.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import anyio
import httpx
import orjson
import yaml
from tests.live.livecfg import CONFIG_DIR, merge_patch
from tools.bootstrap_dev import INVOKE_ROLE, platform_config

from noema.api.middleware.routing import OperationTable
from noema.contracts.bundle import default_bundle
from noema.contracts.validate import ContractValidator, SseEvent
from noema.core.secret import SecretStr

PROJECT: Final = os.environ.get("NOEMA_LIVE_STACK", "noema-live")
STATE: Final = Path.home() / ".noema" / "stacks" / f"{PROJECT}.json"
TENANT, MAIN, ROLE = "t_acme", "main", "developer"
REPO: Final = Path(__file__).resolve().parents[2]
Json = dict[str, Any]


def stack_state() -> Json | None:
    """The stack's bootstrap state, or None when no stack was brought up."""
    return orjson.loads(STATE.read_bytes()) if STATE.exists() else None


def stack_environ(
    state: Json, *, credentials_file: str, sts: str | None, mock_url: str | None, bearer: SecretStr | None
) -> dict[str, str]:
    """The process environment of the in-process api (spec-05 §5) plus the ambient AWS chain (`credentials_file`;
    `sts` None = AWS STS; `mock_url` = the rehearsal's wire mock for bedrock-runtime and mantle)."""
    host, endpoints = state["host_env"], state["endpoints"]
    overrides = {"secretsmanager": endpoints["moto"], "s3": endpoints["moto"]}
    if sts is not None:
        overrides["sts"] = sts
    if mock_url is not None:
        overrides |= {"bedrock-runtime": mock_url, "mantle": f"{mock_url}/v1"}
    env = {key: host[key] for key in ("NOEMA_DB_DSN_REF", "NOEMA_REDIS_URL", "NOEMA_S3_BUCKET")}
    env |= {
        "NOEMA_ENV": "dev",
        "NOEMA_AWS_ENDPOINT_OVERRIDES": orjson.dumps(overrides).decode(),
        "NOEMA_CREDENTIALS_BROKER": "ambient",
        "NOEMA_EXPECTED_PODS": "1",
        "NOEMA_POD_ID": "live-smoke",
        "AWS_SHARED_CREDENTIALS_FILE": credentials_file,
        "AWS_PROFILE": "default" if sts is not None else os.environ.get("NOEMA_LIVE_AWS_PROFILE", "default"),
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    if bearer is not None:
        env["AWS_BEARER_TOKEN_BEDROCK"] = bearer.get_secret_value()
    return env


@contextmanager
def patched_environ(values: Mapping[str, str]) -> Iterator[None]:
    """`os.environ` updated with `values` for the block, then restored exactly."""
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


async def admin(state: Json, argv: Sequence[str], *, operator: str = "noema-bootstrap-a") -> Json:
    """One `noema-admin` (`python -m noema.identity.cli`) run against the stack; its JSON stdout."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "NOEMA_ENV": str(state.get("env", "dev")),
        "NOEMA_CLI_DSN": f"postgresql://noema_cli:noema-cli-dev-only@{state['endpoints']['postgres']}/noema",
        "NOEMA_AWS_ENDPOINT_OVERRIDES": orjson.dumps({"secretsmanager": state["endpoints"]["moto"]}).decode(),
        "AWS_ACCESS_KEY_ID": "noema-compose-fake",
        "AWS_SECRET_ACCESS_KEY": "noema-compose-fake",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "NOEMA_OPERATOR_KEY": str(state["operators"][operator]["key"]),
    }
    done = await anyio.run_process([sys.executable, "-m", "noema.identity.cli", *argv], env=env, cwd=REPO, check=False)
    assert done.returncode == 0, (argv, done.stderr.decode()[-2000:])
    result: Json = orjson.loads(done.stdout)
    return result


async def activate_live_config(state: Json, tmp: Path, account: str, *, smoke_router: bool = False) -> int:
    """Propose (operator A) and approve (operator B) the stack config with t_acme on the ambient `account` and no
    labeling samples (`smoke_router`: plus the L8 router). Returns the active version."""
    listed = (await admin(state, ["config", "list"]))["data"]
    version = 1 + max(int(v["version"]) for v in listed)
    doc = platform_config(version, prices_ref=state["config"].get("prices_ref"))
    patch: Json = {
        "tenants": {
            TENANT: {"aws_account_id": account, "invoke_role_arn": f"arn:aws:iam::{account}:role/{INVOKE_ROLE}"}
        },
        "labeling": {"sample": {"uniform_rate": 0, "uncertainty_rate": 0}},
    }
    if smoke_router:
        patch |= yaml.safe_load((CONFIG_DIR / "smoke-router.patch.yaml").read_text())
    path = tmp / f"platform-live-v{version}.yaml"
    path.write_text(yaml.safe_dump(merge_patch(doc, patch), sort_keys=False))
    proposed = await admin(state, ["config", "propose", str(path)])
    await admin(state, ["config", "approve", str(proposed["version"])], operator="noema-bootstrap-b")
    return int(proposed["version"])


def principal_key(state: Json, tenant: str = TENANT, project: str = MAIN, role: str = ROLE) -> str:
    return str(state["tenants"][tenant]["projects"][project]["principals"][role]["key"])


@dataclass
class Checked:
    """httpx on the in-process api; every response is validated against the bundled contract."""

    http: httpx.AsyncClient
    validator: ContractValidator = field(default_factory=ContractValidator)
    table: OperationTable = field(default_factory=lambda: OperationTable(default_bundle().operations()))

    def operation(self, method: str, path: str) -> str:
        match = self.table.match(method, path.split("?")[0])
        assert match is not None, (method, path)
        return match.operation.operation_id

    async def request(self, method: str, path: str, *, json: Json | None = None, expect: int = 200) -> httpx.Response:
        content = None if json is None else orjson.dumps(json)
        headers = {} if json is None else {"content-type": "application/json"}
        response = await self.http.request(method, path, content=content, headers=headers)
        op = self.operation(method, path)
        self.validator.check_response(op, response.status_code, dict(response.headers), response.content, json)
        assert response.status_code == expect, (response.status_code, response.text[:800])
        return response

    async def sse(self, path: str, json: Json) -> tuple[list[SseEvent], dict[str, int]]:
        """A streamed POST read to its end, validated frame by frame; client times (ms) of the first
        `generation.delta` frame and of the end."""
        marks: dict[str, int] = {}
        chunks: list[bytes] = []
        t0 = time.monotonic()
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        async with self.http.stream("POST", path, content=orjson.dumps(json), headers=headers) as response:
            async for chunk in response.aiter_raw():
                chunks.append(chunk)
                if "first_delta" not in marks and b"event: generation.delta" in b"".join(chunks[-2:]):
                    marks["first_delta"] = round((time.monotonic() - t0) * 1000)
        marks["e2e"] = round((time.monotonic() - t0) * 1000)
        body = b"".join(chunks)
        assert response.status_code == 200, body[:800]
        return self.validator.check_sse_stream(self.operation("POST", path), body), marks
