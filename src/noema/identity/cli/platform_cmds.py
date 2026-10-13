"""`noema-admin` platform commands: config validate/propose/approve/rollback/list, prices import, calibrators.

Config integrity in Phase 1 (IMPL-044): two-person approval in the database (approver != proposer) plus the full
validation (schema, `check_config`, P1 rules) at propose and again at every pod load. `config approve` optionally
takes the file the approver reviewed and then refuses unless its digest equals the proposed version's; without it,
the stored body is read (`admin.config_body`). After the activation it retires every calibrator artifact whose
(profile, version) left the active config (ADR-003 §5, reason `profile_version_retired`). Calibrators activate by
two people: `approve` (A), then `activate` (B != A).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml

from noema.config.loader import ConfigError, digest_of, load_config, validate_config
from noema.config.prices import PriceCatalogError, parse_price_catalog
from noema.core.values import JsonValue
from noema.db.system import PgAdminCalls
from noema.identity.cli.context import AdminContext, CliError, plain

RETIRABLE: Final = frozenset({"fitted", "accepted", "active"})


def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError as exc:
        raise CliError("invalid_argument", f"cannot read {path}: {type(exc).__name__}") from None


def checked_config(ctx: AdminContext, path: str) -> tuple[dict[str, Any], str]:
    """Steps 1-5 of spec-05 §3 (plus a `file:` price catalog) -> (filled body, digest); CliError lists errors."""
    text = _read(path)
    try:
        filled, errors = validate_config(text, env=ctx.env)
    except (ConfigError, ValueError, yaml.YAMLError) as exc:
        raise CliError("invalid_config", f"{type(exc).__name__}: the file is not a config document") from None
    if not errors and str(filled["platform"]["cost"].get("prices_ref") or "").startswith("file:"):
        try:
            load_config(text, env=ctx.env, base_dir=Path(path).parent)
        except ConfigError as exc:
            errors = list(exc.errors)
    if errors:
        raise CliError("invalid_config", "; ".join(errors))
    return filled, digest_of(filled)


async def config_validate(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    """Local only (no database): every error, or the digest."""
    filled, digest = checked_config(ctx, args.file)
    return {"valid": True, "config_version": filled["config_version"], "digest": digest}


async def config_propose(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    filled, digest = checked_config(ctx, args.file)
    admin = await ctx.admin()
    version = await admin.config_propose(await ctx.operator(admin), digest, filled)
    return {"version": version, "digest": digest, "status": "proposed"}


async def config_approve(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    """The second person activates a proposed version; then calibrators of profile versions that left the active
    config are retired. With `--file`, the reviewed file must be the stored version (digest check)."""
    checked = None if args.file is None else checked_config(ctx, args.file)
    admin = await ctx.admin()
    op = await ctx.operator(admin)
    if checked is None:
        body = await admin.config_body(op, args.version)
        if body is None:
            raise CliError("not_found", f"config version {args.version} does not exist")
        profiles = body.get("decision_profiles")
    else:
        filled, digest = checked
        stored = {int(r["version"]): r for r in await admin.list_config_versions(op)}  # type: ignore[call-overload]
        row = stored.get(args.version)
        if row is None:
            raise CliError("not_found", f"config version {args.version} does not exist")
        if row["digest"] != digest or filled.get("config_version") != args.version:
            raise CliError("digest_mismatch", "the file is not the proposed config version (digest differs)")
        profiles = filled["decision_profiles"]
    if not isinstance(profiles, dict):
        raise CliError("invalid_state", f"config version {args.version} has no decision_profiles object")
    await admin.config_approve(op, args.version)
    retired = await retire_left_calibrators(admin, op, profiles)
    return {"version": args.version, "status": "active", "retired_calibrators": retired}


async def retire_left_calibrators(admin: PgAdminCalls, op: str, profiles: Mapping[str, Any]) -> list[JsonValue]:
    """Retire (`profile_version_retired`) every fitted/accepted/active artifact whose profile version is not in
    `profiles` (the active config's `decision_profiles`)."""
    live = {(pid, str(p["version"])) for pid, p in profiles.items()}
    retired: list[JsonValue] = []
    for cal in await admin.list_calibrators(op):
        if cal["status"] in RETIRABLE and (cal["profile_id"], cal["profile_version"]) not in live:
            await admin.calibrator_retire(op, str(cal["id"]), reason="profile_version_retired")
            retired.append(str(cal["id"]))
    return retired


async def config_rollback(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    """Re-propose a superseded version; another operator approves it."""
    admin = await ctx.admin()
    await admin.config_rollback(await ctx.operator(admin), args.version)
    return {"version": args.version, "status": "proposed"}


async def config_list(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    admin = await ctx.admin()
    return {"data": [plain(r) for r in await admin.list_config_versions(await ctx.operator(admin))]}


async def prices_import(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    """Validate a prices.yaml and store it as the next `platform.price_catalog` version (prices_ref db:<n>)."""
    try:
        doc = yaml.safe_load(_read(args.file))
        if not isinstance(doc, dict):
            raise CliError("invalid_prices", "the prices file is not a mapping")
        parse_price_catalog(doc)
    except PriceCatalogError as exc:
        raise CliError("invalid_prices", "; ".join(exc.errors)) from None
    except yaml.YAMLError:
        raise CliError("invalid_prices", "the prices file is not YAML") from None
    body = json.loads(json.dumps(doc, default=str))  # dates as ISO strings, the JSON body of the catalog row
    digest = "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    admin = await ctx.admin()
    version = await admin.import_prices(await ctx.operator(admin), digest, body)
    return {"version": version, "prices_ref": f"db:{version}", "digest": digest}


async def calibrators_list(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    admin = await ctx.admin()
    rows = await admin.list_calibrators(await ctx.operator(admin), scope_key=args.scope, status=args.status)
    return {"data": [plain(r) for r in rows]}


async def calibrators_transition(ctx: AdminContext, args: argparse.Namespace) -> JsonValue:
    """`approve` (operator A), `activate` (operator B != A), `retire` (`--reason manual|profile_version_retired`)."""
    admin = await ctx.admin()
    op = await ctx.operator(admin)
    if args.action == "approve":
        await admin.calibrator_approve(op, args.calibrator_id)
    elif args.action == "activate":
        await admin.calibrator_activate(op, args.calibrator_id)
    else:
        await admin.calibrator_retire(op, args.calibrator_id, reason=args.reason)
    return {"calibrator_id": args.calibrator_id, "action": args.action}
