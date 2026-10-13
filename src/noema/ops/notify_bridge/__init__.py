"""noema.ops.notify_bridge (spec-11 §4): the dedicated LISTEN connection and the pg_notify -> Redis pub/sub bridge."""

from noema.ops.notify_bridge.bridge import PG_CHANNEL, NotifyBridge, redis_channels
from noema.ops.notify_bridge.listener import PgListener

__all__ = ["PG_CHANNEL", "NotifyBridge", "PgListener", "redis_channels"]
