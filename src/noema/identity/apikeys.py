"""API and operator keys (spec-09 §2, IMPL-008, IMPL-044): the token formats, minting and the peppered HMAC.

- API key: `ak_{live|test}_{key_id}.{secret}`; operator key (CLI only): `op_{key_id}.{secret}`. `key_id` is a
  26-character Crockford ULID, `secret` 43 base64url characters (32 random bytes).
- Stored hash: `hmac-sha256:v{p}:{hex}` = HMAC-SHA256(pepper_v{p}, f"{key_id}.{secret}").
- Pepper: the platform secret `noema/api-key-pepper`, a JSON object `{"version": p, "pepper": "<>= 32 chars>"}`.
  The current version is the secret's current value; the previous one is its `AWSPREVIOUS` version (Secrets Manager
  moves that label on every new value). Only these two are ever loaded; any other `p` fails verification.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets as _random
from dataclasses import dataclass
from typing import Final, Literal

import anyio
import orjson

from noema.core.errors import ErrorCode, NoemaError, SecretMissing
from noema.core.protocols.credentials import SecretsProvider
from noema.core.secret import SecretStr

PEPPER_REF: Final = "noema/api-key-pepper"
PREVIOUS_VERSION: Final = "AWSPREVIOUS"
MIN_PEPPER_CHARS: Final = 32
SECRET_BYTES: Final = 32
KeyEnv = Literal["live", "test"]

_ULID: Final = r"[0-9A-HJKMNP-TV-Z]{26}"
_B64URL43: Final = r"[A-Za-z0-9_-]{43}"
# spec-09 §2 step 0: the scheme word is case-insensitive, the rest exact.
BEARER_RE: Final = re.compile(rf"(?i:bearer) ak_(live|test)_({_ULID})\.({_B64URL43})")
OPERATOR_RE: Final = re.compile(rf"op_({_ULID})\.({_B64URL43})")
HASH_RE: Final = re.compile(r"hmac-sha256:v([1-9][0-9]{0,8}):([0-9a-f]{64})")


@dataclass(frozen=True, slots=True)
class ParsedKey:
    """A syntactically valid presented key (the secret stays wrapped)."""

    env: KeyEnv
    key_id: str
    secret: SecretStr


@dataclass(frozen=True, slots=True)
class Pepper:
    """One pepper version (`key` is the HMAC key)."""

    version: int
    key: SecretStr


@dataclass(frozen=True, slots=True)
class PepperRing:
    """The current pepper and, during a rotation, the previous one. `partial`: the previous version could not be read
    (a backend failure, not a missing version), so `previous` is the one loaded before, if any; reload soon."""

    current: Pepper
    previous: Pepper | None = None
    partial: bool = False

    def get(self, version: int) -> Pepper | None:
        """The pepper of `version` if it is the current or the previous one, else None."""
        if version == self.current.version:
            return self.current
        if self.previous is not None and version == self.previous.version:
            return self.previous
        return None


def parse_bearer(header: str) -> ParsedKey | None:
    """Parse `Authorization: Bearer ak_...` (spec-09 §2 step 0); None for anything else (OIDC tokens included)."""
    match = BEARER_RE.fullmatch(header)
    if match is None:
        return None
    env: KeyEnv = "live" if match.group(1) == "live" else "test"
    return ParsedKey(env=env, key_id=match.group(2), secret=SecretStr(match.group(3)))


def parse_operator_key(token: str) -> tuple[str, SecretStr] | None:
    """`op_{key_id}.{secret}` -> (key_id, secret), else None."""
    match = OPERATOR_RE.fullmatch(token.strip())
    if match is None:
        return None
    return match.group(1), SecretStr(match.group(2))


def new_secret() -> str:
    """43 base64url characters from 32 random bytes."""
    return base64.urlsafe_b64encode(_random.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def _digest(pepper: Pepper, key_id: str, secret: str) -> bytes:
    msg = f"{key_id}.{secret}".encode()
    return hmac.new(pepper.key.get_secret_value().encode(), msg, hashlib.sha256).digest()


def hash_key(pepper: Pepper, key_id: str, secret: str) -> str:
    """The stored form `hmac-sha256:v{p}:{hex}`."""
    return f"hmac-sha256:v{pepper.version}:{_digest(pepper, key_id, secret).hex()}"


def hash_version(key_hash: str) -> int | None:
    """The pepper version named by a stored hash, or None if it is malformed."""
    match = HASH_RE.fullmatch(key_hash)
    return None if match is None else int(match.group(1))


def verify_key(ring: PepperRing, key_hash: str, key_id: str, secret: SecretStr) -> Pepper | None:
    """The pepper that verifies `key_id.secret` against `key_hash` (constant-time compare), or None: a malformed
    hash, a pepper version that is neither current nor previous, or a wrong secret."""
    match = HASH_RE.fullmatch(key_hash)
    if match is None:
        return None
    pepper = ring.get(int(match.group(1)))
    if pepper is None:
        return None
    expected = bytes.fromhex(match.group(2))
    ok = hmac.compare_digest(expected, _digest(pepper, key_id, secret.get_secret_value()))
    return pepper if ok else None


def mint_api_key(ring: PepperRing, env: KeyEnv, key_id: str) -> tuple[SecretStr, str]:
    """A new API key for `key_id`: (the token, printed once; its stored hash under the current pepper)."""
    secret = new_secret()
    return SecretStr(f"ak_{env}_{key_id}.{secret}"), hash_key(ring.current, key_id, secret)


def mint_operator_key(ring: PepperRing, key_id: str) -> tuple[SecretStr, str]:
    """A new operator key: (`op_{key_id}.{secret}`, printed once; its stored hash)."""
    secret = new_secret()
    return SecretStr(f"op_{key_id}.{secret}"), hash_key(ring.current, key_id, secret)


def parse_pepper(raw: bytes) -> Pepper:
    """Decode one pepper secret value; NoemaError(internal_error) when malformed (never echoing it)."""
    try:
        value = orjson.loads(raw)
    except orjson.JSONDecodeError:
        value = None
    version = value.get("version") if isinstance(value, dict) else None
    pepper = value.get("pepper") if isinstance(value, dict) else None
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or not isinstance(pepper, str)
        or len(pepper) < MIN_PEPPER_CHARS
    ):
        raise NoemaError(ErrorCode.INTERNAL_ERROR, "The API key pepper secret is malformed.")
    return Pepper(version=version, key=SecretStr(pepper, register=True))


def pepper_value(version: int, pepper: str) -> bytes:
    """The secret value of one pepper version (provisioning and tests)."""
    return orjson.dumps({"version": version, "pepper": pepper})


async def load_peppers(
    secrets: SecretsProvider, ref: str = PEPPER_REF, *, fallback: PepperRing | None = None
) -> PepperRing:
    """The current pepper (required: its NoemaError(internal_error) propagates) and the previous one, read
    concurrently. No `AWSPREVIOUS` version, a malformed one or one not older than the current one means no previous
    pepper. A backend failure on the previous read keeps the newest pepper of `fallback` (the ring loaded before)
    that is older than the current one, and marks the ring `partial`."""
    reads: dict[str | None, bytes | NoemaError] = {}

    async def read(version: str | None) -> None:
        try:
            reads[version] = await secrets.get(ref, version=version)
        except NoemaError as exc:
            reads[version] = exc

    async with anyio.create_task_group() as tg:
        tg.start_soon(read, None)
        tg.start_soon(read, PREVIOUS_VERSION)
    raw_current, raw_previous = reads[None], reads[PREVIOUS_VERSION]
    if isinstance(raw_current, NoemaError):
        raise raw_current
    current = parse_pepper(raw_current)
    if isinstance(raw_previous, NoemaError):
        if isinstance(raw_previous, SecretMissing):
            return PepperRing(current=current)
        older = [] if fallback is None else [fallback.current, fallback.previous]
        kept = max((p for p in older if p is not None and p.version < current.version), key=_version, default=None)
        return PepperRing(current=current, previous=kept, partial=True)
    try:
        previous: Pepper | None = parse_pepper(raw_previous)
    except NoemaError:
        previous = None
    if previous is not None and previous.version >= current.version:
        previous = None
    return PepperRing(current=current, previous=previous)


def _version(pepper: Pepper) -> int:
    return pepper.version
