"""F2 (spec-10 §4, 06 §7): AssumeRole failure / `AccessDeniedException`.

- Credential chain (ingress): tenant B's account moves (its cached STS session no longer matches the config) and its
  next AssumeRole fails: tenant A's ExternalId (`external_id_invalid`), a missing ExternalId secret
  (`external_id_unavailable`), STS + Secrets Manager unreachable (toxiproxy `moto` disabled). Every B turn is 503
  `tenant_access_misconfigured`, `retryable: false`, with 0 mock requests; tenant A's turns keep completing.
  (moto never denies AssumeRole itself; STS `AccessDenied` classification is SEC-STS-05/06/07/09 with trust_enforcer.)
- Generation member: `access_denied` on Opus for tenant A under opus-deep: `error{tenant_access_misconfigured}`, no
  fallback attempt; the shared Opus breaker stays closed and tenant B's opus-deep turns complete on Opus.
- Decision member: `access_denied` on Haiku: the member is `error` (tenant_access_misconfigured), the decision
  degrades (IMPL-057) and every turn completes.
"""

from __future__ import annotations

from collections.abc import Callable

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import mock_calls, mock_seq, turn_body
from tests.fault import kit
from tests.fault.stackctl import Toxiproxy, circuit_states, metric_total, patched

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
MOVED = "555566667777"
BETA_REF = "aws-sm://platform/tenants/t_beta/external-id"
PER_VARIANT = max(10, kit.TURNS // 10)


def move_beta(external_id_ref: str) -> Callable[[kit.Json], None]:
    def patch(doc: kit.Json) -> None:
        doc["tenants"]["t_beta"].update(
            aws_account_id=MOVED,
            invoke_role_arn=f"arn:aws:iam::{MOVED}:role/AghooBedrockInvoke",
            external_id_ref=external_id_ref,
        )

    return patch


async def beta_status(stack: Stack, replica: int) -> int:
    probe = turn_body("Access probe: the capital of France?", stream=False)
    return (await stack.client(replica, tenant="t_beta").request("POST", "/v1/turns", json=probe)).status_code


async def until_beta(stack: Stack, status: int, *, timeout_s: float = 40.0) -> None:
    """Until a t_beta turn answers `status` on both replicas (the config, or the 10 s negative cache, took effect)."""
    with anyio.fail_after(timeout_s):
        for replica in (0, 1):
            while True:
                if await beta_status(stack, replica) == status:
                    break
                await anyio.sleep(0.5)


async def variant(stack: Stack, mock: Mock, name: str) -> kit.Json:
    before = await mock_seq(mock)
    alerts0 = await metric_total(stack, "aghoo_tenant_access_failures_total", tenant="t_beta")
    records = await kit.run_turns(stack, 2 * PER_VARIANT, lambda i, t: kit.body(f"f02a-{name}-{t}"))
    calls = await mock_calls(mock, before)
    a = [r for r in records if r.tenant == "t_acme"]
    b = [r for r in records if r.tenant == "t_beta"]
    kit.assert_s1(records)
    envelopes = [r.envelope or {} for r in b]
    errors = [e.get("error", {}) for e in envelopes]
    numbers = {
        "b_outcomes": kit.counts(kit.outcome(r) for r in b),
        "b_causes": kit.counts((e.get("details") or {}).get("cause") for e in errors),
        "b_retryable": kit.counts(e.get("retryable") for e in errors),
        "b_ingress_ms": kit.summary([r.e2e_ms for r in b]),
        "b_mock_requests": len(kit.tagged(calls, f"f02a-{name}-t_beta")),
        "tenant_access_failures_counted": await metric_total(
            stack, "aghoo_tenant_access_failures_total", tenant="t_beta"
        )
        - alerts0,
        "a_outcomes": kit.counts(kit.outcome(r) for r in a),
    }
    assert all(r.status == 503 for r in b), numbers
    assert all(e.get("code") == "tenant_access_misconfigured" and e.get("retryable") is False for e in errors), numbers
    assert numbers["b_mock_requests"] == 0, numbers
    assert all(r.completed for r in a), numbers
    return numbers


async def test_f02_assume_role_failure_is_a_typed_503_before_any_model_call(
    stack: Stack, mock: Mock, toxi: Toxiproxy, base_version: int
) -> None:
    results: dict[str, kit.Json] = {}
    for name, ref in (("foreign", "aws-sm://platform/tenants/t_acme/external-id"), ("missing", f"{BETA_REF}-gone")):
        async with patched(stack, base_version, move_beta(ref)):
            await until_beta(stack, 503)
            results[name] = await variant(stack, mock, name)
        await until_beta(stack, 200)
    await toxi.enable("moto", False)
    try:
        async with patched(stack, base_version, move_beta(BETA_REF)):
            await until_beta(stack, 503)
            results["moto_down"] = await variant(stack, mock, "motodown")
    finally:
        await toxi.enable("moto", True)
    await until_beta(stack, 200)
    kit.record_result("F2a", per_variant_turns=2 * PER_VARIANT, **results)


async def test_f02_generation_access_denied_never_falls_back(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    denied = kit.gen_fault(kit.OPUS, "access_denied", 100)
    denied["match"]["prompt_contains"] = "f02b-t_acme"
    await mock.load(kit.scenario("fault-F2b", [denied, kit.gen(kit.OPUS), kit.gen(kit.SOL)], kit.members()))
    before = await mock_seq(mock)
    records = await kit.run_turns(stack, kit.TURNS // 2, lambda i, t: kit.body(f"f02b-{t}", "opus-deep"))
    calls = await mock_calls(mock, before)
    kit.mark_budget_used()
    a = [r for r in records if r.tenant == "t_acme"]
    b = [r for r in records if r.tenant == "t_beta"]
    sol_for_a = [c for c in kit.tagged(calls, "f02b-t_acme") if c["model"] == kit.SOL and c["op"] == "converse-stream"]
    errors = [r.error or {} for r in a]
    opus_circuit = await circuit_states(stack, kit.OPUS)
    kit.record_result(
        "F2b",
        turns=len(records),
        a_outcomes=kit.counts(kit.outcome(r) for r in a),
        a_retryable=kit.counts(e.get("retryable") for e in errors),
        a_attempt_models=kit.counts(tuple(x["model"] for x in r.started) for r in a),
        a_sol_requests=len(sol_for_a),
        b_outcomes=kit.counts(kit.outcome(r) for r in b),
        opus_circuit_after=opus_circuit,
    )
    kit.assert_s1(records)
    assert all(e.get("code") == "tenant_access_misconfigured" and e.get("retryable") is False for e in errors)
    assert all([x["model"] for x in r.started] == [kit.OPUS] for r in a) and not sol_for_a
    assert all(r.completed and r.model_of(r.committed) == kit.OPUS for r in b)
    assert all(state in (None, 0.0) for state in opus_circuit), opus_circuit  # tenant access is not provider health


async def test_f02_decision_member_access_denied_degrades_the_decision(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    denied = kit.fault_rule({"model": kit.HAIKU}, "access_denied", 50)
    await mock.load(kit.scenario("fault-F2c", [kit.gen(kit.SOL)], [kit.members()[0], denied], kit.members()[1:]))
    records = await kit.run_turns(stack, kit.TURNS // 2, lambda i, t: kit.body(f"f02c-{t}"))
    kit.mark_budget_used()
    haiku = [v for r in records for v in kit.member_votes(r, kit.HAIKU)]
    others = [v for r in records for v in kit.member_votes(r) if v["model"] != kit.HAIKU]
    haiku_circuit = await circuit_states(stack, kit.HAIKU)
    kit.record_result(
        "F2c",
        turns=len(records),
        outcomes=kit.counts(kit.outcome(r) for r in records),
        haiku_votes=kit.counts((v["status"], (v.get("error") or {}).get("code")) for v in haiku),
        other_votes=kit.counts(v["status"] for v in others),
        decision_status=kit.counts(r.of("decision.completed")[0]["decision"]["status"] for r in records),
        haiku_circuit_after=haiku_circuit,
    )
    kit.assert_s1(records)
    assert all(r.completed for r in records)
    assert haiku and all((v["status"], v["error"]["code"]) == ("error", "tenant_access_misconfigured") for v in haiku)
    assert all(state in (None, 0.0) for state in haiku_circuit), haiku_circuit
