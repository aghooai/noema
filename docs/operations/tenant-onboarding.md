# Tenant onboarding, ExternalId rotation and offboarding

Each tenant has its own AWS account (owner decision D2). Noema assumes the role `AghooBedrockInvoke` in that
account with an ExternalId and signs every model call with the session's credentials (ADR-001). Onboarding has an
**IaC part** (AWS Organizations, StackSets, Service Quotas; not in this repo and **not run** in Phase 1, where moto
simulates STS and IAM) and a **`noema-admin` part** (run against a stack while writing this document). The design's
steps are 02 §10 and the 08 §6 runbook "Tenant onboarding"; the policies are 08 §5.

## 1. Steps

| # | Step | Where | Notes |
|---|---|---|---|
| 1 | Vend the tenant account | AWS Organizations (IaC) | record the 12-digit account id |
| 2 | Add `tenants.<tenant_id>` to the config (account id, `invoke_role_arn`, `external_id_ref`, defaults, limits) and activate it: `config propose` (operator A), `config approve` (operator B) | `noema-admin` | [configuration.md](configuration.md) sections 3 and 5; activation inserts the `noema.tenant` row. The config only references the secret of step 3 |
| 3 | Mint the ExternalId: `noema-admin tenants external-id create --tenant <tenant_id>` | `noema-admin` | writes `platform/tenants/<tenant_id>/external-id` (`aghoo-<tenant_id>-<32 base62>`) and prints it **once**, for step 4 |
| 4 | Deploy `AghooBedrockInvoke` with the trust policy for that ExternalId | StackSets (IaC) | section 2. In the platform account (not the tenant's), create the tenant's KMS key `alias/noema-<env>-<tenant_id>` for its objects in `noema-<env>` (staging, prod) |
| 5 | Enable model access for every model the tenant may use | Bedrock console or IaC | whether the Anthropic models need a per-account use-case form is UNVERIFIED (design 02 §10) |
| 6 | Request quotas | Service Quotas | mirror them in `tenants.<t>.limits.model_quotas` (a config change) so admission throttles before Bedrock does |
| 7 | Capability check | - | Phase 1 has no per-tenant probe runner (design W6). At start each api and jobs process logs every config capability claim the recorded probes refute (WARNING `model capability probe discrepancy`) |
| 8 | Create projects, principals and API keys | `noema-admin` | [configuration.md](configuration.md) section 6; hand the keys to the tenant (printed once) |
| 9 | First call | the tenant | a decision or turn with the new key; a misconfigured account answers 503 `tenant_access_misconfigured` with `details.cause` (section 3) |

This is the order `tools/bootstrap_dev.py` uses on a dev stack (config, then ExternalId, then the role in moto, then
projects, principals and keys).

## 2. The tenant role (IaC; design 08 §5)

Trust policy of `AghooBedrockInvoke` in the tenant account: allow `sts:AssumeRole` to the platform's data-plane and
job-runner roles with `"Condition": {"StringEquals": {"sts:ExternalId": "<the value of step 3>"}}`. The platform
workload role may assume `arn:aws:iam::*:role/AghooBedrockInvoke` only with an ExternalId matching `aghoo-*` and may
read `platform/tenants/*` secrets. The role name is fixed: the broker refuses an `invoke_role_arn` that is not
`arn:aws:iam::<aws_account_id>:role/AghooBedrockInvoke` (cause `role_not_allowlisted`).

Permissions: `bedrock:InvokeModel` (Converse) and `bedrock:InvokeModelWithResponseStream` (ConverseStream) on
both the inference-profile ARNs and the foundation-model ARNs of the allowed models; the Mantle chat action
(UNVERIFIED in the design, Phase 0 task 0.8a); Phase 1 uses neither Transcribe (no audio input) nor the tenant media bucket of 08 §5:
every object is in the platform bucket `noema-<env>` ([architecture.md](../architecture.md) section 8).
The full JSON is in design 08 §5. The platform role names are a deployment choice: the dev bootstrap trusts
`noema-api` and `aghoo-jobs` of the platform account `123456789012` (moto), where 08 §5 names `aghoo-data-plane`
and `aghoo-jobs`.

## 3. When the first call fails

The broker fails closed with 503 `tenant_access_misconfigured` (`retryable: false`), counts
`aghoo_tenant_access_failures_total{tenant,cause}` and caches the failure for 10 s per tenant and pod
(ADR-001 §2):

| `cause` | Fix |
|---|---|
| `role_not_allowlisted` | `invoke_role_arn` must be `arn:aws:iam::<aws_account_id>:role/AghooBedrockInvoke` with the config's account |
| `external_id_unavailable` | the secret `platform/tenants/<tenant_id>/external-id` is missing or unreadable by the platform role |
| `external_id_invalid` | the secret's value is not `aghoo-<tenant_id>-<32 base62>` (often another tenant's secret) |
| `access_denied` | the trust policy does not list the current ExternalId or the platform role; check CloudTrail in the tenant account |
| `sts_unavailable` | STS throttling, 5xx or timeout; retry later |
| `account_mismatch` | the assumed role is in another account than `aws_account_id`; security event, page security |
| `model_access_denied` | step 5 (model access) or the role's model list is missing a model; a decision member degrades (IMPL-057), a generation attempt does not fall back (IMPL-016) |

Runbook: [runbooks.md](runbooks.md#tenant-access-misconfigured).

## 4. ExternalId rotation (ADR-001 §3)

Never automatic. Two `noema-admin` steps around two trust-policy changes:

```bash
$A tenants external-id rotate --tenant <tenant_id>    # stages the new value as AWSPENDING and prints it once
#   IaC: the trust policy lists BOTH values (StringEquals on a list)
$A tenants external-id promote --tenant <tenant_id>   # AWSCURRENT moves to the new value, the old one becomes AWSPREVIOUS
#   wait one STS session lifetime (3,600 s) plus the 300 s secrets cache, so every broker has refreshed
#   IaC: remove the old value from the trust policy
```

Brokers keep reading `AWSCURRENT` until `promote`, so nothing changes for traffic before it. Verified on a stack:
`rotate` printed the new value with the notice "Add this value to the tenant trust policy next to the current one,
then run `tenants external-id promote --tenant t_beta`", and `promote` answered "Brokers switch at their next
refresh; remove the old value from the trust policy then."

## 5. Offboarding

Phase 1 has no tenant purge command. The steps with what exists:
1. Revoke every key: `$A keys list --tenant <t>`, then `$A keys revoke <key_id>` for each (refused by every pod
   within 60 s at worst).
2. Remove `tenants.<t>` from the config and activate it (two operators): the tenant row is soft-deleted, and the
   key lookup returns no row for a deleted tenant (401).
3. Delete or export data through the API (`DELETE /v1/sessions/{id}`, `DELETE /v1/files/{id}`, run by the jobs and
   media-worker processes) or let the retention sweeper (hourly) remove expired rows and objects.
4. IaC: remove the platform roles from the trust policy, then close the account (design 08 §6).

A bulk tenant export and purge (and the 24 h deletion SLA of the design runbook) are not built; see
[runbooks.md](runbooks.md#deletion-job-stuck).
