"""What one reservation debits (spec-11 §1.2): its GCRA buckets, its slot sets and their lease, from the current
config, the principal and the work graph.

- `key:{key_id}:rpm|tpm` when the principal has a key, `prj:{tenant}:{project}:rpm|tpm` when it has a project
  (system principals have neither); limits: the tenant override, else `platform.limits`.
- `mdl:{tenant}:{alias}:rpm|tpm` for every alias of the graph with `tenants.<t>.limits.model_quotas[alias]`: sliding
  windows (`gcra.quota_bucket`), so no 60 s window admits more than the quota (Wave E2 QL-2).
- RPM buckets debit calls (`total_calls`, or the alias's calls); TPM buckets debit `tokens_in + tokens_out`, or the
  alias's `tokens_by_model`.
- Labeling graphs (any `labeling` call) keep `LABELING_HEADROOM` free in every bucket they debit and in every
  slot set they take a place in (`Slot.limit`), so best-effort labeling never fills a bulkhead user traffic needs.
- Slots: the project's stream slots (`RedisKeys.streams(tenant, project)`, one member per stream, cap
  `concurrent_streams_per_project`) and one bulkhead place per quota'd alias (`RedisKeys.streams(tenant,
  "mdl:" + alias)`, cap `ceil(rpm / 6)`: 10 s of quota). The lease ends 30 s after the request's deadline.
"""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from typing import Final

from noema.config.models import PlatformLimitsCfg, TenantLimitsCfg
from noema.core.clock import Deadline
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.ops import WorkGraph
from noema.core.redis_keys import RedisKeys
from noema.ops.admission.gcra import LABELING_HEADROOM, Bucket, Slot, admission_bucket, quota_bucket

LEASE_GRACE_MS: Final = 30_000  # a dead pod's slot frees itself this long after the request's deadline
MAX_REQUEST_MS: Final = 600_000  # the largest Aghoo-Deadline-Ms: bounds the lease of a reservation without deadline
BULKHEAD_WINDOW_DIVISOR: Final = 6  # a bulkhead holds 10 s of the per-minute model quota (spec-11 §1.2)


@dataclass(frozen=True, slots=True)
class Plan:
    """One reservation's debits. `member` is its unique id in the slot sets."""

    tenant_id: str
    graph: WorkGraph
    buckets: tuple[Bucket, ...]
    slots: tuple[Slot, ...]
    member: str
    labeling: bool
    lease_ms: float  # wall ms when the slots expire on their own: the deadline + LEASE_GRACE_MS

    @property
    def slot_keys(self) -> frozenset[str]:
        return frozenset(s.key for s in self.slots)


def _limit(tenant: TenantLimitsCfg | None, platform: PlatformLimitsCfg, name: str) -> int:
    override = getattr(tenant, name) if tenant is not None else None
    value: int = override if override is not None else getattr(platform, name)
    return value


def build_plan(
    snapshot: ConfigSnapshot,
    keys: RedisKeys,
    principal: Principal,
    graph: WorkGraph,
    deadline: Deadline | None,
    now_ms: float,
) -> Plan:
    """The buckets and slots of one reservation (see the module docstring)."""
    platform = snapshot.platform.limits
    tenant = snapshot.tenant(principal.tenant_id).limits
    labeling = any(kind == "labeling" for _, kind in graph.calls)
    headroom = LABELING_HEADROOM if labeling else 0.0
    tokens = graph.tokens_in + graph.tokens_out
    buckets: list[Bucket] = []
    scopes: list[tuple[str, str]] = []
    if principal.key_id is not None:
        scopes.append((f"key:{principal.key_id}", "_per_key"))
    if principal.project_id:
        scopes.append((f"prj:{principal.tenant_id}:{principal.project_id}", "_per_project"))
    for prefix, suffix in scopes:
        buckets.append(
            admission_bucket(
                f"{prefix}:rpm",
                "requests",
                _limit(tenant, platform, "rpm" + suffix),
                graph.total_calls,
                headroom=headroom,
            )
        )
        buckets.append(
            admission_bucket(
                f"{prefix}:tpm", "tokens", _limit(tenant, platform, "tpm" + suffix), tokens, headroom=headroom
            )
        )
    member = f"{principal.request_id}:{secrets.token_hex(6)}"
    slots: list[Slot] = []
    if graph.streams > 0:
        members = (member,) if graph.streams == 1 else tuple(f"{member}#{i}" for i in range(graph.streams))
        cap = _limit(tenant, platform, "concurrent_streams_per_project")
        slots.append(Slot(keys.streams(principal.tenant_id, principal.project_id), cap, members, headroom))
    quotas = tenant.model_quotas if tenant is not None else {}
    calls_by_alias: dict[str, int] = {}
    for (alias, _kind), n in graph.calls.items():
        calls_by_alias[alias] = calls_by_alias.get(alias, 0) + n
    for alias in sorted(calls_by_alias.keys() | graph.tokens_by_model.keys()):
        quota = quotas.get(alias)
        if quota is None:
            continue
        prefix = f"mdl:{principal.tenant_id}:{alias}"
        n_calls, n_tokens = calls_by_alias.get(alias, 0), graph.tokens_by_model.get(alias, 0)
        buckets.append(quota_bucket(f"{prefix}:rpm", "requests", quota.rpm, n_calls, alias=alias, headroom=headroom))
        buckets.append(quota_bucket(f"{prefix}:tpm", "tokens", quota.tpm, n_tokens, alias=alias, headroom=headroom))
        cap = math.ceil(quota.rpm / BULKHEAD_WINDOW_DIVISOR)
        slots.append(Slot(keys.streams(principal.tenant_id, "mdl:" + alias), cap, (member,), headroom))
    lease_ms = now_ms + (deadline.remaining_ms() if deadline is not None else MAX_REQUEST_MS) + LEASE_GRACE_MS
    return Plan(principal.tenant_id, graph, tuple(buckets), tuple(slots), member, labeling, lease_ms)
