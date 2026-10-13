"""Every Redis key and pub/sub channel name (spec-02 §13).

All keys start with the namespace `ns`: "noema:" in production, "ns:<agent>:<worker>:<uuid>:" in tests, so parallel
test sessions never collide and teardown can `SCAN MATCH {ns}* + UNLINK`. Pub/sub payloads are orjson objects;
every payload that concerns a tenant carries `tenant_id`, and receivers drop messages for another tenant
(spec-09 §7).
"""

from __future__ import annotations

import re
import zlib
from typing import Final

OUTBOX_SHARDS: Final = 8
_GLOB_SPECIAL: Final = re.compile(r"([*?\[\]\\])")


def _glob_escape(s: str) -> str:
    return _GLOB_SPECIAL.sub(r"\\\1", s)


class RedisKeys:
    """Key builder for one namespace. Raises ValueError for an empty namespace or one not ending in ":"."""

    __slots__ = ("ns",)

    def __init__(self, ns: str) -> None:
        if not ns or not ns.endswith(":"):
            raise ValueError("RedisKeys namespace must be non-empty and end with ':'")
        self.ns: Final[str] = ns

    # streams and keys -------------------------------------------------------------------------------------------
    def turn_events(self, turn_id: str) -> str:
        """Stream: the per-turn event-log mirror (IMPL-021)."""
        return f"{self.ns}turn:{turn_id}:events"

    def outbox(self, shard: int) -> str:
        """Stream: one outbox shard (consumer group "drain"); `shard` = `outbox_shard(tenant_id)`."""
        if not 0 <= shard < OUTBOX_SHARDS:
            raise ValueError(f"outbox shard must be in [0, {OUTBOX_SHARDS})")
        return f"{self.ns}outbox:{shard}"

    @staticmethod
    def outbox_shard(tenant_id: str) -> int:
        """`crc32(tenant_id) % 8`."""
        return zlib.crc32(tenant_id.encode()) % OUTBOX_SHARDS

    def l2(self, tenant_id: str, digest: str) -> str:
        """String: one L2 decision-cache artifact."""
        return f"{self.ns}dec:l2:{tenant_id}:{digest}"

    def l2_pattern(self, tenant_id: str) -> str:
        """SCAN MATCH pattern of every L2 key of one tenant (`DecisionCache.purge_tenant`); glob characters in the
        tenant id are escaped."""
        return f"{_glob_escape(self.ns)}dec:l2:{_glob_escape(tenant_id)}:*"

    def gcra(self, bucket: str) -> str:
        """String: a GCRA theoretical arrival time in ms (bucket names: spec-11 §1.2)."""
        return f"{self.ns}gcra:{bucket}"

    def streams(self, tenant_id: str, project_id: str) -> str:
        """ZSET reservation_id -> lease expiry: the project's concurrent-stream slots (spec-11 §1.2)."""
        return f"{self.ns}streams:{tenant_id}:{project_id}"

    def retry_budget(self, tenant_account: str, model: str) -> str:
        """The coarse (1 s) shared mirror of a local retry budget (IMPL-078)."""
        return f"{self.ns}retry:{tenant_account}:{model}"

    def labeling_daily(self, tenant_id: str, day: str) -> str:
        """Counter: labeling items sampled for one tenant on one UTC day (`day` = "YYYYMMDD"); INCR by the
        `labeling.item` job (EXPIRE 2 days), so `labeling.sample.max_per_day_per_tenant` is exact cluster-wide."""
        return f"{self.ns}labeling:daily:{tenant_id}:{day}"

    def session_queue(self, session_id: str) -> str:
        """FIFO tickets of turns waiting for a session (spec-04 §2)."""
        return f"{self.ns}session:{session_id}:queue"

    def session_pending(self, session_id: str) -> str:
        """Set while a turn finalize waits in the outbox (IMPL-022); TTL 86400 s."""
        return f"{self.ns}session:{session_id}:pending"

    def turn_owner(self, turn_id: str) -> str:
        """Running-turn registry entry {pod, tenant_id, project_id, principal_id, session_owner_principal_id};
        TTL 60 s (spec-04 §11)."""
        return f"{self.ns}turn:{turn_id}:owner"

    def turn_attach(self, turn_id: str) -> str:
        """Last delivered event time of a turn (abandon watchdog)."""
        return f"{self.ns}turn:{turn_id}:attach"

    # pub/sub channels -------------------------------------------------------------------------------------------
    def breaker_events(self) -> str:
        """Channel {model, region, tenant_account?, state, until_ms?} (adapters)."""
        return f"{self.ns}ch:breaker_events"

    def calibrators_changed(self) -> str:
        """Channel {scope_key, n} (notify bridge)."""
        return f"{self.ns}ch:calibrators_changed"

    def keys_revoked(self) -> str:
        """Channel {tenant_id, key_id} (notify bridge)."""
        return f"{self.ns}ch:keys_revoked"

    def config_changed(self) -> str:
        """Channel {version} (notify bridge)."""
        return f"{self.ns}ch:config_changed"

    def idem_done(self, scope_hash: str, key: str) -> str:
        """Channel {tenant_id, record_id} (IdempotencyStore)."""
        return f"{self.ns}ch:idem_done:{scope_hash}:{key}"

    def session_released(self, session_id: str) -> str:
        """Channel {tenant_id, session_id, last_seq} (turns)."""
        return f"{self.ns}ch:session_released:{session_id}"

    def turn_cancel(self) -> str:
        """Channel {tenant_id, turn_id} (turns)."""
        return f"{self.ns}ch:turn_cancel"

    def file_events(self, file_id: str) -> str:
        """Channel {tenant_id, file_id, status} (media-pipeline; FileService.watch)."""
        return f"{self.ns}ch:file_events:{file_id}"
