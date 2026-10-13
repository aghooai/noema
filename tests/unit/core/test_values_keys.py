"""Value types, Principal and RedisKeys (spec-02 §1, §13)."""

from __future__ import annotations

import zlib
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from noema.core.principal import ALL_SCOPES, Principal, TenantRef, system_principal
from noema.core.redis_keys import OUTBOX_SHARDS, RedisKeys
from noema.core.values import Cost, Degradation, DocumentBlock, ImageBlock, Message, TextBlock, UsageRecord

pytestmark = pytest.mark.unit


def test_usage_record_addition() -> None:
    a = UsageRecord(input_tokens=10, output_tokens=5, cached_input_tokens=2, cache_write_input_tokens=1)
    b = UsageRecord(input_tokens=1, output_tokens=2, reasoning_tokens=3, tokens_estimated=True)
    total = a + b
    assert total == UsageRecord(11, 7, 2, 1, 3, True)
    assert (a + a).tokens_estimated is False


def test_values_are_frozen_and_slotted() -> None:
    for value in (
        UsageRecord(1, 1),
        Cost(Decimal("0.000001"), estimated=True),
        Degradation(from_="native", to="ocr_text", reason="model_capability", part_index=0),
        TextBlock("hi"),
        ImageBlock("png", b"x", 1, 1),
        DocumentBlock("pdf", "doc-1", b"%PDF"),
        Message("user", (TextBlock("hi"),)),
        TenantRef("t_a", "111111111111", "us-east-1"),
    ):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            value.__setattr__(next(iter(type(value).__dataclass_fields__)), None)


def test_system_principal() -> None:
    p = system_principal("t_acme", purpose="labeling", request_id="req_1")
    assert p == Principal(
        tenant_id="t_acme",
        project_id="",
        principal_id="system:labeling",
        key_id=None,
        roles=frozenset({"tenant_admin"}),
        scopes=ALL_SCOPES,
        request_id="req_1",
    )
    assert len(ALL_SCOPES) == 7
    with pytest.raises(ValueError, match="tenant_id"):
        system_principal("", purpose="x", request_id="r")


def test_redis_keys_are_namespaced() -> None:
    keys = RedisKeys("ns:agent:w0:abcd1234:")
    built = [
        keys.turn_events("turn_1"),
        keys.outbox(3),
        keys.l2("t_a", "d1"),
        keys.gcra("key:k1:rpm"),
        keys.streams("t_a", "prj_1"),
        keys.retry_budget("111111111111", "sol-6.1"),
        keys.breaker_events(),
        keys.calibrators_changed(),
        keys.keys_revoked(),
        keys.idem_done("s1", "key-1"),
        keys.session_queue("ses_1"),
        keys.session_released("ses_1"),
        keys.session_pending("ses_1"),
        keys.turn_owner("turn_1"),
        keys.turn_cancel(),
        keys.turn_attach("turn_1"),
        keys.file_events("file_1"),
        keys.config_changed(),
    ]
    assert all(k.startswith("ns:agent:w0:abcd1234:") for k in built)
    assert len(set(built)) == len(built)
    assert keys.l2("t_a", "d1") == "ns:agent:w0:abcd1234:dec:l2:t_a:d1"
    assert keys.l2_pattern("t_a") == "ns:agent:w0:abcd1234:dec:l2:t_a:*"
    assert RedisKeys("noema:").l2("t_a", "x") == "noema:dec:l2:t_a:x"


def test_redis_key_patterns_escape_glob_characters() -> None:
    assert RedisKeys("noema:").l2_pattern("t*[a]?") == r"noema:dec:l2:t\*\[a\]\?:*"


def test_redis_keys_validation() -> None:
    for bad in ("", "noema"):
        with pytest.raises(ValueError, match="namespace"):
            RedisKeys(bad)
    keys = RedisKeys("noema:")
    with pytest.raises(ValueError, match="shard"):
        keys.outbox(OUTBOX_SHARDS)
    assert RedisKeys.outbox_shard("t_acme") == zlib.crc32(b"t_acme") % 8
    assert {RedisKeys.outbox_shard(f"t_{i}") for i in range(200)} == set(range(OUTBOX_SHARDS))
