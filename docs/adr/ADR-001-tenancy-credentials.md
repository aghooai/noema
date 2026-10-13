# ADR-001: Tenancy and credential model (STS, ExternalId, Mantle auth)

- Status: adopted by IMPL-007 (Wave A, 2026-10-09; reviewed in Wave A2). The D2 "per call" reading (Decision 1) awaits owner acknowledgement (OWNER-CHECKPOINT §a item 1). §3 rotation amended in Wave C2a (2026-10-10): two CLI steps (`rotate`, `promote`).
- Owners: security architect (author), identity (implements `identity/sts.py`, `identity/secrets.py`), adapters (consumes `TenantCredentials`).
- Resolves: gap #7 as **IMPL-007** in [`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md) (canonical; the security analyst's `security.md`, with its ids IMPL-SEC-07 and IMPL-SEC-N6, was never written). Related: IMPL-076 (adapters fetch credentials per call; error taxonomy), spec-02 §3 (frozen Protocol text), spec-09 (security spec).
- Refs: README.md:38 (D2), 01-feasibility-study.md:71 and :300, 02-architecture.md:136, :162, :203, :850, :879, 04-caching-routing-security.md:104, :191, :212, :215-222, 06-validation-plan.md:181, :217, 08-technical-reference.md:321-392, :419, :488, 05-implementation-plan.md:73.

## Context

D2 gives each tenant its own AWS account. Every Bedrock call runs under that account's credentials. The data plane and the job runner assume the tenant role `AghooBedrockInvoke` with an `ExternalId` (08:321-350).

The docs conflict on how often AssumeRole is called:
- "per-call" in 04:191, 01:300 and README:38;
- "cached until 5 min before expiry" in 02:203, 02:879 and 08:488.

Calling STS once per model call would add 15 or more STS round trips to each turn (15 decision calls plus attempts, 04:276). That breaks the 25 ms overhead SLO (08:467), and STS AssumeRole is rate-limited per account.

Mantle (DeepSeek logprobs) is another open point. SigV4 with assumed-role credentials is UNVERIFIED (04:222, 02:850). This workspace has only shown that Mantle accepts a Bedrock API key as `Authorization: Bearer`.

Phase 1 constraints:
- no IAM or AWS resource changes;
- STS is simulated with moto;
- live smoke uses ambient credentials only, with at most 60 calls.

## Decision

### 1. One cached STS session per (pod, tenant). "Per call" means per-call credentials, not per-call AssumeRole

Every model call (decision members, generation attempts, verification, labeling) uses the credentials of the tenant that owns the request. Each pod caches the STS session in memory only, per tenant:

| Parameter | Value | Note |
|---|---|---|
| `DurationSeconds` | 3600 | The ≤1 h maximum for role chaining (the workload role is itself assumed), 02:136 |
| Soft refresh | `expires_at − 600 s` | The first caller in this window starts one background refresh and keeps using the current credentials. This keeps the refresh off the hot path (02:203) |
| Hard refresh | `expires_at − 300 s` | Credentials in this window are never handed out. Callers wait on one single-flight refresh (08:488) |
| Negative cache | 10 s per tenant per pod | After a failure, callers get the same typed error without another STS call. This bounds STS traffic to 1 call per 10 s per tenant per pod |
| STS client | regional endpoint of the platform region (`us-east-1`); `retries={"mode": "standard", "max_attempts": 1}`; connect timeout 1 s, read timeout 2 s | "No retry" (04:104, 06:181) |
| `RoleSessionName` | `noema-{deployment}-{pod_id}` truncated to 64 chars, charset `[\w+=,.@-]` | CloudTrail attribution per pod (04:191) |
| Storage | process memory only | Never written to Redis, Postgres, disk, logs or traces. Core dumps are off (`RLIMIT_CORE=0`, set at process start) |

This is an interpretation of the D2 text, not a change to D2. One account per tenant and tenant credentials on every call both still hold. Doc patch (IMPL-007; applied through the combined rev-6 payload, spec-08 §14.9): 04:191, 01:300 and README:38 become "per-tenant STS session (cached ≤1 h, refreshed 5–10 min before expiry) used for every call".

### 2. Fail closed

Any failure to obtain tenant credentials raises `TenantAccessMisconfigured(cause)`. The API maps it to 503 `upstream_error` / `tenant_access_misconfigured` with `retryable: false` (03:58):
- there is no in-request retry;
- there is no fallback to other credentials, ambient credentials or another tenant;
- the run emits an `audit`/alert event and counts `aghoo_tenant_access_failures_total{tenant,cause}`.

| `cause` (in `error.details.cause`) | Trigger | STS called? |
|---|---|---|
| `role_not_allowlisted` | `invoke_role_arn` doesn't fully match `^arn:aws:iam::(\d{12}):role/AghooBedrockInvoke$`, or group 1 ≠ `aws_account_id` (mirrors the workload policy resource, 08:330) | no |
| `external_id_unavailable` | Secrets Manager error, or the secret is missing | no |
| `external_id_invalid` | The secret value doesn't fully match `^aghoo-{re.escape(tenant_id)}-[A-Za-z0-9]{32}$`. This catches a ref that points at another tenant's secret | no |
| `access_denied` | STS `AccessDenied`, `InvalidClientTokenId`, `ExpiredToken`, `RegionDisabledException`, `MalformedPolicyDocument` | yes (once) |
| `sts_unavailable` | STS throttling, 5xx or a timeout. Fail closed per 04:104. While the cached credentials are still before the hard refresh, a failed *soft* refresh doesn't fail requests | yes (once) |
| `account_mismatch` | The account parsed from `AssumedRoleUser.Arn` ≠ `aws_account_id`; or, at an adapter call, `creds.tenant_id`/`creds.account_id` ≠ the request's tenant (see 4) | — |
| `model_access_denied` | Bedrock `AccessDeniedException` for a model (from the adapter, 04:104). The broker cache is not invalidated | — |
| `ambient_account_mismatch` | AmbientBroker: the caller account ≠ the tenant's `aws_account_id` | — |

Bedrock `ExpiredTokenException` / `UnrecognizedClientException` → the adapter calls `broker.invalidate(tenant_id)` and fails that call as `access_denied`. The next call re-assumes the role. `invalidate` evicts only an entry at least 60 s old, so a Bedrock-side rejection loop costs at most 1 AssumeRole per minute per tenant per pod.

### 3. ExternalIds

- **Format**: `aghoo-<tenant_id>-<32 base62 chars>` (≈190 bits). The workload policy accepts `aghoo-*` (08:331); the tenant trust policy uses `StringEquals` on the exact value (08:347).
- **Storage**: Secrets Manager `platform/tenants/<tenant_id>/external-id`, referenced as `aws-sm://platform/tenants/<tenant_id>/external-id` (yaml:10). It's read at every session refresh (04:221) and held in memory next to the session.
- **Minting**: `noema-admin tenants external-id create --tenant <id>` writes the secret (moto locally) and prints the value once, for the operator who configures the trust policy (IaC, out of scope).
- **Rotation** (two CLI steps, never automatic; amended in Wave C2a):
  1. `noema-admin tenants external-id rotate --tenant <id>` stages the new value as `AWSPENDING` (brokers keep reading `AWSCURRENT`) and prints it once.
  2. After the IaC adds it to the trust policy next to the current value, `noema-admin tenants external-id promote --tenant <id>` moves `AWSCURRENT` to it (Secrets Manager labels the old one `AWSPREVIOUS`).
  3. The old value leaves the trust policy (IaC) once every broker has refreshed: one session lifetime plus the 300 s secrets cache.

### 4. Account id asserted on every adapter call

```python
# noema/core/protocols/credentials.py (frozen in B1)
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from noema.core.secret import SecretStr          # repr/str -> "**********"

@dataclass(frozen=True, slots=True)
class AwsCredentials:
    access_key_id: str
    secret_access_key: SecretStr
    session_token: SecretStr | None
    expires_at: datetime | None                  # None only for AmbientBroker static creds

@dataclass(frozen=True, slots=True)
class MantleAuth:
    mode: Literal["sigv4", "bearer"]
    bearer_token: SecretStr | None               # set iff mode == "bearer"
    expires_at: datetime | None

@dataclass(frozen=True, slots=True)
class TenantCredentials:
    tenant_id: str
    account_id: str                              # parsed from AssumedRoleUser.Arn (StsBroker) or GetCallerIdentity (Ambient)
    region: str                                  # tenants.<id>.home_region; never taken from ~/.aws/config
    aws: AwsCredentials
    mantle: MantleAuth
    expires_at: datetime | None
    source: Literal["sts", "ambient"]

class CredentialBroker(Protocol):
    async def get(self, tenant_id: str) -> TenantCredentials: ...   # raises TenantAccessMisconfigured
    def invalidate(self, tenant_id: str) -> None: ...

def assert_creds_for(creds: TenantCredentials, tenant_id: str, aws_account_id: str) -> None:
    """Called by every adapter immediately before signing or sending any byte."""
    if creds.tenant_id != tenant_id or creds.account_id != aws_account_id:
        raise TenantAccessMisconfigured(cause="account_mismatch")
```

The adapters call `broker.get(principal.tenant_id)` on every model call (a memory lookup), then `assert_creds_for(creds, principal.tenant_id, snapshot.tenant(principal.tenant_id).aws_account_id)`. A mismatch increments `aghoo_security_violations_total{kind="account_mismatch"}` and emits the `account_mismatch` security event (spec-09 §1) through the adapters' optional `SecurityEventSink` (identity's `SecurityEvents`, `details.source = "adapter"`; the audit row has no `request_id`, which `ModelRequest` does not carry; C2a). Connection pools are keyed by `(tenant_id, account_id, adapter)` (08:488).

Per-tenant aiobotocore clients get their credentials from the broker through an `AioRefreshableCredentials`:
- Its refresh callback is bound to one `tenant_id`. It calls `broker.get(tenant_id)` and `assert_creds_for`, then returns `{access_key, secret_key, token, expiry_time}`. The pool therefore survives credential rotation.
- Its refresh windows must match the broker: a subclass sets `_advisory_refresh_timeout = soft_refresh_s` (600) and `_mandatory_refresh_timeout = hard_refresh_s` (300). With botocore's defaults (900/600), a session inside the broker's soft window (exp − 10 … −5 min) would fail botocore's mandatory refresh ("refreshed credentials are still expired").
- The client is always built with that credentials object and an explicit `region_name`. It never resolves the default chain (env, `~/.aws`, IMDS). On the dev host, ambient `AWS_*` variables would otherwise sign tenant calls with the platform account, and `assert_creds_for` couldn't see it (SEC-STS-15).
- Mantle (httpx) reads `creds.mantle` from the same `broker.get` result on every call.

### 5. StsBroker

```python
# noema/identity/sts.py (identity role)
class StsBroker(CredentialBroker):
    def __init__(self, *, config: ConfigRegistry, secrets: SecretsProvider, sts: StsClient, clock: Clock,
                 mantle_mode: Literal["sigv4", "bearer_short_term"], deployment: str, pod_id: str,
                 duration_s: int = 3600, soft_refresh_s: int = 600, hard_refresh_s: int = 300,
                 negative_ttl_s: float = 10.0) -> None: ...
    async def start(self) -> None: ...          # lifespan; no STS call (sessions are created lazily)
    async def get(self, tenant_id: str) -> TenantCredentials: ...
    def invalidate(self, tenant_id: str) -> None: ...   # sync; no-op if the entry is < 60 s old
    async def aclose(self) -> None: ...          # cancels background refreshes (lifespan)

class StsClient(Protocol):                       # thin aiobotocore wrapper; fake in unit tests
    async def assume_role(self, *, role_arn: str, external_id: str, session_name: str,
                          duration_s: int) -> AssumeRoleResult: ...   # AssumeRoleResult(creds, assumed_role_arn)
```

`get()` algorithm:
1. A cached entry before the hard refresh is returned. If it's inside the soft window, no refresh is running and `now ≥ entry.next_refresh_at`, one background refresh starts. A failed background refresh sets `entry.next_refresh_at = now + negative_ttl_s`, keeps the cached entry, and counts `aghoo_tenant_access_failures_total{cause}`; callers are not failed. So a broken STS gets at most 1 call per 10 s per tenant per pod from the soft window too.
2. Otherwise, a negative entry that hasn't expired re-raises its stored error.
3. Otherwise, take the per-tenant `anyio.Lock`, re-check the cache, then `_refresh()`:
   1. local role-ARN check;
   2. ExternalId read and format check;
   3. one AssumeRole;
   4. parse the account from `AssumedRoleUser.Arn` and assert it;
   5. build `MantleAuth`;
   6. store the entry.

   A failure stores a negative entry and raises.
4. 1,000 concurrent callers for a cold tenant produce exactly one AssumeRole (single flight).

### 6. Mantle authentication

| `credentials.mantle_auth` | How | Status | Requires |
|---|---|---|---|
| `bearer_short_term` (**Phase 1 default**) | A short-term Bedrock API key, presigned offline from the assumed-role credentials with the pinned `aws-bedrock-token-generator` library (no network call). Valid until `min(creds.expires_at, 12 h)`, cached in `MantleAuth`, sent as `Authorization: Bearer` | Bearer auth on Mantle is live-verified in this workspace, with a long-term key. Short-term keys from *assumed-role* credentials are UNVERIFIED | `bedrock:CallWithBearerToken` in the tenant role (doc patch to 08 §5, UNVERIFIED) |
| `sigv4` | SigV4 with the assumed-role credentials. Service name UNVERIFIED (adapters determine it) | UNVERIFIED (04:222, 02:850) | the Mantle IAM action, UNVERIFIED (08:376) |

Never allowed:
- long-term Bedrock API keys, outside AmbientBroker dev and live-smoke mode (04:222).

Why bearer is the default:
- it's the only Mantle auth with live evidence here;
- both modes are implemented and selectable by config.

The live smoke spends 2 of its 60 calls on probing Mantle auth with ambient IAM-user credentials:
1. a short-term token generated from them;
2. SigV4.

Results go to `reports/live/mantle-auth.json`. Phase 0 decides the final default for assumed roles. The wire mock accepts both modes and asserts the configured one.

### 7. AmbientBroker (dev and live smoke only)

- The constructor raises `ForbiddenInEnvironment` if `NOEMA_ENV ∉ {"dev", "test"}`, so it can't run in staging or prod. The live smoke runs with `NOEMA_ENV=dev` plus `NOEMA_LIVE=1`.
- At `start()` it resolves the default credential chain exactly once (it is the only component that does; §4), then calls `sts:GetCallerIdentity` once with an explicit region from the tenant config (the host's `~/.aws/config` default region `us-west` is invalid, 03-env). It records the caller `Account`. `StsBroker` and `AmbientBroker` both expose `start()`/`aclose()`, which the app lifespan calls.
- `get(tenant_id)` raises `ambient_account_mismatch` unless `tenant.aws_account_id == caller_account`. The dev config maps one tenant `t_dev` to the ambient account.
- Mantle in ambient mode is `bearer` from `AWS_BEARER_TOKEN_BEDROCK`. It's read once and never logged or printed (03-env "Never print it").
- `source="ambient"` appears on every span (`aghoo.credentials.source`), so live-smoke traffic is distinguishable.

### 8. Endpoint overrides

Endpoint overrides are process settings, not tenant config: the env var `NOEMA_AWS_ENDPOINT_OVERRIDES` holds a JSON object whose keys come from the closed set {`sts`, `secretsmanager`, `s3`, `bedrock-runtime`, `mantle`} and whose values are `http(s)` URLs (the wire mock, moto, minio; spec-10 §2.1). They are honoured only when `NOEMA_ENV ∈ {"dev", "test"}`. Anywhere else a non-empty value makes `composition.build()` refuse to start (spec-01 §3): an override in prod could send tenant credentials to an attacker-controlled endpoint. Unknown keys are a startup error in every environment.

## How tests simulate cross-account access with moto

Moto 5.2.3 `sts.assume_role` doesn't evaluate trust policies or `ExternalId` ("the role does not need to exist", `moto/sts/models.py`). It does record `external_id` on the `AssumedRole`, and it switches to the target account for later calls. So:
- AWS-side enforcement is a **Phase 0 live gate** (05 task 0.1), and is reported *not run* here;
- our side is tested as follows.

| Fixture | Purpose |
|---|---|
| `moto_server` | `moto.server.ThreadedMotoServer` on port 0, in-process, so the tests can read `moto.sts.models.sts_backends` / `moto.iam.models.iam_backends`. aiobotocore talks to it over HTTP, which avoids the in-process `mock_aws` + aiobotocore incompatibility |
| `two_tenants` | Tenant `t_a` → account `111111111111`, `t_b` → `222222222222`. Each gets an IAM role `AghooBedrockInvoke` whose trust policy has the platform data-plane role and `StringEquals sts:ExternalId`. Secrets live in the platform account `123456789012` |
| `trust_enforcer` (`tests/fakes/sts_trust.py`) | Registers a `before-call.sts.AssumeRole` handler on the broker's client. It loads the target role's trust policy from the moto IAM backend, checks `Principal.AWS` ∋ the caller ARN and `StringEquals sts:ExternalId`, and raises `botocore.exceptions.ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole")` otherwise. This simulates AWS for our code paths |
| `FakeStsClient` (`tests/fakes`) | Unit-level `StsClient` with the same trust-table semantics, a call counter and controllable latency/failures |

| Test id | Scenario | Expected |
|---|---|---|
| SEC-STS-01 | Happy path, `t_a` | AssumeRole called with the role ARN, the exact ExternalId (asserted via `sts_backends[...].assumed_roles[-1].external_id`), `DurationSeconds=3600` and the session-name pattern; `creds.account_id == "111111111111"` |
| SEC-STS-02 | 1,000 concurrent `get("t_a")`, cold | exactly 1 AssumeRole |
| SEC-STS-03 | Fake clock at `exp − 9 min` | returns the cached creds immediately; exactly 1 background refresh |
| SEC-STS-04 | Fake clock at `exp − 4 min` | callers wait; 1 refresh; new creds |
| SEC-STS-05 | Wrong ExternalId (right prefix, wrong random part); `trust_enforcer` denies | 503 `tenant_access_misconfigured`, `retryable: false`, cause `access_denied`; exactly 1 STS call across 50 requests within 10 s (negative cache); wire mock receives 0 requests |
| SEC-STS-06 | `t_b.external_id_ref` points at `t_a`'s secret | cause `external_id_invalid`; 0 STS calls |
| SEC-STS-07 | Non-allowlisted ARN: role `Admin` in the right account, or `AghooBedrockInvoke` in another account (the config checker already rejects the latter, check_config.py:293) | cause `role_not_allowlisted`; 0 STS calls |
| SEC-STS-08 | Adapter gets `t_b` creds for a `t_a` request (broker stub swaps them) | `account_mismatch` before any wire byte; mock request count 0; violation counter +1 |
| SEC-STS-09 | Secrets Manager down (moto stopped) | cause `external_id_unavailable`; no fallback creds |
| SEC-STS-10 | Trust policy lists only `aghoo-jobs`, but the API's data-plane principal calls | `access_denied` (confused-deputy check of the trust principal) |
| SEC-STS-11 | AmbientBroker with `NOEMA_ENV=prod` | constructor raises; with moto `GetCallerIdentity` = `123456789012` and tenant account `111111111111` → `ambient_account_mismatch` |
| SEC-STS-12 | Full STS + adapter suite with log/trace/Redis/DB capture | 0 matches for the AWS key id, session token and Bedrock key patterns (09-security §4) |
| SEC-STS-13 | `bearer_short_term` | token expiry ≤ creds expiry; the token never appears in captured telemetry; re-minted after the creds refresh |
| SEC-STS-14 | Endpoint override set with `NOEMA_ENV=prod` | startup refuses |
| SEC-STS-15 | `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_PROFILE` set to a third account in the test process; a `t_a` Converse call | the wire mock's recorded SigV4 `Credential=` access key id equals the `t_a` broker key; mantle uses `t_a`'s token |
| SEC-STS-16 | STS fails (throttling) during the soft window; 200 calls over 30 s on a fake clock | every call is served with the cached creds; ≤ 4 AssumeRole calls (1 per 10 s); then, at `exp − 4 min`, callers get `sts_unavailable` (negative-cached) |
| SEC-STS-17 | aiobotocore client with the broker-backed refreshable credentials, fake clock stepping from `exp − 16 min` to `exp − 4 min` | no botocore `RuntimeError`; exactly one blocking refresh, at `exp − 5 min` |

Fault gate 06:181 ("AssumeRole failure / AccessDenied → typed 503, no retry, 100% classification") = SEC-STS-05/06/07/09 plus the adapter's `AccessDeniedException` mock case (`model_access_denied`). Tests live in `tests/integration/identity/test_credential_broker.py` (01-04, 06, 09, 11, 13, 16, 17) and `tests/security/test_sts.py` (05, 07, 08, 10, 12, 14, 15); owners identity and qa-security.

## Consequences

- The overhead SLO holds: STS is off the hot path except for one blocking refresh per tenant per pod per hour, and only if the soft refresh failed.
- A pod holds every active tenant's session in memory. A pod compromise exposes ≤1 h credentials for those tenants, limited to `bedrock:Invoke*` on the allowlisted ARNs (08:352-386). Accepted. Same exposure as the per-call design, which also holds credentials in memory during calls.
- Revoking a tenant (offboarding, 08:410) takes up to 1 h to reach already-issued sessions. The runbook adds an IAM `aws:TokenIssueTime` deny for an immediate cut (IaC; documented in 08 §6, doc patch).
- ExternalId rotation is two CLI steps (`rotate`, `promote`) around two IaC trust-policy changes (§3).

## Alternatives rejected

- **Per-call AssumeRole** (literal 04:191): 15+ STS calls per turn, the STS rate limit, latency.
- **Sharing sessions across pods through Redis**: violates "memory only" (02:136) and adds a credential store to protect.
- **Automatic fallback from SigV4 to Bearer on Mantle errors**: hides misconfiguration. Fail closed instead.
- **Static per-tenant Bedrock API keys**: long-term credentials (04:222 forbids).
