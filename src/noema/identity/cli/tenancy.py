"""`noema-admin` tenancy commands: bootstrap, operators, projects, principals, API keys, tenant ExternalIds.

Keys are minted here (`key_id` = ULID, 32 random bytes of secret), hashed with the current pepper, stored as
`hmac-sha256:v{p}:<hex>` only, and printed once. ExternalIds (`aghoo-<tenant>-<32 base62>`, ADR-001 §3) are
written to Secrets Manager `platform/tenants/<tenant>/external-id`, printed once and audited (`admin.record_audit`);
a rotation is staged (`AWSPENDING`) and promoted in a second step.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import secrets as _random
import string
from datetime import UTC, datetime
from typing import Any, Final, Literal

import asyncpg
from botocore.exceptions import BotoCoreError, ClientError

from noema.core.errors import NoemaError
from noema.core.principal import ALL_ROLES, ALL_SCOPES
from noema.core.values import JsonValue
from noema.db.system import AdminError
from noema.identity.apikeys import mint_api_key, mint_operator_key
from noema.identity.cli.context import AdminContext, CliError, plain

BASE62: Final = string.ascii_letters + string.digits
TENANT_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")  # one Secrets Manager path segment
CURRENT: Final = "AWSCURRENT"
PENDING: Final = "AWSPENDING"


def _csv(value: str | None) -> list[str]:
    return [] if not value else [v.strip() for v in value.split(",") if v.strip()]


def _key_output(key_id: str, token: str, **extra: JsonValue) -> dict[str, JsonValue]:
    return {"key_id": key_id, "key": token, "notice": "Store this key now: it is shown only once.", **extra}


async def bootstrap(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    """`bootstrap --operator <name>`: the first operator (noema_owner connection; one-time)."""
    admin = await ctx.admin(owner=True)
    key_id = ctx.ids.ulid()
    token, key_hash = mint_operator_key(await ctx.peppers(), key_id)
    operator_id = await admin.bootstrap_operator(args.operator, key_id, key_hash)
    return _key_output(key_id, token.get_secret_value(), operator_id=operator_id)


async def operators_create(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    admin = await ctx.admin()
    op = await ctx.operator(admin)
    key_id = ctx.ids.ulid()
    token, key_hash = mint_operator_key(await ctx.peppers(), key_id)
    operator_id = await admin.create_operator(op, args.name, key_id, key_hash)
    return _key_output(key_id, token.get_secret_value(), operator_id=operator_id)


async def operators_revoke(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    admin = await ctx.admin()
    await admin.revoke_operator(await ctx.operator(admin), args.operator_id)
    return {"operator_id": args.operator_id, "revoked": True}


async def projects_create(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    admin = await ctx.admin()
    op = await ctx.operator(admin)
    project_id = await admin.create_project(op, args.tenant, args.name, default_data_class=args.default_data_class)
    return {"project_id": project_id, "tenant_id": args.tenant}


async def projects_list(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    admin = await ctx.admin()
    rows = await admin.list_projects(await ctx.operator(admin), args.tenant)
    return {"data": [plain(r) for r in rows]}


async def principals_create(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    roles = _csv(args.roles)
    if not roles or set(roles) - ALL_ROLES:
        raise CliError("invalid_argument", f"--roles must be a non-empty subset of {sorted(ALL_ROLES)}")
    admin = await ctx.admin()
    op = await ctx.operator(admin)
    principal_id = await admin.create_principal(op, args.tenant, args.project, args.subject, roles)
    return {"principal_id": principal_id}


async def keys_create(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    """`keys create`: an API key for a principal (`--scopes`, `--expires`, `--ip-allow`), printed once."""
    env: Literal["live", "test"] = args.env
    if env == "test" and ctx.env == "prod":
        raise CliError("invalid_argument", "ak_test_ keys are refused with NOEMA_ENV=prod (spec-09 §2)")
    scopes = _csv(args.scopes)
    if not scopes or set(scopes) - ALL_SCOPES:
        raise CliError("invalid_argument", f"--scopes must be a non-empty subset of {sorted(ALL_SCOPES)}")
    try:
        networks = [ipaddress.ip_network(c, strict=False) for c in _csv(args.ip_allow)]
    except ValueError:
        raise CliError("invalid_argument", "--ip-allow must be a comma-separated CIDR list") from None
    expires = _expiry(args.expires)
    admin = await ctx.admin()
    op = await ctx.operator(admin)
    ring = await ctx.peppers()
    key_id = ctx.ids.ulid()
    token, key_hash = mint_api_key(ring, env, key_id)
    await admin.create_api_key(
        op,
        tenant_id=args.tenant,
        project_id=args.project,
        principal_id=args.principal,
        key_id=key_id,
        key_hash=key_hash,
        pepper_version=ring.current.version,
        env=env,
        scopes=scopes,
        ip_allowlist=networks or None,
        expires_at=expires,
    )
    listed: list[JsonValue] = list(scopes)
    return _key_output(key_id, token.get_secret_value(), scopes=listed)


def _expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CliError("invalid_argument", "--expires must be an ISO 8601 timestamp") from None
    parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    if parsed <= datetime.now(UTC):
        raise CliError("invalid_argument", "--expires must be in the future")
    return parsed


async def keys_list(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    admin = await ctx.admin()
    rows = await admin.list_api_keys(await ctx.operator(admin), args.tenant, project_id=args.project)
    return {"data": [plain(r) for r in rows]}


async def keys_revoke(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    """Sets revoked_at; pods evict the key within 1 s (keys_revoked) or 60 s (cache TTL)."""
    admin = await ctx.admin()
    await admin.revoke_api_key(await ctx.operator(admin), args.key_id)
    return {"key_id": args.key_id, "revoked": True}


def new_external_id(tenant_id: str) -> str:
    """`aghoo-<tenant_id>-<32 base62>` (about 190 random bits)."""
    return f"aghoo-{tenant_id}-" + "".join(_random.choice(BASE62) for _ in range(32))


async def external_id(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    """`tenants external-id create|rotate|promote --tenant <id>` (ADR-001 §3). create writes the first value; rotate
    stages a new value as `AWSPENDING` (brokers keep reading `AWSCURRENT`) so it can be added to the trust policy
    next to the current one; promote then makes it current. Every step is audited without the value; a value is
    printed exactly once, even when the audit fails after the write (then exit 1 with the refusal on stderr)."""
    if not TENANT_ID.fullmatch(args.tenant):
        raise CliError("invalid_argument", "--tenant must match [A-Za-z0-9_-]{1,64}.")
    admin = await ctx.admin()
    op = await ctx.operator(admin)
    name = f"platform/tenants/{args.tenant}/external-id"
    client = await ctx.secrets.client.get()
    result: dict[str, JsonValue] = {"tenant_id": args.tenant, "external_id_ref": f"aws-sm://{name}"}
    try:
        if args.action == "promote":
            versions = await _promote(client, name)
            result["notice"] = "Brokers switch at their next refresh; remove the old value from the trust policy then."
        else:
            value = new_external_id(args.tenant)
            if args.action == "create":
                response = await client.create_secret(Name=name, SecretString=value)
                notice = "Configure the tenant trust policy with this value now: it is shown only once."
            else:
                response = await client.put_secret_value(SecretId=name, SecretString=value, VersionStages=[PENDING])
                notice = (
                    "Add this value to the tenant trust policy next to the current one, then run "
                    f"`tenants external-id promote --tenant {args.tenant}`: it is shown only once."
                )
            versions = {"version_id": str(response.get("VersionId", ""))}
            result.update(external_id=value, notice=notice)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        reason = {"ResourceExistsException": "conflict", "ResourceNotFoundException": "not_found"}.get(code, "error")
        raise CliError(reason, f"Secrets Manager refused the {args.action} ({code or 'error'}).") from None
    except (BotoCoreError, OSError):
        raise CliError("unavailable", "Secrets Manager is unavailable.") from None
    result.update(versions)
    try:
        await admin.record_audit(
            op,
            tenant_id=args.tenant,
            action=f"tenant.external_id.{args.action}",
            resource_kind="tenant",
            resource_id=args.tenant,
            details={"secret": name, **versions},
        )
    except (AdminError, NoemaError, asyncpg.PostgresError, asyncpg.InterfaceError, OSError):
        message = f"The {args.action} is done but its audit row could not be recorded; record it by hand."
        raise CliError("audit_failed", message, output=result) from None
    return result


async def _promote(client: Any, name: str) -> dict[str, JsonValue]:
    """Move `AWSCURRENT` to the `AWSPENDING` version (Secrets Manager labels the old one `AWSPREVIOUS`)."""
    stages = (await client.describe_secret(SecretId=name)).get("VersionIdsToStages") or {}
    pending = next((v for v, labels in stages.items() if PENDING in labels), None)
    current = next((v for v, labels in stages.items() if CURRENT in labels), None)
    if pending is None or pending == current:
        raise CliError("invalid_state", "No staged ExternalId to promote: run `tenants external-id rotate` first.")
    move: dict[str, str] = {"SecretId": name, "VersionStage": CURRENT, "MoveToVersionId": pending}
    if current is not None:
        move["RemoveFromVersionId"] = current
    await client.update_secret_version_stage(**move)
    await client.update_secret_version_stage(SecretId=name, VersionStage=PENDING, RemoveFromVersionId=pending)
    return {"version_id": pending, "previous_version_id": current}
