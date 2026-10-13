"""Key formats, minting and the peppered HMAC (spec-09 §2, IMPL-008)."""

from __future__ import annotations

import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.secrets import FakeSecretsProvider
from tests.unit.identity.helpers import KEY_ID, PEPPER_V1, PEPPER_V2, RING_V2, pepper, pepper_secrets

from noema.core.errors import ErrorCode, NoemaError
from noema.core.secret import SecretStr
from noema.identity.apikeys import (
    PEPPER_REF,
    PREVIOUS_VERSION,
    PepperRing,
    hash_key,
    hash_version,
    load_peppers,
    mint_api_key,
    mint_operator_key,
    new_secret,
    parse_bearer,
    parse_operator_key,
    parse_pepper,
    pepper_value,
    verify_key,
)

SECRET = "Zm9vYmFyLXNlY3JldC1wYXJ0LW9mLWFuLWFwaS1rZXk"


def test_bearer_format() -> None:
    parsed = parse_bearer(f"Bearer ak_live_{KEY_ID}.{SECRET}")
    assert parsed is not None and parsed.env == "live" and parsed.key_id == KEY_ID
    assert parsed.secret.get_secret_value() == SECRET
    assert parse_bearer(f"bEaReR ak_test_{KEY_ID}.{SECRET}") is not None  # the scheme word is case-insensitive
    for bad in (
        f"ak_test_{KEY_ID}.{SECRET}",
        f"Bearer AK_test_{KEY_ID}.{SECRET}",
        f"Bearer ak_prod_{KEY_ID}.{SECRET}",
        f"Bearer ak_test_{KEY_ID.lower()}.{SECRET}",
        f"Bearer ak_test_{KEY_ID[:-1]}I.{SECRET}",  # I is not Crockford
        f"Bearer ak_test_{KEY_ID}.{SECRET}=",
        f"Bearer  ak_test_{KEY_ID}.{SECRET}",
        f"Bearer ak_test_{KEY_ID}.{SECRET}\n",
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
    ):
        assert parse_bearer(bad) is None, bad


def test_minted_keys_round_trip() -> None:
    token, key_hash = mint_api_key(RING_V2, "test", KEY_ID)
    parsed = parse_bearer(f"Bearer {token.get_secret_value()}")
    assert parsed is not None and len(parsed.secret.get_secret_value()) == 43
    assert key_hash.startswith("hmac-sha256:v2:") and hash_version(key_hash) == 2
    assert verify_key(RING_V2, key_hash, KEY_ID, parsed.secret) == RING_V2.current
    assert verify_key(RING_V2, key_hash, KEY_ID, SecretStr(SECRET)) is None
    assert verify_key(RING_V2, key_hash, "01J9ZQ7ABCDEFGHJKMNPQRSTVX", parsed.secret) is None
    op, op_hash = mint_operator_key(RING_V2, KEY_ID)
    parsed_op = parse_operator_key(op.get_secret_value())
    assert parsed_op is not None and parsed_op[0] == KEY_ID
    assert verify_key(RING_V2, op_hash, KEY_ID, parsed_op[1]) is not None
    assert parse_operator_key(f"ak_test_{KEY_ID}.{SECRET}") is None
    assert len({new_secret() for _ in range(50)}) == 50


def test_verification_uses_the_named_pepper_version() -> None:
    old = hash_key(pepper(1, PEPPER_V1), KEY_ID, SECRET)
    assert verify_key(RING_V2, old, KEY_ID, SecretStr(SECRET)) == RING_V2.previous
    ring_v3 = PepperRing(current=pepper(3, "x" * 40), previous=pepper(2, PEPPER_V2))
    assert verify_key(ring_v3, old, KEY_ID, SecretStr(SECRET)) is None  # older than the previous version
    assert verify_key(RING_V2, "hmac-sha256:v2:zz", KEY_ID, SecretStr(SECRET)) is None
    assert verify_key(RING_V2, "argon2id$...", KEY_ID, SecretStr(SECRET)) is None
    assert hash_version("hmac-sha256:v0:" + "0" * 64) is None


def test_pepper_secret_format() -> None:
    assert parse_pepper(pepper_value(4, PEPPER_V2)).version == 4
    for raw in (
        b"not json",
        b"[]",
        b'{"version": 0, "pepper": "' + b"x" * 40 + b'"}',
        b'{"version": 1, "pepper": "short"}',
    ):
        with pytest.raises(NoemaError) as err:
            parse_pepper(raw)
        assert err.value.code is ErrorCode.INTERNAL_ERROR and PEPPER_V2 not in err.value.message


async def test_load_peppers_current_and_previous() -> None:
    clock = FakeClock()
    ring = await load_peppers(pepper_secrets(clock, current=2))
    assert ring.current.version == 2 and ring.previous is not None and ring.previous.version == 1
    only = await load_peppers(pepper_secrets(clock, current=1))
    assert only.current.version == 1 and only.previous is None
    with pytest.raises(NoemaError) as err:
        await load_peppers(FakeSecretsProvider(clock))
    assert err.value.code is ErrorCode.INTERNAL_ERROR
    weird = FakeSecretsProvider(clock)
    weird.put(PEPPER_REF, pepper_value(5, PEPPER_V1), version="AWSPREVIOUS")
    weird.put(PEPPER_REF, pepper_value(2, PEPPER_V2))
    assert (await load_peppers(weird)).previous is None  # a "previous" newer than current is ignored
    assert not only.partial and not ring.partial


class PreviousDown:
    """A provider whose AWSPREVIOUS reads fail as a backend outage (throttling, timeout, 5xx)."""

    def __init__(self, inner: FakeSecretsProvider, message: str = "The secrets backend is unavailable.") -> None:
        self.inner = inner
        self.message = message

    async def get(self, ref: str, *, version: str | None = None) -> bytes:
        if version == PREVIOUS_VERSION:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, self.message)
        return await self.inner.get(ref, version=version)


async def test_previous_backend_failure_is_partial_and_keeps_the_fallback() -> None:
    """A missing AWSPREVIOUS means no previous pepper; a failed read of it is not "missing": the ring is partial and
    keeps the newest older pepper of the ring loaded before (its current one after a rotation)."""
    clock = FakeClock()
    v3 = PepperRing(current=pepper(3, "c" * 40), previous=pepper(2, PEPPER_V2))
    secrets = FakeSecretsProvider(clock)
    secrets.put(PEPPER_REF, pepper_value(2, PEPPER_V2), version=PREVIOUS_VERSION)
    secrets.put(PEPPER_REF, pepper_value(3, "c" * 40))
    down = PreviousDown(secrets)
    cold = await load_peppers(down)
    assert cold.current.version == 3 and cold.previous is None and cold.partial
    same = await load_peppers(down, fallback=v3)
    assert same.partial and same.previous is not None and same.previous.version == 2
    before_rotation = PepperRing(current=pepper(2, PEPPER_V2), previous=pepper(1, PEPPER_V1))
    rotated = await load_peppers(down, fallback=before_rotation)
    assert rotated.partial and rotated.previous is not None and rotated.previous.version == 2
    newer = PepperRing(current=pepper(4, "d" * 40))
    assert (await load_peppers(down, fallback=newer)).previous is None  # never one >= current
    missing = await load_peppers(pepper_secrets(clock, current=1), fallback=v3)
    assert missing.previous is None and not missing.partial  # ResourceNotFound-style: no previous version
    # the type decides (core.errors.SecretMissing), not the text: a plain internal_error with the same message is an
    # outage (identity.md item 6)
    assert (await load_peppers(PreviousDown(secrets, "A platform secret is missing."), fallback=v3)).partial
