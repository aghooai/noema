"""SE12 / SEC-AUTH-01 on the stack: every spec-09 §2 authentication failure (format, unknown key, bad secret, env
mismatch, revoked, expired, IP not allowed) answers a byte-identical 401 `invalid_api_key` body apart from
`request_id`, on both replicas, and neither the body nor any header echoes the presented key (its key id, its
secret or any 8-character piece of the secret). The presented keys are written to the artifacts so the log scan
(test_zz_secrets_scan.py) checks that no secret half reached the logs either."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import anyio
import orjson
import pytest
from tests.e2e.conftest import Stack
from tests.e2e.suite.kit import Json, admin
from tests.e2e.suite.multipod import keyed, mint_key
from tests.security.artifacts import artifact_dir

from noema.core.principal import ALL_SCOPES

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(180)]
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def random_key(prefix: str = "ak_test_") -> str:
    key_id = "".join(secrets.choice(CROCKFORD) for _ in range(26))
    return f"{prefix}{key_id}.{secrets.token_urlsafe(32)[:43]}"


async def mint(stack: Stack, *extra: str) -> Json:
    who = stack.principal("t_acme", "main", "developer")
    argv = ["keys", "create", "--tenant", "t_acme", "--project", stack.project_id("t_acme", "main")]
    argv += ["--principal", str(who["principal_id"]), "--scopes", ",".join(sorted(ALL_SCOPES)), "--env", "test"]
    return await admin(stack, [*argv, *extra])


def pieces(secret: str) -> set[str]:
    return {secret[i : i + 8] for i in range(len(secret) - 7)}


async def test_every_401_is_identical_and_never_echoes_the_key(stack: Stack) -> None:
    real = str(stack.principal("t_acme", "main", "developer")["key"])
    key_id, secret = real.removeprefix("ak_test_").split(".")
    revoked = str((await mint_key(stack, "t_acme", "main", "developer"))["key"])
    await admin(stack, ["keys", "revoke", revoked.removeprefix("ak_test_").split(".")[0]])
    expires = (datetime.now(UTC) + timedelta(seconds=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
    expiring = str((await mint(stack, "--expires", expires))["key"])
    ip_denied = str((await mint(stack, "--ip-allow", "203.0.113.0/24"))["key"])
    assert (await keyed(stack, 0, expiring).request("GET", "/v1/profiles")).status_code == 200
    await anyio.sleep((datetime.fromisoformat(expires) - datetime.now(UTC)).total_seconds() + 1.0)
    presented = {
        "format": "not-a-key",
        "format_live_short": "ak_live_short.x",
        "unknown_key": random_key(),
        "bad_secret": f"ak_test_{key_id}.{secrets.token_urlsafe(32)[:43]}",
        "env_mismatch": f"ak_live_{key_id}.{secret}",
        "revoked": revoked,
        "expired": expiring,
        "ip_denied": ip_denied,
    }
    bodies: dict[str, Json] = {}
    problems: list[str] = []
    with anyio.fail_after(30):  # revocation reaches both pods within 5 s (cache TTL 60 s otherwise)
        for reason, key in presented.items():
            for replica in (0, 1):
                while True:
                    response = await keyed(stack, replica, key).request("GET", "/v1/profiles")
                    if response.status_code == 401 or reason != "revoked":
                        break
                    await anyio.sleep(0.25)
                body = response.json()
                error = {k: v for k, v in body["error"].items() if k != "request_id"}
                bodies[f"{reason}@{replica}"] = error
                echoed = "\n".join([response.text, *(f"{k}: {v}" for k, v in response.headers.items())])
                secret_half = key.split(".", 1)[-1]
                leaked = [p for p in pieces(secret_half) if p in echoed] if len(secret_half) >= 8 else []
                if response.status_code != 401 or leaked or (len(key) > 20 and key[8:34] in echoed):
                    problems.append(f"{reason}@{replica}: {response.status_code} echoed={len(leaked)}")
    assert problems == []
    reference = bodies["unknown_key@0"]
    assert (reference["code"], reference["message"]) == ("invalid_api_key", "Invalid API key."), reference
    assert {k: v for k, v in bodies.items() if v != reference} == {}
    private = artifact_dir() / "secrets"
    private.mkdir(mode=0o700, exist_ok=True)
    (private / "presented-keys.json").write_bytes(orjson.dumps(presented))
    (private / "presented-keys.json").chmod(0o600)
