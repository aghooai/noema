"""`noema-admin` (IMPL-044): the only admin surface in Phase 1. `python -m noema.identity.cli <command> ...`.

Commands: `bootstrap`, `operators create|revoke`, `config validate|propose|approve|rollback|list`,
`tenants external-id create|rotate|promote`, `projects create|list`, `principals create`, `keys create|list|revoke`,
`calibrators list|approve|activate|retire`, `prices import`. Output: one JSON object on stdout; refusals one JSON
object `{"error": reason, "message": ...}` on stderr (database and Secrets Manager failures included, never with
the DSN). Exit codes: 0 done, 1 refused, 2 usage, 3 operator authentication failed. Secrets (operator/API keys,
ExternalIds) are printed once and never logged.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TextIO

import anyio
import asyncpg
import orjson
from botocore.exceptions import BotoCoreError, ClientError

from noema.core.errors import NoemaError
from noema.core.values import JsonValue
from noema.db.engine import WrongDatabaseRole
from noema.db.system import AdminError
from noema.identity.cli import platform_cmds as pc
from noema.identity.cli import tenancy as tc
from noema.identity.cli.context import AdminContext, CliError

Command = Callable[[AdminContext, argparse.Namespace], Awaitable[JsonValue]]
log = logging.getLogger("noema.identity.cli")


def _sub(
    parent: argparse._SubParsersAction[argparse.ArgumentParser], name: str, fn: Command, help_: str
) -> argparse.ArgumentParser:
    parser = parent.add_parser(name, help=help_)
    parser.set_defaults(command=fn)
    return parser


def build_parser() -> argparse.ArgumentParser:
    """The full command tree (see the module docstring)."""
    root = argparse.ArgumentParser(prog="noema-admin", description="Noema operator CLI (Phase 1).")
    top = root.add_subparsers(dest="group", required=True)
    p = _sub(top, "bootstrap", tc.bootstrap, "create the first operator (NOEMA_DB_OWNER_DSN; one-time)")
    p.add_argument("--operator", required=True, help="operator name")

    ops = top.add_parser("operators", help="operator keys").add_subparsers(dest="action", required=True)
    _sub(ops, "create", tc.operators_create, "create an operator").add_argument("--name", required=True)
    _sub(ops, "revoke", tc.operators_revoke, "revoke an operator").add_argument("operator_id")

    cfg = top.add_parser("config", help="platform config versions").add_subparsers(dest="action", required=True)
    _sub(cfg, "validate", pc.config_validate, "validate a config file (local)").add_argument("file")
    _sub(cfg, "propose", pc.config_propose, "store a validated config as proposed").add_argument("file")
    p = _sub(cfg, "approve", pc.config_approve, "activate a proposed version (second operator)")
    p.add_argument("version", type=int)
    p.add_argument("--file", help="the reviewed config file (its digest must match); default: the stored body")
    _sub(cfg, "rollback", pc.config_rollback, "re-propose a superseded version").add_argument("version", type=int)
    _sub(cfg, "list", pc.config_list, "list config versions")

    tenants = top.add_parser("tenants", help="tenant AWS access").add_subparsers(dest="what", required=True)
    ext = tenants.add_parser("external-id", help="tenant ExternalIds (ADR-001 §3)").add_subparsers(
        dest="action", required=True
    )
    for action, text in (
        ("create", "write the tenant's first ExternalId"),
        ("rotate", "stage a new ExternalId (AWSPENDING) for the trust policy"),
        ("promote", "make the staged ExternalId current (after the trust policy lists both)"),
    ):
        _sub(ext, action, tc.external_id, text).add_argument("--tenant", required=True)

    prj = top.add_parser("projects", help="projects").add_subparsers(dest="action", required=True)
    p = _sub(prj, "create", tc.projects_create, "create a project")
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", required=True)
    p.add_argument(
        "--default-data-class", default="internal", choices=("public", "internal", "confidential", "restricted")
    )
    _sub(prj, "list", tc.projects_list, "list a tenant's projects").add_argument("--tenant", required=True)

    prn = top.add_parser("principals", help="principals").add_subparsers(dest="action", required=True)
    p = _sub(prn, "create", tc.principals_create, "create a principal")
    for flag in ("--tenant", "--project", "--subject", "--roles"):
        p.add_argument(flag, required=True)

    keys = top.add_parser("keys", help="API keys").add_subparsers(dest="action", required=True)
    p = _sub(keys, "create", tc.keys_create, "mint an API key (printed once)")
    for flag in ("--tenant", "--project", "--principal", "--scopes"):
        p.add_argument(flag, required=True)
    p.add_argument("--env", choices=("live", "test"), default="live")
    p.add_argument("--expires", help="ISO 8601 expiry")
    p.add_argument("--ip-allow", help="comma-separated CIDRs")
    p = _sub(keys, "list", tc.keys_list, "list a tenant's keys (never hashes)")
    p.add_argument("--tenant", required=True)
    p.add_argument("--project")
    _sub(keys, "revoke", tc.keys_revoke, "revoke a key").add_argument("key_id")

    cal = top.add_parser("calibrators", help="calibrator artifacts (ADR-003)").add_subparsers(
        dest="action", required=True
    )
    p = _sub(cal, "list", pc.calibrators_list, "list artifacts")
    p.add_argument("--scope", help="'platform' or a tenant id")
    p.add_argument("--status")
    for action in ("approve", "activate", "retire"):
        p = _sub(cal, action, pc.calibrators_transition, f"{action} an artifact")
        p.add_argument("calibrator_id")
        if action == "retire":
            p.add_argument("--reason", choices=("manual", "profile_version_retired"), default="manual")

    prices = top.add_parser("prices", help="price catalog").add_subparsers(dest="action", required=True)
    _sub(prices, "import", pc.prices_import, "import a prices.yaml as the next catalog version").add_argument("file")
    return root


async def run(args: argparse.Namespace, environ: Mapping[str, str]) -> JsonValue:
    """Run one parsed command; the context is closed afterwards."""
    ctx = AdminContext.from_environ(environ)
    try:
        command: Command = args.command
        return await command(ctx, args)
    finally:
        await ctx.aclose()


async def amain(argv: Sequence[str] | None, *, environ: Mapping[str, str], stdout: TextIO, stderr: TextIO) -> int:
    """Parse, run and print one command (inside a running event loop); returns the exit code."""
    args = build_parser().parse_args(argv)
    try:
        result = await run(args, environ)
    except CliError as exc:
        if exc.output is not None:  # done in part: what was done (e.g. a written ExternalId) is still printed once
            stdout.write(orjson.dumps(exc.output, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode() + "\n")
        return _refuse(stderr, exc.reason, exc.message, exc.exit_code)
    except AdminError as exc:
        return _refuse(stderr, exc.reason, exc.message, 3 if exc.reason == "operator_invalid" else 1)
    except NoemaError as exc:
        return _refuse(stderr, exc.code.value, exc.message, 1)
    except WrongDatabaseRole:
        return _refuse(stderr, "invalid_argument", "The DSN logs in as the wrong database role.", 1)
    except asyncpg.InvalidAuthorizationSpecificationError:  # wrong password or unknown role (28xxx)
        return _refuse(stderr, "invalid_argument", "The database refused the DSN credentials.", 1)
    except asyncpg.InvalidCatalogNameError:
        return _refuse(stderr, "invalid_argument", "The DSN names a database that does not exist.", 1)
    except asyncpg.PostgresError as exc:
        return _refuse(stderr, "unavailable", f"The database refused the command (SQLSTATE {exc.sqlstate}).", 1)
    except (asyncpg.InterfaceError, OSError):
        return _refuse(stderr, "unavailable", "The database connection failed.", 1)
    except (BotoCoreError, ClientError):
        return _refuse(stderr, "unavailable", "Secrets Manager is unavailable.", 1)
    stdout.write(orjson.dumps(result, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode() + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The process entry point (`python -m noema.identity.cli`); returns the exit code."""
    code: int = anyio.run(lambda: amain(argv, environ=os.environ, stdout=sys.stdout, stderr=sys.stderr))
    return code


def _refuse(err: TextIO, reason: str, message: str, code: int) -> int:
    err.write(orjson.dumps({"error": reason, "message": message}).decode() + "\n")
    return code
