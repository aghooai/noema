"""Liveness and readiness (owner: ops-ledger; spec-11 §7). No authentication and no error envelope.

- `GET /healthz`: 200 while the event loop answers (liveness).
- `GET /readyz`: 200 when the process has a loaded config snapshot and is not draining, else 503. Draining starts
  with the first shutdown signal (`ProcessRuntime.draining`, spec-11 §7 step 1), so load balancers stop routing
  new requests before uvicorn stops accepting them. A degraded dependency (Postgres, Redis, S3) does not make the
  pod unready (06 §7 availability: `store: false` turns and `/v1/decisions` keep working). Neither probe has a body
  (the contract declares none; spec-11 §7, ops-ledger request 2: an unauthenticated probe discloses no dependency
  states, and probes read only the status).

`/metrics` is not served here: `composition.start` exposes it on `NOEMA_METRICS_PORT` (telemetry).
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import APIRouter, Request
from starlette.responses import Response

from noema.api import deps

router = APIRouter()
log: Final = logging.getLogger("noema.ops.health")


def ready(svc: deps.ApiServices) -> bool:
    """True when the config snapshot is loaded (and valid: the registry only holds checked snapshots) and the
    process is not draining."""
    if svc.runtime.draining:
        return False
    try:
        snapshot = svc.config.current()
    except Exception as exc:  # no snapshot loaded yet, or the registry failed to load a valid one
        log.warning("readyz: no usable config snapshot (%s)", type(exc).__name__)
        return False
    return bool(snapshot.digest)


@router.get("/healthz", name="healthz")
async def healthz(request: Request) -> Response:
    """healthz: 200 while the event loop answers."""
    return Response(status_code=200)


@router.get("/readyz", name="readyz")
async def readyz(request: Request, svc: deps.ServicesDep) -> Response:
    """readyz: 200 ready, 503 draining or without a config snapshot."""
    return Response(status_code=200 if ready(svc) else 503)
