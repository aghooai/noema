"""Fake-specific behavior beyond the Protocol suites: FakeClock settling and autojump, `deadline_scope`, the
decision-result builder (property test against the contract), the STS trust enforcer, `FakeStsClient`,
`FakeBroker.swap`, `FakeS3Client` and `FakeWorld.use_scenario`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import anyio
import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.clock import FakeClock, FakeIdGen
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.credentials import FakeBroker, FakeStsClient
from tests.fakes.decision import FakeQuestionCompiler
from tests.fakes.decision_result import MemberResult, MemberStatus, build_answer, build_result, round_units
from tests.fakes.s3 import FakeS3Client
from tests.fakes.secrets import FakeSecretsProvider
from tests.fakes.sts_trust import make_handler, trust_allows
from tests.fakes.world import build_world

from noema.contracts.validate import ContractValidator
from noema.core.clock import deadline_scope
from noema.core.errors import ErrorCode, NoemaError, SecretMissing, TenantAccessMisconfigured
from noema.core.protocols.credentials import assert_creds_for
from noema.core.values import UsageRecord

from .kits import principal

ROLE = "arn:aws:iam::111122223333:role/AghooBedrockInvoke"
CALLER = "arn:aws:iam::123456789012:role/noema-api"


async def test_advance_wakes_in_deadline_order() -> None:
    clock = FakeClock()
    log: list[tuple[str, int]] = []

    async def sleeper(name: str, *delays: float) -> None:
        for d in delays:
            await clock.sleep(d)
            log.append((name, round(clock.elapsed_ms())))

    async with anyio.create_task_group() as tg:
        tg.start_soon(sleeper, "a", 0.3, 0.3)
        tg.start_soon(sleeper, "b", 0.5)
        tg.start_soon(sleeper, "c", 0.1)
        await clock.advance(1.0)
    assert log == [("c", 100), ("a", 300), ("b", 500), ("a", 600)]
    assert clock.elapsed_ms() == 1000 and clock.pending() == 0


async def test_autojump_and_deadline_scope() -> None:
    clock = FakeClock()
    async with clock.autojump():
        async with deadline_scope(clock, clock.monotonic() + 2.5) as scope:
            await clock.sleep(3600)
        assert scope.cancelled_caught and round(clock.elapsed_ms()) == 2500
        with pytest.raises(ZeroDivisionError):  # body exceptions pass through unwrapped
            async with deadline_scope(clock, clock.monotonic() + 1):
                raise ZeroDivisionError
    with pytest.raises(RuntimeError):
        async with clock.autojump():
            await clock.advance(1)


async def test_fake_ids_are_deterministic() -> None:
    a, b = FakeIdGen(FakeClock()), FakeIdGen(FakeClock())
    assert [a.new("turn") for _ in range(3)] == [b.new("turn") for _ in range(3)]


_dist = st.lists(st.floats(0.01, 1.0), min_size=2, max_size=6)


@settings(max_examples=60, deadline=None)
@given(
    k=st.integers(2, 6),
    members=st.lists(st.tuples(_dist, st.sampled_from(["ok", "ok", "error", "late"])), min_size=1, max_size=3),
)
def test_built_results_pass_the_contract(k: int, members: list[tuple[list[float], str]]) -> None:
    question: dict[str, Any] = {
        "type": "choice",
        "name": "department",
        "instructions": "Route.",
        "choices": [{"value": f"c{i}"} for i in range(k)],
    }
    compiler = FakeQuestionCompiler(FakeConfigRegistry())
    qs = compiler.compile(
        compiler._config.current().decision_profile("router-default"),
        [question],
        origin="native",
        output_format="native",
    )
    q = qs.questions[0]
    results = []
    for i, (raw, status) in enumerate(members):
        weights = (raw + [0.01] * k)[:k]
        total = sum(weights)
        dist = {o.key: w / total for o, w in zip(q.options, weights, strict=True)}
        results.append(
            MemberResult(
                f"m{i + 1}",
                f"alias{i}",
                f"model{i}",
                "bedrock-converse",
                "verbalized",
                1.0,
                cast("MemberStatus", status),
                100 * (i + 1),
                dist if status == "ok" else None,
            )
        )
    answer = build_answer(q, results, q_req=1, bands={"high_gte": 0.75, "low_lt": 0.4}, final=True)
    result = build_result(
        decision_id="dec_1",
        status="completed",
        answers=[answer],
        profile_id="router-default",
        profile_version=1,
        question_signature=qs.signature,
        prompt_templates={"m1": "verbalized-dist@1"},
        deadline_ms=1000,
        trace_id="0" * 32,
        started_at_ms=1_791_633_600_000,
        completed_at_ms=1_791_633_601_000,
        usage=UsageRecord(1, 1),
    )
    validator = ContractValidator()
    validator.validate_schema("getDecision", 200, "application/json", result)
    validator.check_decision_result(result, {"questions": [question], "input": "x", "include": ["members"]})


def test_round_units_sums_to_one() -> None:
    out = round_units({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}, ["a", "b", "c"])
    assert sum(out.values()) == pytest.approx(1.0) and out["a"] == 0.3334


def test_trust_enforcer_semantics() -> None:
    policy = {
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": CALLER},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": "aghoo-x"}},
            }
        ]
    }
    assert trust_allows(policy, CALLER, "aghoo-x")
    assert not trust_allows(policy, CALLER, "aghoo-y")
    assert not trust_allows(policy, "arn:aws:iam::123456789012:role/aghoo-jobs", "aghoo-x")
    handler = make_handler(CALLER, lambda arn: policy if arn == ROLE else None)
    handler(params={"body": {"RoleArn": ROLE, "ExternalId": "aghoo-x"}})
    with pytest.raises(ClientError):
        handler(params={"body": {"RoleArn": ROLE, "ExternalId": "wrong"}})
    with pytest.raises(ClientError):
        handler(params={"body": {"RoleArn": "arn:aws:iam::111122223333:role/Admin", "ExternalId": "aghoo-x"}})


async def test_fake_sts_client() -> None:
    sts = FakeStsClient(FakeClock(), caller_arn=CALLER)
    sts.allow(ROLE, "aghoo-x")
    result = await sts.assume_role(role_arn=ROLE, external_id="aghoo-x", session_name="noema-t_acme", duration_s=3600)
    assert result.assumed_role_arn.startswith("arn:aws:sts::111122223333:assumed-role/AghooBedrockInvoke/")
    assert result.creds.access_key_id.startswith("ASIA") and len(sts.calls) == 1
    with pytest.raises(ClientError):
        await sts.assume_role(role_arn=ROLE, external_id="nope", session_name="s", duration_s=3600)
    sts.fail_next("Throttling")
    with pytest.raises(ClientError):
        await sts.assume_role(role_arn=ROLE, external_id="aghoo-x", session_name="s", duration_s=3600)


async def test_fake_secrets_tell_a_missing_version_from_an_outage() -> None:
    # identity.md item 6: no previous pepper (SecretMissing) vs a backend that cannot be read (plain internal_error)
    secrets = FakeSecretsProvider(FakeClock(), {"noema/api-key-pepper": "pepper-one"})
    with pytest.raises(SecretMissing):
        await secrets.get("noema/api-key-pepper", version="v9")
    with pytest.raises(SecretMissing):
        await secrets.get("noema/does-not-exist")
    secrets.fail("noema/api-key-pepper")
    with pytest.raises(NoemaError) as err:
        await secrets.get("noema/api-key-pepper", version="v1")
    assert err.value.code is ErrorCode.INTERNAL_ERROR and not isinstance(err.value, SecretMissing)


async def test_broker_swap_is_caught_by_the_account_assertion() -> None:
    from tests.fakes.config import example_body, patched_example, snapshot_from

    t_b = {
        **json.loads(json.dumps(example_body()["tenants"]["t_acme"])),
        "aws_account_id": "444455556666",
        "invoke_role_arn": "arn:aws:iam::444455556666:role/AghooBedrockInvoke",
        "external_id_ref": "aws-sm://platform/tenants/t_b/external-id",
    }
    reg = FakeConfigRegistry(snapshot_from(patched_example({"tenants": {"t_b": t_b}})))
    broker = FakeBroker(FakeClock(), reg)
    broker.swap("t_acme", "t_b")
    creds = await broker.get("t_acme")
    with pytest.raises(TenantAccessMisconfigured):
        assert_creds_for(creds, "t_acme", "111122223333")


async def test_fake_s3_round_trip() -> None:
    s3 = FakeS3Client(("noema-test",))
    await s3.put_object(Bucket="noema-test", Key="t_acme/idempotency/idem_1", Body=b"x" * 10)
    got = await s3.get_object(Bucket="noema-test", Key="t_acme/idempotency/idem_1")
    assert await got["Body"].read() == b"x" * 10
    listed = await s3.list_objects_v2(Bucket="noema-test", Prefix="t_acme/")
    assert listed["KeyCount"] == 1
    await s3.delete_object(Bucket="noema-test", Key="t_acme/idempotency/idem_1")
    with pytest.raises(ClientError):
        await s3.get_object(Bucket="noema-test", Key="t_acme/idempotency/idem_1")


async def test_fake_page_images_manifest_declares_every_page() -> None:
    # media-negotiation.md item 9: `page_images.bytes` is the total of the page reads
    world = build_world()

    async def body() -> AsyncIterator[bytes]:
        yield b"%PDF-1.7\n" + b"\x03" * 64

    who = principal()
    ref = await world.files.upload(who, body(), declared_type="application/pdf", filename="d.pdf", session_id=None)
    manifest = next(d for d in ref.derivatives if d.kind == "page_images")
    assert manifest.pages is not None and manifest.pages > 1
    pages = [
        await world.files.read_derivative(who, ref.file_id, "page_images", page=n) for n in range(1, manifest.pages + 1)
    ]
    assert manifest.bytes == sum(len(page) for page in pages)


async def test_world_use_scenario_switches_every_fake() -> None:
    world = build_world()
    harness = world.use_scenario({"scenario": "s", "rules": [{"match": {}, "answer": {"raw_text": "A"}}]})
    assert world.harness is harness and world.decisions.harness is harness
    assert all(a.harness is harness for a in world.adapters.adapters.values())
