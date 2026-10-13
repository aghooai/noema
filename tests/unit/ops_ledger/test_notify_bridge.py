"""NotifyBridge.forward (spec-11 §4): the three channels, payload unchanged, malformed/unknown dropped, Redis down."""

from __future__ import annotations

from typing import Any, cast

import orjson
import pytest
from tests.fakes.clock import FakeClock

from noema.core.redis_keys import RedisKeys
from noema.core.secret import SecretStr
from noema.ops.notify_bridge import NotifyBridge, PgListener

from .redis_stub import MemoryRedis

pytestmark = pytest.mark.unit
KEYS = RedisKeys("ns:t:")


def bridge() -> tuple[NotifyBridge, MemoryRedis]:
    redis = MemoryRedis()
    listener = PgListener(SecretStr("postgresql://unused@127.0.0.1:1/x"), FakeClock())
    return NotifyBridge(listener, cast(Any, redis), KEYS), redis


@pytest.mark.parametrize(
    ("channel", "payload", "target"),
    [
        ("config_changed", {"version": 7}, KEYS.config_changed()),
        ("calibrators_changed", {"scope_key": "t_acme", "n": 3}, KEYS.calibrators_changed()),
        ("keys_revoked", {"tenant_id": "t_acme", "key_id": "01J0"}, KEYS.keys_revoked()),
    ],
)
async def test_each_channel_is_bridged_unchanged(channel: str, payload: dict[str, Any], target: str) -> None:
    b, redis = bridge()
    assert await b.forward(orjson.dumps({"channel": channel, "payload": payload}).decode())
    assert [(c, orjson.loads(m)) for c, m in redis.published] == [(target, payload)]


@pytest.mark.parametrize(
    "raw", ["not json", "[1, 2]", '{"channel": "jobs", "payload": {}}', '{"channel": "config_changed"}', '{"x": 1}']
)
async def test_malformed_or_unknown_is_dropped(raw: str) -> None:
    b, redis = bridge()
    assert not await b.forward(raw)
    assert redis.published == [] and b.forwarded == 0


async def test_redis_down_drops_and_carries_on() -> None:
    b, redis = bridge()
    redis.down = True
    assert not await b.forward('{"channel": "config_changed", "payload": {"version": 2}}')
    redis.down = False
    assert await b.forward('{"channel": "config_changed", "payload": {"version": 3}}')
    assert b.forwarded == 1
