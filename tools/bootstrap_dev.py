"""Seed a dev or e2e stack: `python -m tools.bootstrap_dev --compose-project noema-e2e` (make e2e-bootstrap).

Dev/test stacks only (NOEMA_ENV dev or test; moto and tools/mockbedrock, never AWS or a model API). Steps, each
idempotent (a re-run reuses the state file of the previous run and only adds what is missing):
1. Postgres: the four noema roles with the dev passwords, the database, `alembic upgrade head` (to
   `0003_operator_proof`).
2. moto: the bucket `noema-<env>`; the platform secrets `noema/api-key-pepper` (identity format
   `{"version": 1, "pepper": ...}`), `noema/input-digest-pepper`, `noema/db-dsn` (the DSN the stack's containers
   use: `postgres:5432`, or `toxiproxy:15432` for fault runs) and `noema/db-dsn-host` (the published port, for
   host-run processes: `NOEMA_DB_DSN_REF=noema/db-dsn-host`).
3. noema-admin (in-process `amain`, the operator CLI of IMPL-044): operator A (`bootstrap`) and operator B
   (`operators create`); the platform config = the vendored platform.example.yaml plus tenant t_beta, proposed by A
   and approved by B (two-person rule; the stack's pods load it within 5 s); per tenant its ExternalId
   (`tenants external-id create`), projects `main` and `second`, one principal per role and one `ak_test_` key per
   principal (every scope).
4. moto IAM/STS: per tenant account the role `AghooBedrockInvoke` trusting the platform roles with the tenant's
   ExternalId (moto evaluates no trust policy; ADR-001 "How tests simulate cross-account access").
5. The state file (mode 0600 in a 0700 directory outside the repo, default ~/.noema/stacks/<project>.json) with
   the endpoints, operator keys, tenants, projects, principals and API keys, plus `<project>.env`: the environment
   for host-run processes (`set -a; . ~/.noema/stacks/<project>.env`). Keys are printed nowhere else.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import anyio
import yaml
from aiobotocore.session import get_session

from noema.core.principal import ALL_SCOPES
from noema.core.secret import SecretStr
from noema.db.alembic.runner import upgrade
from noema.db.engine import ROLE_APP, ROLE_CLI, ROLE_OWNER, connect, create_database, ensure_roles, secure_database
from noema.identity.cli.noema_admin import amain
from noema.ops.prices.placeholder import placeholder_catalog

REPO: Final = Path(__file__).resolve().parents[1]
COMPOSE_FILE: Final = REPO / "deploy" / "compose" / "docker-compose.yml"
EXAMPLE_CONFIG: Final = REPO / "src" / "noema" / "contracts" / "_vendor" / "examples" / "platform.example.yaml"
STATE_DIR: Final = Path.home() / ".noema" / "stacks"
REGION: Final = "us-east-1"
PLATFORM_ACCOUNT: Final = "123456789012"  # moto's default account = the platform account
FAKE_AWS_KEY: Final = "noema-compose-fake"  # the compose app containers' AWS keys (moto accepts any)
PG_SUPERUSER: Final = ("postgres", "noema-dev-only")  # deploy/compose defaults (dev-only)
DB_NAME: Final = "noema"
TENANTS: Final[Mapping[str, str]] = {"t_acme": "111122223333", "t_beta": "444455556666"}  # tenant -> AWS account
INVOKE_ROLE: Final = "AghooBedrockInvoke"
TRUSTED_ROLES: Final = (
    f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/noema-api",
    f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/aghoo-jobs",
)
# project name -> principals (state key -> roles); every principal gets one ak_test_ key with every scope
PRINCIPALS: Final[Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "main": {
        "developer": ("developer",),
        "developer2": ("developer",),
        "tenant_admin": ("tenant_admin",),
        "reviewer": ("reviewer",),
        "end_user": ("end_user",),
        "auditor": ("auditor",),
    },
    "second": {"developer": ("developer",)},
}
OPERATORS: Final = ("noema-bootstrap-a", "noema-bootstrap-b")
# FAKE dev prices (USD per million tokens) for every model of the stack's catalog; never real AWS prices
DEV_PRICES: Final[Mapping[str, str]] = {
    "input": "1.5",
    "output": "6",
    "cache_read": "0.15",
    "cache_write_5m": "1.875",
    "cache_write_1h": "3",
}
DEV_PRICES_FROM: Final = date(2026, 1, 1)


class BootstrapError(RuntimeError):
    """A step failed; the message names it (never a secret)."""


def dev_password(role: str) -> str:
    """The dev-only login password of a noema role (`noema.db.alembic.runner bootstrap` in dev/test)."""
    return f"{role.replace('_', '-')}-dev-only"


def dsn(role: str, host: str, db: str = DB_NAME) -> str:
    return f"postgresql://{role}:{dev_password(role)}@{host}/{db}"


@dataclass(frozen=True)
class Target:
    """Where the stack is: host-side addresses (this tool and host-run processes) and the in-stack database host."""

    project: str
    env: str
    pg_host: str  # host:port of the published postgres (host side)
    moto_url: str  # host side
    stack_db_host: str = "postgres:5432"  # what the containers use for noema/db-dsn
    redis_url: str = ""
    mock_url: str = ""
    apis: tuple[str, ...] = ()
    metrics: tuple[tuple[str, ...], ...] = ()  # per api replica: every worker's /metrics endpoint
    toxiproxy_url: str = ""
    db_name: str = DB_NAME
    pg_superuser: tuple[str, str] = PG_SUPERUSER

    @property
    def bucket(self) -> str:
        return f"noema-{self.env}"

    @property
    def admin_dsn(self) -> str:
        user, password = self.pg_superuser
        return f"postgresql://{user}:{password}@{self.pg_host}/postgres"

    def admin_environ(self, operator_key: str | None = None) -> dict[str, str]:
        """The noema-admin environment (`amain(environ=...)`); Secrets Manager credentials come from os.environ."""
        environ = {
            "NOEMA_ENV": self.env,
            "NOEMA_CLI_DSN": dsn(ROLE_CLI, self.pg_host, self.db_name),
            "NOEMA_DB_OWNER_DSN": dsn(ROLE_OWNER, self.pg_host, self.db_name),
            "NOEMA_AWS_ENDPOINT_OVERRIDES": json.dumps({"secretsmanager": self.moto_url}),
        }
        if operator_key is not None:
            environ["NOEMA_OPERATOR_KEY"] = operator_key
        return environ

    def host_env(self) -> dict[str, str]:
        """The environment of a host-run Noema process against this stack (make api-serve, the perf harness)."""
        overrides = {"sts": self.moto_url, "secretsmanager": self.moto_url, "s3": self.moto_url}
        if self.mock_url:
            overrides |= {"bedrock-runtime": self.mock_url, "mantle": f"{self.mock_url}/v1"}
        env = {
            "NOEMA_ENV": self.env,
            "NOEMA_AWS_ENDPOINT_OVERRIDES": json.dumps(overrides),
            "NOEMA_DB_DSN_REF": "noema/db-dsn-host",
            "NOEMA_S3_BUCKET": self.bucket,
            "NOEMA_EXPECTED_PODS": "3",
            "AWS_ACCESS_KEY_ID": FAKE_AWS_KEY,
            "AWS_SECRET_ACCESS_KEY": FAKE_AWS_KEY,
            "AWS_REGION": REGION,
            "AWS_DEFAULT_REGION": REGION,
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        if self.redis_url:
            env["NOEMA_REDIS_URL"] = self.redis_url
        return env


@dataclass
class State:
    """The state file's content (see the module docstring); `data` is written as JSON."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {}
        return cls(path, data if isinstance(data, dict) else {})

    def section(self, *keys: str) -> dict[str, Any]:
        node = self.data
        for key in keys:
            node = node.setdefault(key, {})
        return node

    def save(self) -> None:
        write_private(self.path, json.dumps(self.data, indent=2, sort_keys=True) + "\n")


def write_private(path: Path, text: str) -> None:
    """Write `text` atomically to `path` with mode 0600 (directory 0700); refuses a path inside the repository."""
    path = path.expanduser().resolve()
    if path.is_relative_to(REPO):
        raise BootstrapError(f"refusing to write secrets inside the repository: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# --- 1. Postgres --------------------------------------------------------------------------------------------------
async def seed_database(target: Target) -> None:
    """Roles (dev passwords), the database and `alembic upgrade head` (idempotent)."""
    admin = await connect(SecretStr(target.admin_dsn), expected_role=None)
    try:
        passwords = {role: SecretStr(dev_password(role)) for role in (ROLE_OWNER, ROLE_APP, ROLE_CLI)}
        await ensure_roles(admin, passwords=passwords)
        if await admin.fetchval("SELECT true FROM pg_catalog.pg_database WHERE datname = $1", target.db_name):
            await secure_database(admin, target.db_name)
        else:
            await create_database(admin, target.db_name)
    finally:
        await admin.close()
    owner = SecretStr(dsn(ROLE_OWNER, target.pg_host, target.db_name))
    await anyio.to_thread.run_sync(upgrade, owner)  # alembic runs its own event loop


# --- 2. moto: bucket and platform secrets ---------------------------------------------------------------------------
def _aws(service: str, url: str, creds: Mapping[str, str] | None = None) -> Any:
    kwargs: dict[str, Any] = {"region_name": REGION, "endpoint_url": url}
    if creds is None:
        kwargs |= {"aws_access_key_id": FAKE_AWS_KEY, "aws_secret_access_key": FAKE_AWS_KEY}
    else:
        kwargs |= {
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"],
        }
    session: Any = get_session()  # create_client is overloaded per literal service name
    return session.create_client(service, **kwargs)


async def _secret(client: Any, name: str, value: str | None, *, keep: bool) -> str:
    """Create the secret; an existing one keeps its value when `keep` (peppers) or is updated to `value`."""
    try:
        current = str((await client.get_secret_value(SecretId=name))["SecretString"])
    except client.exceptions.ResourceNotFoundException:
        current = None
    if current is not None and (keep or current == value):
        return current
    if value is None:
        raise BootstrapError(f"secret {name} has no value to write")
    if current is None:
        await client.create_secret(Name=name, SecretString=value)
    else:
        await client.put_secret_value(SecretId=name, SecretString=value)
    return value


async def seed_moto(target: Target) -> None:
    """The bucket and the four platform secrets (spec-09 §4)."""
    async with _aws("s3", target.moto_url) as s3:
        with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou, s3.exceptions.BucketAlreadyExists):
            await s3.create_bucket(Bucket=target.bucket)
    pepper = json.dumps({"version": 1, "pepper": secrets.token_urlsafe(36)})
    async with _aws("secretsmanager", target.moto_url) as sm:
        await _secret(sm, "noema/api-key-pepper", pepper, keep=True)  # existing keys' hashes depend on it
        await _secret(sm, "noema/input-digest-pepper", secrets.token_hex(32), keep=True)
        await _secret(sm, "noema/db-dsn", dsn(ROLE_APP, target.stack_db_host, target.db_name), keep=False)
        await _secret(sm, "noema/db-dsn-host", dsn(ROLE_APP, target.pg_host, target.db_name), keep=False)


# --- 3. noema-admin -----------------------------------------------------------------------------------------------
async def admin(target: Target, argv: Sequence[str], operator_key: str | None = None) -> dict[str, Any]:
    """One noema-admin command in-process; its JSON output. BootstrapError with the refusal otherwise."""
    out, err = io.StringIO(), io.StringIO()
    code = await amain(list(argv), environ=target.admin_environ(operator_key), stdout=out, stderr=err)
    if code != 0:
        raise BootstrapError(f"noema-admin {' '.join(argv[:2])} refused (exit {code}): {err.getvalue().strip()}")
    result: dict[str, Any] = json.loads(out.getvalue())
    return result


async def _operator_works(target: Target, key: str) -> bool:
    out, err = io.StringIO(), io.StringIO()
    return await amain(["config", "list"], environ=target.admin_environ(key), stdout=out, stderr=err) == 0


async def seed_operators(target: Target, state: State) -> tuple[str, str]:
    """Operators A and B (two-person rule for config approvals); reused from the state file when they still work."""
    ops = state.section("operators")
    keys: list[str] = []
    for index, name in enumerate(OPERATORS):
        entry = ops.get(name)
        if entry is None or not await _operator_works(target, str(entry["key"])):
            if index == 0:  # a fresh stack (`bootstrap` is one-time): nothing recorded earlier exists in it
                created = await admin(target, ["bootstrap", "--operator", name])
                for stale in ("tenants", "config", "prices"):
                    state.data.pop(stale, None)
            else:
                created = await admin(target, ["operators", "create", "--name", name], keys[0])
            entry = ops[name] = {
                "operator_id": created["operator_id"],
                "key_id": created["key_id"],
                "key": created["key"],
            }
            state.save()
        keys.append(str(entry["key"]))
    return keys[0], keys[1]


def platform_config(version: int, *, prices_ref: str | None = None) -> dict[str, Any]:
    """platform.example.yaml with `config_version` = `version`, tenant t_beta (a copy of t_acme's settings) and,
    when given, `platform.cost.prices_ref` (the stack's dev price catalog, `db:<n>`)."""
    doc: dict[str, Any] = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    doc["config_version"] = version
    if prices_ref is not None:
        doc.setdefault("platform", {}).setdefault("cost", {})["prices_ref"] = prices_ref
    template = doc["tenants"]["t_acme"]
    for tenant_id, account in TENANTS.items():
        tenant = json.loads(json.dumps(template))
        tenant.update(
            aws_account_id=account,
            invoke_role_arn=f"arn:aws:iam::{account}:role/{INVOKE_ROLE}",
            external_id_ref=f"aws-sm://platform/tenants/{tenant_id}/external-id",
            home_region=REGION,
        )
        doc["tenants"][tenant_id] = tenant
    return doc


def dev_prices() -> dict[str, Any]:
    """The stack's price catalog: every model of the example config in every tenant region at fixed FAKE dev prices
    (USD per million tokens; not AWS prices), so usage lines cost more than 0 and are not `cost_estimated`."""
    doc = placeholder_catalog(platform_config(1), effective_from=DEV_PRICES_FROM)
    for entry in doc["entries"]:
        entry["usd_per_mtok"] = dict(DEV_PRICES)
    return doc


async def seed_prices(target: Target, state: State, op_a: str) -> str:
    """`noema-admin prices import` of `dev_prices()` once per stack; returns its `db:<n>` prices_ref."""
    recorded = state.data.get("prices")
    if recorded is not None:
        return str(recorded["prices_ref"])
    with tempfile.TemporaryDirectory(prefix="noema-bootstrap-") as tmp:
        path = Path(tmp) / "prices.yaml"
        path.write_text(yaml.safe_dump(dev_prices(), sort_keys=False))
        imported = await admin(target, ["prices", "import", str(path)], op_a)
    state.data["prices"] = {k: imported[k] for k in ("version", "prices_ref", "digest")}
    state.save()
    return str(imported["prices_ref"])


async def seed_config(target: Target, state: State, op_a: str, op_b: str) -> dict[str, Any]:
    """Propose (A) and approve (B) the stack config (with the dev price catalog) unless the active version is the
    one recorded with the same catalog."""
    prices_ref = await seed_prices(target, state, op_a)
    listed = (await admin(target, ["config", "list"], op_a)).get("data", [])
    active = next((v for v in listed if v.get("status") == "active"), None)
    recorded = state.data.get("config")
    same = recorded is not None and recorded.get("prices_ref") == prices_ref
    if active is not None and recorded is not None and same and recorded.get("version") == active.get("version"):
        return dict(recorded)
    version = 1 + max((int(v["version"]) for v in listed), default=0)
    with tempfile.TemporaryDirectory(prefix="noema-bootstrap-") as tmp:
        path = Path(tmp) / f"platform-v{version}.yaml"
        path.write_text(yaml.safe_dump(platform_config(version, prices_ref=prices_ref), sort_keys=False))
        proposed = await admin(target, ["config", "propose", str(path)], op_a)
    await admin(target, ["config", "approve", str(version)], op_b)
    state.data["config"] = {
        "version": version,
        "digest": proposed["digest"],
        "tenants": sorted(TENANTS),
        "prices_ref": prices_ref,
    }
    state.save()
    return dict(state.data["config"])


# --- 4. tenants: ExternalIds, invoke roles, projects, principals, API keys ------------------------------------------
async def _external_id(target: Target, tenant_id: str, op: str) -> str:
    """The tenant's ExternalId: created through noema-admin once, read back from moto afterwards."""
    name = f"platform/tenants/{tenant_id}/external-id"
    async with _aws("secretsmanager", target.moto_url) as sm:
        try:
            return str((await sm.get_secret_value(SecretId=name))["SecretString"])
        except sm.exceptions.ResourceNotFoundException:
            pass
    created = await admin(target, ["tenants", "external-id", "create", "--tenant", tenant_id], op)
    return str(created["external_id"])


async def seed_invoke_role(target: Target, account: str, external_id: str) -> str:
    """`AghooBedrockInvoke` in the tenant account (moto: STS into the account, then IAM), trusting the platform's
    api and jobs roles with the ExternalId condition (ADR-001 §3)."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": list(TRUSTED_ROLES)},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": external_id}},
            }
        ],
    }
    async with _aws("sts", target.moto_url) as sts:
        assumed = await sts.assume_role(
            RoleArn=f"arn:aws:iam::{account}:role/noema-bootstrap", RoleSessionName="noema-bootstrap"
        )
    async with _aws("iam", target.moto_url, assumed["Credentials"]) as iam:
        try:
            role = await iam.create_role(RoleName=INVOKE_ROLE, AssumeRolePolicyDocument=json.dumps(policy))
        except iam.exceptions.EntityAlreadyExistsException:
            await iam.update_assume_role_policy(RoleName=INVOKE_ROLE, PolicyDocument=json.dumps(policy))
            role = await iam.get_role(RoleName=INVOKE_ROLE)
    return str(role["Role"]["Arn"])


async def seed_tenant(target: Target, state: State, tenant_id: str, op: str) -> None:
    account = TENANTS[tenant_id]
    tenant = state.section("tenants", tenant_id)
    tenant["aws_account_id"] = account
    tenant["external_id"] = await _external_id(target, tenant_id, op)
    tenant["invoke_role_arn"] = await seed_invoke_role(target, account, tenant["external_id"])
    existing = {
        p["name"]: p["id"] for p in (await admin(target, ["projects", "list", "--tenant", tenant_id], op))["data"]
    }
    for project, principals in PRINCIPALS.items():
        entry = state.section("tenants", tenant_id, "projects", project)
        if project not in existing:
            existing[project] = (
                await admin(target, ["projects", "create", "--tenant", tenant_id, "--name", project], op)
            )["project_id"]
        entry["project_id"] = existing[project]
        for key_name, roles in principals.items():
            who = state.section("tenants", tenant_id, "projects", project, "principals", key_name)
            if "principal_id" not in who:
                subject = f"{key_name}@{project}.{tenant_id}.example"
                argv = ["principals", "create", "--tenant", tenant_id, "--project", entry["project_id"]]
                created = await admin(target, [*argv, "--subject", subject, "--roles", ",".join(roles)], op)
                who.update(principal_id=created["principal_id"], subject=subject, roles=list(roles))
                state.save()
            if "key" not in who:
                argv = ["keys", "create", "--tenant", tenant_id, "--project", entry["project_id"]]
                argv += ["--principal", who["principal_id"], "--scopes", ",".join(sorted(ALL_SCOPES)), "--env", "test"]
                created = await admin(target, argv, op)
                who.update(key_id=created["key_id"], key=created["key"])
                state.save()
    state.save()


def endpoints(target: Target) -> dict[str, Any]:
    """The state file's `endpoints`; `metrics` holds one list per api replica of every worker's endpoint."""
    return {
        "api": list(target.apis),
        "metrics": [list(urls) for urls in target.metrics],
        "mockbedrock": target.mock_url,
        "moto": target.moto_url,
        "postgres": target.pg_host,
        "redis": target.redis_url,
        "toxiproxy": target.toxiproxy_url,
    }


async def bootstrap(target: Target, state: State) -> State:
    """Every step (module docstring) against `target`; returns the saved state."""
    if target.env not in ("dev", "test"):
        raise BootstrapError("bootstrap_dev seeds dev/test stacks only")
    await seed_database(target)
    await seed_moto(target)
    op_a, op_b = await seed_operators(target, state)
    await seed_config(target, state, op_a, op_b)
    for tenant_id in TENANTS:
        await seed_tenant(target, state, tenant_id, op_a)
    state.data.update(
        project=target.project,
        env=target.env,
        bucket=target.bucket,
        updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        endpoints=endpoints(target),
        host_env=target.host_env(),
    )
    state.save()
    env_text = "".join(f"{k}={_shell_quote(v)}\n" for k, v in sorted(target.host_env().items()))
    write_private(state.path.with_suffix(".env"), env_text)
    return state


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


# --- the command line ---------------------------------------------------------------------------------------------
def compose_port(project: str, service: str, port: int) -> str | None:
    """`127.0.0.1:<published>` of a service port of the compose project, or None when it is not running."""
    argv = ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), "port", service, str(port)]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    text = out.stdout.strip()
    if out.returncode != 0 or not text or text.endswith(":0"):
        return None
    return "127.0.0.1:" + text.rsplit(":", 1)[1]


def service_env(project: str, service: str) -> dict[str, str]:
    """The environment of a service's running container (`docker inspect`), {} when it does not run."""
    base = ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE)]
    ps = subprocess.run([*base, "ps", "-q", service], capture_output=True, text=True, timeout=60, check=False)
    container = ps.stdout.strip().splitlines()[0] if ps.returncode == 0 and ps.stdout.strip() else ""
    if not container:
        return {}
    argv = ["docker", "inspect", "-f", "{{json .Config.Env}}", container]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=True)
    return dict(item.split("=", 1) for item in json.loads(out.stdout))


def discover(project: str, *, env: str, stack_db_host: str) -> Target:
    """The target of a running compose project (base services required; app services when they run)."""

    def need(service: str, port: int) -> str:
        found = compose_port(project, service, port)
        if found is None:
            raise BootstrapError(f"{service} of compose project {project} is not running (make e2e-up)")
        return found

    def url(service: str, port: int, scheme: str = "http") -> str:
        found = compose_port(project, service, port)
        return f"{scheme}://{found}" if found else ""

    def worker_metrics(service: str) -> tuple[str, ...]:
        """Every worker's endpoint of a running api replica: each of its WEB_CONCURRENCY uvicorn workers serves the
        first free port of NOEMA_METRICS_PORT .. + WEB_CONCURRENCY - 1 (composition.serve_metrics)."""
        env = service_env(project, service)
        if not env:
            return ()
        workers, first = int(env.get("WEB_CONCURRENCY") or 1), int(env.get("NOEMA_METRICS_PORT") or 9464)
        urls = tuple(url(service, first + i) for i in range(workers))
        if not all(urls):
            last = first + workers - 1
            raise BootstrapError(f"{service} runs {workers} api workers but does not publish metrics {first}..{last}")
        return urls

    apis = tuple(u for u in (url("api-1", 8000), url("api-2", 8000)) if u)
    metrics = tuple(m for m in (worker_metrics("api-1"), worker_metrics("api-2")) if m)
    return Target(
        project=project,
        env=env,
        pg_host=need("postgres", 5432),
        moto_url="http://" + need("moto", 5000),
        stack_db_host=stack_db_host,
        redis_url=url("valkey", 6379, "redis") + "/0" if url("valkey", 6379) else "",
        mock_url=url("mockbedrock", 8080),
        apis=apis,
        metrics=metrics,
        toxiproxy_url=url("toxiproxy", 8474),
    )


def summary(state: State) -> str:
    """What to tell the operator: where things are, never a key."""
    data = state.data
    lines = [f"bootstrapped {data.get('project')} (NOEMA_ENV={data.get('env')}); state file {state.path} (0600)"]
    for name, value in sorted(data.get("endpoints", {}).items()):
        lines.append(f"  {name}: {value}")
    for tenant_id, tenant in sorted(data.get("tenants", {}).items()):
        projects = ", ".join(f"{p}={v['project_id']}" for p, v in sorted(tenant.get("projects", {}).items()))
        lines.append(f"  {tenant_id} (account {tenant.get('aws_account_id')}): {projects}")
    lines.append(
        f"  config v{data.get('config', {}).get('version')} active; host env: {state.path.with_suffix('.env')}"
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.bootstrap_dev", description=__doc__.splitlines()[0])
    parser.add_argument("--compose-project", default="noema-e2e", help="the compose project (default %(default)s)")
    parser.add_argument("--env", default="dev", choices=("dev", "test"), help="the stack's NOEMA_ENV (default dev)")
    parser.add_argument(
        "--db-host", default="postgres:5432", help="in-stack postgres host:port (fault runs: toxiproxy:15432)"
    )
    parser.add_argument("--state", type=Path, help="the state file (default ~/.noema/stacks/<project>.json)")
    args = parser.parse_args(argv)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):  # moto only (the endpoints are local)
        os.environ[name] = FAKE_AWS_KEY
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    path = args.state or STATE_DIR / f"{args.compose_project}.json"
    try:
        target = discover(args.compose_project, env=args.env, stack_db_host=args.db_host)
        state = anyio.run(bootstrap, target, State.load(path))
    except BootstrapError as exc:
        sys.stderr.write(f"bootstrap failed: {exc}\n")
        return 1
    sys.stdout.write(summary(state) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
