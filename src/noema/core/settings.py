"""`ProcessSettings`: the validated process environment of spec-05 §5 (parsed by `composition.process_settings`).

Domains read it as `services.settings` (pod id, object bucket, endpoint overrides), never `os.environ` directly.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from noema.core.secret import SecretStr

Env = Literal["test", "dev", "staging", "prod"]
SandboxBackend = Literal["remote", "bwrap", "inproc_rlimit"]  # the media-worker's backends (ADR-002 §7)


@dataclass(frozen=True, slots=True)
class ProcessSettings:
    """The process variables (spec-05 §5 plus the connection settings).

    `pod_id`: this process's identity in leases and owners (`<pod>:<request_id>` idempotency owners, `<pod>:<pid>`
    job workers, `turn_owner`, the STS broker); NOEMA_POD_ID, default the host name. `s3_bucket`: NOEMA_S3_BUCKET,
    default `noema-<env>` (spec-03 §2). `redis_url`: NOEMA_REDIS_URL. `db_dsn_ref`: the platform secret holding the
    noema_app DSN (NOEMA_DB_DSN_REF, default `noema/db-dsn`, spec-09 §4). `drain_delay_s`: seconds between SIGTERM
    (draining, `/readyz` 503) and uvicorn's own shutdown (NOEMA_DRAIN_DELAY_S, spec-11 §7). `dev_api_key`: dev/test
    only, a key registered on the seed authenticator (NOEMA_DEV_API_KEY). The media-worker's sandbox (ADR-002 §6-§7):
    `sandbox_backend` (NOEMA_SANDBOX_BACKEND, default `remote`; `bwrap` dev/test, `inproc_rlimit` test only),
    `spool_dir` (NOEMA_SPOOL_DIR, default `/spool`) and `sandbox_socket` (NOEMA_SANDBOX_SOCKET, sandboxd's socket,
    default `<spool_dir>/sandboxd.sock`); both paths are absolute. `redis_max_connections`: the connections of the
    process's Redis pool, pub/sub ones included (NOEMA_REDIS_MAX_CONNECTIONS, default 256; a caller waits at most
    0.5 s for a free one, Wave E3). `token_estimator_table`: an absolute path to a TokenEstimator refit table, the
    JSON `noema.ops.cost.estimator_fit` writes (NOEMA_TOKEN_ESTIMATOR_TABLE; unset: the shipped
    `TOKEN_ESTIMATOR_V1`, spec-11 §2)."""

    env: Env
    endpoint_overrides: Mapping[str, str]
    contract_validate: bool
    expected_pods: int
    metrics_port: int | None
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    agent_id: str | None
    pod_id: str = "local"
    s3_bucket: str = "noema-test"
    redis_url: str = "redis://127.0.0.1:26379/0"
    db_dsn_ref: str = "noema/db-dsn"
    drain_delay_s: float = 0.0
    dev_api_key: SecretStr | None = None
    sandbox_backend: SandboxBackend = "remote"
    spool_dir: str = "/spool"
    sandbox_socket: str = "/spool/sandboxd.sock"
    redis_max_connections: int = 256
    token_estimator_table: str | None = None
