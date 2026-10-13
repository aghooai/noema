# Configuration and administration (`noema-admin`)

Two kinds of settings: the **process environment** (per process, read at startup: [deployment.md](deployment.md)
section 3) and the **platform config** (one versioned document in Postgres, activated by two operators and
hot-loaded by every pod). Projects, principals, API keys, operators, prices and calibrators are database rows
managed with `noema-admin` (IMPL-038, IMPL-044).

Every command in this file was run against a bootstrapped e2e stack (`noema-docs-qs`, 2026-10-12) unless it says
"not run". Outputs are JSON on stdout; a refusal prints `{"error": <reason>, "message": ...}` on stderr and exits 1
(2 for a usage error, 3 when the operator key fails to authenticate). Database and Secrets Manager failures are
refusals too (`invalid_argument`, `unavailable`, or `internal_error` "The database is temporarily unavailable." when
the database is unreachable: seen with `config list` against a closed port), never a traceback and never the DSN.

## 1. Running `noema-admin`

`python -m noema.identity.cli` (from a checkout: `uv run --frozen --no-sync python -m noema.identity.cli`). It opens
its own `noema_cli` connection and never uses the service composition.

| Variable | Needed for |
|---|---|
| `NOEMA_ENV` | `test`, `dev`, `staging` or `prod`; unset means `prod` (the strictest rules) |
| `NOEMA_CLI_DSN` | every command except `bootstrap` and `config validate`: the `noema_cli` DSN |
| `NOEMA_DB_OWNER_DSN` | `bootstrap` only (the `noema_owner` DSN) |
| `NOEMA_OPERATOR_KEY` | every command except `bootstrap` and `config validate`: `op_<key_id>.<secret>` |
| `NOEMA_AWS_ENDPOINT_OVERRIDES` | dev/test only: `{"secretsmanager": "<moto url>"}` |
| AWS credentials | Secrets Manager access in the platform account (default chain; the peppers, ExternalIds) |

For a stack seeded by `make e2e-up`, the values come from the state file (`~/.noema/stacks/<project>.json`):
the DSN host is `endpoints.postgres`, the moto URL `endpoints.moto`, the operator keys
`operators.noema-bootstrap-a.key` and `operators.noema-bootstrap-b.key` (two operators, for the two-person rules),
and the dev password of `noema_cli` is `noema-cli-dev-only`:

```bash
S=~/.noema/stacks/noema-e2e.json
export NOEMA_ENV=dev AWS_ACCESS_KEY_ID=noema-compose-fake AWS_SECRET_ACCESS_KEY=noema-compose-fake AWS_DEFAULT_REGION=us-east-1
export AWS_EC2_METADATA_DISABLED=true AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null   # never the host's own AWS setup
export NOEMA_CLI_DSN="postgresql://noema_cli:noema-cli-dev-only@$(jq -r .endpoints.postgres $S)/noema"
export NOEMA_AWS_ENDPOINT_OVERRIDES="{\"secretsmanager\": \"$(jq -r .endpoints.moto $S)\"}"
export NOEMA_OPERATOR_KEY=$(jq -r '.operators["noema-bootstrap-a"].key' $S)
A="uv run --frozen --no-sync python -m noema.identity.cli"
$A --help
```

## 2. Operators

| Command | Effect |
|---|---|
| `noema-admin bootstrap --operator <name>` | the first operator (one time; `NOEMA_DB_OWNER_DSN`); prints its key once (not run here: the stack was already bootstrapped) |
| `noema-admin operators create --name <name>` | another operator; prints `key`, `key_id`, `operator_id` once |
| `noema-admin operators revoke <operator_id>` | revokes an operator (not run here) |

Keep at least two live operators: config activation and calibrator activation need a second one. Since migration
`0003_operator_proof` the database verifies the operator key on the connection, so two approvals need two keys.

## 3. The platform config

One YAML document validated against the design's `config.schema.json` (contract revision 6): `platform` (limits,
idempotency, events, sessions, decision cache, cost, media, credentials, data classes), `tenants`, `models`,
`templates`, `decision_profiles`, `generation_profiles`, `routing`, `labeling`. Reference: the design's
`08-technical-reference.md` section 2 and spec-05 ([impl-spec/05-config-extensions.md](../impl-spec/05-config-extensions.md)).
The shipped example is `src/noema/contracts/_vendor/examples/platform.example.yaml`.

Loading order (a failure at any step rejects the whole document): YAML; JSON Schema; defaults (including
`tenants.<t>.defaults.decision_profile = lightweight-model-decide`, D6); the design checker
(`check_config.semantic_errors`); the server-side Phase 1 rules (P1-T1, P1-R1, P1-D1 to P1-D6, P1-G1, P1-C1 to P1-C4, P1-L1, P1-P1 of spec-05 §3; for example
`routing.default_profile` required, `commit_on_quorum` false, no `best_of_n`/`fuse` targets, `file:` prices only in
dev/test); the price catalog named by `platform.cost.prices_ref`; the digest (sha256 of the canonical JSON).

### 3.1 Change a config: validate, propose, approve

```bash
$A config validate platform-v2.yaml                  # local, no DB: {"config_version", "digest", "valid": true} or every error
$A config propose platform-v2.yaml                   # operator A: stored as proposed
$A config approve 2                                  # operator A again: refused, "two_person: the approver must differ from the proposer"
NOEMA_OPERATOR_KEY=<operator B key> $A config approve 2   # active; the previous active version becomes superseded
$A config list                                       # every version with status, proposer, approver, digest
```

`config approve --file <path>` approves only if the file's digest matches the proposed version (the reviewer checks
the file they read). Activation runs in one transaction, synchronises `noema.tenant` with the config's `tenants`
(tenants missing from it are soft-deleted) and notifies the pods (`pg_notify`, then Redis `config_changed`).
Every pod loads the new snapshot at once, or within 5 s from its poll if Redis is down. Running turns keep the
snapshot they pinned. A pod that cannot load a version (for example a rule failure at load) keeps its current
snapshot and logs one ERROR.

### 3.2 Roll back

```bash
$A config rollback 1                                 # re-proposes the superseded version 1
NOEMA_OPERATOR_KEY=<operator B key> $A config approve 1
```

Rollback is a proposal like any other, under the same two-person rule.

## 4. Prices

The price catalog is a database table; a config pins one version. `prices.yaml` (schema
`src/noema/config/prices.schema.json`):

```yaml
version: 1
entries:
  - model: us.anthropic.claude-haiku-5-5        # the Bedrock id, or a models.<alias>.price_ref
    region: us-east-1
    tier: on_demand
    effective_from: 2026-10-01
    usd_per_mtok: { input: "TBD", output: "TBD", cache_read: "TBD", cache_write_5m: "TBD", cache_write_1h: "TBD" }
```

```bash
$A prices import prices.yaml        # {"version": 2, "prices_ref": "db:2", "digest": ...}
```

Then set `platform.cost.prices_ref: "db:2"` in a config version and propose and approve it (section 3.1). Values
are decimal strings or `"TBD"`; a TBD or missing component prices at 0 and marks the usage line
`cost_estimated: true`. Lookup: `models.<alias>.price_ref` if set, else the model id, plus the call's region; the
newest entry with `effective_from` not in the future wins (IMPL-019). Phase 1 has no real Bedrock prices: the dev
stacks import fixed fake prices (`tools/bootstrap_dev.py`), and production needs a real catalog before cost
figures mean anything (K1's cost half is NOT-RUN [P0], [reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md)).

## 5. Tenants

A tenant is a `tenants.<tenant_id>` entry of the config (one AWS account per tenant, D2):

| Field | Meaning |
|---|---|
| `aws_account_id`, `invoke_role_arn` | the tenant account and its `arn:aws:iam::<account>:role/AghooBedrockInvoke` (the broker refuses any other role name or account) |
| `external_id_ref` | `aws-sm://platform/tenants/<tenant_id>/external-id` |
| `home_region` | the Mantle endpoint region |
| `defaults` | `decision_profile` (default `lightweight-model-decide`), `generation_profile` |
| `request_overrides` | the decision and generation profiles a request may pin (else 403 `profile_not_allowed`) |
| `limits` | per-tenant admission limits and Bedrock quota mirrors (spec-05 §1.2), for example `rpm_per_key`; every `model_quotas` key must be a `models` alias |

Adding, changing or removing a tenant is a config change (section 3.1). The account side and the ExternalId are in
[tenant-onboarding.md](tenant-onboarding.md).

## 6. Projects, principals and API keys

```bash
$A projects create --tenant t_acme --name docs-demo [--default-data-class internal]   # {"project_id": "prj_..."}
$A projects list --tenant t_acme
$A principals create --tenant t_acme --project <prj_id> --subject dev@docs-demo.example --roles developer
$A keys create --tenant t_acme --project <prj_id> --principal <prn_id> --scopes turns,decisions,sessions --env test
$A keys list --tenant t_acme [--project <prj_id>]     # never shows hashes
$A keys revoke <key_id>                               # {"key_id": ..., "revoked": true}
```

| Item | Values |
|---|---|
| Roles (`--roles`, comma-separated) | `tenant_admin`, `developer`, `reviewer`, `end_user`, `auditor` (IMPL-083 matrix: spec-09 §3) |
| Scopes (`--scopes`) | `turns`, `decisions`, `sessions`, `files.read`, `files.write`, `usage.read`, `admin.read` |
| Key environment (`--env`) | `live` (`ak_live_...`) or `test` (`ak_test_...`); test keys are refused when `NOEMA_ENV=prod` |
| Optional | `--expires <ISO 8601>`, `--ip-allow <CIDR,...>` (the client IP honours `NOEMA_TRUSTED_PROXIES`) |
| Data classes (`--default-data-class`) | `public`, `internal`, `confidential`, `restricted` |

`keys create` prints the key once (`key`, `key_id`, `scopes`, `notice`); only its HMAC is stored. A revoked key is
refused by every pod within about 1 s with Redis up (`keys_revoked` pub/sub) and within the 60 s key-cache TTL
with Redis down (spec-09 §2; e2e `test_multipod.py` checks 5 s). Verified: a decision with the new key returned
200, and the same request after `keys revoke` returned 401 `invalid_api_key`.

## 7. Calibrators (ADR-003)

| Command | Rule |
|---|---|
| `$A calibrators list [--scope platform\|<tenant_id>] [--status <status>]` | run here: `{"data": []}` (no artifact exists before Phase 0 labels) |
| `$A calibrators approve <calibrator_id>` | an `accepted` artifact (not run here) |
| `$A calibrators activate <calibrator_id>` | by a different operator than the approver (two-person, IMPL-072; not run here) |
| `$A calibrators retire [--reason manual\|profile_version_retired] <calibrator_id>` | not run here |

Activation reaches the pods within 5 s (`calibrators_changed` plus the poll). A drifting active artifact is
downgraded by the labeling drift job (`aghoo_calibrator_downgrades_total`); see [runbooks.md](runbooks.md#noema-calibrator-ece-drift).
