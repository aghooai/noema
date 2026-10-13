"""The `noema-admin` process context (IMPL-044): environment, the operator check, connections and secrets.

Environment:
- `NOEMA_ENV` (`test|dev|staging|prod`; unset = `prod`, the strictest rules);
- `NOEMA_OPERATOR_KEY` (`op_{key_id}.{secret}`): every command except `bootstrap` and `config validate`;
- `NOEMA_CLI_DSN`: the `noema_cli` connection; `NOEMA_DB_OWNER_DSN`: the `noema_owner` connection (`bootstrap`);
- `NOEMA_AWS_ENDPOINT_OVERRIDES`: the `secretsmanager` endpoint, honored only with NOEMA_ENV dev/test (ADR-001 §8).
The CLI never uses `noema.composition`: it opens its own `noema_cli` connection (spec-01 §2 rule 6).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, cast
from urllib.parse import urlparse

from noema.core.clock import SystemClock
from noema.core.ids import UlidIdGen
from noema.core.secret import SecretStr
from noema.core.settings import Env
from noema.core.values import JsonValue
from noema.db.engine import ROLE_CLI, ROLE_OWNER, DbConnection, connect
from noema.db.system import PgAdminCalls
from noema.identity.apikeys import PepperRing, hash_key, hash_version, load_peppers, parse_operator_key, verify_key
from noema.identity.aws import PLATFORM_REGION
from noema.identity.secrets import SecretsManagerProvider

ENVS: Final = frozenset({"test", "dev", "staging", "prod"})
OVERRIDE_KEYS: Final = frozenset({"sts", "secretsmanager", "s3", "bedrock-runtime", "mantle"})

log = logging.getLogger("noema.identity.cli")


class CliError(Exception):
    """A refusal with an exit code (1 refused, 3 operator authentication failed) and a reason for the output.
    `output`, when set, is printed on stdout before the refusal (a command that failed after a step that cannot be
    repeated, e.g. a written secret)."""

    def __init__(self, reason: str, message: str, *, exit_code: int = 1, output: JsonValue | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.exit_code = exit_code
        self.output = output


def parse_env(environ: Mapping[str, str]) -> Env:
    env = environ.get("NOEMA_ENV") or "prod"
    if env not in ENVS:
        raise CliError("invalid_argument", f"NOEMA_ENV must be one of {sorted(ENVS)}")
    return cast(Env, env)


def endpoint_overrides(environ: Mapping[str, str], env: Env) -> dict[str, str]:
    """`NOEMA_AWS_ENDPOINT_OVERRIDES`, validated like the process settings (ADR-001 §8)."""
    raw = (environ.get("NOEMA_AWS_ENDPOINT_OVERRIDES") or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise CliError("invalid_argument", "NOEMA_AWS_ENDPOINT_OVERRIDES is not JSON") from None
    if not isinstance(value, dict) or set(value) - OVERRIDE_KEYS:
        raise CliError("invalid_argument", "NOEMA_AWS_ENDPOINT_OVERRIDES has unknown keys or is not an object")
    for url in value.values():
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise CliError("invalid_argument", "endpoint overrides must be http(s) URLs")
    if env not in ("dev", "test"):
        raise CliError("forbidden", f"endpoint overrides are not allowed with NOEMA_ENV={env} (ADR-001 §8)")
    return {str(k): str(v) for k, v in value.items()}


@dataclass
class AdminContext:
    """Everything a command needs; connections and secrets are opened lazily and closed by `aclose()`."""

    environ: Mapping[str, str]
    env: Env
    overrides: Mapping[str, str]
    ids: UlidIdGen = field(default_factory=lambda: UlidIdGen(SystemClock()))
    _conns: list[DbConnection] = field(default_factory=list)
    _secrets: SecretsManagerProvider | None = None
    _ring: PepperRing | None = None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> AdminContext:
        env = parse_env(environ)
        return cls(environ=environ, env=env, overrides=endpoint_overrides(environ, env))

    def _dsn(self, name: str) -> SecretStr:
        value = self.environ.get(name)
        if not value:
            raise CliError("invalid_argument", f"{name} is not set")
        return SecretStr(value)

    async def admin(self, *, owner: bool = False) -> PgAdminCalls:
        """Admin calls over a `noema_cli` connection (`noema_owner` for bootstrap)."""
        name, role = ("NOEMA_DB_OWNER_DSN", ROLE_OWNER) if owner else ("NOEMA_CLI_DSN", ROLE_CLI)
        conn = await connect(self._dsn(name), expected_role=role)
        self._conns.append(conn)
        return PgAdminCalls(conn)

    @property
    def secrets(self) -> SecretsManagerProvider:
        if self._secrets is None:
            self._secrets = SecretsManagerProvider(
                SystemClock(), region=PLATFORM_REGION, endpoint_url=self.overrides.get("secretsmanager")
            )
        return self._secrets

    async def peppers(self) -> PepperRing:
        if self._ring is None:
            self._ring = await load_peppers(self.secrets)
        return self._ring

    async def operator(self, admin: PgAdminCalls) -> str:
        """The authenticated operator id (NOEMA_OPERATOR_KEY), also proved on `admin`'s connection, so the database
        accepts this operator id on it (and no other); CliError(exit 3) otherwise, never echoing it."""
        parsed = parse_operator_key(self.environ.get("NOEMA_OPERATOR_KEY") or "")
        refused = CliError("operator_invalid", "Operator authentication failed.", exit_code=3)
        if parsed is None:
            raise refused
        key_id, secret = parsed
        row = await admin.lookup_operator(key_id)
        if row is None or row.get("revoked_at") is not None:
            raise refused
        ring = await self.peppers()
        key_hash = str(row["key_hash"])
        pepper = verify_key(ring, key_hash, key_id, secret)
        if pepper is None:
            if ring.partial and ring.previous is None and (hash_version(key_hash) or 0) < ring.current.version:
                raise CliError("unavailable", "Secrets Manager is unavailable (the previous pepper could not be read).")
            raise refused
        op = str(row["id"])
        # the database checks the key too (0003, Wave E): this connection can act as this operator only
        await admin.prove_operator(key_id, secret.get_secret_value(), pepper.version, pepper.key.get_secret_value())
        if pepper.version != ring.current.version:  # re-hash under the current pepper (requests item 5)
            new_hash = hash_key(ring.current, key_id, secret.get_secret_value())
            if await admin.rehash_operator(op, new_hash, ring.current.version):
                log.info("operator key %s re-hashed with pepper v%d", key_id, ring.current.version)
                current = ring.current
                await admin.prove_operator(
                    key_id, secret.get_secret_value(), current.version, current.key.get_secret_value()
                )
        return op

    async def aclose(self) -> None:
        for conn in self._conns:
            await conn.close()
        self._conns.clear()
        if self._secrets is not None:
            await self._secrets.aclose()


def plain(row: object) -> JsonValue:
    """A JSON-printable copy of an admin row (timestamps ISO 8601, networks and other values as strings)."""
    if isinstance(row, dict):
        return {str(k): plain(v) for k, v in row.items()}
    if isinstance(row, list | tuple):
        return [plain(v) for v in row]
    if isinstance(row, datetime):
        return row.isoformat()
    if isinstance(row, str | int | float | bool) or row is None:
        return row
    return str(row)
