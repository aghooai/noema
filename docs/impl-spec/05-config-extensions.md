# 05 — Configuration extensions (rev 6) and Phase 1 config rules

Wave A, lead architect. Owner: foundation (`src/noema/config/`). Resolutions: IMPL-019, IMPL-026, IMPL-031, IMPL-038, IMPL-039, IMPL-084, ADR-001 §6 and §8, ADR-003 §3. Everything below is **optional with design-value defaults**, so `examples/platform.example.yaml` stays valid unchanged. The JSON Schema text goes into `contracts/config.schema.json` through the combined rev-6 payload (spec-08 §14.4).

## 1. Schema additions (`config.schema.json`)

### 1.1 Top level: `platform`

```json
"platform": { "$ref": "#/$defs/Platform" }
```

```json
"Platform": {
  "type": "object", "additionalProperties": false,
  "description": "Platform-wide operational settings (Phase 1). All optional; defaults are the initial design values.",
  "properties": {
    "limits": { "type": "object", "additionalProperties": false, "properties": {
      "max_json_body_bytes": { "type": "integer", "minimum": 1048576, "maximum": 104857600, "default": 52428800 },
      "rpm_per_key":       { "type": "integer", "minimum": 1, "default": 6000 },
      "tpm_per_key":       { "type": "integer", "minimum": 1, "default": 20000000 },
      "rpm_per_project":   { "type": "integer", "minimum": 1, "default": 30000 },
      "tpm_per_project":   { "type": "integer", "minimum": 1, "default": 100000000 },
      "concurrent_streams_per_project": { "type": "integer", "minimum": 1, "default": 1000 },
      "admission_queue_timeout_ms": { "type": "integer", "minimum": 0, "maximum": 30000, "default": 2000 } } },
    "idempotency": { "type": "object", "additionalProperties": false, "properties": {
      "ttl_s": { "type": "integer", "minimum": 3600, "maximum": 604800, "default": 86400 } } },
    "events": { "type": "object", "additionalProperties": false, "properties": {
      "resume_window_s": { "type": "integer", "minimum": 30, "maximum": 3600, "default": 300 } } },
    "sessions": { "type": "object", "additionalProperties": false, "properties": {
      "queue_timeout_ms": { "type": "integer", "minimum": 0, "maximum": 30000, "default": 2000 } } },
    "decision_cache": { "type": "object", "additionalProperties": false, "properties": {
      "ttl_s": { "type": "integer", "minimum": 0, "maximum": 86400, "default": 600 } } },
    "cost": { "type": "object", "additionalProperties": false, "properties": {
      "cancel_overrun_tokens": { "type": "integer", "minimum": 0, "maximum": 4096, "default": 64 },
      "prices_ref": { "type": "string", "pattern": "^(db:[1-9][0-9]*|file:\\S+)$",
        "description": "Price catalog (IMPL-019): db:<version> of platform.price_catalog, or file:<path> in dev/test only. Absent: every price is TBD (cost_estimated)." } } },
    "media": { "type": "object", "additionalProperties": false, "properties": {
      "inline_wait_ms": { "type": "integer", "minimum": 100, "maximum": 30000, "default": 3000 } } },
    "credentials": { "type": "object", "additionalProperties": false, "properties": {
      "mantle_auth": { "enum": ["bearer_short_term", "sigv4"], "default": "bearer_short_term" } } },
    "data_classes": { "type": "object", "additionalProperties": false,
      "properties": { "public": { "$ref": "#/$defs/DataClassPolicy" }, "internal": { "$ref": "#/$defs/DataClassPolicy" },
                      "confidential": { "$ref": "#/$defs/DataClassPolicy" }, "restricted": { "$ref": "#/$defs/DataClassPolicy" } } }
  }
},
"DataClassPolicy": {
  "type": "object", "additionalProperties": false,
  "properties": {
    "store_content": { "enum": ["full", "redacted", "metadata_only"] },
    "retain_days": { "type": "integer", "minimum": 1, "maximum": 3650 },
    "l2_cache": { "type": "boolean" } }
}
```

RPM and TPM count **every scheduled model call** (04:L276). With the shipped config one turn reserves 28 calls (20 router decision calls, 4 verification calls, 4 generation attempts; spec-11 §1.1) and is refunded to the calls actually made when it completes. The rate defaults above are placeholders: the docs give no values. Operators set real ones per tenant (`tenants.<t>.limits`), and the Phase 1 load test sets its own (200 concurrent streams at about 8 s per turn is ≈ 1,500 turns/min, ≈ 42,000 reserved calls/min, far above `rpm_per_key`).

`data_classes` overrides merge **per field** over the table below: `{"confidential": {"retain_days": 60}}` keeps `store_content: redacted` and `l2_cache: false`.

Data-class defaults (03:L392, 04:L147):

| class | store_content | retain_days | l2_cache |
|---|---|---|---|
| public | full | 90 | true |
| internal | full | 90 | true |
| confidential | redacted | 30 | false |
| restricted | metadata_only | 7 | false |

`l2_cache` may only be true for `public` and `internal` (P1 rule P1-C4); restricted data never enters L2 (04:L160).

### 1.2 `Tenant.limits` (per-tenant overrides; Bedrock quota mirrors)

```json
"limits": { "type": "object", "additionalProperties": false, "properties": {
  "rpm_per_key": { "type": "integer", "minimum": 1 }, "tpm_per_key": { "type": "integer", "minimum": 1 },
  "rpm_per_project": { "type": "integer", "minimum": 1 }, "tpm_per_project": { "type": "integer", "minimum": 1 },
  "concurrent_streams_per_project": { "type": "integer", "minimum": 1 },
  "model_quotas": { "type": "object", "additionalProperties": { "type": "object", "additionalProperties": false,
     "required": ["rpm", "tpm"], "properties": { "rpm": { "type": "integer", "minimum": 1 }, "tpm": { "type": "integer", "minimum": 1 } } },
     "description": "Per model alias: the tenant account's Bedrock quota, so admission throttles before Bedrock does (04:L276)." } } }
```

### 1.3 `Labeling.acceptance` (ADR-003 §3)

```json
"acceptance": { "type": "object", "additionalProperties": false, "properties": {
  "ece_max": { "type": "number", "minimum": 0, "maximum": 0.05, "default": 0.05 },
  "n_min": { "type": "integer", "minimum": 300, "default": 300 },
  "class_n_min": { "type": "integer", "minimum": 30, "default": 30 },
  "consensus_min": { "type": "number", "minimum": 0.6, "maximum": 1, "default": 0.6 },
  "drift_ece_max": { "type": "number", "minimum": 0, "maximum": 1, "default": 0.08 },
  "subset_min_share": { "type": "number", "minimum": 0, "maximum": 1, "default": 0.05 } } }
```

The schema bounds encode the IMPL-039 rule: the config is never looser than the contract constants (python_sdk_types:L21-23). The existing `family_overlap_lift_max` gets `"maximum": 0.10`.

### 1.4 Not added (with reasons)

| Requested | Why not |
|---|---|
| `limits.decision_deadline_max_ms` | dschema already bounds `deadline_ms` to 100–30,000 (IMPL-038) |
| projects, principals, API keys | DB rows created by `noema-admin` (IMPL-044) |
| per-project end-user JWKS | `x-aghoo-end-user` is deferred (IMPL-033) |
| a per-effort reasoning reserve map | decision templates use a code table (spec-06 §5.5) with `reasoning_reserve_tokens` as a floor; it is part of the template, not config (`EFFORT_RESERVE` in `config/models.py`, shared by decision, labeling and admission with `DECISION_PROMPT_OVERHEAD = 600`, spec-11 §1.1) |
| `decision_views.*` | `VIEW_BUDGET_V1` is a code constant versioned with the resolver (IMPL-075) |
| per-route stream timeouts | code table `ROUTE_TIMEOUTS_V1` in `config/models.py`, read per attempt by `ConfigSnapshot.route_timeouts(model_alias, effort)` with the member's effective effort (IMPL-015, IMPL-084), as (connect, first event, first answer, idle) from 04:L127-132: `sol-6.1` at effort `none`/`low`/null (3, 5, 8, 30 s) (the `sol-fast` row); `glm-5.3` (3, 10, 30, 30) (`glm-reason`); every other (model, effort), including `opus-5.5` and Sol at `medium` or higher (the Sol member of `opus-deep` and `glm-reason`), (3, 30, 30, 30) (`opus-deep`). So both `race-sol-glm` members and every fallback member get their own model's row. Phase 0 resets them from p99 |
| a generation `max_tokens` default | code constant `GEN_DEFAULT_MAX_TOKENS = 4096`, used when neither the spec, the member nor `max_output_tokens_cap` sets one (Sol and Opus have no cap; spec-04 §5.1, IMPL-084) |
| budgets, retention policies per project | Phase 2 |
| hierarchical-method deadline or limit keys (a minimum `deadline_ms`, a round split, the 0.9 coverage, the 3-group cap) | none (IMPL-027). Both rounds run inside the request's `deadline_ms` (schema 100–30,000; default = the profile's), and a round that doesn't fit is an `unavailable / deadline_exceeded` answer. `HIER_ROUND1_SHARE = 0.5`, `HIER_EXPAND_UNITS = 9,000` and `HIER_MAX_EXPANDED = 3` are code constants (spec-06 §19). `templates.*.max_choices` (existing; ≤ 16 / 26 by `check_config`) sets `L`, and so which questions are hierarchical |
| an idempotency inline-size or object-store key | none (IMPL-085). `IDEM_INLINE_MAX_BYTES = 1 MiB` is a code constant; larger bodies go to the existing `noema-<env>` bucket under `<tenant_id>/idempotency/` with `platform.idempotency.ttl_s` |

### 1.5 `Model.context_window` (IMPL-084)

```json
"context_window": { "type": "integer", "minimum": 1024,
  "description": "Input + output tokens the model accepts per request. Absent: 128000 (DEFAULT_CONTEXT_WINDOW, a placeholder until set from the model card). Used by the capability-fit hard filter (04:L32) and the context budget (IMPL-045)." }
```

Added to `$defs.Model.properties`; optional, so the example yaml stays valid. `ModelCapabilities.context_window` (spec-02 §4.1) carries the filled value.

## 2. `prices.yaml` (IMPL-019; schema `src/noema/config/prices.schema.json`, foundation)

```yaml
version: 1
entries:
  - model: us.anthropic.claude-haiku-5-5        # Bedrock id (or a models.<alias>.price_ref value)
    region: us-east-1
    tier: on_demand                              # Phase 1: on_demand only
    effective_from: 2026-10-01
    usd_per_mtok: { input: "TBD", output: "TBD", cache_read: "TBD", cache_write_5m: "TBD", cache_write_1h: "TBD" }
```

- Values are decimal strings or `"TBD"`. `"TBD"` and missing components price at 0 with `cost_estimated: true`.
- Lookup key: `models.<alias>.price_ref` if set, else `models.<alias>.id`, plus the call's region; the newest entry with `effective_from ≤ now` wins (IMPL-019). No entry → every component is TBD.
- `noema-admin prices import <file>` validates the file against `prices.schema.json` and stores it as the next `platform.price_catalog` version (`admin.import_prices`); a config then pins it with `platform.cost.prices_ref: "db:<version>"`, and `ConfigSnapshot.prices` is loaded with the snapshot, so a turn prices against the catalog of its pinned config. `file:<path>` reads the yaml directly and is refused outside dev/test (P1-P1).

## 3. Loader behavior and Phase 1 rules (`config/loader.py`, `config/rules.py`)

Loading order:
1. YAML;
2. JSON Schema (cschema rev 6);
3. fill defaults: the §1 `default` values, the `data_classes` table (per field), `labeling.acceptance`, and the P1-T1 fill;
4. `check_config.semantic_errors` (the existing rules plus P1-C1–C4, spec-08 §14.4);
5. the server-only P1 rules below (`config/rules.py`). Each has a negative test in `tests/unit/config/test_rules_p1.py`.
6. load the price catalog named by `platform.cost.prices_ref`; compute `digest` = sha256 of the canonical JSON of the filled config.

A failure at any step rejects the whole config (`config validate` prints every error; a running pod keeps its current snapshot).

| Id | Rule | Source |
|---|---|---|
| P1-T1 | Fill `tenants.<t>.defaults.decision_profile = "lightweight-model-decide"` when it is absent; reject if the resolved id isn't a `decision_profiles` key | IMPL-031 |
| P1-R1 | `routing.default_profile` is required | IMPL-026 |
| P1-D1 | `routing.speculation.commit_on_quorum` must be false | IMPL-027 |
| P1-D2 | no `batch_questions: true` | IMPL-027 |
| P1-D3 | member elicitations ∈ {`token_logprobs`, `verbalized`} | IMPL-027 |
| P1-D4 | `aggregation = linear_pool` | IMPL-027 |
| P1-D5 | member templates ∈ {`logprob-codes@2`, `verbalized-dist@1`, `verify-dist@1`} with their elicitation | IMPL-027 |
| P1-D6 | every question asked by `routing.router_profile` and `routing.verification.decision_profile` has `K ≤ L(profile)`: platform questions stay flat even though request questions may be hierarchical | IMPL-027 |
| P1-G1 | no binding or `intent_routes` value targets a `best_of_n`/`fuse` profile; such profiles are not in `defaults.generation_profile` | IMPL-036 |
| P1-C1 | `labeling.acceptance` and `family_overlap_lift_max` within the contract bounds (enforced by the schema) and `drift_ece_max ≥ ece_max` | IMPL-039 |
| P1-C2 | `routing.confidence_bands.low_lt < high_gte` | IMPL-039 |
| P1-C3 | `escalation.questions` names only questions of the source profile's own `questions`; an empty list is allowed | IMPL-068 |
| P1-C4 | `platform.data_classes.{confidential,restricted}.l2_cache` must not be true | IMPL-038 |
| P1-L1 | every `tenants.<t>.limits.model_quotas` key is a `models` alias (and in `allowed_models` when that is set) | IMPL-038 |
| P1-P1 | `platform.cost.prices_ref` of the form `file:` only when `NOEMA_ENV ∈ {dev, test}`; `db:<n>` must exist in `platform.price_catalog` at activation | IMPL-019 |

Activation:
- `noema-admin config propose` validates (steps 1–6) and stores `platform.config_version(status = proposed)`.
- `approve` by a second operator → `active` (the previous active row → `superseded`, in one transaction) and `admin.sync_tenants`; `pg_notify` → notify bridge → Redis `config_changed` (spec-11 §4).
- `rollback <version>` re-activates a `superseded` version under the same two-person rule.
- Every pod loads the new snapshot on `config_changed`, and polls the active version every 5 s (Redis down). Running turns keep the snapshot they pinned (spec-02 §2).

## 4. Tests (foundation)

- One negative test per new schema bound and per P1 rule (§3).
- The unchanged example yaml still loads, with the defaults applied and checked.
- A snapshot round-trip: loader output → frozen `*Cfg` dataclasses → `digest` is stable.
- The `check_config.py` negative-test count rises from 45 to 49: P1-C1–C4 go into `check_config.py` (spec-08 §14.4). P1-T1, P1-R1, P1-D1–D6, P1-G1, P1-L1 and P1-P1 live in `config/rules.py`, not in the design checker, so the design examples are unaffected.

## 5. Process environment (not config)

Settings that are per process, not per config version. `composition.build()` validates them at startup (`composition.process_settings`, spec-01 §3) into `ProcessSettings` (`core/settings.py`, read as `services.settings`, never `os.environ`); an invalid value is a startup error.

| Variable | Values | Used by |
|---|---|---|
| `NOEMA_ENV` | `test` \| `dev` \| `staging` \| `prod` | every environment gate (AmbientBroker, endpoint overrides, InprocRlimit sandbox, `file:` prices, `ak_test_` keys, the dev cookie proxy) |
| `NOEMA_AWS_ENDPOINT_OVERRIDES` | JSON object, keys ⊆ {`sts`, `secretsmanager`, `s3`, `bedrock-runtime`, `mantle`}, `http(s)` URLs | ADR-001 §8: only when `NOEMA_ENV ∈ {dev, test}` (the wire mock, moto, minio; spec-10 §2.1); a non-empty value elsewhere refuses startup |
| `NOEMA_CONTRACT_VALIDATE` | `1` or unset | test-mode `ContractValidator` (IMPL-041); refused in prod |
| `NOEMA_EXPECTED_PODS` | integer ≥ 1 | the Redis-down local GCRA divisor (spec-11 §1.2) |
| `NOEMA_METRICS_PORT` | port | `/metrics`, never the API port (spec-11 §7) |
| `NOEMA_TRUSTED_PROXIES` | CIDR list | client IP for `ip_allowlist` (spec-09 §2) |
| `NOEMA_POD_ID` | string; default the host name | `ProcessSettings.pod_id`: idempotency owners `<pod>:<request_id>`, job workers `<pod>:<pid>`, `turn_owner` |
| `NOEMA_S3_BUCKET` | bucket; default `noema-<env>` | every object (spec-03 §2) |
| `NOEMA_REDIS_URL` | `redis://`, `rediss://` or `unix://` URL; required outside dev/test | the Redis client (dev/test default: `NOEMA_TEST_REDIS_URL`, else the local valkey) |
| `NOEMA_DB_DSN_REF` | platform secret name; default `noema/db-dsn` | the `noema_app` DSN (spec-09 §4) |
| `NOEMA_REDIS_MAX_CONNECTIONS` | integer 8–10,000; default 256 | the process's ONE Redis `BlockingConnectionPool`, pub/sub connections included: past it a caller waits up to 0.5 s for a released connection, then fails like an unreachable Redis (IMPL-088) |
| `NOEMA_TOKEN_ESTIMATOR_TABLE` | absolute path; unset = the shipped `TOKEN_ESTIMATOR_V1` | a TokenEstimator refit, the JSON `python -m noema.ops.cost.estimator_fit` writes; it replaces the table (an alias not in it counts chars ÷ 4); an unreadable or invalid file refuses startup (IMPL-029c, spec-11 §2) |
| `NOEMA_DRAIN_DELAY_S` | seconds, 0–120; default 5 (0 in test) | SIGTERM → uvicorn's own shutdown, with `/readyz` 503 first (spec-11 §7) |
| `NOEMA_DEV_API_KEY` | `ak_test_<26>.<43>`; dev/test only (elsewhere startup is refused) | one documented key on the seed authenticator (smoke tests) |
| `NOEMA_SANDBOX_BACKEND` | `remote` (default) \| `bwrap` (dev/test) \| `inproc_rlimit` (test only) | the media-worker's `SandboxRunner` (ADR-002 §7; `check_component("sandbox.<backend>", env)`) |
| `NOEMA_SPOOL_DIR`, `NOEMA_SANDBOX_SOCKET` | absolute paths; default `/spool` and `<spool_dir>/sandboxd.sock` | the media-worker's spool and sandboxd's socket (ADR-002 §6) |
| `NOEMA_CREDENTIALS_BROKER` | `sts` (default, every env) \| `ambient` (dev/test only; `check_component("broker.ambient", env)`) | the credential broker: `StsBroker`, or `AmbientBroker` with the developer's own credentials for the live smoke (ADR-001 §7, spec-10 §6) |
| `NOEMA_BREAKER_TTFT_RULE` | `on` (default) \| `off` (dev/test only; `check_component("breaker.ttft_off", env)`) | `off` disarms the service breakers' p95-TTFT rule (`BreakerPolicy(ttft_factor=inf)`); the failure-ratio rule stays armed. Set by the e2e, UI and perf stacks on the wire mock, whose scenarios change a model's latency up to 30× between tests; the fault stack runs `on` (IMPL-078 amendment) |
| `WEB_CONCURRENCY` | integer 1–64; default 1; 2 for a 2-vCPU api pod | uvicorn `--workers`: api processes per pod (IMPL-087). With N > 1, `pod_id` = `<NOEMA_POD_ID or host name>-<pid>`, the Redis-down divisor is `NOEMA_EXPECTED_PODS × N`, and worker *i* serves `/metrics` on `NOEMA_METRICS_PORT + i`. Never set for `jobs` or `media-worker` |
| `NOEMA_MAX_TURNS_PER_PROCESS` | integer 0–100,000; default 120; 0 = no cap | the per-process cap on running turns plus in-flight createDecision and facade requests; above it 429 `queue_timeout` with `retry-after: 1` (IMPL-087, spec-11 §7). Sized for the L1 workload; set it from the production turn mix |
| `NOEMA_OPERATOR_KEY` | operator key | `noema-admin` only (IMPL-044) |
| `NOEMA_LIVE`, `NOEMA_AGENT_ID` | `1`; agent id | tests only (spec-10 §1.1, §6) |
