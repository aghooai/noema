"""Security events (spec-09 §1): one content-free log record, a counter and an `audit_event` row.

Names (closed set): `account_mismatch`, `ambient_account_mismatch`, `tenant_access_misconfigured`,
`pubsub_tenant_mismatch` (identity raises these; the `sandbox.*` names belong to media). The audit row goes through
the outbox (`actor_type = 'system'`), so emitting never blocks or fails the caller.
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Final, Literal

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.protocols.storage import Outbox, OutboxRecord
from noema.core.protocols.telemetry import Telemetry
from noema.core.values import JsonValue

SecurityEventName = Literal[
    "account_mismatch", "ambient_account_mismatch", "tenant_access_misconfigured", "pubsub_tenant_mismatch"
]
PLATFORM_TENANT: Final = "__platform__"
ACTOR: Final = "system:identity"

log = logging.getLogger("noema.identity.security")


class SecurityEvents:
    """Emits spec-09 §1 security events. Every dependency is optional except the logger: without telemetry no
    counter is incremented; without an outbox (the CLI) no audit row is written."""

    def __init__(
        self,
        *,
        telemetry: Telemetry | None = None,
        outbox: Outbox | None = None,
        ids: IdGen | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._outbox = outbox
        self._ids = ids
        self._clock = clock

    def emit(
        self,
        name: SecurityEventName,
        *,
        tenant_id: str | None,
        request_id: str | None = None,
        job_id: str | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        """Log, count and audit one event. `details` must hold ids and causes only, never content or secrets."""
        tenant = tenant_id or PLATFORM_TENANT
        ref = f"request_id={request_id}" if request_id else f"job_id={job_id}" if job_id else "request_id="
        log.warning(
            "security name=%s tenant_id=%s %s kind=%s",
            name,
            tenant,
            ref,
            name,
            extra={"event": "security", "code": name, "tenant": tenant},
        )
        if self._telemetry is not None:
            self._telemetry.instruments.add("aghoo_security_violations_total", kind=name)
        if self._outbox is None or self._ids is None or self._clock is None:
            return
        audit_id = self._ids.new("aud")
        at = self._clock.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        payload: dict[str, JsonValue] = {
            "tenant_id": tenant,
            "id": audit_id,
            "actor_type": "system",
            "actor_id": ACTOR,
            "action": f"security.{name}",
            "resource_kind": "security",
            "resource_id": None,
            "details": dict(details or {}),
            "request_id": request_id,
            "at": at,
        }
        self._outbox.put(OutboxRecord(kind="audit_event", id=audit_id, tenant_id=tenant, payload=payload))
